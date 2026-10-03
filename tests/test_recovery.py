import asyncio
import io
import json

import httpx
import pytest

from services import recovery


def test_cutoff_requires_timezone_and_valid_date():
    assert recovery._before("2026-01-02T03:00:00+09:00") == "20260101180000"
    with pytest.raises(ValueError):
        recovery._before("2026-01-01")
    with pytest.raises(ValueError):
        recovery._before("20269999000000")


def test_archive_identity_keeps_mobile_hosts_and_every_query_parameter():
    assert recovery._same_url("http://example.com/p?a=1&b=2", "https://example.com/p?b=2&a=1")
    assert not recovery._same_url("https://m.example.com/p", "https://example.com/p")
    assert not recovery._same_url("https://example.com/p?ref=1", "https://example.com/p?ref=2")


def test_historical_commoncrawl_selection_skips_newer_crawls():
    indexes = [{"id": "CC-MAIN-2026-38"}, {"id": "CC-MAIN-2025-51"},
               {"id": "CC-MAIN-2025-47"}, {"id": "CC-MAIN-2024-51"}]
    chosen = recovery._collections_before(indexes, "20251201000000", 2)
    assert [entry["id"] for entry in chosen] == ["CC-MAIN-2025-47", "CC-MAIN-2024-51"]


def test_candidates_filter_wrong_post_and_after_deletion(monkeypatch):
    monkeypatch.setattr(recovery, "_is_safe_url", lambda _: (True, "", "8.8.8.8"))
    target = "https://www.inven.co.kr/board/webzine/2097/1"
    rows = [["timestamp", "original", "statuscode", "mimetype", "digest"],
            ["20250101000000", target, "200", "text/html", "a"],
            ["20270101000000", target, "200", "text/html", "b"],
            ["20250101000000", target + "2", "200", "text/html", "c"],
            ["20250101000000", target, "404", "text/html", "d"]]

    async def get(*a, **kw):
        return json.dumps(rows).encode(), {}, 200

    monkeypatch.setattr(recovery, "_get", get)
    report = asyncio.run(recovery.find_candidates(target, None, before="20260101000000", commoncrawl_indexes=0))
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["content_verified"] is False


def test_archive_fetch_rejects_redirects_and_oversize(monkeypatch):
    monkeypatch.setattr(recovery, "_is_safe_url", lambda _: (True, "", "8.8.8.8"))

    async def run(response, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            return await recovery._get(client, "https://web.archive.org/test", host="web.archive.org", **kwargs)

    with pytest.raises(ValueError, match="302"):
        asyncio.run(run(httpx.Response(302, headers={"location": "http://127.0.0.1/"})))
    with pytest.raises(ValueError, match="exceeds"):
        asyncio.run(run(httpx.Response(200, content=b"oversized"), limit=3))


def test_commoncrawl_record_checks_original_time_and_response():
    from warcio.warcwriter import WARCWriter
    from warcio.statusandheaders import StatusAndHeaders
    candidate = {"original_url": "https://example.com/article/1", "timestamp": "20250101000000"}
    buf = io.BytesIO()
    writer = WARCWriter(buf, gzip=True)
    record = writer.create_warc_record(candidate["original_url"], "response",
        payload=io.BytesIO(b"<article>stored original</article>"),
        http_headers=StatusAndHeaders("200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1"),
        warc_headers_dict={"WARC-Date": "2025-01-01T00:00:00Z"})
    writer.write_record(record)
    body, _ = recovery._warc_html(buf.getvalue(), candidate)
    assert b"stored original" in body
    with pytest.raises(ValueError, match="URL mismatch"):
        recovery._warc_html(buf.getvalue(), {**candidate, "original_url": "https://example.com/article/2"})
    with pytest.raises(ValueError, match="timestamp mismatch"):
        recovery._warc_html(buf.getvalue(), {**candidate, "timestamp": "20260101000000"})


def test_recovery_does_not_mix_live_images_or_publish(monkeypatch, tmp_path):
    from services import article_content, preservation
    candidate = {"provider": "wayback", "timestamp": "20250101000000",
                 "original_url": "https://example.com/article/1",
                 "archive_url": "https://web.archive.org/web/20250101000000id_/https://example.com/article/1"}

    async def get(*a, **kw):
        return b"<article>old text</article>", {"content-type": "text/html"}, 200

    async def store(capture, client, *, root, fetch_media):
        assert fetch_media is False
        assert capture["provenance"]["snapshot_timestamp"] == "20250101000000"
        assert capture["provenance"]["retrieved_at"] != capture["provenance"]["snapshot_timestamp"]
        return {"state": "partial", "manifest_path": "a/manifest.json"}

    monkeypatch.setattr(recovery, "_get", get)
    monkeypatch.setattr(article_content, "extract_article", lambda *a: {
        "status": "live", "reason": "", "body_text": "old text", "media_urls": ["https://example.com/old.jpg"],
        "parser_version": "test"})
    monkeypatch.setattr(preservation, "preserve_capture", store)
    report = asyncio.run(recovery.recover_candidate(candidate, None, root=tmp_path))
    assert report["published"] is False
    assert report["stored"]["state"] == "partial"


def test_image_only_historical_article_requires_archived_media_before_preservation(monkeypatch, tmp_path):
    from services import article_content, preservation

    original = "https://www.inven.co.kr/board/webzine/2097/123"
    candidate = {"provider": "wayback", "timestamp": "20250101000000",
                 "original_url": original,
                 "archive_url": f"https://web.archive.org/web/20250101000000id_/{original}"}
    html = '<div id="powerbbsContent"><img src="https://cdn.example/old-evidence.jpg"></div>'
    article = article_content.extract_article(original, html)
    # The real parser recognizes a live image-only post; failing because the
    # article itself was unrecognized would not exercise historical-media safety.
    assert article["status"] == "live" and not article["body_text"]
    assert article["media_urls"] == ["https://cdn.example/old-evidence.jpg"]
    fetches = []
    preservation_calls = []

    async def get(url, **kwargs):
        fetches.append(url)
        return html.encode(), {"content-type": "text/html; charset=utf-8"}, 200

    async def forbidden_preservation(*args, **kwargs):
        preservation_calls.append(args)
        pytest.fail("An image-only historical shell must not be persisted as a recovered article")

    async def archive_get(client, url, **kwargs):
        return await get(url, **kwargs)

    monkeypatch.setattr(recovery, "_get", archive_get)
    monkeypatch.setattr(preservation, "preserve_capture", forbidden_preservation)
    with pytest.raises(ValueError, match="당시 이미지까지 확보"):
        asyncio.run(recovery.recover_candidate(candidate, None, root=tmp_path))
    assert fetches == [candidate["archive_url"]]
    assert preservation_calls == []
    assert list(tmp_path.iterdir()) == []
