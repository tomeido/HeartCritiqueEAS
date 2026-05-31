"""
오래된 미박제 글 자동 정리 (Story Cleanup).

미박제(arweave_tx_id IS NULL) 글은 hunter.count_recent_pending() 의 7일 윈도우에서만
빠질 뿐 DB 에는 영원히 남아 무한 누적된다. 영구 박제는 Arweave 에 올라간 글뿐이고,
투표로 선택받지 못한 후보는 일정 기간 뒤 만료시키는 것이 타임캡슐 설계 철학과도 부합한다.

- 대상: arweave_tx_id IS NULL  AND  created_at < now - CLEANUP_AGE_DAYS
        AND  vote_count <= CLEANUP_MAX_VOTES
- 박제된 글(arweave_tx_id 있음)·투표받은 후보(기본 0표 초과)는 필터로 제외 → 절대 삭제 안 됨.
- votes·citation_checks 는 stories(id) on delete cascade 라 함께 정리된다.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from services.db import get_db

logger = logging.getLogger(__name__)

CLEANUP_ENABLED = os.environ.get("STORY_CLEANUP_ENABLED", "true").lower() != "false"
# 생성 후 이 일수가 지난 미박제 글만 정리 대상
CLEANUP_AGE_DAYS = int(os.environ.get("STORY_CLEANUP_AGE_DAYS", "14"))
# 이 표 수 이하만 삭제(기본 0 → 한 표도 없는 글만). 투표받은 후보는 보존.
CLEANUP_MAX_VOTES = int(os.environ.get("STORY_CLEANUP_MAX_VOTES", "0"))
CLEANUP_INTERVAL_SEC = int(os.environ.get("STORY_CLEANUP_INTERVAL_SEC", "21600"))  # 6시간
# 부팅 안정화 + tracker/hunter 와 시간 분산
CLEANUP_INITIAL_DELAY_SEC = int(os.environ.get("STORY_CLEANUP_INITIAL_DELAY_SEC", "120"))


def cleanup_old_pending() -> int:
    """오래된 저득표 미박제 글을 삭제하고 삭제된 글 수를 반환.
    박제된 글(arweave_tx_id 있음)은 필터로 제외되어 절대 삭제되지 않는다."""
    try:
        db = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=CLEANUP_AGE_DAYS)).isoformat()
        resp = (
            db.table("stories")
            .delete()
            .is_("arweave_tx_id", "null")
            .lte("vote_count", CLEANUP_MAX_VOTES)
            .lt("created_at", cutoff)
            .execute()
        )
        deleted = len(resp.data or [])
        if deleted:
            logger.info(
                f"[cleanup] {deleted}건 정리 "
                f"(미박제·{CLEANUP_AGE_DAYS}일↑·{CLEANUP_MAX_VOTES}표↓)"
            )
        return deleted
    except Exception as e:
        logger.warning(f"[cleanup] failed: {e}")
        return 0


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
