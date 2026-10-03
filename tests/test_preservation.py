"""Private captures: real bytes/restore, hostile redirects, bounded omissions."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from services import preservation as p, tracker

# A complete transparent 1x1 PNG, not merely an invented signature.
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082")
URL = "https://www.inven.co.kr/board/webzine/2097/1234"
IMAGE = "https://upload3.inven.co.kr/upload/image.png"


@pytest.fixture(autouse=True)
def safe_dns(monkeypatch):
    monkeypatch.setattr(tracker, "_is_safe_url", lambda _: (True, "", "93.184.216.34"))
    monkeypatch.setattr(p, "MIN_FREE_BYTES", 0)


def payload(**overrides):
    return {"raw_body": b'<article>original text<img src="image.png"><script>alert(1)</script></article>',
            "source_url": URL, "final_url": URL, "fetched_at": "2026-09-26T12:00:00+00:00",
            "http_code": 200, "response_headers": {"Content-Type": "text/html", "Set-Cookie": "secret=yes"},
            "html_truncated": False, "body_text": 'original <script>alert("no")</script> text',
            "parser_version": "test-v1", "media_urls": [IMAGE], **overrides}


def preserve(tmp_path, capture=None, handler=None, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler or (lambda _: httpx.Response(
                200, content=PNG, headers={"content-type": "image/png"})))) as client:
            return await p.preserve_capture(capture or payload(), client, root=tmp_path, **kwargs)
    return asyncio.run(run())


def manifest(root, result):
    path = root / result["manifest_path"]
    return path, json.loads(path.read_bytes())


def test_immutable_capture_restores_exact_html_images_and_offline(tmp_path):
    first = preserve(tmp_path)
    second = preserve(tmp_path)
    assert first["state"] == "complete"
    assert first["manifest_path"] != second["manifest_path"]
    path, saved = manifest(tmp_path, first)
    assert (path.parent / saved["html"]["path"]).read_bytes() == payload()["raw_body"]
    assert (path.parent / saved["media"][0]["path"]).read_bytes() == PNG
    assert "set-cookie" not in saved["response_headers"]
    assert saved["response_headers"]["content-type"] == "text/html"
    offline = (path.parent / "index.html").read_text()
    assert "<script>" not in offline
    assert "&lt;script&gt;" in offline
    assert 'default-src &#' not in offline
    assert "default-src 'none'" in offline
    assert saved["media"][0]["path"] in offline
    assert path.stat().st_mode & 0o077 == 0
    verified = p.verify_capture(first["manifest_path"], first["manifest_sha256"], tmp_path)
    assert verified["valid"]
    assert verified["objects_verified"] == 3
    # Restore to a different private root without network access.
    import shutil
    restored = tmp_path.parent / (tmp_path.name + "-restored")
    shutil.copytree(tmp_path, restored)
    assert p.verify_capture(first["manifest_path"], first["manifest_sha256"], restored)["valid"]


def test_integrity_detects_modified_object_and_manifest(tmp_path):
    result = preserve(tmp_path)
    path, saved = manifest(tmp_path, result)
    (path.parent / saved["media"][0]["path"]).write_bytes(b"tampered")
    assert not p.verify_capture(result["manifest_path"], result["manifest_sha256"], tmp_path)["valid"]
    path.write_bytes(path.read_bytes() + b" ")
    check = p.verify_capture(result["manifest_path"], result["manifest_sha256"], tmp_path)
    assert "manifest_hash_mismatch" in check["errors"]
    assert not p.verify_capture("../outside.json", root=tmp_path)["valid"]


def test_http_and_svg_omissions_are_partial_not_complete(tmp_path):
    def handler(request):
        if request.url.path.endswith(".svg"):
            return httpx.Response(200, content=b'<svg onload="alert(1)"/>', headers={"content-type": "image/svg+xml"})
        return httpx.Response(403)
    result = preserve(tmp_path, payload(media_urls=[IMAGE, IMAGE.replace(".png", ".svg")]), handler)
    _, saved = manifest(tmp_path, result)
    assert result["state"] == "partial" and result["media_failed"] == 2
    assert [x["reason"] for x in saved["media"]] == ["http_error", "unsupported_content_type"]
    assert p.verify_capture(result["manifest_path"], result["manifest_sha256"], tmp_path)["valid"]


def test_per_hop_dns_pin_and_no_client_credentials(tmp_path, monkeypatch):
    calls, resolutions = [], []
    def safe(url):
        resolutions.append(url)
        if "redirected" in url:
            return False, "blocked_ip:127.0.0.1", ""
        return True, "", "93.184.216.34"
    monkeypatch.setattr(tracker, "_is_safe_url", safe)
    def handler(request):
        calls.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "upload3.inven.co.kr"
        assert request.extensions["sni_hostname"] == "upload3.inven.co.kr"
        assert request.headers["referer"] == URL
        assert not request.headers.get("cookie")
        assert not request.headers.get("authorization")
        return httpx.Response(302, headers={"location": "https://upload3.inven.co.kr/redirected.png"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     cookies={"session": "private"}, headers={"Authorization": "Bearer private"}) as client:
            return await p.preserve_capture(payload(), client, root=tmp_path)
    result = asyncio.run(run())
    _, saved = manifest(tmp_path, result)
    assert len(calls) == 1 and len(resolutions) == 2
    assert saved["media"][0]["reason"].startswith("unsafe_url:")


def test_disallowed_host_and_private_redirect_not_fetched(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    result = preserve(tmp_path, payload(media_urls=["https://attacker.example/x.png", IMAGE]), handler)
    _, saved = manifest(tmp_path, result)
    assert len(calls) == 1
    assert all(item["reason"] == "host_or_url_not_allowed" for item in saved["media"])
    assert result["state"] == "partial"


def test_media_count_and_size_budgets_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "MAX_MEDIA", 2)
    monkeypatch.setattr(p, "MAX_MEDIA_BYTES", len(PNG) - 1)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    result = preserve(tmp_path, payload(media_urls=[IMAGE + f"?id={n}" for n in range(3)]), handler)
    _, saved = manifest(tmp_path, result)
    assert len(calls) == 2 and result["media_failed"] == 3
    assert [x["reason"] for x in saved["media"]] == ["byte_limit", "byte_limit", "page_media_budget"]
    assert result["state"] == "partial"


def test_total_media_budget_preserves_only_affordable_images(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "MAX_TOTAL_MEDIA_BYTES", len(PNG))
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    result = preserve(tmp_path, payload(media_urls=[IMAGE + f"?id={n}" for n in range(2)]), handler)
    assert len(calls) == 1 and result["media_saved"] == 1 and result["media_failed"] == 1


def test_historical_capture_does_not_fetch_todays_images(tmp_path):
    def forbidden(_):
        raise AssertionError("Historical recovery must not fetch live media")
    result = preserve(tmp_path, payload(provenance={"provider": "wayback", "snapshot_timestamp": "20200101120000",
                                                  "archive_url": "https://archive.example/a", "token": "never saved"}),
                      forbidden, fetch_media=False)
    _, saved = manifest(tmp_path, result)
    assert saved["media"][0]["reason"] == "historical_media_not_restored"
    assert saved["provenance"]["provider"] == "wayback"
    assert "token" not in saved["provenance"]
    assert result["state"] == "partial"


def test_storage_budget_refuses_new_capture_preserves_old(tmp_path, monkeypatch):
    first = preserve(tmp_path)
    monkeypatch.setattr(p, "MAX_STORE_BYTES", 1)
    with pytest.raises(OSError, match="capture_store_full"):
        preserve(tmp_path)
    assert p.verify_capture(first["manifest_path"], first["manifest_sha256"], tmp_path)["valid"]
    assert len(list(tmp_path.rglob("manifest.json"))) == 1
    assert not list(tmp_path.rglob(".pending-*"))


def test_truncated_html_never_complete_and_credentials_are_redacted(tmp_path):
    raw = b'<meta name="csrf-token" content="secret123"><input value="password123" name="password"><script>var csrf_token="token123";</script><article>body</article>'
    result = preserve(tmp_path, payload(raw_body=raw, html_truncated=True, media_urls=[],
                                       source_url=URL + "?token=private&n=1"))
    path, saved = manifest(tmp_path, result)
    stored = (path.parent / saved["html"]["path"]).read_bytes()
    assert all(secret not in stored for secret in (b"secret123", b"password123", b"token123"))
    assert "private" not in saved["source_url"]
    assert result["state"] == "partial"
    assert saved["html"]["credential_fields_redacted"]
    assert saved["html"]["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert p.verify_capture(result["manifest_path"], result["manifest_sha256"], tmp_path)["valid"]


def test_empty_capture_cannot_claim_preserved(tmp_path):
    with pytest.raises(ValueError, match="requires_original_and_article"):
        preserve(tmp_path, payload(body_text=None, media_urls=[]))
    assert not list(tmp_path.rglob("manifest.json"))


def test_capture_deadline_marks_pending_images_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "PAGE_MEDIA_DEADLINE_SEC", 0)
    result = preserve(tmp_path)
    _, saved = manifest(tmp_path, result)
    assert result["state"] == "partial"
    assert saved["media"][0]["reason"] == "page_media_budget"
