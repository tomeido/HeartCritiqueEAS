"""목록·상세 읽기의 응답성, 최소 조회, 최신 데이터 계약을 실제 로컬 DB로 검증."""

import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.stories as stories
import services.threshold as threshold
import services.tracker as tracker
from services.localdb import LocalClient


class RecordingQuery:
    def __init__(self, db, name):
        self.db = db
        self.name = name
        self.query = db.client.table(name)
        self.columns = ""

    def select(self, columns, **kwargs):
        self.columns = columns
        self.query = self.query.select(columns, **kwargs)
        return self

    def execute(self):
        self.db.queries.append((self.name, self.columns))
        if self.db.before_execute:
            self.db.before_execute(self.name, self.columns)
        return self.query.execute()

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.query = getattr(self.query, name)(*args, **kwargs)
            return self
        return call


class RecordingDB:
    def __init__(self):
        self.client = LocalClient(":memory:", engine="sqlite")
        self.queries = []
        self.before_execute = None

    def table(self, name):
        return RecordingQuery(self, name)


@pytest.fixture
def api(monkeypatch):
    db = RecordingDB()
    monkeypatch.setattr(stories, "get_db", lambda: db)
    monkeypatch.setattr(tracker, "get_db", lambda: db)
    monkeypatch.setattr(stories, "get_wayback_map", lambda _: {})
    monkeypatch.setattr(stories, "_capture_cols_ok", None)
    monkeypatch.setattr(stories, "_value_col_ok", None)
    monkeypatch.setattr(threshold, "DYNAMIC_THRESHOLD_ENABLED", False)
    app = FastAPI()
    app.include_router(stories.router)

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    yield app, db
    db.client._conn.close()


def insert_story(db, **overrides):
    row = {"category": "kindness", "body": "목록과 전문에 표시할 글"}
    row.update(overrides)
    return db.client.table("stories").insert(row).execute().data[0]


def test_list_reads_only_badge_fields_and_retains_witnessed_signals(api):
    app, db = api
    story = insert_story(db, arweave_tx_id="__pending__", arweave_url="temporary")
    for index, (status, code, witnessed) in enumerate([
        ("deleted", 404, True), ("deleted", 404, False),
        ("deleted", 200, True), ("blocked", 403, True),
    ]):
        db.client.table("citation_checks").insert({
            "story_id": story["id"], "url": f"https://example.com/{index}",
            "status": status, "http_code": code,
            "baseline_at": "2026-09-01T00:00:00+00:00" if witnessed else None,
        }).execute()

    response = TestClient(app).get("/api/stories")
    assert response.status_code == 200
    result = response.json()[0]
    assert result["body"] == story["body"]
    assert "citations" not in result
    assert result["citation_count"] == 4
    assert result["deleted_count"] == 3
    assert result["blocked_count"] == 1
    assert result["effective_threshold"] == max(1, threshold.DEFAULT_THRESHOLD - 2)
    assert result["urgency"] == "high"
    assert result["arweave_tx_id"] is None and result["arweave_url"] is None
    assert len(db.queries) == 2
    assert db.queries[1] == (
        "citation_checks", "story_id,url,status,http_code,baseline_at",
    )


def test_empty_list_does_not_query_tracking(api):
    app, db = api
    assert TestClient(app).get("/api/stories").json() == []
    assert len(db.queries) == 1


def test_missing_optional_column_is_not_retried_in_same_request(api):
    app, db = api
    insert_story(db, value_score=8)

    def missing_capture(_name, columns):
        if "from_capture" in columns:
            raise RuntimeError("42703: column from_capture does not exist")

    db.before_execute = missing_capture
    response = TestClient(app).get("/api/stories")
    assert response.status_code == 200
    assert response.json()[0]["value_score"] == 8
    story_queries = [columns for name, columns in db.queries if name == "stories"]
    assert len(story_queries) == 2
    assert "from_capture" in story_queries[0]
    assert "from_capture" not in story_queries[1]
    assert "value_score" in story_queries[1]


def test_list_and_detail_keep_vote_and_story_updates_fresh(api):
    app, db = api
    story = insert_story(db)
    client = TestClient(app)
    assert client.get("/api/stories").json()[0]["vote_count"] == 0
    db.client.table("stories").update({
        "vote_count": 2, "body": "수정된 전체 본문",
    }).eq("id", story["id"]).execute()
    for url in ["/api/stories", f"/api/stories/{story['id']}"]:
        response = client.get(url)
        assert response.status_code == 200
        row = response.json()[0] if url == "/api/stories" else response.json()
        assert row["vote_count"] == 2
        assert row["body"] == "수정된 전체 본문"


@pytest.mark.parametrize("detail", [False, True])
def test_slow_story_query_does_not_block_other_requests(api, detail):
    app, db = api
    story = insert_story(db)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_query(name, _columns):
        if name == "stories":
            started.set()
            release.wait(timeout=3)
            finished.set()

    db.before_execute = slow_query

    async def check_responsiveness():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            path = f"/api/stories/{story['id']}" if detail else "/api/stories"
            loading = asyncio.create_task(client.get(path))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                probe = await client.get("/probe")
                assert probe.status_code == 200
                assert not finished.is_set(), "DB 조회 중 다른 요청이 정지함"
            finally:
                release.set()
                response = await loading
            assert response.status_code == 200

    asyncio.run(check_responsiveness())
