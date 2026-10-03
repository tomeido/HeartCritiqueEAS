"""Durable discovery and first-capture recovery, using the real DB contract."""

import asyncio
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from services import collector, discovery, localdb
from services.localdb import LocalClient


ENGINES = ["sqlite"] + (["turso"] if localdb.resolve_engine("turso") == "turso" else [])
NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


@pytest.fixture(params=ENGINES)
def db(request, monkeypatch):
    database = LocalClient(":memory:", engine=request.param)
    monkeypatch.setattr(collector, "get_db", lambda: database)
    monkeypatch.setattr(collector, "_source_results", {})
    monkeypatch.setattr(collector, "_feed_cursor", 0)
    monkeypatch.setattr(collector, "_promo_cols_supported", None)
    monkeypatch.setattr(collector, "_value_col_supported", None)
    monkeypatch.setattr(collector, "wayback_enqueue", lambda _: None)

    async def no_sleep():
        pass

    monkeypatch.setattr(collector, "_sleep_jitter", no_sleep)
    return database


def item(url):
    return {"url": url, "title": "이웃을 도와준 소식", "summary": "따뜻한 이야기"}


def queue_row(db, url, source="a", **overrides):
    return db.table("discovery_queue").insert({
        **item(url), "source": source, "feed": f"https://{source}.example/rss",
        "first_seen": NOW.isoformat(), "next_attempt_at": NOW.isoformat(),
        **overrides,
    }).execute().data[0]


def observation(url, text="삭제 전에 보존할 실제 본문"):
    return {"net": "ok", "http_code": 200, "final_url": url, "text": text,
            "text_len": 1000, "text_hash": "page-hash",
            "article_hash": hashlib.sha256(text.encode()).hexdigest(),
            "bot_challenge": False, "del_match": False, "blk_match": False}


def test_discovered_before_budget_and_survives_feed_and_process_loss(tmp_path, monkeypatch):
    path = str(tmp_path / "discovery.db")
    database = LocalClient(path, engine="sqlite")
    monkeypatch.setattr(collector, "get_db", lambda: database)
    monkeypatch.setattr(collector, "COMMUNITY_FEEDS", [("a", "https://a.example/rss")])
    monkeypatch.setattr(collector, "COLLECTOR_MAX_CAPTURE_PER_CYCLE", 0)
    monkeypatch.setattr(collector, "_source_results", {})
    monkeypatch.setattr(collector, "_feed_cursor", 0)
    entries = [item(f"https://a.example/post/{i}") for i in range(4)]
    monkeypatch.setattr(collector, "_parse_source", lambda *args: entries)

    async def feed(*args):
        return b"feed", 200

    async def no_sleep():
        pass

    monkeypatch.setattr(collector, "_fetch_feed", feed)
    monkeypatch.setattr(collector, "_sleep_jitter", no_sleep)
    initial = asyncio.run(collector.poll_feeds(None))
    assert initial["captured"] == 0
    assert initial["queue_pending"] == 4 and initial["queue_oldest_at"]
    assert database.table("discovery_queue").select("id", count="exact").execute().count == 4
    database._conn.close()
    database = LocalClient(path, engine="sqlite")
    entries.clear()  # all URLs have left the listing after restart
    monkeypatch.setattr(collector, "COLLECTOR_MAX_CAPTURE_PER_CYCLE", 4)
    saved = []

    async def capture(db, source, feed, entry, client):
        saved.append(entry["url"])
        return True

    monkeypatch.setattr(collector, "_capture", capture)
    result = asyncio.run(collector.poll_feeds(None))
    assert result["captured"] == 4 and result["discovered"] == 0
    assert result["queue_pending"] == result["queue_retry"] == 0
    assert result["queue_oldest_at"] is None
    assert len(set(saved)) == 4
    assert {r["status"] for r in database.table("discovery_queue").select("status").execute().data} == {"captured"}


