"""
오래된 미박제 글 자동 정리 (Story Cleanup).

미박제(arweave_tx_id IS NULL) 글은 hunter.count_recent_pending() 의 7일 윈도우에서만
빠질 뿐 DB 에는 영원히 남아 무한 누적된다. 영구 박제는 Arweave 에 올라간 글뿐이고,
투표로 선택받지 못한 후보는 일정 기간 뒤 만료시키는 것이 타임캡슐 설계 철학과도 부합한다.

- 대상: arweave_tx_id IS NULL  AND  created_at < now - CLEANUP_AGE_DAYS
        AND  vote_count <= CLEANUP_MAX_VOTES  AND  from_capture = false
- 기본 한도(CLEANUP_MAX_VOTES)는 '박제 임계값 바로 아래'(DEFAULT_THRESHOLD-1). 0/1/2표처럼
  임계값에 못 미친 채 방치된 오래된 후보를 정리한다. (과거엔 0표만 지워 1~2표 후보가 무한 누적됐다.)
- 박제된 글(arweave_tx_id 있음)·업로드 중(__pending__, IS NULL 아님)·임계값 도달 후보(기본
  초과)·캡처 승격글(from_capture, '삭제된 원본'의 공개 기록 + 삭제 시 캡처 dangling)은 보존.
- votes·citation_checks 는 stories(id) on delete cascade 라 함께 정리된다.
"""

import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

from postgrest.exceptions import APIError

from services.db import get_db
from services.threshold import DEFAULT_THRESHOLD

logger = logging.getLogger(__name__)

CLEANUP_ENABLED = os.environ.get("STORY_CLEANUP_ENABLED", "true").lower() != "false"
# 생성 후 이 일수가 지난 미박제 글만 정리 대상. 0/음수면 갓 생성된 글까지 즉시
# 삭제되므로 최소 1일로 하한 고정(운영자 오설정 가드).
CLEANUP_AGE_DAYS = max(1, int(os.environ.get("STORY_CLEANUP_AGE_DAYS", "14")))
# 이 표 수 이하면 삭제. 기본 = 박제 임계값-1(임계값 미달=커뮤니티가 영구박제를 안 고른 글).
# 임계값에 도달했는데도 미박제인 글(박제 실패 등)은 보존돼 수동 점검에 남는다.
CLEANUP_MAX_VOTES = int(
    os.environ.get("STORY_CLEANUP_MAX_VOTES", str(max(0, DEFAULT_THRESHOLD - 1)))
)
CLEANUP_INTERVAL_SEC = int(os.environ.get("STORY_CLEANUP_INTERVAL_SEC", "21600"))  # 6시간
# 부팅 안정화 + tracker/hunter 와 시간 분산
CLEANUP_INITIAL_DELAY_SEC = int(os.environ.get("STORY_CLEANUP_INITIAL_DELAY_SEC", "120"))
# .in_() 는 ID 를 URL 에 나열하므로, 대량 후보 시 URI 한도 초과(414)를 막으려 배치로 끊는다.
CLEANUP_BATCH = int(os.environ.get("STORY_CLEANUP_BATCH", "200"))


def cleanup_old_pending() -> int:
    """오래된 저득표 미박제 글을 삭제하고 삭제된 글 수를 반환.
    원자적 RPC(delete_orphan_pending_stories)가 있으면 그것으로 — votes 테이블을 같은
    스냅샷에서 직접 확인해 vote-TOCTOU 를 완전히 차단 — 삭제한다. RPC 가 아직 설치되지
    않았으면(마이그레이션 전) 기존 배치 삭제로 폴백한다."""
    try:
        db = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=CLEANUP_AGE_DAYS)).isoformat()
        try:
            resp = db.rpc(
                "delete_orphan_pending_stories",
                {"p_cutoff": cutoff, "p_max_votes": CLEANUP_MAX_VOTES},
            ).execute()
            n = resp.data if isinstance(resp.data, int) else int(resp.data or 0)
            if n:
                logger.info(
                    f"[cleanup] {n}건 정리 (원자적 RPC · 미박제·{CLEANUP_AGE_DAYS}일↑·"
                    f"실표 {CLEANUP_MAX_VOTES}↓)"
                )
            return n
        except APIError as e:
            # PGRST202 = 함수 없음(마이그레이션 전). 레거시 배치 삭제로 폴백.
            if getattr(e, "code", None) == "PGRST202" or \
                    "delete_orphan_pending_stories" in str(e):
                logger.info("[cleanup] 원자적 RPC 미설치 — 레거시 배치 삭제로 폴백 "
                            "(supabase_migration_2026-06.sql 적용 권장)")
                return _cleanup_legacy_batched(db, cutoff)
            raise
    except Exception as e:
        logger.warning(f"[cleanup] failed: {e}")
        return 0


