"""글 공유 라우트(GET /s/{story_id}) 테스트.

크롤러에는 글별 OG 메타를, 사람 브라우저에는 /#story= 리다이렉트를 주는 경로.
안전성 핵심: uuid 정규화(비표준 표기·인젝션 차단), 없는 글/오류의 홈 폴백,
본문의 HTML 이스케이프(OG 메타에 사용자 생성 텍스트가 들어간다).
"""

import uuid as uuid_mod
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
import services.db as db_mod

SID = "0a945a2c-1111-4111-8111-222222222222"


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def limit(self, n):
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


class FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "stories"
        return FakeQuery(self._rows)


@pytest.fixture
def client():
    return TestClient(main.app)


def _patch_story(monkeypatch, rows):
    monkeypatch.setattr(db_mod, "get_db", lambda: FakeDB(rows))


def test_share_serves_og_meta_for_sealed_story(monkeypatch, client):
    _patch_story(monkeypatch, [{
        "id": SID, "category": "kindness",
        "body": "지하철에서 쓰러진 노인을 부축한 시민의 이야기가 올라왔다고 한다.",
        "arweave_tx_id": "TX123",
    }])
    r = client.get(f"/s/{SID}", follow_redirects=False)
    assert r.status_code == 200
    html = r.text
    assert '🗄 박제됨 · 따뜻한 선행' in html          # og:title 에 박제 상태
    assert '지하철에서 쓰러진' in html               # og:description 에 본문 발췌
    assert f'/#story={SID}' in html                  # 사람 브라우저 리다이렉트 목표
    assert f'/s/{SID}' in html                       # og:url 영속 링크


def test_share_pending_marker_not_sealed(monkeypatch, client):
    """업로드 진행 중 마커('__pending__')는 '박제됨'으로 노출되지 않는다."""
    _patch_story(monkeypatch, [{
        "id": SID, "category": "critique", "body": "본문", "arweave_tx_id": "__pending__",
    }])
    r = client.get(f"/s/{SID}", follow_redirects=False)
    assert r.status_code == 200
    assert "박제됨" not in r.text
    assert "인류애가 흔들리는 사건" in r.text


def test_share_invalid_uuid_redirects_home(client):
    r = client.get("/s/not-a-uuid", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def test_share_unknown_story_redirects_home(monkeypatch, client):
    _patch_story(monkeypatch, [])
    r = client.get(f"/s/{SID}", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def test_share_db_error_redirects_home(monkeypatch, client):
    def boom():
        raise RuntimeError("connection reset")
    monkeypatch.setattr(db_mod, "get_db", boom)
    r = client.get(f"/s/{SID}", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def test_share_normalizes_nonstandard_uuid(monkeypatch, client):
    """대시 없는 32자 표기도 uuid.UUID 를 통과한다 — 정규형(36자)으로 통일해
    비표준 원문이 HTML/링크에 그대로 박히지 않아야 한다."""
    _patch_story(monkeypatch, [{
        "id": SID, "category": "kindness", "body": "본문", "arweave_tx_id": None,
    }])
    compact = SID.replace("-", "")
    r = client.get(f"/s/{compact}", follow_redirects=False)
    assert r.status_code == 200
    assert f"/#story={SID}" in r.text     # 정규형으로 변환됨
    assert compact not in r.text          # 비표준 원문은 어디에도 없음


def test_share_escapes_body_html(monkeypatch, client):
    """본문(사용자 생성 텍스트에서 유래)이 OG 메타에 들어갈 때 HTML 이스케이프."""
    payload = '"><script>alert(1)</script>'
    _patch_story(monkeypatch, [{
        "id": SID, "category": "critique", "body": payload, "arweave_tx_id": None,
    }])
    r = client.get(f"/s/{SID}", follow_redirects=False)
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text   # 원문 그대로 삽입 금지
    assert "&lt;script&gt;" in r.text                  # 이스케이프되어 존재
