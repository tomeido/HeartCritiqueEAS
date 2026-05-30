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
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.db import get_db

import logging
logger = logging.getLogger(__name__)

# 정적 fallback 값 (DB 조회 실패 시 또는 동적 비활성화 시 사용)
DEFAULT_THRESHOLD = int(os.environ.get("VOTE_THRESHOLD", "3"))

# 동적 임계값: 활성 투표자 수에 따라 자동 스케일
DYNAMIC_THRESHOLD_ENABLED = (
    os.environ.get("DYNAMIC_THRESHOLD", "true").lower() != "false"
)
MIN_BASE_THRESHOLD = int(os.environ.get("MIN_VOTE_THRESHOLD", str(DEFAULT_THRESHOLD)))
MAX_BASE_THRESHOLD = int(os.environ.get("MAX_VOTE_THRESHOLD", "12"))
# +1 임계값 / N 활성 투표자
VOTERS_PER_VOTE = int(os.environ.get("VOTERS_PER_VOTE", "30"))
# 활성 기간 (며칠 안에 투표한 유저를 활성으로 카운트)
ACTIVE_WINDOW_DAYS = int(os.environ.get("ACTIVE_WINDOW_DAYS", "7"))
# 캐시 TTL
_BASE_CACHE_TTL = 300  # 5분
_base_cache: dict = {"value": None, "expires_at": 0.0, "voters": 0}


def get_dynamic_base_threshold() -> dict:
    """반환: {threshold, active_voters, dynamic}
    활성 투표자 수에 따라 기본 임계값을 산출. 5분 캐시. 비활성화 시 정적값."""
    if not DYNAMIC_THRESHOLD_ENABLED:
        return {
            "threshold": DEFAULT_THRESHOLD,
            "active_voters": 0,
            "dynamic": False,
        }

    now = time.time()
    if _base_cache["value"] is not None and now < _base_cache["expires_at"]:
        return {
            "threshold": _base_cache["value"],
            "active_voters": _base_cache["voters"],
            "dynamic": True,
        }

    try:
        db = get_db()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=ACTIVE_WINDOW_DAYS)
        ).isoformat()
        resp = (
            db.table("votes").select("user_id").gte("created_at", cutoff).execute()
        )
        unique_voters = len({v["user_id"] for v in (resp.data or [])})
        scaled = MIN_BASE_THRESHOLD + (unique_voters // VOTERS_PER_VOTE)
        threshold = max(MIN_BASE_THRESHOLD, min(MAX_BASE_THRESHOLD, scaled))
    except Exception as e:
        logger.warning(f"[threshold] dynamic calc failed: {e}")
        threshold = DEFAULT_THRESHOLD
        unique_voters = 0

    _base_cache["value"] = threshold
    _base_cache["voters"] = unique_voters
    _base_cache["expires_at"] = now + _BASE_CACHE_TTL
    return {
        "threshold": threshold,
        "active_voters": unique_voters,
        "dynamic": True,
    }


def compute_effective_threshold(
    gap_score: Optional[str] = None,
    deleted_count: int = 0,
    blocked_count: int = 0,
) -> dict:
    """반환: {threshold, urgency, reason, base_threshold, active_voters}"""
    base_info = get_dynamic_base_threshold()
    base = base_info["threshold"]
    active_voters = base_info["active_voters"]

    extra = {"base_threshold": base, "active_voters": active_voters}

    # high urgency: -2 (최소 1)
    if deleted_count >= 1 or gap_score == "extreme":
        threshold = max(1, base - 2)
        if deleted_count >= 1 and gap_score == "extreme":
            reason = f"🚨 출처 {deleted_count}개 삭제됨 + 언론 보도 0건"
        elif deleted_count >= 1:
            reason = f"🚨 출처 {deleted_count}개가 이미 삭제됨"
        else:
            reason = "🚨 메이저 언론 보도 0건"
        return {"threshold": threshold, "urgency": "high", "reason": reason, **extra}

    # medium urgency: -1
    if gap_score == "high" or blocked_count >= 1:
        threshold = max(1, base - 1)
        if gap_score == "high" and blocked_count >= 1:
            reason = f"🔍 언론 보도 격차 + 출처 {blocked_count}개 차단"
        elif gap_score == "high":
            reason = "🔍 언론 보도 격차 큼"
        else:
            reason = f"⛔ 출처 {blocked_count}개 차단됨"
        return {"threshold": threshold, "urgency": "medium", "reason": reason, **extra}

    # normal
    return {"threshold": base, "urgency": "normal", "reason": None, **extra}


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
