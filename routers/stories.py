import asyncio
import logging
import os
import re
import uuid

from fastapi import APIRouter, HTTPException, Request

from services.archive import PENDING_MARKER
from services.db import get_db
from services.hunter import count_recent_pending
from services.llm import generate
from services.ratelimit import check_recheck_ratelimit, check_story_ratelimit
from services.threshold import (
    DEFAULT_THRESHOLD,
    compute_effective_threshold,
    count_citation_signals,
)
from services.tracker import (
    get_status_map,
    is_untrackable_source,
    recheck_one_story,
    register_citations,
)
from services.wayback import get_wayback_map

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

# 미박제 글 전역 상한 — 익명 생성이 DB/디스크를 무한 적재하지 못하게 (hunter 와 별개 한도)
STORY_MAX_PENDING = int(os.environ.get("STORY_MAX_PENDING", "50"))

# 목록(list_stories)용 컬럼. 캡처 승격(009)·가치 점수(012) 컬럼은 미적용 환경에서 400
# 나므로 한 번 시도 후 실패하면 해당 묶음만 빼고 폴백, 그 결과를 캐시한다(매 요청 이중질의 방지).
_LIST_BASE_COLS = (
    "id,category,body,vote_count,archived_at,arweave_tx_id,arweave_url,"
    "created_at,gap_score,community_count,news_count,poetic_reason,volatility_score"
)
_LIST_CAPTURE_COLS = ",from_capture,origin_captured_url,captured_hard_deleted_at"
_LIST_VALUE_COLS = ",value_score"
_capture_cols_ok: bool | None = None
_value_col_ok: bool | None = None


