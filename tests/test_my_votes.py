"""'내 투표' 조회(GET /api/my/votes) 테스트.

계약: 본인 토큰으로만 자신의 투표 story_id 목록을 받는다(최신순, 상한 500).
무토큰/무효토큰은 401, DB 일시 오류는 503(내용 누출 없이).
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
import routers.votes as votes_mod


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filters = {}

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self.filters["limit"] = n
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


class FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None

    def table(self, name):
        assert name == "votes"
        self.last_query = FakeQuery(self._rows)
        return self.last_query


@pytest.fixture
def client():
    return TestClient(main.app)


def test_my_votes_returns_ids_for_own_user(monkeypatch, client):
    db = FakeDB([{"story_id": "s1", "created_at": "2026-07-03T00:00:00+00:00"},
                 {"story_id": "s2", "created_at": "2026-07-01T00:00:00+00:00"}])
    monkeypatch.setattr(votes_mod, "_verify_token", lambda auth: "user-abc")
    monkeypatch.setattr(votes_mod, "get_db", lambda: db)
    r = client.get("/api/my/votes", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200
    assert r.json() == {"story_ids": ["s1", "s2"], "total": 2}
    # 본인 user_id 로만 필터됐는지 (다른 사용자 이력 노출 차단)
    assert db.last_query.filters["user_id"] == "user-abc"
    assert db.last_query.filters["limit"] == 500


def test_my_votes_requires_token(client):
    r = client.get("/api/my/votes")
    assert r.status_code == 401


def test_my_votes_db_error_returns_503(monkeypatch, client):
    monkeypatch.setattr(votes_mod, "_verify_token", lambda auth: "user-abc")
    def boom():
        raise RuntimeError("connection reset")
    monkeypatch.setattr(votes_mod, "get_db", boom)
    r = client.get("/api/my/votes", headers={"Authorization": "Bearer t"})
    assert r.status_code == 503
    assert "connection" not in r.text   # 내부 오류 내용 비노출
