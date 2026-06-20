"""
캡처→공개 스토리 승격 다리 (Promoter).

미션의 잃어버린 고리: collector 가 살아있을 때 캡처해 비공개로 보관한 글(captured_posts)이
*실제로 삭제되면*, 그것을 익명·헤지된 문학 스토리로 공개 박제 파이프라인에 올린다. 지금까지
captured_posts 는 dead-end(아무리 죽어도 아무도 못 봄)였다 — 이 모듈이 그걸 메운다.

이게 '삭제될 글을 정말로 찾아 박제'하는 사이트의 핵심이다. 검색(Tavily)은 구조적으로 이미
삭제된 글을 못 주므로, 살아있을 때 잡아두고(collector) → 죽는 걸 감시하고(tracker) →
죽은 걸 공개(promoter)하는 경로만이 진짜 사라지는 글을 박제한다.

안전 원칙(적대적 리뷰 전면 반영 — 되돌릴 수 없는 Arweave 박제이므로):
  1. 자동 승격 트리거는 *hard 삭제(HTTP 404/410)* 뿐. soft(본문패턴/리다이렉트/본문급감)는
     오탐 자가정정 가능성이 있어 절대 자동 승격하지 않는다(tracker/threshold hard-only 원칙).
  2. PII 게이트: 공개 전 captured body_text 를 services.pii 로 스캔, 구조적 식별자(주민번호·
     전화·이메일·카드·계좌)가 검출되면 자동 승격 차단 → 수동 검토(blocked_pii). LLM 출력도 재스캔.
  3. 익명화: 원본 raw body 는 절대 그대로 공개하지 않는다. 기존 PROMPT_KINDNESS/CRITIQUE 의
     '없는 사실 창작 금지·익명화·헤지' 규칙을 통과한 LLM 재작성문(stories.body)만 공개한다.
  4. critique(기업 비위) 카테고리는 명예훼손 노출이 커, 기본적으로 자동 승격하지 않고 수동
     검토 큐(pending_review)로 보낸다(PROMOTER_AUTO_CRITIQUE=true 로만 자동화).
  5. 멱등: stories.origin_captured_url UNIQUE + promoted_story_id 로 중복 승격 차단.
  6. 토큰 안전: 소배치·저빈도·본문 truncate(generate_from_text). 기본 비활성(옵트인).

⚠️ 기본 비활성(PROMOTER_ENABLED=false). migrations/009 적용 + 운영자 명시 활성화 필요.
"""

import asyncio
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.db import get_db
from services.llm import generate_from_text
from services.pii import scan as pii_scan
from services.tracker import register_citations
from services.volatility import ACCUSATION_RE, ENTITY_RE

import logging
logger = logging.getLogger(__name__)

PROMOTER_ENABLED = os.environ.get("PROMOTER_ENABLED", "false").lower() == "true"
PROMOTER_INTERVAL_SEC = int(os.environ.get("PROMOTER_INTERVAL_SEC", "1800"))   # 30분
PROMOTER_INITIAL_DELAY_SEC = int(os.environ.get("PROMOTER_INITIAL_DELAY_SEC", "150"))
# 한 주기당 승격 상한(토큰 안전: 캡처 전체본문 입력이라 generate 보다 토큰이 크다).
PROMOTER_BATCH = max(1, int(os.environ.get("PROMOTER_BATCH", "3")))
# critique(기업 비위)는 기본 자동 승격 금지 → 수동 검토 큐로(명예훼손 노출 최소화).
PROMOTER_AUTO_CRITIQUE = os.environ.get("PROMOTER_AUTO_CRITIQUE", "false").lower() == "true"
# 승격 최소 삭제확률(0~10, 결정적 점수). hard 삭제가 이미 강한 게이트라 기본 0(추가 필터 옵션).
PROMOTER_MIN_VOLATILITY = int(os.environ.get("PROMOTER_MIN_VOLATILITY", "0"))

# 모듈 상태(대시보드/stats 용 — hunter.get_status 와 동형).
_last_run_at: Optional[datetime] = None
_next_run_at: Optional[datetime] = None
_last_result: Optional[dict] = None


