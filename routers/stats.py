"""신호 누적 대시보드 API + 시계열."""

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from services.db import get_db
from services.hunter import get_status as get_hunter_status
from services.collector import get_status as get_collector_status
from services.promoter import get_status as get_promoter_status
from services.wayback import get_status as get_wayback_status
from services.threshold import (
    DEFAULT_THRESHOLD,
    get_dynamic_base_threshold,
)

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# /api/stats 는 프론트가 init·생성·투표·재검사·박제 폴링마다 호출하므로 빈도가 높다.
# 전체 테이블 풀스캔 대신 count 쿼리로 집계하고, 60초 프로세스 캐시로 폴링 폭주를 흡수.
_STATS_TTL = 60
_stats_cache: dict = {"value": None, "expires_at": 0.0}
_ts_cache: dict = {}  # days -> {"value", "expires_at"}


def _count(table: str, build=None) -> int | None:
    """count='exact' + head=True 로 행 전송 없이 개수만 조회.
    실패 시 None 을 돌려 '진짜 0' 과 '조회 실패' 를 구분한다(0 캐시 굳음 방지)."""
    q = get_db().table(table).select("*", count="exact", head=True)
    if build is not None:
        q = build(q)
    try:
        return q.execute().count or 0
    except Exception as e:
        logger.warning(f"[stats] count failed ({table}): {e}")
        return None


@router.get("/stats")
async def get_stats():
    now_t = time.time()
    if _stats_cache["value"] is not None and now_t < _stats_cache["expires_at"]:
        # 캐시본은 매번 새로 계산되는 hunter/collector 상태만 갱신해 신선도 유지
        cached = _stats_cache["value"]
        cached["hunter"] = get_hunter_status()
        cached["collector"] = get_collector_status()
        cached["promoter"] = get_promoter_status()
        cached["wayback"] = {**cached.get("wayback", {}), **get_wayback_status()}
        return cached

    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()

    total = _count("stories")
    by_category = {
        "kindness": _count("stories", lambda q: q.eq("category", "kindness")),
        "critique": _count("stories", lambda q: q.eq("category", "critique")),
    }
    # 진짜 박제만 카운트 — archived_at 은 업로드 성공 시에만 기록된다('__pending__' 마커가
    # arweave_tx_id 에 들어가 not-null 매칭으로 과대집계되던 것 방지).
    archived = _count("stories", lambda q: q.not_.is_("archived_at", "null"))
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

    # 선제 수집 현황(captured_posts). 선택 기능(migrations/006)이라 테이블이 없으면 None→0.
    # complete 게이트(아래)에는 넣지 않아 수집기 미설정이 stats 캐싱을 막지 못하게 한다.
    captured_total = _count("captured_posts")
    captured_status = {
        st: _count("captured_posts", lambda q, st=st: q.eq("status", st))
        for st in ("live", "deleted", "blocked", "error", "unchecked")
    }
    # hard 삭제 확정(승격 후보) + 실제 승격된 공개 스토리 수(009 미적용이면 None→0).
    captured_hard_deleted = _count(
        "captured_posts", lambda q: q.not_.is_("hard_deleted_at", "null"))
    promoted_total = _count("stories", lambda q: q.eq("from_capture", True))
    # 승격 파이프라인 상태 분포(promotion_status): 공개 박제(promoted) /
    # PII 차단·보류(blocked_pii) / critique 수동검토 대기(pending_review) / 부적합(skipped).
    promotion_status = {
        st: _count("captured_posts", lambda q, st=st: q.eq("promotion_status", st))
        for st in ("promoted", "blocked_pii", "pending_review", "skipped")
    }

    # Wayback 위임 박제 현황(선택, migrations/007). complete 게이트 밖.
    wayback_status_counts = {
        st: _count("wayback_snapshots", lambda q, st=st: q.eq("status", st))
        for st in ("queued", "pending", "success", "error")
    }

    stories_24h = _count("stories", lambda q: q.gte("created_at", day_ago))
    archives_24h = _count("stories", lambda q: q.gte("archived_at", day_ago))

    base_info = get_dynamic_base_threshold()

    # 카운트가 하나라도 일시적 DB 오류로 실패(None)하면, 0 을 60초간 캐시해 잘못된
    # 0(예: '이야기 0', '박제 0')으로 굳히는 대신 직전 캐시본(있으면)을 그대로 돌려준다.
    all_counts = [
        total, archived, citations_total, votes_total, stories_24h, archives_24h,
        *by_category.values(), *gap_dist.values(), *citation_status.values(),
    ]
    complete = all(c is not None for c in all_counts)
    if not complete:
        stale = _stats_cache["value"]
        if stale is not None:
            stale["hunter"] = get_hunter_status()
            stale["collector"] = get_collector_status()
            stale["promoter"] = get_promoter_status()
            stale["wayback"] = {**stale.get("wayback", {}), **get_wayback_status()}
            return stale
        # 캐시도 없으면(콜드스타트) 0 으로 표시하되 캐시는 남기지 않아 다음 호출이 곧 재시도.

    def z(v):  # None(조회 실패) → 0 으로 표시만 보정
        return v or 0

    by_category = {k: z(v) for k, v in by_category.items()}
    gap_dist = {k: z(v) for k, v in gap_dist.items()}
    citation_status = {k: z(v) for k, v in citation_status.items()}
    total_z = z(total)
    archived_z = z(archived)

    result = {
        "stories": {
            "total": total_z,
            "archived": archived_z,
            "pending": max(0, total_z - archived_z),
            "by_category": by_category,
        },
        "gap": {
            **gap_dist,
            "high_or_extreme": gap_dist["high"] + gap_dist["extreme"],
        },
        "citations": {
            "total": z(citations_total),
            **citation_status,
        },
        "votes": {"total": z(votes_total)},
        "captured": {
            "total": z(captured_total),
            "hard_deleted": z(captured_hard_deleted),
            "promoted": z(promoted_total),
            "promotion": {k: z(v) for k, v in promotion_status.items()},
            **{k: z(v) for k, v in captured_status.items()},
        },
        "wayback": {
            **{k: z(v) for k, v in wayback_status_counts.items()},
            **get_wayback_status(),
        },
        "recent": {
            "stories_24h": z(stories_24h),
            "archives_24h": z(archives_24h),
        },
        "threshold": {
            "base": base_info["threshold"],
            "active_voters": base_info["active_voters"],
            "dynamic": base_info["dynamic"],
            "fallback": DEFAULT_THRESHOLD,
        },
        "hunter": get_hunter_status(),
        "collector": get_collector_status(),
        "promoter": get_promoter_status(),
    }
    # 모든 카운트가 성공했을 때만 캐시(실패분을 60초 동안 0 으로 들고 있지 않게).
    if complete:
        _stats_cache["value"] = result
        _stats_cache["expires_at"] = now_t + _STATS_TTL
    return result