# from_capture 컬럼(009) 지원 여부 — 1회 probe 후 캐시. 미설치(009 미적용) 환경에서
# legacy 폴백 쿼리에 .eq("from_capture", ...)를 넣으면 400 으로 cleanup 이 실패하므로 가드한다.
# (정상 설치는 from_capture 가 있으면 RPC 도 있어 legacy 가 돌지 않지만, 폴백을 방어적으로.)
_from_capture_supported: bool | None = None


def _has_from_capture(db) -> bool:
    global _from_capture_supported
    if _from_capture_supported is None:
        try:
            db.table("stories").select("from_capture").limit(1).execute()
            _from_capture_supported = True
        except Exception:
            _from_capture_supported = False
            logger.info("[cleanup] stories.from_capture 미설치(009 미적용) — legacy 폴백에서 "
                        "캡처 보존 필터 생략(해당 환경엔 캡처글 자체가 없음)")
    return _from_capture_supported


def _cleanup_legacy_batched(db, cutoff: str) -> int:
    """RPC 미설치 시 폴백. 후보를 배치로 끊어 votes 실표수 재확인 후 삭제.
    select↔delete 사이 들어온 투표에 대한 잔여 TOCTOU 창은 RPC 적용 시 사라진다."""
    keep_capture = _has_from_capture(db)
    cand_q = (
        db.table("stories")
        .select("id")
        .is_("arweave_tx_id", "null")
        .lte("vote_count", CLEANUP_MAX_VOTES)
        .lt("created_at", cutoff)
    )
    if keep_capture:
        cand_q = cand_q.eq("from_capture", False)   # 캡처 승격글(삭제된 원본 기록)은 보존
    cand = cand_q.execute()
    cand_ids = [r["id"] for r in (cand.data or [])]
    if not cand_ids:
        return 0

    deleted = 0
    for i in range(0, len(cand_ids), CLEANUP_BATCH):
        batch = cand_ids[i:i + CLEANUP_BATCH]
        vrows = (
            db.table("votes").select("story_id").in_("story_id", batch).execute()
        )
        actual = Counter(r["story_id"] for r in (vrows.data or []))
        to_delete = [sid for sid in batch if actual.get(sid, 0) <= CLEANUP_MAX_VOTES]
        if not to_delete:
            continue
        del_q = (
            db.table("stories")
            .delete()
            .in_("id", to_delete)
            .is_("arweave_tx_id", "null")
            .lte("vote_count", CLEANUP_MAX_VOTES)
        )
        if keep_capture:
            del_q = del_q.eq("from_capture", False)   # 캡처 승격글 보존(이중 안전)
        resp = del_q.execute()
        deleted += len(resp.data or [])

    if deleted:
        logger.info(
            f"[cleanup] {deleted}건 정리 (레거시 배치 · 미박제·{CLEANUP_AGE_DAYS}일↑·"
            f"실표 {CLEANUP_MAX_VOTES}↓)"
        )
    return deleted


async def background_loop() -> None:
    """앱 lifespan 동안 도는 오래된 글 정리 루프."""
    if not CLEANUP_ENABLED:
        logger.info("[cleanup] 비활성화 (STORY_CLEANUP_ENABLED=false)")
        return

    logger.info(
        f"[cleanup] 시작 · interval={CLEANUP_INTERVAL_SEC}s · "
        f"age={CLEANUP_AGE_DAYS}일 · max_votes={CLEANUP_MAX_VOTES}"
    )

    try:
        await asyncio.sleep(CLEANUP_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        return

    while True:
        try:
            await asyncio.to_thread(cleanup_old_pending)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"[cleanup] loop error: {e}")
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SEC)
        except asyncio.CancelledError:
            return
