"""이미 삭제된 공개 글의 과거 사본 탐색·비공개 복구. 공개/승격/원본 덮어쓰기 없음."""

import asyncio
import io
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

import httpx

from services.tracker import _decode_body, _host_header, _is_safe_url

MAX_ARCHIVE_BYTES = 2_000_000
MAX_WARC_BYTES = 4_000_000
_TS = re.compile(r"\d{14}")
_CC_PATH = re.compile(r"crawl-data/CC-MAIN-[0-9-]+/segments/[0-9.]+/warc/[A-Za-z0-9_.-]+\.warc\.gz")


def _before(value=None):
    if value is None:
        return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if _TS.fullmatch(value):
        datetime.strptime(value, "%Y%m%d%H%M%S")
        return value
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("before에 UTC Z 또는 시간대가 필요합니다")
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def _same_url(left, right):
    # 저장소의 http/https 변형만 허용. 게시판 ID·글 ID 쿼리는 유지한다.
    try:
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        a, b = urlsplit(left), urlsplit(right)
        if a.scheme not in {"http", "https"} or b.scheme not in {"http", "https"}:
            return False
        if a.username or a.password or b.username or b.password:
            return False
        if (a.port not in (None, 80, 443) or b.port not in (None, 80, 443)) and a.port != b.port:
            return False
        return (a.hostname == b.hostname and a.path == b.path
                and sorted(parse_qsl(a.query, keep_blank_values=True))
                == sorted(parse_qsl(b.query, keep_blank_values=True)))
    except ValueError:
        return False


async def _get(client, url, *, host, params=None, headers=None, limit=MAX_ARCHIVE_BYTES):
    """고정 archive 호스트만, DNS 핀·리다이렉트 금지·응답 크기/시간 한도."""
    u = httpx.URL(url)
    if u.scheme != "https" or u.host != host or u.username or u.password or u.port not in (None, 443):
        raise ValueError("허용되지 않은 archive endpoint")
    safe, reason, ip = await asyncio.to_thread(_is_safe_url, str(u))
    if not safe:
        raise ValueError(f"unsafe archive endpoint: {reason}")
    async with asyncio.timeout(25):
        async with client.stream("GET", u.copy_with(host=ip), params=params,
                                 headers={"User-Agent": "HeartCritiqueEASArchiveLookup/1.0",
                                          **(headers or {}), "Host": _host_header(u)},
                                 extensions={"sni_hostname": u.host},
                                 follow_redirects=False, timeout=20) as resp:
            if resp.status_code not in (200, 206):
                raise ValueError(f"archive HTTP {resp.status_code}")
            chunks = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    raise ValueError("archive response exceeds limit")
                chunks.append(chunk)
            return b"".join(chunks), dict(resp.headers), resp.status_code


def _valid_record(original, timestamp, target, before):
    if not isinstance(timestamp, str) or not _TS.fullmatch(timestamp):
        return False
    try:
        datetime.strptime(timestamp, "%Y%m%d%H%M%S")
    except ValueError:
        return False
    return timestamp <= before and isinstance(original, str) and _same_url(original, target)


def _collections_before(indexes, cutoff, limit):
    """Use crawl-week names to select bounded collections near the requested date.

    Collection weeks are discovery hints only: each returned record is still
    checked against the exact UTC cutoff. Unknown collection formats are skipped.
    """
    eligible = []
    for entry in indexes:
        match = re.fullmatch(r"CC-MAIN-(\d{4})-(\d{2})", entry.get("id", ""))
        if not match:
            continue
        try:
            start = datetime.fromisocalendar(int(match[1]), int(match[2]), 1).strftime("%Y%m%d%H%M%S")
        except ValueError:
            continue
        if start <= cutoff:
            eligible.append((start, entry))
    eligible.sort(key=lambda row: row[0], reverse=True)
    return [entry for _, entry in eligible[:max(0, min(limit, 5))]]


