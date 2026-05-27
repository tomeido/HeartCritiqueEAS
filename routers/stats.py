"""신호 누적 대시보드 API + 시계열."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from services.db import get_db
from services.threshold import (
    DEFAULT_THRESHOLD,
    get_dynamic_base_threshold,
)

router = APIRouter(prefix="/api")


@router.get("/stats")
async def get_stats():
    db = get_db()

    # 모든 스토리 (필요한 필드만)
    stories_resp = (
        db.table("stories")
        .select("category,arweave_tx_id,gap_score,vote_count,archived_at,created_at")
        .execute()
    )
    stories = stories_resp.data or []

    total = len(stories)
    by_category = {"kindness": 0, "critique": 0}
    archived = 0
    gap_dist = {"extreme": 0, "high": 0, "medium": 0, "low": 0, "none": 0}

    for s in stories:
        cat = s.get("category")
        if cat in by_category:
            by_category[cat] += 1
        if s.get("arweave_tx_id"):
            archived += 1
        g = s.get("gap_score")
        if g and g in gap_dist:
            gap_dist[g] += 1

    # citation_checks 상태별
    checks_resp = db.table("citation_checks").select("status").execute()
    checks = checks_resp.data or []
    citation_status = {"live": 0, "deleted": 0, "blocked": 0, "error": 0, "unchecked": 0}
    for c in checks:
        st = c.get("status")
        if st in citation_status:
            citation_status[st] += 1
    citations_total = len(checks)

    # 투표 총합
    votes_resp = db.table("votes").select("id", count="exact").execute()
    votes_total = votes_resp.count or 0

    # 최근 24시간 활동
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()
    stories_24h = sum(1 for s in stories if (s.get("created_at") or "") >= day_ago)
    archives_24h = sum(
        1 for s in stories
        if s.get("archived_at") and s["archived_at"] >= day_ago
    )

    # 동적 임계값 정보
    base_info = get_dynamic_base_threshold()

    return {
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
    }


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
