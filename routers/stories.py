import asyncio
import logging
import os
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
    }


@router.get("/stories")
async def list_stories(limit: int = 50):
    db = get_db()
    # 음수/0 limit 이 PostgREST 에서 500 나지 않게 하한도 클램프.
    limit = max(1, min(limit, 200))
    # 목록은 citations(jsonb) 자체를 전송하지 않는다(무겁다). 카운트는 추적 레코드 수로.
    resp = (
        db.table("stories")
        .select("id,category,body,vote_count,archived_at,arweave_tx_id,arweave_url,"
                "created_at,gap_score,community_count,news_count")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    stories = resp.data or []
    ids = [s["id"] for s in stories]
    # 출처 추적 상태는 배지/임계값 보조 정보일 뿐 — 조회가 실패해도 목록 자체는
    # 내려준다(추적 조회 한 번의 일시 오류로 전체 목록이 500 나지 않게).
    try:
        status_map = await asyncio.to_thread(get_status_map, ids)
    except Exception as e:
        logger.warning(f"[stories] get_status_map 실패 — 추적 정보 없이 목록 반환: {e}")
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
async def get_story(story_id: str):
    _ensure_uuid(story_id)
    db = get_db()
    resp = db.table("stories").select("*").eq("id", story_id).limit(1).execute()
    if not resp.data:
        raise HTTPException(404, "스토리를 찾을 수 없습니다")
    story = resp.data[0]
    _mask_pending(story)

    # 추적 정보 머지
    status_map = await asyncio.to_thread(get_status_map, [story_id])
    by_url = status_map.get(story_id, {})

    # 이 스토리에 추적 레코드가 없으면 (옛 데이터) 즉시 등록
    if not by_url and story.get("citations"):
        await asyncio.to_thread(register_citations, story_id, story["citations"])

    # Wayback 스냅샷 상태 머지(조회 실패해도 본문은 내려가게 best-effort)
    cite_urls = [c.get("uri") for c in (story.get("citations") or [])]
    try:
        wayback_by_url = await asyncio.to_thread(get_wayback_map, cite_urls)
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
