"""Wayback 위임 박제 회귀 테스트 (순수 파서/빌더 위주).

HTTP/DB 가 필요한 경로(save_now·process_batch)는 제외하고, 응답 파싱·URL 빌드·큐 적재
게이팅(봇차단 도메인 제외)만 검증한다.
"""

import services.wayback as wb


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

    # 켜지면 적재하되 봇차단 도메인(fmkorea)·중복은 제외
    monkeypatch.setattr(wb, "WAYBACK_ENABLED", True)
    n = wb.enqueue([
        "https://theqoo.net/1",
        "https://www.fmkorea.com/123",   # untrackable → 제외
        "https://theqoo.net/1",          # 중복 → 1회만
        "https://clien.net/9",
    ])
    assert n == 2
    urls = {r["url"] for r in captured["rows"]}
    assert urls == {"https://theqoo.net/1", "https://clien.net/9"}
    assert all(r["status"] == "queued" for r in captured["rows"])
