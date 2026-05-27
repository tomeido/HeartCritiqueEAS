"""신호 누적 대시보드 API."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from services.db import get_db

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
    }
