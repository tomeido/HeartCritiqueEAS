"""수집 목록 API와 폴링 상태를 실제 로컬 DB·격리된 HTTP로 검증한다."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import stats
from services import collector
from services.localdb import LocalClient


@pytest.fixture
def polling(monkeypatch):
    db = LocalClient(":memory:", engine="sqlite")
    monkeypatch.setattr(collector, "get_db", lambda: db)
    monkeypatch.setattr(collector, "_source_results", {})
    monkeypatch.setattr(collector, "_feed_cursor", 0)
    monkeypatch.setattr(collector, "_promo_cols_supported", None)
    monkeypatch.setattr(collector, "_value_col_supported", None)
    monkeypatch.setattr(collector, "wayback_enqueue", lambda _: None)

    async def no_wait():
        pass

    monkeypatch.setattr(collector, "_sleep_jitter", no_wait)
    yield db
    db._conn.close()


def rss(url):
    return f'<rss><channel><item><title>이웃의 도움</title><link>{url}</link></item></channel></rss>'.encode()


def test_sources_endpoint_without_db_or_network(monkeypatch):
    def forbidden():
        pytest.fail("Public source directory must not access private DB")

    monkeypatch.setattr(stats, "get_db", forbidden)
    monkeypatch.setattr(collector, "get_db", forbidden)
    monkeypatch.setattr(collector, "COLLECTOR_ENABLED", False)
    monkeypatch.setattr(collector, "_source_results", {})
    app = FastAPI()
    app.include_router(stats.router)
    response = TestClient(app).get("/api/sources")
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["feeds"] == sum(s["enabled"] for s in data["sources"])
    assert {s["status"] for s in data["sources"]} <= {"pending", "disabled"}
    assert all(s["last_checked_at"] is None for s in data["sources"])
    assert not any(key in response.text for key in ("body_text", "rss_summary", "baseline_hash"))
    data["sources"][0]["name"] = "mutated"
    assert collector.get_sources_status()["sources"][0]["name"] != "mutated"


def test_poll_records_failures_and_keeps_other_sources_running(polling, monkeypatch):
    feeds = [(name, f"https://{name}.test/feed") for name in ("blocked", "empty", "broken", "ok", "dup")]
    monkeypatch.setattr(collector, "COMMUNITY_FEEDS", feeds)
    monkeypatch.setattr(collector, "COLLECTOR_MAX_CAPTURE_PER_CYCLE", 10)
    original = collector._parse_source

    def parse(source, feed, raw):
        if source == "broken":
            raise ValueError("private details should not reach API")
        return original(source, feed, raw)

    monkeypatch.setattr(collector, "_parse_source", parse)
    seen = []

    async def capture(db, source, feed, item, client):
        seen.append(item["url"])
        return True

    monkeypatch.setattr(collector, "_capture", capture)

    def handler(request):
        host = request.url.host
        if host == "blocked.test":
            return httpx.Response(403)
        return httpx.Response(200, content=b"<html>changed markup</html>" if host == "empty.test"
                              else rss("https://article.test/1"))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collector.poll_feeds(client)

    result = asyncio.run(run())
    assert result["captured"] == result["discovered"] == 1
    assert seen == ["https://article.test/1"]
    states = collector._source_results
    assert [states[url]["status"] for _, url in feeds] == ["blocked", "empty", "error", "ok", "ok"]
    assert states[feeds[0][1]]["http_code"] == 403
    assert "private details" not in str(states)
    assert states[feeds[3][1]]["captured"] == 1


def test_small_capture_budget_rotates_across_all_sources(polling, monkeypatch):
    feeds = [(name, f"https://{name}.test/feed") for name in ("a", "b", "c")]
    monkeypatch.setattr(collector, "COMMUNITY_FEEDS", feeds)
    monkeypatch.setattr(collector, "COLLECTOR_MAX_CAPTURE_PER_CYCLE", 1)
    seen = []

    async def capture(db, source, feed, item, client):
        seen.append(source)
        return True

    monkeypatch.setattr(collector, "_capture", capture)

    async def run():
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=rss(str(req.url) + "/1")))
        async with httpx.AsyncClient(transport=transport) as client:
            for _ in feeds:
                await collector.poll_feeds(client)

    asyncio.run(run())
    assert seen == ["a", "b", "c"]


@pytest.mark.parametrize("challenge", [False, True])
def test_capture_persists_only_real_article_body(polling, monkeypatch, challenge):
    text = "보안 시스템에 의해 보호되고 있습니다" if challenge else "이웃을 도와준 따뜻한 이야기입니다."

    async def observation(*args, **kwargs):
        return {"net": "ok", "http_code": 200, "final_url": "https://example.com/1",
                "text": text, "text_len": len(text), "text_hash": "hash",
                "bot_challenge": challenge, "del_match": False, "blk_match": False}

    monkeypatch.setattr(collector, "fetch_observation", observation)
    result = asyncio.run(collector._capture(polling, "example.com", "https://example.com/rss",
                                           {"url": "https://example.com/1", "title": "이웃의 도움"}, None))
    row = polling.table("captured_posts").select("*").execute().data[0]
    assert result is (not challenge)
    assert row["body_text"] == (None if challenge else text)
    assert bool(row["captured_at"]) is (not challenge)
    assert row["content_hash"] == (None if challenge else "hash")


def test_ppomppu_rss_articles_use_working_https_endpoint():
    item = collector._parse_feed(rss("http://www.ppomppu.co.kr/zboard/view.php?id=freeboard&amp;no=42"))[0]
    assert item["url"] == "https://www.ppomppu.co.kr/zboard/view.php?id=freeboard&no=42"
    # Host suffix lookalikes must not be rewritten.
    item = collector._parse_feed(rss("http://ppomppu.co.kr.evil.test/42"))[0]
    assert item["url"].startswith("http://")


def test_failed_first_capture_is_retried_but_preserved_or_deleted_urls_are_not(polling):
    for index, fields in enumerate([
        {"status": "error", "http_code": 403},
        {"status": "live", "captured_at": "2026-09-26T00:00:00+00:00", "body_text": "본문"},
        {"status": "deleted", "http_code": 404},
    ]):
        polling.table("captured_posts").insert({
            "source": "example.com", "feed": "https://example.com/rss",
            "url": f"https://example.com/{index}", **fields,
        }).execute()
    urls = [f"https://example.com/{index}" for index in range(3)]
    assert collector._existing_urls(polling, urls) == set(urls[1:])


def test_new_html_registry_dispatches_through_real_capture_pipeline(polling, monkeypatch):
    source = next(s for s in collector.COMMUNITY_SOURCES if s["id"] == "dcinside-best")
    monkeypatch.setattr(collector, "COMMUNITY_FEEDS", [(source["domain"], source["url"])])

    async def observation(url, client, capture_text, capture_artifacts=False):
        assert url == "https://gall.dcinside.com/board/view/?id=dcbest&no=123"
        return {"net": "ok", "http_code": 200, "final_url": url,
                "text": "이웃의 도움으로 위기를 극복한 이야기입니다.", "text_len": 25,
                "text_hash": "article-hash", "del_match": False, "blk_match": False}

    monkeypatch.setattr(collector, "fetch_observation", observation)
    markup = '<table><tr><td class="gall_tit"><a href="/board/view/?id=dcbest&amp;no=123">이웃의 도움</a></td></tr></table>'

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, content=markup.encode()))) as client:
            return await collector.poll_feeds(client)

    result = asyncio.run(run())
    assert result["discovered"] == result["captured"] == 1
    row = polling.table("captured_posts").select("*").execute().data[0]
    assert row["source"] == "dcinside.com"
    assert row["body_text"] and row["content_hash"] == "article-hash"
    status = next(s for s in collector.get_sources_status()["sources"] if s["id"] == source["id"])
    assert status["status"] == "ok" and status["captured"] == 1


@pytest.mark.parametrize("raises", [False, True])
def test_capture_failure_stops_that_feed_and_preserves_other_sources(polling, monkeypatch, raises):
    feeds = [("failed", "https://failed.test/feed"), ("ok", "https://ok.test/feed")]
    monkeypatch.setattr(collector, "COMMUNITY_FEEDS", feeds)
    monkeypatch.setattr(collector, "COLLECTOR_MAX_CAPTURE_PER_CYCLE", 4)
    attempted = []

    async def capture(db, source, feed, item, client):
        attempted.append(source)
        if source == "failed" and raises:
            raise ConnectionError("private storage error")
        return source == "ok"

    monkeypatch.setattr(collector, "_capture", capture)

    def handler(req):
        # Two candidates per source, so a failed source must not use the spare budget.
        items = "".join(f'<item><title>이웃의 도움</title><link>{req.url}/{n}</link></item>'
                        for n in (1, 2))
        return httpx.Response(200, content=f"<rss><channel>{items}</channel></rss>".encode())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collector.poll_feeds(client)

    result = asyncio.run(run())
    assert attempted.count("failed") == 1
    assert result["captured"] == attempted.count("ok") == 2
    state = collector._source_results[feeds[0][1]]
    assert state["status"] == "error" and state["attempted"] == state["capture_errors"] == 1
    assert "private storage" not in str(state)