def test_retry_backoff_survives_rediscovery_and_pauses_entire_source(db):
    first = queue_row(db, "https://a.example/1")
    queue_row(db, "https://a.example/2")
    queue_row(db, "https://b.example/1", source="b")
    claimed = discovery.claim(db, first, NOW)
    discovery.finish(db, claimed, False, NOW)
    discovery.enqueue(db, "a", "https://a.example/rss", [item(first["url"])])
    retry = db.table("discovery_queue").select("*").eq("url", first["url"]).execute().data[0]
    assert retry["attempts"] == 1 and retry["status"] == "retry"
    assert datetime.fromisoformat(retry["next_attempt_at"]) == NOW + timedelta(seconds=discovery.RETRY_BASE_SEC)
    assert [source for source, _ in discovery.ready(db, ["a", "b"], 3, NOW)] == ["b"]
    due = NOW + timedelta(seconds=discovery.RETRY_BASE_SEC + 1)
    assert {source for source, _ in discovery.ready(db, ["a", "b"], 3, due)} == {"a", "b"}


def test_crashed_lease_expires_and_single_claim_wins(db):
    row = queue_row(db, "https://a.example/1")
    claimed = discovery.claim(db, row, NOW)
    assert claimed["attempts"] == 1
    assert discovery.claim(db, row, NOW) is None
    assert discovery.ready(db, ["a"], 1, NOW + timedelta(seconds=1)) == []
    after = NOW + timedelta(seconds=discovery.CLAIM_SEC + 1)
    recovered = discovery.ready(db, ["a"], 1, after)[0][1][0]
    assert discovery.claim(db, recovered, after)["attempts"] == 2


def test_soft_deleted_post_does_not_starve_source(db):
    gone = queue_row(db, "https://a.example/1")
    live = queue_row(db, "https://a.example/2", first_seen=(NOW + timedelta(seconds=1)).isoformat())
    db.table("captured_posts").insert({
        "source": "a", "url": gone["url"], "status": "deleted", "http_code": 200,
        "reason": "article deletion notice",
    }).execute()
    discovery.finish(db, discovery.claim(db, gone, NOW), False, NOW)
    assert discovery.ready(db, ["a"], 2, NOW)[0][1][0]["url"] == live["url"]
    later = NOW + timedelta(seconds=discovery.RETRY_BASE_SEC + 1)
    due = discovery.ready(db, ["a"], 2, later)[0][1]
    assert [row["url"] for row in due] == [live["url"], gone["url"]]
    assert due[1]["status"] == "retry" and due[1]["cooldown"] is False


def test_fairness_uses_durable_attempts_and_groups_multiple_feeds(db):
    a = queue_row(db, "https://a.example/1")
    queue_row(db, "https://a.example/2", feed="https://a.example/other-feed")
    queue_row(db, "https://b.example/1", source="b")
    claim = discovery.claim(db, a, NOW)
    discovery.finish(db, claim, True, NOW)
    groups = discovery.ready(db, ["a", "a", "b"], 4, NOW)
    assert [source for source, _ in groups] == ["b", "a"]
    assert sum(len(rows) for _, rows in groups) == 2


def test_backoff_is_capped_and_hard_deleted_is_terminal(db):
    row = queue_row(db, "https://a.example/deleted", attempts=999)
    claim = discovery.claim(db, row, NOW)
    discovery.finish(db, claim, False, NOW)
    retry = db.table("discovery_queue").select("*").eq("id", row["id"]).execute().data[0]
    assert datetime.fromisoformat(retry["next_attempt_at"]) == NOW + timedelta(seconds=discovery.RETRY_MAX_SEC)
    db.table("captured_posts").insert({"source": "a", "url": row["url"],
                                       "status": "deleted", "http_code": 404}).execute()
    later = NOW + timedelta(seconds=discovery.RETRY_MAX_SEC)
    discovery.finish(db, discovery.claim(db, retry, later), False, later)
    terminal = db.table("discovery_queue").select("*").eq("id", row["id"]).execute().data[0]
    assert terminal["status"] == "deleted" and terminal["next_attempt_at"] is None
    assert discovery.ready(db, ["a"], 1, later) == []


