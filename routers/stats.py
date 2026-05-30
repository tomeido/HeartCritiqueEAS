"""신호 누적 대시보드 API + 시계열."""

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from services.db import get_db
from services.hunter import get_status as get_hunter_status
from services.threshold import (
    DEFAULT_THRESHOLD,
    get_dynamic_base_threshold,
)

router = APIRouter(prefix="/api")

# /api/stats 는 프론트가 init·생성·투표·재검사·박제 폴링마다 호출하므로 빈도가 높다.
# 전체 테이블 풀스캔 대신 count 쿼리로 집계하고, 60초 프로세스 캐시로 폴링 폭주를 흡수.
_STATS_TTL = 60
_stats_cache: dict = {"value": None, "expires_at": 0.0}


def _count(table: str, build=None) -> int:
    """count='exact' + head=True 로 행 전송 없이 개수만 조회."""
    q = get_db().table(table).select("*", count="exact", head=True)
    if build is not None:
        q = build(q)
    try:
        return q.execute().count or 0
    except Exception as e:
        print(f"[stats] count failed ({table}): {e}")
        return 0


@router.get("/stats")
async def get_stats():
    now_t = time.time()
    if _stats_cache["value"] is not None and now_t < _stats_cache["expires_at"]:
        # 캐시본은 매번 새로 계산되는 hunter 상태만 갱신해 신선도 유지
        cached = _stats_cache["value"]
        cached["hunter"] = get_hunter_status()
        return cached

    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()

    total = _count("stories")
    by_category = {
        "kindness": _count("stories", lambda q: q.eq("category", "kindness")),
        "critique": _count("stories", lambda q: q.eq("category", "critique")),
    }
    archived = _count("stories", lambda q: q.not_.is_("arweave_tx_id", "null"))
    gap_dist = {
        "extreme": _count("stories", lambda q: q.eq("gap_score", "extreme")),
        "high":    _count("stories", lambda q: q.eq("gap_score", "high")),
        "medium":  _count("stories", lambda q: q.eq("gap_score", "medium")),
    }

    citations_total = _count("citation_checks")
    citation_status = {
        st: _count("citation_checks", lambda q, st=st: q.eq("status", st))
        for st in ("live", "deleted", "blocked", "error", "unchecked")
    }

    votes_total = _count("votes")

    stories_24h = _count("stories", lambda q: q.gte("created_at", day_ago))
    archives_24h = _count("stories", lambda q: q.gte("archived_at", day_ago))

    base_info = get_dynamic_base_threshold()

    result = {
        "stories": {
            "total": total,
            "archived": archived,
            "pending": total - archived,
            "by_category": by_category,
        },
        "gap": {
            **gap_dist,
            "high_or_extreme": gap_dist["high"] + gap_dist["extreme"],
        },
        "citations": {
            "total": citations_total,
            **citation_status,
        },
        "votes": {"total": votes_total},
        "recent": {
            "stories_24h": stories_24h,
            "archives_24h": archives_24h,
        },
        "threshold": {
            "base": base_info["threshold"],
            "active_voters": base_info["active_voters"],
            "dynamic": base_info["dynamic"],
            "fallback": DEFAULT_THRESHOLD,
        },
        "hunter": get_hunter_status(),
    }
    _stats_cache["value"] = result
    _stats_cache["expires_at"] = now_t + _STATS_TTL
    return result


@router.get("/stats/timeseries")
async def timeseries(days: int = 30):
    """일별 신규/박제/삭제 감지 카운트. UI 차트용."""
    days = max(1, min(days, 90))
    db = get_db()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()

    # 스토리 (created_at + archived_at)
    stories_resp = (
        db.table("stories")
        .select("created_at,archived_at")
        .gte("created_at", (now - timedelta(days=days + 90)).isoformat())  # archived는 더 옛것도
        .execute()
    )
    # 삭제 감지 (last_checked 시점 사용)
    deletions_resp = (
        db.table("citation_checks")
        .select("last_checked")
        .eq("status", "deleted")
        .gte("last_checked", cutoff)
        .execute()
    )
    # 투표
    votes_resp = (
        db.table("votes")
        .select("created_at")
        .gte("created_at", cutoff)
        .execute()
    )

    by_date: dict = defaultdict(
        lambda: {"stories": 0, "archives": 0, "deletions": 0, "votes": 0}
    )

    for s in stories_resp.data or []:
        c = (s.get("created_at") or "")[:10]
        if c and c >= cutoff[:10]:
            by_date[c]["stories"] += 1
        a = (s.get("archived_at") or "")[:10]
        if a and a >= cutoff[:10]:
            by_date[a]["archives"] += 1
    for d in deletions_resp.data or []:
        k = (d.get("last_checked") or "")[:10]
        if k:
            by_date[k]["deletions"] += 1
    for v in votes_resp.data or []:
        k = (v.get("created_at") or "")[:10]
        if k:
            by_date[k]["votes"] += 1

    today = now.date()
    out = []
    for i in range(days - 1, -1, -1):
        date = (today - timedelta(days=i)).isoformat()
        b = by_date.get(date, {"stories": 0, "archives": 0, "deletions": 0, "votes": 0})
        out.append({"date": date, **b})
    return out