def _ensure_uuid(story_id: str) -> None:
    """uuid 컬럼에 잘못된 형식을 넘기면 PostgREST 가 22P02 로 500 을 내므로 선검증."""
    try:
        uuid.UUID(story_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(404, "스토리를 찾을 수 없습니다")


def _mask_pending(story: dict) -> None:
    """업로드 진행 중 임시 마커('__pending__')가 UI에 '박제됨'으로 새어나가지 않게 정리."""
    if story.get("arweave_tx_id") == PENDING_MARKER:
        story["arweave_tx_id"] = None
        story["arweave_url"] = None


def _augment_with_status(story: dict, status_by_url: dict, wayback_by_url: dict | None = None) -> dict:
    """citations 배열에 track_status / track_last_checked / deleted_count 머지.
    deleted_count/blocked_count 는 표시용 raw 카운트(soft 포함)다.
    wayback_by_url 가 있으면 각 출처에 archive_url(중립 외부 스냅샷)도 머지한다."""
    citations = story.get("citations") or []
    wayback_by_url = wayback_by_url or {}
    deleted = 0
    blocked = 0
    for c in citations:
        info = status_by_url.get(c.get("uri"))
        if info:
            c["track_status"] = info["status"]
            c["track_last_checked"] = info["last_checked"]
            c["track_http_code"] = info["http_code"]
            c["track_reason"] = info.get("reason")
            c["track_first_seen"] = info.get("first_seen")
            c["track_check_count"] = info.get("check_count", 0)
            c["track_next_check_at"] = info.get("next_check_at")
            c["track_error_count"] = info.get("error_count", 0)
            # 콘텐츠 지문(투명성): 첫 생존 확인 시점 가시 텍스트의 sha256.
            # 원문 재공개 없이 '그 시각에 이 내용이 존재했음'을 제3자가 대조 가능.
            c["content_fingerprint"] = info.get("baseline_hash")
            c["fingerprint_at"] = info.get("baseline_at")
            c["track_untrackable"] = is_untrackable_source(
                c.get("uri"), info["http_code"], info.get("reason"))
            if info["status"] == "deleted":
                deleted += 1
            elif info["status"] == "blocked":
                blocked += 1
        else:
            c["track_status"] = "unchecked"
            c["track_untrackable"] = is_untrackable_source(c.get("uri"))
        # Wayback 위임 스냅샷: 성공분만 영속 링크를 노출('삭제 전 원본 스냅샷' 증거).
        wb = wayback_by_url.get(c.get("uri"))
        if wb:
            c["archive_status"] = wb.get("status")
            if wb.get("status") == "success" and wb.get("snapshot_url"):
                c["archive_url"] = wb["snapshot_url"]
    story["citations"] = citations
    story["deleted_count"] = deleted
    story["blocked_count"] = blocked
    return story


@router.post("/story")
async def create_story(request: Request, category: str | None = None):
    if category and category not in ("kindness", "critique"):
        raise HTTPException(400, "category는 kindness 또는 critique만 허용")

    # 레이트리밋: 무인증 생성 엔드포인트의 비용 폭탄·DoS 방어
    allowed, retry_after, reason = check_story_ratelimit(request)
    if not allowed:
        raise HTTPException(
            429, f"요청이 너무 많습니다. {reason}. {retry_after}초 후 다시 시도하세요.",
            headers={"Retry-After": str(retry_after)},
        )

    # 미박제 글 전역 상한: 익명 남용으로 인한 무한 적재 차단
    pending = await asyncio.to_thread(count_recent_pending)
    if pending >= STORY_MAX_PENDING:
        raise HTTPException(
            503, f"미박제 글이 한도({STORY_MAX_PENDING})에 도달했습니다. "
                 f"기존 글에 투표해 박제가 진행된 뒤 다시 시도하세요.",
        )

    try:
        result = await asyncio.to_thread(generate, category)
    except Exception as e:
        # 원시 예외 텍스트(내부 upstream URL·provider 응답본문)를 클라이언트에 그대로
        # 노출하지 않는다 — 서버에만 로깅하고 일반 메시지로 응답.
        logger.warning(f"[story] 생성 실패: {e!r}")
        raise HTTPException(503, "이야기 생성에 일시적으로 실패했습니다. 잠시 후 다시 시도하세요.")

    # 적합성 게이트: 검색 결과에 진짜 해당 카테고리 글이 없으면 빈 본문을 박제하지 않고
    # 503 으로 알린다(잠시 후 재시도 유도). no_fit 응답을 INSERT 하면 안 된다.
    if result.get("no_fit") or not (result.get("body") or "").strip():
        raise HTTPException(
            503, "지금은 박제할 만한 적합한 글을 찾지 못했습니다. 잠시 후 다시 시도하세요.",
        )

    gap = result.get("gap_data") or {}
    db = get_db()
    resp = db.table("stories").insert({
        "category": result["category"],
        "body": result["body"],
        "citations": result["citations"],
        "search_queries": result["search_queries"],
        "vote_count": 0,
        "gap_score": gap.get("gap_score"),
        "community_count": gap.get("community_count"),
        "news_count": gap.get("news_count"),
        "poetic_reason": result.get("poetic_reason"),
        "volatility_score": result.get("volatility_score"),
    }).execute()
    story_id = resp.data[0]["id"]

    # 새 citation 들을 추적 테이블에 등록 (백그라운드 루프가 곧 검사함)
    await asyncio.to_thread(register_citations, story_id, result["citations"])

    return {
        "story_id": story_id,
        "category": result["category"],
        "text": result["text"],
        "body": result["body"],
        "citations": result["citations"],
        "provider": result["provider"],
        "model": result["model"],
        "gap_score": gap.get("gap_score"),
        "community_count": gap.get("community_count"),
        "news_count": gap.get("news_count"),
        "poetic_reason": result.get("poetic_reason"),
        "volatility_score": result.get("volatility_score"),
    }


# 검색어 정제: PostgREST or_/ilike 구조 문자(* % _ , ( ) \ ")를 제거해 필터 깨짐·와일드카드
# 인젝션을 막고 정제된 부분일치 키워드만 남긴다(길이 상한 80). 한글·영문·공백·하이픈은 보존.
_SEARCH_STRIP_RE = re.compile(r'[%_,()*\\"]+')


def _sanitize_search(q: str | None) -> str:
    if not q:
        return ""
    cleaned = _SEARCH_STRIP_RE.sub(" ", q)
    return " ".join(cleaned.split())[:80]


def _get_list_status_map(db, story_ids: list[str]) -> dict:
    """목록 배지·임계값에 필요한 필드만 조회(상세의 추적 이력·지문은 제외)."""
    if not story_ids:
        return {}
    resp = (
        db.table("citation_checks")
        .select("story_id,url,status,http_code,baseline_at")
        .in_("story_id", story_ids)
        .execute()
    )
    out: dict = {}
    for row in resp.data or []:
        out.setdefault(row["story_id"], {})[row["url"]] = row
    return out


@router.get("/stories")
def list_stories(limit: int = 50, q: str | None = None):
    # Supabase/로컬 DB 와 동적 임계값 조회는 동기 API다. FastAPI 의 작업 스레드에서
    # 전체 읽기를 실행해 첫 목록을 읽는 동안 다른 요청의 이벤트 루프를 막지 않는다.
    global _capture_cols_ok
    db = get_db()
    # 음수/0 limit 이 PostgREST 에서 500 나지 않게 하한도 클램프.
    limit = max(1, min(limit, 200))
    # 검색어가 있으면 본문·시적 사유 부분일치로 전체 아카이브를 서버에서 검색.
    term = _sanitize_search(q)

    # 목록은 citations(jsonb) 자체를 전송하지 않는다(무겁다). 카운트는 추적 레코드 수로.
    def _query(cols: str):
        sel = db.table("stories").select(cols)
        if term:
            sel = sel.or_(f"body.ilike.*{term}*,poetic_reason.ilike.*{term}*")
        return sel.order("created_at", desc=True).limit(limit).execute()

    # tracker._is_missing_column_error 와 달리 '어느 컬럼이 없는가'의 귀속(아래 msg 검사)을
    # 따로 해야 해서 일반 판별만 하는 로컬 헬퍼를 둔다 — 공용 헬퍼는 generic-OR-특정컬럼
    # 판정이라 묶음별(009/012) 캐시 귀속에 그대로 쓰면 오귀속된다. 판별 문자열을 바꿀 땐
    # tracker 쪽과 함께 갱신할 것.
    def _is_missing_col(e: Exception) -> bool:
        msg = str(e).lower()
        return ("42703" in msg or "pgrst204" in msg
                or "does not exist" in msg or "could not find" in msg)

    # 선택 컬럼 묶음(009 캡처·012 가치)을 각각 시도하고, '컬럼 부재'만 영구 캐시(False)
    # 한다. 일시적 연결오류를 캐시하면 프로세스 기동 직후 한 번 실패했다는 이유로 배지가
    # 재시작 전까지 영영 사라진다 → 일시 오류는 캐시하지 말고 이번 요청만 폴백한다.
    # 묶음별 독립 캐시: 009만 적용된 환경에서 012 부재가 from_capture 배지를 끄지 않게.
    global _value_col_ok
    attempts = []
    if _capture_cols_ok is not False and _value_col_ok is not False:
        attempts.append((_LIST_BASE_COLS + _LIST_CAPTURE_COLS + _LIST_VALUE_COLS, "both"))
    if _capture_cols_ok is not False:
        attempts.append((_LIST_BASE_COLS + _LIST_CAPTURE_COLS, "capture"))
    if _value_col_ok is not False:
        attempts.append((_LIST_BASE_COLS + _LIST_VALUE_COLS, "value"))
    attempts.append((_LIST_BASE_COLS, "base"))

    resp = None
    last_err: Exception | None = None
    for cols, tier in attempts:
        # 바로 앞 시도에서 부재가 확인된 컬럼은 같은 요청 안에서도 다시 질의하지 않는다.
        if tier in ("both", "capture") and _capture_cols_ok is False:
            continue
        if tier in ("both", "value") and _value_col_ok is False:
            continue
        try:
            resp = _query(cols)
            if tier in ("both", "capture"):
                _capture_cols_ok = True
            if tier in ("both", "value"):
                _value_col_ok = True
            break
        except Exception as e:
            last_err = e
            if not _is_missing_col(e):
                logger.warning(f"[stories] 선택컬럼 일시 조회 실패(캐시 안 함, tier={tier}): {e!r}")
                continue   # 일시 오류: 캐시 없이 다음(더 좁은) 조합 시도
            msg = str(e).lower()
            if "from_capture" in msg or "origin_captured_url" in msg \
                    or "captured_hard_deleted_at" in msg:
                _capture_cols_ok = False
            if "value_score" in msg:
                _value_col_ok = False
    if resp is None:
        # 모든 조합 실패 = base(항상 마지막 시도)까지 이미 실패한 연쇄 일시 오류.
        # 같은 쿼리를 5번째로 재실행하지 않고(어차피 같은 장애) 503 으로 정직하게 알린다.
        logger.warning(f"[stories] 목록 조회 전 조합 실패: {last_err!r}")
        raise HTTPException(503, "목록 조회에 일시적으로 실패했습니다. 잠시 후 다시 시도하세요.")
    stories = resp.data or []
    ids = [s["id"] for s in stories]
    # 출처 추적 상태는 배지/임계값 보조 정보일 뿐 — 조회가 실패해도 목록 자체는
    # 내려준다(추적 조회 한 번의 일시 오류로 전체 목록이 500 나지 않게).
    try:
        status_map = _get_list_status_map(db, ids)
    except Exception as e:
        logger.warning(f"[stories] 목록 추적 조회 실패 — 추적 정보 없이 목록 반환: {e}")
        status_map = {}
    for s in stories:
        _mask_pending(s)
        urls_status = status_map.get(s["id"], {})
        sig = count_citation_signals(list(urls_status.values()))
        s["deleted_count"] = sig["deleted"]      # 표시용 raw (배지/필터)
        s["blocked_count"] = sig["blocked"]
        s["citation_count"] = len(urls_status)
        # 동적 임계값: 자동 박제 판단과 일치하도록 hard 신호(404/410/403)로만 인하
        eff = compute_effective_threshold(
            s.get("gap_score"), sig["hard_deleted"], sig["hard_blocked"]
        )
        s["effective_threshold"] = eff["threshold"]
        s["urgency"] = eff["urgency"]
        s["default_threshold"] = DEFAULT_THRESHOLD
    return stories


@router.get("/stories/{story_id}")
def get_story(story_id: str):
    # 상세 역시 전체 동기 DB 작업을 FastAPI 작업 스레드에서 처리한다.
    _ensure_uuid(story_id)
    db = get_db()
    resp = db.table("stories").select("*").eq("id", story_id).limit(1).execute()
    if not resp.data:
        raise HTTPException(404, "스토리를 찾을 수 없습니다")
    story = resp.data[0]
    _mask_pending(story)

    # 추적 정보 머지
    status_map = get_status_map([story_id])
    by_url = status_map.get(story_id, {})

    # 이 스토리에 추적 레코드가 없으면 (옛 데이터) 즉시 등록
    if not by_url and story.get("citations"):
        register_citations(story_id, story["citations"])

    # Wayback 스냅샷 상태 머지(조회 실패해도 본문은 내려가게 best-effort)
    cite_urls = [c.get("uri") for c in (story.get("citations") or [])]
    try:
        wayback_by_url = get_wayback_map(cite_urls)
    except Exception:
        wayback_by_url = {}

    out = _augment_with_status(story, by_url, wayback_by_url)
    # 동적 임계값 머지: 자동 박제 판단과 일치하도록 hard 신호(404/410/403)로만 인하
    sig = count_citation_signals(list(by_url.values()))
    eff = compute_effective_threshold(
        out.get("gap_score"),
        sig["hard_deleted"],
        sig["hard_blocked"],
    )
    out["effective_threshold"] = eff["threshold"]
    out["urgency"] = eff["urgency"]
    out["urgency_reason"] = eff["reason"]
    out["default_threshold"] = DEFAULT_THRESHOLD
    return out


@router.post("/recheck/{story_id}")
async def manual_recheck(story_id: str, request: Request):
    """수동 재검사 트리거. 응답에 새 상태 포함."""
    _ensure_uuid(story_id)
    allowed, retry_after, reason = check_recheck_ratelimit(request)
    if not allowed:
        raise HTTPException(
            429, f"{reason}. {retry_after}초 후 다시 시도하세요.",
            headers={"Retry-After": str(retry_after)},
        )
    n = await recheck_one_story(story_id)
    if n == 0:
        # 추적 레코드가 없으면 등록 후 한 번 검사
        db = get_db()
        resp = db.table("stories").select("citations").eq("id", story_id).limit(1).execute()
        if not resp.data:
            raise HTTPException(404, "스토리를 찾을 수 없습니다")
        citations = resp.data[0].get("citations") or []
        if not citations:
            return {"checked": 0}
        await asyncio.to_thread(register_citations, story_id, citations)
        n = await recheck_one_story(story_id)

    status_map = await asyncio.to_thread(get_status_map, [story_id])
    return {"checked": n, "statuses": status_map.get(story_id, {})}


# 수동 승격(어드민): captured_posts 의 한 글을 공개 스토리로 올린다. critique 캡처는
# 기본 자동 승격이 막혀 pending_review 로 쌓이므로, 운영자가 검토 후 이 경로로 공개한다.
# ADMIN_TOKEN 미설정이면 엔드포인트 자체를 숨긴다(404). PII 게이트는 수동에서도 유지된다.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()


@router.post("/admin/promote")
async def admin_promote(request: Request, url: str, category: str | None = None):
    import secrets
    if not ADMIN_TOKEN:
        raise HTTPException(404, "Not Found")
    token = request.headers.get("x-admin-token", "")
    if not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(403, "권한이 없습니다")
    if category and category not in ("kindness", "critique"):
        raise HTTPException(400, "category는 kindness 또는 critique만 허용")
    from services.promoter import promote_captured_url
    res = await promote_captured_url(url, category)
    if not res.get("ok"):
        raise HTTPException(409, res.get("reason") or "승격 실패")
    return res
