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
# 1표 미만은 '인간 합의 없는 자동·영구 박제'라 무조건 1로 하한 고정(운영자 오설정 가드).
DEFAULT_THRESHOLD = max(1, int(os.environ.get("VOTE_THRESHOLD", "3")))

# 동적 임계값: 활성 투표자 수에 따라 자동 스케일
DYNAMIC_THRESHOLD_ENABLED = (
    os.environ.get("DYNAMIC_THRESHOLD", "true").lower() != "false"
)
MIN_BASE_THRESHOLD = max(1, int(os.environ.get("MIN_VOTE_THRESHOLD", str(DEFAULT_THRESHOLD))))
MAX_BASE_THRESHOLD = max(MIN_BASE_THRESHOLD, int(os.environ.get("MAX_VOTE_THRESHOLD", "12")))
# +1 임계값 / N 활성 투표자 (0 이면 ZeroDivisionError → 최소 1)
VOTERS_PER_VOTE = max(1, int(os.environ.get("VOTERS_PER_VOTE", "30")))
# 활성 기간 (며칠 안에 투표한 유저를 활성으로 카운트)
ACTIVE_WINDOW_DAYS = int(os.environ.get("ACTIVE_WINDOW_DAYS", "7"))

# ── 발행량 기반 난이도 보정 (비트코인 difficulty retarget 풍) ────────────────────
# 일정 기간(ISSUANCE_WINDOW_DAYS) 동안 '발제된(생성된)' 스토리 수로 박제 임계값을 보정한다.
# 비트코인이 채굴 속도에 맞춰 난이도를 재조정하듯, 글 공급이 많으면(목표 초과) 임계값을 올려
# 영구 박제를 더 귀하게(어렵게), 적으면 내려 덜 까다롭게 만든다 → 박제 발행 속도를 자율 조절.
ISSUANCE_ADJUST_ENABLED = os.environ.get("ISSUANCE_ADJUST_ENABLED", "true").lower() != "false"
ISSUANCE_WINDOW_DAYS = max(1, int(os.environ.get("ISSUANCE_WINDOW_DAYS", "7")))   # 재조정 주기(창)
# 목표 발행량: 이 수준이면 보정 0. 기본 28 ≈ hunter 4글/일 × 7일(자연 생성률).
ISSUANCE_TARGET = max(1, int(os.environ.get("ISSUANCE_TARGET", "28")))
# 목표 대비 이 수만큼 초과/미달할 때마다 임계값 ±1.
ISSUANCE_PER_STEP = max(1, int(os.environ.get("ISSUANCE_PER_STEP", "10")))
# 보정 상하한(±). 한 신호가 임계값을 과도하게 흔들지 않게 클램프.
ISSUANCE_MAX_ADJUST = max(0, int(os.environ.get("ISSUANCE_MAX_ADJUST", "4")))

# 캐시 TTL
_BASE_CACHE_TTL = 300  # 5분
_base_cache: dict = {
    "value": None, "expires_at": 0.0, "voters": 0,
    "issuance_count": 0, "issuance_adjust": 0,
}


def _issuance_adjustment(db, now_dt) -> tuple[int, int]:
    """최근 ISSUANCE_WINDOW_DAYS 동안 생성된 스토리 수 → (count, 임계값 보정치).
    공급 많으면 +(어렵게), 적으면 -(쉽게). 보정은 ±ISSUANCE_MAX_ADJUST 로 클램프."""
    if not ISSUANCE_ADJUST_ENABLED:
        return 0, 0
    cutoff = (now_dt - timedelta(days=ISSUANCE_WINDOW_DAYS)).isoformat()
    try:
        resp = (
            db.table("stories").select("id", count="exact", head=True)
            .gte("created_at", cutoff).execute()
        )
        count = resp.count or 0
    except Exception as e:
        logger.warning(f"[threshold] issuance count 실패: {e}")
        return 0, 0
    # 비트코인 retarget 유사: (실제 - 목표)/스텝 을 정수 보정으로, 상하한 클램프.
    raw = (count - ISSUANCE_TARGET) // ISSUANCE_PER_STEP
    adj = max(-ISSUANCE_MAX_ADJUST, min(ISSUANCE_MAX_ADJUST, raw))
    return count, adj


