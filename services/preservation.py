"""Private, immutable HTML/image captures and a script-free offline reading copy.

This is intentionally separate from public story archives. Originals are never
served by an API. ``state=complete`` covers the bounded HTML response and images
identified by the article parser, not a pixel-perfect browser/WARC crawl.
"""

import asyncio
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from services import tracker

CAPTURE_ROOT = Path(os.environ.get("CAPTURE_ROOT", "data/captures"))
MAX_MEDIA = int(os.environ.get("CAPTURE_MAX_MEDIA", "12"))
MAX_MEDIA_BYTES = int(os.environ.get("CAPTURE_MAX_MEDIA_BYTES", "4194304"))
MAX_TOTAL_MEDIA_BYTES = int(os.environ.get("CAPTURE_MAX_TOTAL_MEDIA_BYTES", "16777216"))
MEDIA_DEADLINE_SEC = float(os.environ.get("CAPTURE_MEDIA_DEADLINE_SEC", "20"))
PAGE_MEDIA_DEADLINE_SEC = float(os.environ.get("CAPTURE_PAGE_MEDIA_DEADLINE_SEC", "45"))
MAX_STORE_BYTES = int(os.environ.get("CAPTURE_MAX_STORE_BYTES", "10737418240"))
MIN_FREE_BYTES = int(os.environ.get("CAPTURE_MIN_FREE_BYTES", "536870912"))
_CAPTURE_HEADERS = {"content-type", "content-length", "content-encoding", "date",
                    "last-modified", "etag", "cache-control"}