def test_recheck_recovers_first_body_hash_and_wayback_without_listing(db, monkeypatch):
    url = "https://a.example/once-seen"
    queue_row(db, url, status="retry", attempts=2)
    db.table("captured_posts").insert({"source": "a", "url": url, "title": "제보",
                                       "status": "error", "http_code": 503}).execute()
    submitted = []
    monkeypatch.setattr(collector, "wayback_enqueue", submitted.append)

    async def fetch(url, client, capture_text=False, capture_artifacts=False):
        assert capture_text and capture_artifacts
        return observation(url)

    monkeypatch.setattr(collector, "fetch_observation", fetch)
    assert asyncio.run(collector.recheck_captured_batch()) == 1
    row = db.table("captured_posts").select("*").execute().data[0]
    assert row["status"] == "live" and row["body_text"] == "삭제 전에 보존할 실제 본문"
    assert row["captured_at"] and row["content_hash"] == observation(url)["article_hash"]
    assert row["baseline_hash"] == "page-hash" and submitted == [url]
    assert db.table("discovery_queue").select("status").execute().data[0]["status"] == "captured"


def test_recapture_never_overwrites_original(db, monkeypatch):
    url = "https://a.example/1"
    original = db.table("captured_posts").insert({
        "source": "a", "url": url, "status": "live", "body_text": "first evidence",
        "content_hash": "first-hash", "captured_at": NOW.isoformat(),
    }).execute().data[0]

    async def forbidden(*args, **kwargs):
        pytest.fail("Already archived content must not be fetched for a replacement")

    monkeypatch.setattr(collector, "fetch_observation", forbidden)
    assert asyncio.run(collector._capture(db, "a", "https://a.example/rss", item(url), None))
    assert db.table("captured_posts").select("*").execute().data[0] == original


def test_capture_race_keeps_other_workers_original(db, monkeypatch):
    url = "https://a.example/1"

    async def fetch(*args, **kwargs):
        db.table("captured_posts").update({
            "body_text": "other worker original", "captured_at": NOW.isoformat(),
            "content_hash": "original-hash",
        }).eq("url", url).execute()
        return observation(url, "later replacement")

    monkeypatch.setattr(collector, "fetch_observation", fetch)
    assert not asyncio.run(collector._capture(db, "a", "feed", item(url), None))
    saved = db.table("captured_posts").select("*").execute().data[0]
    assert saved["body_text"] == "other worker original"
    assert saved["content_hash"] == "original-hash" and saved["captured_at"] == NOW.isoformat()


def test_recheck_deletion_preserves_original_evidence(db, monkeypatch):
    url = "https://a.example/1"
    db.table("captured_posts").insert({
        "source": "a", "url": url, "status": "live", "body_text": "first evidence",
        "content_hash": "first-hash", "captured_at": NOW.isoformat(),
    }).execute()

    async def deleted(url, client, capture_text=False, capture_artifacts=False):
        assert not capture_text and not capture_artifacts
        return {"net": "http", "http_code": 404, "final_url": url}

    monkeypatch.setattr(collector, "fetch_observation", deleted)
    assert asyncio.run(collector.recheck_captured_batch()) == 1
    row = db.table("captured_posts").select("*").execute().data[0]
    assert row["status"] == "deleted" and row["hard_deleted_at"]
    assert row["body_text"] == "first evidence" and row["content_hash"] == "first-hash"
    assert row["captured_at"] == NOW.isoformat()