async def find_candidates(url, client, *, before=None, commoncrawl_indexes=3):
    """정확한 URL의 삭제 관측 이전 사본만 탐색. 후보 ≠ 본문 복구 성공."""
    safe, reason, _ = await asyncio.to_thread(_is_safe_url, url)
    if not safe:
        raise ValueError(f"unsafe original URL: {reason}")
    cutoff = _before(before)
    result = {"url": url, "before": cutoff, "candidates": [], "errors": [],
              "searched_at": datetime.now(timezone.utc).isoformat(), "commoncrawl_collections": []}
    try:
        raw, _, _ = await _get(client, "https://web.archive.org/cdx/search/cdx",
                              host="web.archive.org", params={"url": url, "matchType": "exact",
                              "output": "json", "filter": "statuscode:200", "to": cutoff,
                              "fl": "timestamp,original,statuscode,mimetype,digest", "limit": "-10"})
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("bad CDX response")
        if data:
            for values in data[1:]:
                row = dict(zip(data[0], values))
                ts, original = row.get("timestamp"), row.get("original")
                if (row.get("statuscode") == "200" and row.get("mimetype") == "text/html"
                        and _valid_record(original, ts, url, cutoff)):
                    result["candidates"].append({"provider": "wayback", "timestamp": ts,
                        "original_url": original, "archive_url": f"https://web.archive.org/web/{ts}id_/{original}",
                        "digest": row.get("digest"), "content_verified": False})
    except Exception as e:
        result["errors"].append({"provider": "wayback", "error": str(e)[:180]})
    if commoncrawl_indexes:
        try:
            raw, _, _ = await _get(client, "https://index.commoncrawl.org/collinfo.json",
                                   host="index.commoncrawl.org")
            indexes = json.loads(raw)
            if not isinstance(indexes, list):
                raise ValueError("bad Common Crawl collection response")
            for index in _collections_before(indexes, cutoff, commoncrawl_indexes):
                endpoint = index.get("cdx-api", "")
                if not re.fullmatch(r"https://index\.commoncrawl\.org/CC-MAIN-[0-9-]+-index", endpoint):
                    continue
                result["commoncrawl_collections"].append(index.get("id"))
                try:
                    raw, _, _ = await _get(client, endpoint, host="index.commoncrawl.org",
                                          params={"url": url, "matchType": "exact", "output": "json",
                                                  "filter": "status:200", "limit": "10"})
                    for line in raw.splitlines():
                        row = json.loads(line)
                        original, ts = row.get("url"), row.get("timestamp")
                        if (str(row.get("status")) != "200" or row.get("mime") != "text/html"
                                or not _valid_record(original, ts, url, cutoff)):
                            continue
                        filename = row.get("filename", "")
                        offset, length = int(row.get("offset", -1)), int(row.get("length", 0))
                        if not _CC_PATH.fullmatch(filename) or offset < 0 or not 0 < length <= MAX_WARC_BYTES:
                            continue
                        result["candidates"].append({"provider": "commoncrawl", "timestamp": ts,
                            "original_url": original, "archive_url": f"https://data.commoncrawl.org/{filename}",
                            "offset": offset, "length": length, "content_verified": False})
                except Exception as e:
                    result["errors"].append({"provider": "commoncrawl", "collection": index.get("id"),
                                             "error": str(e)[:180]})
                await asyncio.sleep(1)
        except Exception as e:
            result["errors"].append({"provider": "commoncrawl", "error": str(e)[:180]})
    result["candidates"].sort(key=lambda c: c["timestamp"], reverse=True)
    return result


def _warc_html(raw, candidate):
    from warcio.archiveiterator import ArchiveIterator
    for record in ArchiveIterator(io.BytesIO(raw)):
        if record.rec_type != "response":
            continue
        if not _same_url(record.rec_headers.get_header("WARC-Target-URI"), candidate["original_url"]):
            raise ValueError("WARC original URL mismatch")
        recorded_at = record.rec_headers.get_header("WARC-Date")
        if not recorded_at or _before(recorded_at) != candidate["timestamp"]:
            raise ValueError("WARC timestamp mismatch")
        if record.http_headers is None or record.http_headers.get_statuscode() != "200":
            raise ValueError("WARC original response is not 200")
        body = record.content_stream().read(MAX_ARCHIVE_BYTES + 1)
        if len(body) > MAX_ARCHIVE_BYTES:
            raise ValueError("WARC decompressed body exceeds limit")
        return body, dict(record.http_headers.headers)
    raise ValueError("WARC response missing")


async def recover_candidate(candidate, client, *, root=None):
    """선택한 과거 HTML을 검사해 비공개 저장. 현재 원본 사이트에 요청하지 않는다."""
    from services.article_content import extract_article
    from services.preservation import preserve_capture

    provider = candidate["provider"]
    if provider == "wayback":
        expected = f"https://web.archive.org/web/{candidate['timestamp']}id_/{candidate['original_url']}"
        if candidate["archive_url"] != expected:
            raise ValueError("Wayback replay URL mismatch")
        raw, headers, _ = await _get(client, expected, host="web.archive.org")
    elif provider == "commoncrawl":
        start, length = int(candidate["offset"]), int(candidate["length"])
        filename = candidate["archive_url"].removeprefix("https://data.commoncrawl.org/")
        if not _CC_PATH.fullmatch(filename) or start < 0 or not 0 < length <= MAX_WARC_BYTES:
            raise ValueError("invalid WARC range")
        raw, headers, status = await _get(client, candidate["archive_url"], host="data.commoncrawl.org",
                                         headers={"Range": f"bytes={start}-{start+length-1}"}, limit=length)
        if status != 206 or len(raw) != length or not headers.get("content-range", "").startswith(f"bytes {start}-{start+length-1}/"):
            raise ValueError("WARC range response mismatch")
        raw, headers = _warc_html(raw, candidate)
    else:
        raise ValueError("unknown archive provider")
    headers = {k.lower(): v for k, v in headers.items()}
    if "html" not in headers.get("content-type", "").lower():
        raise ValueError("archive does not contain HTML")
    text = _decode_body(raw, None, headers.get("content-type", ""))
    article = extract_article(candidate["original_url"], text)
    if article["status"] != "live":
        raise ValueError(f"archive article not verified: {article['reason']}")
    if not (article.get("body_text") or "").strip():
        raise ValueError("이미지만 있는 과거 글은 당시 이미지까지 확보해야 복구할 수 있습니다")
    retrieved = datetime.now(timezone.utc).isoformat()
    capture = {"raw_body": raw, "response_headers": headers, "source_url": candidate["original_url"],
               "final_url": candidate["original_url"], "http_code": 200, "fetched_at": retrieved,
               "html_truncated": False, "body_text": article["body_text"],
               "media_urls": article["media_urls"], "parser_version": article["parser_version"],
               "provenance": {"provider": provider, "snapshot_timestamp": candidate["timestamp"],
                              "original_url": candidate["original_url"], "archive_url": candidate["archive_url"],
                              "retrieved_at": retrieved}}
    stored = await preserve_capture(capture, client, root=root, fetch_media=False)
    return {"provider": provider, "timestamp": candidate["timestamp"], "stored": stored,
            "content_verified": True, "published": False}