_SENSITIVE = re.compile(r"(?:token|secret|password|passwd|authorization|auth|session|cookie|api[-_]?key|signature|nonce|csrf|xsrf)", re.I)
# Deliberately explicit: a public article may link arbitrary hosts. Only its own
# host and the image CDNs used by supported communities may be fetched.
_IMAGE_DOMAINS = {
    "inven.co.kr", "ruliweb.com", "ruliweb.net", "dcinside.com", "dcinside.co.kr",
    "dcimg1.dcinside.co.kr", "dcimg4.dcinside.co.kr", "dcimg5.dcinside.co.kr",
    "ppomppu.co.kr", "donga.com", "mlbpark.com", "clien.net", "bobaedream.co.kr",
    "theqoo.net", "nate.com", "nateimg.co.kr", "pstatic.net", "daumcdn.net",
    "kakaocdn.net", "humoruniv.com", "todayhumor.co.kr", "slrclub.com",
    "ddanzi.com", "dogdrip.net", "82cook.com", "fmkorea.com", "fmkorea.net",
    "quasarzone.com", "quasarzone.co.kr", "dmitory.com", "damoang.net",
    "imgur.com", "i.imgur.com",
}
_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clean_url(url: str) -> str:
    """Never record userinfo or authentication/signed query parameters."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        query = urlencode([(key, "[redacted]" if _SENSITIVE.search(key) else val)
                           for key, val in parse_qsl(parsed.query, keep_blank_values=True)])
        return urlunparse((parsed.scheme, host, parsed.path, parsed.params, query, ""))
    except ValueError:
        return "[invalid URL]"


def _redact_body(raw: bytes) -> tuple[bytes, bool]:
    # Preserve original encoding and every other byte. Transport credentials are
    # never stored; common public-page CSRF/nonce/session fields are redacted too.
    name = rb"(?:[\w-]*(?:token|secret|password|session|cookie|csrf|xsrf|api[_-]?key)[\w-]*)"
    out = raw
    tag_pattern = re.compile(rb"<(?:input|meta)\b[^>]*>", re.I)
    def redact_tag(match):
        tag = match.group(0)
        if re.search(rb"\b(?:name|id)\s*=\s*['\"]?" + name, tag, re.I):
            return re.sub(rb"(\b(?:value|content)\s*=\s*)(['\"])(.*?)\2",
                          rb"\1\2[redacted]\2", tag, flags=re.I | re.S)
        return tag
    out = tag_pattern.sub(redact_tag, out)
    out = re.sub(rb"(['\"]?" + name + rb"['\"]?\s*[:=]\s*)(['\"])(.*?)\2",
                 rb"\1\2[redacted]\2", out, flags=re.I | re.S)
    out = re.sub(rb"((?:document\.)?cookie\s*=\s*)(['\"])(.*?)\2",
                 rb"\1\2[redacted]\2", out, flags=re.I | re.S)
    out = re.sub(rb"([?&](?:[\w-]*(?:token|secret|session|signature|api[_-]?key|csrf|xsrf)[\w-]*)=)[^&\s'\"<>]+",
                 rb"\1[redacted]", out, flags=re.I)
    return out, out != raw


def _media_host_allowed(url: str, page_url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        page_host = (urlparse(page_url).hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("https", "http") or not host or parsed.username or parsed.password:
            return False
        if parsed.port not in (None, 80, 443):
            return False
        if any(_SENSITIVE.search(key) for key, _ in parse_qsl(parsed.query)):
            return False
        return host == page_host or any(host == d or host.endswith("." + d) for d in _IMAGE_DOMAINS)
    except ValueError:
        return False


def _valid_image(body: bytes, content_type: str) -> bool:
    return {
        "image/jpeg": body.startswith(b"\xff\xd8\xff"),
        "image/png": body.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": body.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP",
    }.get(content_type, False)


async def _fetch_media(url: str, page_url: str, client: httpx.AsyncClient,
                       byte_limit: int) -> tuple[dict, bytes | None]:
    """Reuse the tracker's resolver/IP pin and validate every redirect hop."""
    item = {"url": _clean_url(url), "state": "failed"}
    cur = url
    try:
        async with asyncio.timeout(MEDIA_DEADLINE_SEC):
            for _ in range(tracker.MAX_REDIRECTS + 1):
                if not _media_host_allowed(cur, page_url):
                    return {**item, "reason": "host_or_url_not_allowed"}, None
                safe, reason, ip = await asyncio.to_thread(tracker._is_safe_url, cur)
                if not safe:
                    return {**item, "reason": f"unsafe_url:{reason}"}, None
                target = httpx.URL(cur)
                headers = {"User-Agent": tracker.USER_AGENT, "Host": tracker._host_header(target),
                           "Accept": "image/png,image/jpeg,image/gif,image/webp", "Cookie": "", "Authorization": "",
                           "Referer": str(httpx.URL(_clean_url(page_url)))}
                async with client.stream("GET", target.copy_with(host=ip), headers=headers,
                                         extensions={"sni_hostname": target.host}, timeout=tracker.HTTP_TIMEOUT,
                                         follow_redirects=False, auth=None) as response:
                    if 300 <= response.status_code < 400 and "location" in response.headers:
                        cur = str(target.join(response.headers["location"]))
                        continue
                    item.update({"final_url": _clean_url(cur), "http_code": response.status_code})
                    if response.status_code != 200:
                        return {**item, "reason": "http_error"}, None
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type not in _IMAGE_TYPES:
                        return {**item, "reason": "unsupported_content_type", "content_type": content_type}, None
                    try:
                        size = int(response.headers.get("content-length", "0"))
                    except ValueError:
                        size = 0
                    if size > byte_limit:
                        return {**item, "reason": "byte_limit", "truncated": True}, None
                    chunks, total = [], 0
                    async for chunk in response.aiter_bytes(chunk_size=65_536):
                        total += len(chunk)
                        item["received_bytes"] = total
                        if total > byte_limit:
                            return {**item, "reason": "byte_limit", "truncated": True}, None
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if not _valid_image(body, content_type):
                        return {**item, "reason": "invalid_image_signature"}, None
                    return {**item, "state": "saved", "content_type": content_type,
                            "bytes": len(body), "sha256": _sha(body), "extension": _IMAGE_TYPES[content_type]}, body
            return {**item, "reason": "redirect_limit"}, None
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return {**item, "reason": "timeout"}, None
    except Exception as exc:
        return {**item, "reason": f"network:{type(exc).__name__}"}, None


