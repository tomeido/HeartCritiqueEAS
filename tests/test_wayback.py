"""Wayback 위임 박제 회귀 테스트 (순수 파서/빌더 위주).

HTTP/DB 가 필요한 경로(save_now·process_batch)는 제외하고, 응답 파싱·URL 빌드·큐 적재
게이팅(봇차단 도메인 제외)만 검증한다.
"""

import services.wayback as wb
import asyncio
import httpx
import pytest

from services.localdb import LocalClient


def test_snapshot_url_builder():
    assert wb.snapshot_url("20260609123456", "https://theqoo.net/1") == \
        "https://web.archive.org/web/20260609123456/https://theqoo.net/1"


def test_parse_save_job_id_and_error():
    assert wb._parse_save({"url": "https://x/1", "job_id": "spn2-abc"}) == ("spn2-abc", None)
    jid, err = wb._parse_save({"message": "You have already reached the limit"})
    assert jid is None and "limit" in err
    assert wb._parse_save("nope")[0] is None


def test_parse_status_success_pending_error():
    ok = wb._parse_status({"status": "success", "timestamp": "20260609010101",
                           "original_url": "https://clien.net/9"})
    assert ok["status"] == "success"
    assert ok["snapshot_url"] == "https://web.archive.org/web/20260609010101/https://clien.net/9"
    assert wb._parse_status({"status": "pending", "resources": []})["status"] == "pending"
    err = wb._parse_status({"status": "error", "message": "Cloudflare challenge"})
    assert err["status"] == "error" and "Cloudflare" in err["reason"]
    # 알 수 없는/깨진 응답은 보수적으로 pending
    assert wb._parse_status("???")["status"] == "pending"


@pytest.mark.parametrize("override", [
    {"timestamp": None}, {"timestamp": "20259999000000"}, {"original_url": None},
    {"original_url": "javascript:alert(1)"}, {"original_url": "https://user:pass@example.com/1"},
    {"original_url": "/article/1"},
])
def test_status_success_requires_valid_snapshot_metadata(override):
    result = wb._parse_status({"status": "success", "timestamp": "20250101000000",
                               "original_url": "https://example.com/1", **override})
    assert result["status"] == "error"
    assert "snapshot_url" not in result


def test_strict_original_identity_retains_host_path_and_all_query_values():
    target = "https://www.example.com/article/?id=3&ref=one&empty="
    assert wb._same_original(target, "http://www.example.com:80/article/?empty=&ref=one&id=3")
    for other in (
        "https://example.com/article/?id=3&ref=one&empty=",
        "https://m.example.com/article/?id=3&ref=one&empty=",
        "https://www.example.com/article?id=3&ref=one&empty=",
        "https://www.example.com/article/?id=3&ref=two&empty=",
        "https://www.example.com/article/?id=3&ref=one",
        "https://www.example.com/article/?id=3&id=3&ref=one&empty=",
    ):
        assert not wb._same_original(target, other)


def test_parse_user_status_capacity():
    s = wb._parse_user_status({"available": 7, "daily_captures": 90, "daily_captures_limit": 100})
    assert s["available"] == 7 and s["daily_remaining"] == 10
    # 일일 정보 없으면 None (제출은 동시 가용량만으로 판단)
    s2 = wb._parse_user_status({"available": 3})
    assert s2["available"] == 3 and s2["daily_remaining"] is None
    assert wb._parse_user_status("x")["available"] is None


def test_parse_availability_present_and_absent():
    data = {"archived_snapshots": {"closest": {
        "available": True, "status": "200",
        "url": "http://web.archive.org/web/20250101000000/https://bobaedream.co.kr/9",
        "timestamp": "20250101000000"}}}
    snap = wb._parse_availability(data)
    assert snap and snap["timestamp"] == "20250101000000"
    # http → https 정규화
    assert snap["snapshot_url"].startswith("https://web.archive.org/web/")
    # 스냅샷 없음
    assert wb._parse_availability({"url": "x", "archived_snapshots": {}}) is None
    assert wb._parse_availability({"archived_snapshots": {"closest": {"available": False}}}) is None


def test_availability_rejects_error_pages_and_foreign_replay_urls():
    base = {"available": True, "status": "200", "timestamp": "20250101000000",
            "url": "https://web.archive.org/web/20250101000000/https://example.com/1"}
    for override in ({"status": "404"}, {"url": "https://evil.example/web/20250101000000/"},
                     {"timestamp": "not-a-time"}, {"url": "javascript:alert(1)"}):
        assert wb._parse_availability({"archived_snapshots": {"closest": {**base, **override}}}) is None