def get_status() -> dict:
    return {
        "enabled": PROMOTER_ENABLED,
        "interval_sec": PROMOTER_INTERVAL_SEC,
        "batch": PROMOTER_BATCH,
        "auto_critique": PROMOTER_AUTO_CRITIQUE,
        "next_run_at": _next_run_at.isoformat() if _next_run_at else None,
        "last_run_at": _last_run_at.isoformat() if _last_run_at else None,
        "last_result": _last_result,
    }


def classify_category(title: Optional[str], body: Optional[str]) -> str:
    """캡처 글의 카테고리 추정. 고발 어휘가 있으면 critique(기업 비위), 아니면 kindness.
    승격 LLM 프롬프트(PROMPT_KINDNESS/CRITIQUE) 선택과 자동/수동 정책 분기에 쓴다."""
    text = f"{title or ''}\n{body or ''}"
    if ACCUSATION_RE.search(text):
        return "critique"
    # 고발 없이 기업/권력자만 언급되면 대개 미담·정보성 → kindness 로 보수적 분류.
    return "kindness"


def _columns_ready(db) -> bool:
    """migrations/009 컬럼(captured_posts.hard_deleted_at·promotion_status,
    stories.from_capture·origin_captured_url) 미설치면 승격 자체가 불가하므로 가드."""
    try:
        db.table("captured_posts").select(
            "id,hard_deleted_at,promotion_status,promoted_story_id"
        ).limit(1).execute()
        db.table("stories").select("from_capture,origin_captured_url").limit(1).execute()
        return True
    except Exception as e:
        logger.warning(f"[promoter] 009 승격 컬럼 미설치 — migrations/009 적용 필요: {e}")
        return False


def _mark(db, captured_id: str, fields: dict) -> None:
    try:
        db.table("captured_posts").update(fields).eq("id", captured_id).execute()
    except Exception as e:
        logger.warning(f"[promoter] captured 갱신 실패 {captured_id}: {e}")