def _offline_html(manifest: dict, body_text: str) -> bytes:
    parts = ["<!doctype html><html lang=ko><meta charset=utf-8>",
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             "<title>Private article capture</title><style>body{max-width:900px;margin:3rem auto;padding:1rem;font-family:sans-serif}pre{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:100%}small{color:#555}</style>",
             "<h1>보존된 게시물</h1><small>", html.escape(manifest["source_url"]), "<br>",
             html.escape(manifest["fetched_at"]), " · ", html.escape(manifest["state"]),
             "</small><pre>", html.escape(body_text), "</pre>"]
    for item in manifest["media"]:
        if item["state"] == "saved":
            parts.extend(['<figure><img src="', html.escape(item["path"], quote=True),
                          '" alt="보존된 첨부 이미지"><figcaption>', html.escape(item["url"]), "</figcaption></figure>"])
        else:
            parts.extend(["<p>이미지 미확보: ", html.escape(item["url"]), " (", html.escape(item["reason"]), ")</p>"])
    parts.append("</html>")
    return "".join(parts).encode("utf-8")


def _write_file(path: Path, content: bytes) -> None:
    with path.open("xb") as output:
        os.chmod(path, 0o600)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _commit(root: Path, capture: dict, manifest: dict, objects: dict[str, bytes]) -> dict:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    # Serialize budget check and commit across processes. Never remove captures
    # when full: raise so the persistent discovery queue can retry later.
    with (root / ".write.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        os.chmod(root / ".write.lock", 0o600)
        offline = _offline_html(manifest, capture.get("body_text") or "")
        manifest["offline"] = {"path": "index.html", "bytes": len(offline), "sha256": _sha(offline)}
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
        used = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        required = sum(len(value) for value in objects.values()) + len(offline) + len(encoded)
        if used + required > MAX_STORE_BYTES:
            raise OSError("capture_store_full")
        if shutil.disk_usage(root).free - required < MIN_FREE_BYTES:
            raise OSError("capture_disk_reserve")
        url_hash = _sha(manifest["source_url"].encode())
        parent = root / "versions" / url_hash
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:12]
        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
        try:
            for name, payload in objects.items():
                _write_file(staging / name, payload)
            _write_file(staging / "index.html", offline)
            _write_file(staging / "manifest.json", encoded)
            final = parent / version
            os.rename(staging, final)
            fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return {"manifest_path": str((final / "manifest.json").relative_to(root)),
            "manifest_sha256": _sha(encoded), "state": manifest["state"],
            "html_sha256": manifest["html"]["sha256"],
            "media_saved": sum(item["state"] == "saved" for item in manifest["media"]),
            "media_failed": sum(item["state"] != "saved" for item in manifest["media"]),
            "truncated": manifest["html"]["truncated"]}