def test_recheck_skips_disabled_catalog_domains_and_keeps_evidence(db, monkeypatch):
    monkeypatch.setattr(collector, "COMMUNITY_SOURCES", [
        {"domain": "disabled.example", "enabled": False},
        {"domain": "partly-enabled.example", "enabled": False},
        {"domain": "partly-enabled.example", "enabled": True},
    ])
    for source in ("disabled.example", "partly-enabled.example", "custom.example"):
        db.table("captured_posts").insert({
            "source": source, "url": f"https://{source}/1", "status": "live",
            "body_text": "keep original", "captured_at": NOW.isoformat(),
        }).execute()
    seen = []

    async def fetch(url, *args, **kwargs):
        seen.append(url)
        return observation(url)

    monkeypatch.setattr(collector, "fetch_observation", fetch)
    assert asyncio.run(collector.recheck_captured_batch()) == 2
    assert set(seen) == {"https://partly-enabled.example/1", "https://custom.example/1"}
    disabled = (db.table("captured_posts").select("*")
                .eq("source", "disabled.example").execute().data[0])
    assert disabled["last_checked"] is None and disabled["body_text"] == "keep original"


def test_capture_artifact_storage_failure_keeps_first_capture_retryable(db, monkeypatch):
    from services import preservation
    url = "https://a.example/1"

    async def fetch(*args, **kwargs):
        return {**observation(url), "capture": {"raw_body": b"original bytes"}}

    async def failed_storage(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(collector, "fetch_observation", fetch)
    monkeypatch.setattr(preservation, "preserve_capture", failed_storage)
    assert not asyncio.run(collector._capture(db, "a", "feed", item(url), None))
    row = db.table("captured_posts").select("*").execute().data[0]
    assert row["captured_at"] is None and row["body_text"] is None
    assert row["capture_manifest_path"] is None and row["status"] == "error"


@pytest.mark.parametrize("media_saved", [0, 1])
def test_image_only_capture_requires_and_stores_manifest(db, monkeypatch, media_saved):
    from services import preservation
    url = "https://a.example/1"

    async def fetch(*args, **kwargs):
        return {**observation(url, ""), "media_urls": ["https://a.example/1.png"],
                "capture": {"raw_body": b"<img>"}}

    async def preserve(*args, **kwargs):
        return {"manifest_path": "abc/manifest.json", "manifest_sha256": "manifest-hash",
                "state": "partial", "media_saved": media_saved}

    monkeypatch.setattr(collector, "fetch_observation", fetch)
    monkeypatch.setattr(preservation, "preserve_capture", preserve)
    assert asyncio.run(collector._capture(db, "a", "feed", item(url), None)) is bool(media_saved)
    row = db.table("captured_posts").select("*").execute().data[0]
    assert bool(row["captured_at"]) is bool(media_saved)
    assert row["status"] == ("live" if media_saved else "error")
    assert row["body_text"] is None
    assert row["capture_manifest_path"] == "abc/manifest.json"
    assert row["capture_manifest_sha256"] == "manifest-hash" and row["capture_state"] == "partial"


@pytest.mark.parametrize("engine", ENGINES)
def test_existing_database_additive_upgrade_preserves_rows(tmp_path, engine):
    path = str(tmp_path / f"old-{engine}.db")
    old_schema = localdb._DDL
    for line in ("  capture_manifest_path TEXT,\n", "  capture_manifest_sha256 TEXT,\n",
                 "  capture_state      TEXT,\n"):
        old_schema = old_schema.replace(line, "")
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute("INSERT INTO captured_posts(id,source,url,first_seen,body_text) VALUES(?,?,?,?,?)",
                     ("existing", "a", "https://a.example/1", NOW.isoformat(), "keep original"))
    for _ in range(2):  # idempotent restart on both supported engines
        upgraded = LocalClient(path, engine=engine)
        row = upgraded.table("captured_posts").select("*").execute().data[0]
        assert row["body_text"] == "keep original" and row["capture_manifest_path"] is None
        assert upgraded.table("discovery_queue").select("id").execute().data == []
