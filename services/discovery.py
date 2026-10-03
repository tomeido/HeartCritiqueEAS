"""Private durable discovery backlog; capture budgets never discard discovered URLs.

Claims expire after crashes. A failing source has a shared cooldown, while other
sources continue. The oldest last-attempted source goes first even after restart.
"""

import os
from datetime import datetime, timedelta, timezone

RETRY_BASE_SEC = max(60, int(os.environ.get("COLLECTOR_RETRY_BASE_SEC", "600")))
RETRY_MAX_SEC = max(RETRY_BASE_SEC, int(os.environ.get("COLLECTOR_RETRY_MAX_SEC", "86400")))
CLAIM_SEC = 900


def enqueue(db, source: str, feed: str, items: list[dict]) -> int:
    if not items:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"source": source, "feed": feed, "url": it["url"],
             "guid": it.get("guid"), "title": it.get("title"),
             "summary": it.get("summary"), "priority": it.get("_priority", 0),
             "first_seen": now, "next_attempt_at": now, "status": "queued"}
            for it in items]
    # Rediscovery must not reset attempts, a lease, or retry backoff.
    result = db.table("discovery_queue").upsert(
        rows, on_conflict="url", ignore_duplicates=True).execute()
    return len(result.data or [])


def ready(db, sources: list[str], limit: int, now: datetime | None = None) -> list[tuple[str, list[dict]]]:
    """Bounded per-source reads, sorted by durable last attempt for fair scheduling."""
    now = now or datetime.now(timezone.utc)
    ni = now.isoformat()
    groups = []
    for source in dict.fromkeys(sources):
        # Any unexpired source failure/claim pauses that domain, including its
        # other feeds. This avoids retrying a blocked host through fresh URLs.
        paused = (db.table("discovery_queue").select("id")
                  .eq("source", source)
                  .or_("status.eq.capturing,and(status.eq.retry,cooldown.eq.true)")
                  .gt("next_attempt_at", ni).limit(1).execute().data)
        if paused:
            continue
        latest = (db.table("discovery_queue").select("last_attempt_at")
                  .eq("source", source).not_.is_("last_attempt_at", "null")
                  .order("last_attempt_at", desc=True).limit(1).execute().data)
        rows = (db.table("discovery_queue").select("*").eq("source", source)
                .in_("status", ["queued", "retry", "capturing"])
                .lte("next_attempt_at", ni)
                .order("next_attempt_at").order("first_seen").order("priority", desc=True)
                .limit(max(0, limit)).execute().data or [])
        if rows:
            groups.append((latest[0]["last_attempt_at"] if latest else "", source, rows))
    groups.sort(key=lambda group: group[0])
    return [(source, rows) for _, source, rows in groups]


def claim(db, row: dict, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    claimed = (db.table("discovery_queue").update({
        "status": "capturing", "attempts": row["attempts"] + 1,
        "last_attempt_at": now.isoformat(),
        "next_attempt_at": (now + timedelta(seconds=CLAIM_SEC)).isoformat(),
    }).eq("id", row["id"]).eq("status", row["status"])
        .eq("attempts", row["attempts"]).lte("next_attempt_at", now.isoformat())
        .execute().data)
    return claimed[0] if claimed else None


def finish(db, row: dict, success: bool, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    status = "captured" if success else "retry"
    reason = None
    cooldown = not success
    if not success:
        captured = (db.table("captured_posts").select("status,http_code,reason,captured_at")
                    .eq("url", row["url"]).limit(1).execute().data)
        if captured and captured[0].get("captured_at"):
            status = "captured"
        elif captured and captured[0]["status"] == "deleted" and captured[0]["http_code"] in (404, 410):
            status = "deleted"
        elif captured and captured[0]["status"] == "deleted":
            # A soft-deleted individual post remains retryable, but is not a
            # host outage: it must not stall other live posts in this source.
            cooldown = False
        reason = captured[0].get("reason") if captured else "capture failed"
    delay = min(RETRY_MAX_SEC, RETRY_BASE_SEC * 2 ** min(max(0, row["attempts"] - 1), 16))
    (db.table("discovery_queue").update({
        "status": status, "last_error": reason, "cooldown": cooldown,
        "next_attempt_at": ((now + timedelta(seconds=delay)).isoformat()
                            if status == "retry" else None),
    }).eq("id", row["id"]).eq("status", "capturing")
        .eq("attempts", row["attempts"]).execute())


def resolve(db, url: str, status: str) -> None:
    """Rechecks may recover/confirm deletion without waiting for the queue lease."""
    if status not in ("captured", "deleted"):
        raise ValueError("invalid terminal discovery status")
    (db.table("discovery_queue").update({
        "status": status, "next_attempt_at": None, "last_error": None,
    }).eq("url", url).in_("status", ["queued", "retry", "capturing"]).execute())


def summary(db) -> dict:
    """Only aggregate counts/timestamps may be exposed through public status."""
    unfinished = ["queued", "retry", "capturing"]
    pending = (db.table("discovery_queue").select("id", count="exact", head=True)
               .in_("status", unfinished).execute().count)
    retries = (db.table("discovery_queue").select("id", count="exact", head=True)
               .eq("status", "retry").execute().count)
    oldest = (db.table("discovery_queue").select("first_seen")
              .in_("status", unfinished).order("first_seen").limit(1).execute().data)
    return {"queue_pending": pending or 0, "queue_retry": retries or 0,
            "queue_oldest_at": oldest[0]["first_seen"] if oldest else None}