async def preserve_capture(capture: dict, client: httpx.AsyncClient,
                           root: Path | str | None = None,
                           fetch_media: bool = True) -> dict:
    """Store a valid tracker capture; errors propagate for persistent retries."""
    raw = capture.get("raw_body")
    if not isinstance(raw, bytes) or not raw or not (capture.get("body_text") or capture.get("media_urls")):
        raise ValueError("capture_requires_original_and_article")
    if len(raw) > tracker.MAX_BODY_BYTES:
        raise ValueError("capture_exceeds_html_limit")
    stored, redacted = _redact_body(raw)
    raw_hash = _sha(stored)
    raw_path = f"html-{raw_hash}.bin"
    objects = {raw_path: stored}
    media, total = [], 0
    seen = set()
    deadline = asyncio.get_running_loop().time() + PAGE_MEDIA_DEADLINE_SEC
    for index, url in enumerate(capture.get("media_urls") or []):
        if not isinstance(url, str) or url in seen:
            continue
        seen.add(url)
        if not fetch_media:
            media.append({"url": _clean_url(url), "state": "failed", "reason": "historical_media_not_restored"})
            continue
        remaining_time = deadline - asyncio.get_running_loop().time()
        remaining_bytes = min(MAX_MEDIA_BYTES, MAX_TOTAL_MEDIA_BYTES - total)
        if index >= MAX_MEDIA or remaining_bytes <= 0 or remaining_time <= 0:
            media.append({"url": _clean_url(url), "state": "failed", "reason": "page_media_budget"})
            continue
        try:
            async with asyncio.timeout(remaining_time):
                item, body = await _fetch_media(url, capture["final_url"], client, remaining_bytes)
        except asyncio.TimeoutError:
            item, body = {"url": _clean_url(url), "state": "failed", "reason": "page_media_timeout"}, None
        total += item.get("received_bytes", 0)
        if body is not None:
            path = f"image-{item['sha256']}{item.pop('extension')}"
            item["path"] = path
            objects[path] = body
        media.append(item)
    truncated = bool(capture.get("html_truncated"))
    state = "partial" if truncated or any(item["state"] != "saved" for item in media) else "complete"
    body_text = capture.get("body_text") or ""
    manifest = {
        "schema_version": 1, "scope": "article_html_and_discovered_raster_images", "state": state,
        "source_url": _clean_url(capture["source_url"]), "final_url": _clean_url(capture["final_url"]),
        "fetched_at": capture["fetched_at"], "preserved_at": datetime.now(timezone.utc).isoformat(),
        "http_code": capture["http_code"], "parser_version": capture.get("parser_version"),
        "response_headers": {key.lower(): value for key, value in capture.get("response_headers", {}).items()
                             if key.lower() in _CAPTURE_HEADERS},
        "html": {"path": raw_path, "sha256": raw_hash, "bytes": len(stored), "truncated": truncated,
                 "credential_fields_redacted": redacted, "response_sha256": _sha(raw),
                 "representation": "decoded_http_body_bytes"},
        "article_text_sha256": _sha(body_text.encode()), "media": media,
        "limits": {"max_media": MAX_MEDIA, "max_media_bytes": MAX_MEDIA_BYTES,
                   "max_total_media_bytes": MAX_TOTAL_MEDIA_BYTES, "page_media_seconds": PAGE_MEDIA_DEADLINE_SEC},
    }
    if capture.get("provenance"):
        manifest["provenance"] = {
            key: (_clean_url(str(value)) if key.endswith("url") else str(value))
            for key, value in capture["provenance"].items()
            if key in {"provider", "snapshot_timestamp", "original_url", "archive_url", "retrieved_at"}
        }
    return await asyncio.to_thread(_commit, Path(root) if root is not None else CAPTURE_ROOT,
                                   capture, manifest, objects)


def verify_capture(manifest_path: str, expected_sha256: str | None = None,
                   root: Path | str | None = None) -> dict:
    """Read-only integrity audit for backup/restore and deployment checks."""
    base = (Path(root) if root is not None else CAPTURE_ROOT).resolve()
    path = (base / manifest_path).resolve()
    errors, verified = [], 0
    if not path.is_relative_to(base):
        return {"valid": False, "errors": ["manifest_outside_store"], "objects_verified": 0}
    try:
        encoded = path.read_bytes()
        if expected_sha256 and _sha(encoded) != expected_sha256:
            errors.append("manifest_hash_mismatch")
        manifest = json.loads(encoded)
        if manifest.get("schema_version") != 1 or manifest.get("state") not in ("complete", "partial"):
            errors.append("invalid_manifest_schema")
        records = [manifest["html"], manifest["offline"]] + [item for item in manifest["media"] if item["state"] == "saved"]
        for record in records:
            obj = (path.parent / record["path"]).resolve()
            if not obj.is_relative_to(path.parent) or obj == path:
                errors.append("object_path_escape")
                continue
            try:
                content = obj.read_bytes()
                if len(content) != record["bytes"] or _sha(content) != record["sha256"]:
                    errors.append(f"object_hash_mismatch:{record['path']}")
                else:
                    verified += 1
            except OSError:
                errors.append(f"object_missing:{record['path']}")
        if manifest["state"] == "complete" and (manifest["html"]["truncated"] or any(item["state"] != "saved" for item in manifest["media"])):
            errors.append("false_complete")
        return {"valid": not errors, "errors": errors, "objects_verified": verified,
                "state": manifest["state"], "manifest_sha256": _sha(encoded)}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"valid": False, "errors": [f"manifest_unreadable:{type(exc).__name__}"], "objects_verified": verified}