@router.get("/stats/timeseries")
async def timeseries(days: int = 30):
    """일별 신규/박제/삭제 감지 카운트. UI 차트용."""
    days = max(1, min(days, 90))
    now_t = time.time()
    cached = _ts_cache.get(days)
    if cached and now_t < cached["expires_at"]:
        return cached["value"]
    db = get_db()
    now = datetime.now(timezone.utc)
    # 출력은 today-(days-1)..today 의 days 일을 그린다. 쿼리 하한도 그 최저일의 자정에
    # 맞춰야 경계일(부분일)의 행이 버킷에서 누락되지 않는다.
    cutoff = (now - timedelta(days=days - 1)).date().isoformat()

    # 일시적 DB 오류(끊긴 keepalive 등)에 차트 전체가 500 나지 않게 — 직전 캐시(만료됐어도)
    # 가 있으면 그걸, 없으면 빈 시계열을 돌려준다.
    try:
        # 신규 스토리 (created_at 기준)
        stories_resp = (
            db.table("stories")
            .select("created_at")
            .gte("created_at", cutoff)
            .execute()
        )
        # 박제 (archived_at 기준) — created_at 으로 거르면 오래전 생성·최근 박제된 글이
        # 빠져 박제 그래프가 과소집계되므로 archived_at 으로 별도 조회.
        archives_resp = (
            db.table("stories")
            .select("archived_at")
            .not_.is_("archived_at", "null")
            .gte("archived_at", cutoff)
            .execute()
        )
        # 삭제 감지: 최초감지 시각(deleted_at)으로 버킷팅. 매 재검사로 갱신되는
        # last_checked 를 쓰면 막대가 드리프트하므로, 컬럼이 있으면 deleted_at 우선.
        from services.tracker import _has_deleted_at
        if _has_deleted_at(db):
            deletions_resp = (
                db.table("citation_checks")
                .select("deleted_at,last_checked")
                .eq("status", "deleted")
                .gte("deleted_at", cutoff)
                .execute()
            )
        else:
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
    except Exception as e:
        logger.warning(f"[stats] timeseries 조회 실패: {e}")
        if cached:
            return cached["value"]
        return []

    by_date: dict = defaultdict(
        lambda: {"stories": 0, "archives": 0, "deletions": 0, "votes": 0}
    )

    for s in stories_resp.data or []:
        c = (s.get("created_at") or "")[:10]
        if c and c >= cutoff[:10]:
            by_date[c]["stories"] += 1
    for s in archives_resp.data or []:
        a = (s.get("archived_at") or "")[:10]
        if a and a >= cutoff[:10]:
            by_date[a]["archives"] += 1
    for d in deletions_resp.data or []:
        # deleted_at(최초감지) 우선, 없으면 last_checked 폴백.
        k = ((d.get("deleted_at") or d.get("last_checked") or ""))[:10]
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
    _ts_cache[days] = {"value": out, "expires_at": now_t + _STATS_TTL}
    return out