def find_promotable(db, limit: int) -> list:
    """승격 후보: hard 삭제 확정(404/410) + 본문 보유 + 아직 미승격 + 미처리.
    soft 삭제는 절대 포함하지 않는다(hard_deleted_at IS NOT NULL 로 강제)."""
    try:
        q = (
            db.table("captured_posts")
            .select("id,url,title,body_text,volatility_score,hard_deleted_at,promotion_status")
            .not_.is_("hard_deleted_at", "null")
            .not_.is_("body_text", "null")
            .is_("promoted_story_id", "null")
            .is_("promotion_status", "null")   # 미처리만(pending_review/blocked_pii/skipped 제외)
        )
        if PROMOTER_MIN_VOLATILITY > 0:
            q = q.gte("volatility_score", PROMOTER_MIN_VOLATILITY)
        resp = (
            q.order("volatility_score", desc=True, nullsfirst=False)
            .order("hard_deleted_at", desc=False)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.warning(f"[promoter] 후보 조회 실패: {e}")
        return []


def _link_existing(db, captured_id: str, origin_url: str) -> Optional[str]:
    """origin_captured_url 로 이미 승격된 스토리가 있으면 그 id 로 연결(멱등). 없으면 None."""
    try:
        ex = (db.table("stories").select("id")
              .eq("origin_captured_url", origin_url).limit(1).execute())
        if ex.data:
            sid = ex.data[0]["id"]
            _mark(db, captured_id, {"promoted_story_id": sid, "promotion_status": "promoted"})
            return sid
    except Exception as e:
        logger.warning(f"[promoter] 기존 스토리 연결 조회 실패 {origin_url}: {e}")
    return None


def promote_one(db, row: dict, *, auto: bool = True,
                force_category: Optional[str] = None) -> Optional[str]:
    """캡처 1건을 공개 스토리로 승격. 반환: story_id(성공) 또는 None(차단/스킵/실패).
    auto=True 면 카테고리 자동 정책(critique 수동 검토)을 적용한다.
    force_category 가 주어지면 자동 분류 대신 그 카테고리로 강제(수동 어드민용)."""
    captured_id = row["id"]
    url = row["url"]
    title = row.get("title")
    body = row.get("body_text") or ""

    # 1) PII 게이트(원본 본문). 구조적 식별자 검출 시 자동 승격 차단 → 수동 검토.
    scan = pii_scan(body)
    if scan["hit"]:
        logger.info(f"[promoter] PII 검출 {scan['kinds']} → 자동 승격 차단(blocked_pii) {url}")
        _mark(db, captured_id, {"promotion_status": "blocked_pii"})
        return None

    # 2) 카테고리 분류 + 자동 정책(critique 는 기본 수동 검토 큐).
    category = force_category if force_category in ("kindness", "critique") \
        else classify_category(title, body)
    if auto and category == "critique" and not PROMOTER_AUTO_CRITIQUE:
        logger.info(f"[promoter] critique 자동 승격 보류 → 수동 검토 큐(pending_review) {url}")
        _mark(db, captured_id, {"promotion_status": "pending_review"})
        return None

    # 3) 익명·헤지 문학 재작성(검색 grounding 없이 본문만). 토큰 안전 위해 본문 truncate(llm).
    try:
        gen = generate_from_text(body, title, category)
    except Exception as e:
        logger.warning(f"[promoter] LLM 재작성 실패 {url}: {e}")
        return None   # promotion_status 는 비워 둬 다음 주기에 재시도(일시 장애 흡수)
    if gen.get("no_fit") or not (gen.get("body") or "").strip():
        logger.info(f"[promoter] 재작성 결과 no_fit/빈본문 → skip {url}")
        _mark(db, captured_id, {"promotion_status": "skipped"})
        return None

    # 4) PII 재스캔(LLM 출력) — 방어적 2차 검사.
    out_scan = pii_scan(gen["body"])
    if out_scan["hit"]:
        logger.info(f"[promoter] LLM 출력 PII 검출 {out_scan['kinds']} → blocked_pii {url}")
        _mark(db, captured_id, {"promotion_status": "blocked_pii"})
        return None

    # 5) 멱등: 이미 같은 origin 으로 승격된 스토리가 있으면 그것에 연결.
    existing = _link_existing(db, captured_id, url)
    if existing:
        return existing

    # 6) 공개 스토리 INSERT. volatility 는 결정적 점수(captured) 우선, 없으면 LLM 값.
    story_row = {
        "category": gen["category"],
        "body": gen["body"],
        "citations": [{"title": title or url, "uri": url}],
        "search_queries": [],
        "vote_count": 0,
        "poetic_reason": gen.get("poetic_reason"),
        "volatility_score": row.get("volatility_score")
            if row.get("volatility_score") is not None else gen.get("volatility_score"),
        "from_capture": True,
        "origin_captured_url": url,
        "captured_hard_deleted_at": row.get("hard_deleted_at"),
    }
    try:
        ins = db.table("stories").insert(story_row).execute()
        story_id = ins.data[0]["id"]
    except Exception as e:
        # UNIQUE(origin_captured_url) 충돌 = 동시/이전 승격 승자 존재 → 그것에 연결(멱등).
        linked = _link_existing(db, captured_id, url)
        if linked:
            return linked
        logger.warning(f"[promoter] 스토리 INSERT 실패 {url}: {e}")
        return None

    _mark(db, captured_id, {"promoted_story_id": story_id, "promotion_status": "promoted"})

    # 7) 출처(죽은 원본) 추적 등록 → tracker 가 'deleted' 로 표시 + hard 신호로 임계값 인하
    #    → 사람 투표가 모이면 '사라지기 전에' 가 아니라 '사라진 뒤' 박제가 빠르게 트리거된다.
    try:
        register_citations(story_id, story_row["citations"])
    except Exception as e:
        logger.warning(f"[promoter] citation 등록 실패 {story_id}: {e}")

    logger.info(f"[promoter] 승격 완료 {story_id[:8]} ← {url} (category={gen['category']}, "
                f"volatility={story_row['volatility_score']})")
    return story_id


async def run_promotion_batch(limit: int = PROMOTER_BATCH) -> dict:
    """한 주기 승격 배치. 반환 {promoted, blocked, pending, skipped, considered}."""
    db = get_db()
    if not _columns_ready(db):
        return {"promoted": 0, "considered": 0, "error": "migrations/009 미적용"}

    rows = await asyncio.to_thread(find_promotable, db, limit)
    counts = {"promoted": 0, "considered": len(rows)}
    for row in rows:
        sid = await asyncio.to_thread(lambda r=row: promote_one(db, r, auto=True))
        # 상태별 집계는 promote_one 이 captured_posts.promotion_status 로 영속화하므로
        # 여기선 성공만 카운트(나머지는 다음 주기/대시보드에서 status 로 확인).
        if sid:
            counts["promoted"] += 1
    return counts


async def promote_captured_url(url: str, force_category: Optional[str] = None) -> dict:
    """수동(어드민) 승격: 특정 captured URL 을 즉시 승격. PII 게이트는 유지된다.
    critique 자동 보류 정책은 우회(auto=False)하되 PII 차단은 우회하지 않는다.
    반환 {ok, story_id|reason}."""
    db = get_db()
    if not _columns_ready(db):
        return {"ok": False, "reason": "migrations/009 미적용"}
    try:
        resp = (db.table("captured_posts")
                .select("id,url,title,body_text,volatility_score,hard_deleted_at,promotion_status")
                .eq("url", url).limit(1).execute())
    except Exception as e:
        return {"ok": False, "reason": f"조회 실패: {e}"}
    if not resp.data:
        return {"ok": False, "reason": "captured 글 없음"}
    row = resp.data[0]
    if not (row.get("body_text") or "").strip():
        return {"ok": False, "reason": "본문 없음(살아있을 때 캡처 안 됨) — 승격 불가"}
    # 수동 경로: auto=False 라 critique 도 진행(단 PII 게이트는 그대로 유지).
    fc = force_category if force_category in ("kindness", "critique") else None
    sid = await asyncio.to_thread(
        lambda: promote_one(db, row, auto=False, force_category=fc)
    )
    if sid:
        return {"ok": True, "story_id": sid}
    # promote_one 이 상태를 영속화했으므로 그 사유를 읽어 돌려준다.
    try:
        st = (db.table("captured_posts").select("promotion_status")
              .eq("id", row["id"]).limit(1).execute())
        reason = (st.data[0].get("promotion_status") if st.data else None) or "실패"
    except Exception:
        reason = "실패"
    return {"ok": False, "reason": reason}


async def background_promotion_loop() -> None:
    """앱 lifespan 동안 도는 승격 루프(hard 삭제 확정 캡처글 → 공개 스토리)."""
    global _last_run_at, _next_run_at, _last_result

    if not PROMOTER_ENABLED:
        logger.info("[promoter] 비활성화 (PROMOTER_ENABLED=false)")
        return
    db = get_db()
    if not _columns_ready(db):
        logger.warning("[promoter] migrations/009 미적용 — 루프 중단.")
        return

    logger.info(f"[promoter] 시작 · interval={PROMOTER_INTERVAL_SEC}s · batch={PROMOTER_BATCH} "
                f"· auto_critique={PROMOTER_AUTO_CRITIQUE}")
    _next_run_at = datetime.now(timezone.utc) + timedelta(seconds=PROMOTER_INITIAL_DELAY_SEC)
    try:
        await asyncio.sleep(PROMOTER_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        return

    while True:
        try:
            res = await run_promotion_batch()
            _last_run_at = datetime.now(timezone.utc)
            _last_result = {**res, "at": _last_run_at.isoformat()}
            if res.get("promoted"):
                logger.info(f"[promoter] {res['promoted']}건 승격 (후보 {res.get('considered')})")
        except asyncio.CancelledError:
            logger.info("[promoter] cancelled")
            return
        except Exception as e:
            logger.warning(f"[promoter] loop error: {e}")

        jitter = random.uniform(0.9, 1.1)
        delay = max(300, int(PROMOTER_INTERVAL_SEC * jitter))
        _next_run_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