def get_dynamic_base_threshold() -> dict:
    """반환: {threshold, active_voters, dynamic}
    활성 투표자 수에 따라 기본 임계값을 산출. 5분 캐시. 비활성화 시 정적값."""
    if not DYNAMIC_THRESHOLD_ENABLED:
        return {
            "threshold": DEFAULT_THRESHOLD,
            "active_voters": 0,
            "issuance_count": 0,
            "issuance_adjust": 0,
            "dynamic": False,
        }

    now = time.time()
    if _base_cache["value"] is not None and now < _base_cache["expires_at"]:
        return {
            "threshold": _base_cache["value"],
            "active_voters": _base_cache["voters"],
            "issuance_count": _base_cache["issuance_count"],
            "issuance_adjust": _base_cache["issuance_adjust"],
            "dynamic": True,
        }

    issuance_count = issuance_adjust = 0
    try:
        db = get_db()
        now_dt = datetime.now(timezone.utc)
        cutoff = (now_dt - timedelta(days=ACTIVE_WINDOW_DAYS)).isoformat()
        resp = (
            db.table("votes").select("user_id").gte("created_at", cutoff).execute()
        )
        unique_voters = len({v["user_id"] for v in (resp.data or [])})
        scaled = MIN_BASE_THRESHOLD + (unique_voters // VOTERS_PER_VOTE)
        # 발행량 기반 난이도 보정(비트코인 retarget 풍): 공급 많으면 +, 적으면 -.
        issuance_count, issuance_adjust = _issuance_adjustment(db, now_dt)
        threshold = max(1, min(MAX_BASE_THRESHOLD, scaled + issuance_adjust))
    except Exception as e:
        logger.warning(f"[threshold] dynamic calc failed: {e}")
        threshold = max(1, DEFAULT_THRESHOLD)
        unique_voters = 0

    _base_cache["value"] = threshold
    _base_cache["voters"] = unique_voters
    _base_cache["issuance_count"] = issuance_count
    _base_cache["issuance_adjust"] = issuance_adjust
    _base_cache["expires_at"] = now + _BASE_CACHE_TTL
    return {
        "threshold": threshold,
        "active_voters": unique_voters,
        "issuance_count": issuance_count,
        "issuance_adjust": issuance_adjust,
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
    if deleted_count >= 1:
        threshold = max(1, base - 2)
        reason = f"🚨 출처 {deleted_count}개가 이미 삭제됨"
        return {"threshold": threshold, "urgency": "high", "reason": reason, **extra}

    # medium urgency: -1
    if blocked_count >= 1:
        threshold = max(1, base - 1)
        reason = f"⛔ 출처 {blocked_count}개 차단됨"
        return {"threshold": threshold, "urgency": "medium", "reason": reason, **extra}

    # normal
    return {"threshold": base, "urgency": "normal", "reason": None, **extra}


# 자동 박제(되돌릴 수 없음)의 임계값 인하는 '확실한' 신호에만 반응한다.
#   - hard deleted: HTTP 404/410 (본문 패턴 오탐이 끼어들 수 없음)
#   - hard blocked: HTTP 403
# 본문 패턴으로만 잡힌 soft 삭제/차단은 표시(배지/알림)·집계엔 들어가지만, 임계값을 낮춰
# 1표짜리 글을 자동·영구 박제하는 사고를 막기 위해 임계값 인하에는 쓰지 않는다.
# (soft 삭제글도 사람 투표로는 base 임계값에서 정상 박제된다.)
_HARD_DELETED_CODES = (404, 410)
_HARD_BLOCKED_CODE = 403
# 업로드 진행 중 선점 마커 — '박제 완료'로 오인하면 안 됨 (services.archive.PENDING_MARKER).
# 순환 import 회피를 위해 리터럴로 비교.
_PENDING_MARKER = "__pending__"


def count_citation_signals(rows: list) -> dict:
    """citation_checks 행(또는 status/http_code/baseline_at 을 가진 dict)들에서 신호를 집계.
    반환: {deleted, blocked}=표시용 raw, {hard_deleted, hard_blocked}=임계값용.

    임계값 인하는 '목격한 삭제'만 — 우리가 살아있는 걸 직접 확인(baseline_at 캡처)한 출처가
    그 뒤 hard 404/410 으로 사라진 경우만 hard 로 센다. 첫 검사부터 404였던(한 번도 살아있는
    걸 못 본) 링크는 표시용 'deleted' 로는 잡되 hard 에서는 제외한다. 이유:
      · 이미 죽은 링크는 보존할 원본이 없어 '사라지기 전에 박제'할 가치가 없고,
      · 첫 접촉 404 는 일시 장애·안티봇 404 와 구분 불가인데 hard 404 는 sticky(재검사 영구
        제외)라 한 번 오탐이 영구 박제(1표)를 트리거하는 사고가 된다.
    collector/promoter('살아있을 때 잡고→죽는 걸 감시→죽은 걸 공개') 철학과도 정합."""
    deleted = blocked = hard_deleted = hard_blocked = 0
    for r in rows or []:
        st = r.get("status")
        code = r.get("http_code")
        witnessed = bool(r.get("baseline_at"))   # 살아있는 걸 직접 본 적이 있는가
        if st == "deleted":
            deleted += 1
            if code in _HARD_DELETED_CODES and witnessed:
                hard_deleted += 1
        elif st == "blocked":
            blocked += 1
            if code == _HARD_BLOCKED_CODE and witnessed:
                hard_blocked += 1
    return {
        "deleted": deleted,
        "blocked": blocked,
        "hard_deleted": hard_deleted,
        "hard_blocked": hard_blocked,
    }


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
        .select("status,http_code,baseline_at")   # baseline_at: '목격한 삭제'만 hard 로
        .eq("story_id", story_id)
        .execute()
    )
    sig = count_citation_signals(checks_resp.data)

    votes_resp = (
        db.table("votes")
        .select("id", count="exact")
        .eq("story_id", story_id)
        .execute()
    )
    vote_count = votes_resp.count or 0

    # 임계값 인하는 hard 신호만 — soft 본문매치 오탐이 조기 영구박제를 일으키지 않게.
    eff = compute_effective_threshold(
        story.get("gap_score"), sig["hard_deleted"], sig["hard_blocked"]
    )
    tx = story.get("arweave_tx_id")
    return {
        "vote_count": vote_count,
        "deleted_count": sig["deleted"],   # 표시용 raw (배지/알림)
        "blocked_count": sig["blocked"],
        "gap_score": story.get("gap_score"),
        # 선점 마커는 '박제 완료' 아님 → 조기 트리거 스킵/완료 오인 방지
        "archived": bool(tx and tx != _PENDING_MARKER),
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