@pytest.mark.parametrize("original,expected", [
    ("http://www.example.com/article?ref=first&id=3", True),
    ("https://www.example.com/article?id=4&ref=first", False),
    ("https://m.example.com/article?id=3&ref=first", False),
    ("https://www.example.com/article?id=3", False),
    ("javascript:alert(1)", False),
])
def test_availability_binds_replay_original_to_requested_article(original, expected):
    target = "https://www.example.com/article?id=3&ref=first"

    async def run():
        def respond(request):
            assert request.url.params["url"] == target
            return httpx.Response(200, json={"archived_snapshots": {"closest": {
                "available": True, "status": "200", "timestamp": "20250101000000",
                "url": f"https://web.archive.org/web/20250101000000/{original}"}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await wb.availability(target, client)

    assert bool(asyncio.run(run())) is expected


def test_pending_jobs_reject_malformed_or_mismatched_originals():
    db = LocalClient(":memory:", engine="sqlite")
    for job in ("valid", "mismatch", "malformed"):
        db.table("wayback_snapshots").insert({
            "url": f"https://example.com/{job}", "status": "pending", "job_id": job,
            "next_poll_at": "2020-01-01T00:00:00+00:00",
        }).execute()

    async def run():
        def respond(request):
            job = request.url.path.rsplit("/", 1)[1]
            original = "http://example.com/valid" if job == "valid" else "https://example.com/other"
            payload = {"status": "success", "timestamp": "20250101000000", "original_url": original}
            if job == "malformed":
                payload.pop("original_url")
            return httpx.Response(200, json=payload)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await wb._poll_pending(db, client)

    assert asyncio.run(run()) == 3
    rows = {row["job_id"]: row for row in db.table("wayback_snapshots").select("*").execute().data}
    assert rows["valid"]["status"] == "success" and rows["valid"]["snapshot_url"]
    for name in ("mismatch", "malformed"):
        assert rows[name]["status"] == "error"
        assert rows[name]["snapshot_url"] is None and rows[name]["snapshot_timestamp"] is None


def test_no_credentials_queue_looks_up_existing_snapshots_without_post(monkeypatch):
    db = LocalClient(":memory:", engine="sqlite")
    db.table("wayback_snapshots").insert([
        {"url": "https://example.com/found"}, {"url": "https://example.com/missing"}]).execute()
    monkeypatch.setattr(wb, "WAYBACK_ENABLED", True)
    monkeypatch.setattr(wb, "IA_ACCESS_KEY", "")
    monkeypatch.setattr(wb, "IA_SECRET_KEY", "")
    monkeypatch.setattr(wb, "get_db", lambda: db)
    requests = []
    original_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "archive.org"
        if request.url.params.get("url").endswith("/found"):
            return httpx.Response(200, json={"archived_snapshots": {"closest": {
                "available": True, "status": "200", "timestamp": "20250101000000",
                "url": "https://web.archive.org/web/20250101000000/https://example.com/found"}}})
        return httpx.Response(200, json={"archived_snapshots": {}})

    monkeypatch.setattr(wb.httpx, "AsyncClient", lambda: original_client(transport=httpx.MockTransport(respond)))
    result = asyncio.run(wb.process_batch())
    assert result == {"polled": 0, "submitted": 0, "found": 1}
    rows = {r["url"]: r for r in db.table("wayback_snapshots").select("*").execute().data}
    assert rows["https://example.com/found"]["status"] == "success"
    assert rows["https://example.com/missing"]["next_poll_at"]
    asyncio.run(wb.process_batch())
    assert len(requests) == 2  # 미발견 후보를 매 루프 반복 호출하지 않는다.


def test_enqueue_skips_untrackable_and_respects_disabled(monkeypatch):
    captured = {}

    class _Tbl:
        def upsert(self, rows, **k):
            captured["rows"] = rows
            return self
        def execute(self):
            return type("R", (), {"data": []})()

    class _DB:
        def table(self, name):
            captured["table"] = name
            return _Tbl()

    monkeypatch.setattr(wb, "get_db", lambda: _DB())

    # 꺼져 있으면 아무것도 안 함
    monkeypatch.setattr(wb, "WAYBACK_ENABLED", False)
    assert wb.enqueue(["https://theqoo.net/1"]) == 0
    assert "rows" not in captured

    # 공개 본문이 있는 FM코리아도 적재하고 고정 추적 불가 도메인·중복은 제외한다.
    monkeypatch.setattr(wb, "WAYBACK_ENABLED", True)
    n = wb.enqueue([
        "https://theqoo.net/1",
        "https://www.fmkorea.com/123",   # 정상 접근 가능한 출처 → 적재
        "https://issuefeed.dcinside.com/1",  # 고정 로더 → 제외
        "https://www.issuefeed.dcinside.com/2",  # 하위 도메인도 제외
        "https://theqoo.net/1",          # 중복 → 1회만
        "https://clien.net/9",
    ])
    assert n == 3
    urls = {r["url"] for r in captured["rows"]}
    assert urls == {"https://theqoo.net/1", "https://clien.net/9", "https://www.fmkorea.com/123"}
    assert all(r["status"] == "queued" for r in captured["rows"])
