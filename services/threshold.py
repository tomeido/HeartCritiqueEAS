"""
박제 임계값 동적 산출.

기본은 VOTE_THRESHOLD (env, 보통 3표). 검열/삭제 신호가 강하면 임계값을 낮춰서
"사라지기 전에" 박제될 수 있도록 한다. 단, 최소 1표(인간 합의)는 유지.

신호 등급:
  high   : 출처 1개 이상 삭제됨 OR gap_score=extreme (언론 0건)
  medium : gap_score=high (언론 1건) OR 출처 1개 이상 차단됨
  normal : 그 외 (none / low / medium gap)
"""

import os
from typing import Optional

from services.db import get_db

DEFAULT_THRESHOLD = int(os.environ.get("VOTE_THRESHOLD", "3"))


def compute_effective_threshold(
    gap_score: Optional[str] = None,
    deleted_count: int = 0,
    blocked_count: int = 0,
) -> dict:
    """반환: {threshold, urgency, reason}"""
    base = DEFAULT_THRESHOLD

    # high urgency: -2 (최소 1)
    if deleted_count >= 1 or gap_score == "extreme":
        threshold = max(1, base - 2)
        if deleted_count >= 1 and gap_score == "extreme":
            reason = f"🚨 출처 {deleted_count}개 삭제됨 + 언론 보도 0건"
        elif deleted_count >= 1:
            reason = f"🚨 출처 {deleted_count}개가 이미 삭제됨"
        else:
            reason = "🚨 메이저 언론 보도 0건"
        return {"threshold": threshold, "urgency": "high", "reason": reason}

    # medium urgency: -1
    if gap_score == "high" or blocked_count >= 1:
        threshold = max(1, base - 1)
        if gap_score == "high" and blocked_count >= 1:
            reason = f"🔍 언론 보도 격차 + 출처 {blocked_count}개 차단"
        elif gap_score == "high":
            reason = "🔍 언론 보도 격차 큼"
        else:
            reason = f"⛔ 출처 {blocked_count}개 차단됨"
        return {"threshold": threshold, "urgency": "medium", "reason": reason}

    # normal
    return {"threshold": base, "urgency": "normal", "reason": None}


def gather_story_signals(story_id: str) -> dict:
    """스토리 ID 로 현재 신호 정보(gap_score, deleted/blocked count)를 모은 뒤
    effective threshold 계산까지 한 번에. 반환에 vote_count 도 포함."""
    db = get_db()

    story_resp = (
        db.table("stories")
        .select("gap_score,arweave_tx_id,vote_count")
        .eq("id", story_id)
        .limit(1)
        .execute()
    )
    if not story_resp.data:
        return {}
    story = story_resp.data[0]

    checks_resp = (
        db.table("citation_checks")
        .select("status")
        .eq("story_id", story_id)
        .execute()
    )
    deleted = sum(1 for c in (checks_resp.data or []) if c["status"] == "deleted")
    blocked = sum(1 for c in (checks_resp.data or []) if c["status"] == "blocked")

    votes_resp = (
        db.table("votes")
        .select("id", count="exact")
        .eq("story_id", story_id)
        .execute()
    )
    vote_count = votes_resp.count or 0

    eff = compute_effective_threshold(story.get("gap_score"), deleted, blocked)
    return {
        "vote_count": vote_count,
        "deleted_count": deleted,
        "blocked_count": blocked,
        "gap_score": story.get("gap_score"),
        "archived": bool(story.get("arweave_tx_id")),
        **eff,  # threshold, urgency, reason
    }


async def maybe_archive_now(story_id: str) -> bool:
    """현재 vote_count 가 effective threshold 이상이면 즉시 박제 트리거.
    삭제된 출처가 새로 발견되어 임계값이 낮아지는 순간에 호출하면 자동 박제 흐름.
    이미 박제됐거나 투표 부족이면 False."""
    sig = gather_story_signals(story_id)
    if not sig or sig.get("archived"):
        return False
    if sig["vote_count"] < sig["threshold"]:
        return False

    from services.archive import archive_story  # 순환 import 회피
    tx = await archive_story(story_id)
    return tx is not None
