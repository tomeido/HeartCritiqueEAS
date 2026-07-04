"""생성 중복 방지 게이트(services/dedup.py) 테스트.

원칙 검증: 기지(旣知) 출처만 제외하고, DB 실패·비활성 시엔 절대 생성을 막지 않는다
(가용성 우선 — dedup 은 best-effort 필터일 뿐 게이트키퍼가 아니다).
"""

from types import SimpleNamespace

import pytest

from services import dedup


# ── fake DB ──────────────────────────────────────────────────────────────────
class FakeQuery:
    def __init__(self, known_rows, error=None):
        self._known = known_rows
        self._error = error
        self._in_vals = []

    def select(self, *a, **k):
        return self

    def in_(self, col, vals):
        self._in_vals = list(vals)
        return self

    def execute(self):
        if self._error:
            raise self._error
        return SimpleNamespace(
            data=[{"url": u} for u in self._known if u in self._in_vals]
        )


class FakeDB:
    def __init__(self, known_rows, error=None):
        self._q = FakeQuery(known_rows, error)
        self.table_calls = 0

    def table(self, name):
        assert name == "citation_checks"
        self.table_calls += 1
        return self._q


def _patch_db(monkeypatch, known_rows, error=None):
    db = FakeDB(known_rows, error)
    monkeypatch.setattr(dedup, "get_db", lambda: db)
    return db


R1 = {"title": "글1", "url": "https://pann.nate.com/talk/1", "content": "x" * 100}
R2 = {"title": "글2", "url": "https://theqoo.net/hot/2", "content": "y" * 100}


# ── filter_known_sources ─────────────────────────────────────────────────────
def test_filter_drops_known_keeps_fresh(monkeypatch):
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    _patch_db(monkeypatch, [R1["url"]])
    fresh, dropped = dedup.filter_known_sources([R1, R2])
    assert fresh == [R2]
    assert dropped == 1


def test_filter_all_known_returns_empty(monkeypatch):
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    _patch_db(monkeypatch, [R1["url"], R2["url"]])
    fresh, dropped = dedup.filter_known_sources([R1, R2])
    assert fresh == []
    assert dropped == 2


def test_filter_empty_input_no_db_call(monkeypatch):
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    db = _patch_db(monkeypatch, [])
    fresh, dropped = dedup.filter_known_sources([])
    assert fresh == [] and dropped == 0
    assert db.table_calls == 0


def test_filter_db_error_passes_through(monkeypatch):
    """가용성 우선: DB 조회 실패가 생성을 막지 않는다."""
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    _patch_db(monkeypatch, [], error=RuntimeError("connection reset"))
    fresh, dropped = dedup.filter_known_sources([R1, R2])
    assert fresh == [R1, R2]
    assert dropped == 0


def test_filter_disabled_no_db_call(monkeypatch):
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", False)
    db = _patch_db(monkeypatch, [R1["url"]])
    fresh, dropped = dedup.filter_known_sources([R1, R2])
    assert fresh == [R1, R2] and dropped == 0
    assert db.table_calls == 0


# ── all_known (gemini 사후 게이트) ───────────────────────────────────────────
def test_all_known_true_when_every_uri_known(monkeypatch):
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    _patch_db(monkeypatch, [R1["url"], R2["url"]])
    cites = [{"title": "a", "uri": R1["url"]}, {"title": "b", "uri": R2["url"]}]
    assert dedup.all_known(cites) is True


def test_all_known_false_when_any_fresh(monkeypatch):
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    _patch_db(monkeypatch, [R1["url"]])
    cites = [{"title": "a", "uri": R1["url"]}, {"title": "b", "uri": R2["url"]}]
    assert dedup.all_known(cites) is False


def test_all_known_false_on_empty_or_disabled(monkeypatch):
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    _patch_db(monkeypatch, [])
    assert dedup.all_known([]) is False
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", False)
    assert dedup.all_known([{"title": "a", "uri": R1["url"]}]) is False


def test_all_known_db_error_false(monkeypatch):
    """조회 실패 시 '중복 아님'으로 강등 — 생성이 dedup 때문에 죽지 않는다."""
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    _patch_db(monkeypatch, [], error=RuntimeError("boom"))
    assert dedup.all_known([{"title": "a", "uri": R1["url"]}]) is False


# ── llm._groq_search 연결(wiring) ────────────────────────────────────────────
def test_groq_search_filters_known(monkeypatch):
    """검색 3단 폴백 뒤 dedup 이 걸려 기지 출처가 후보에서 빠지는지 확인.
    community_count(격차 신호)는 dedup 이전 값을 유지해야 한다."""
    from services import llm

    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    _patch_db(monkeypatch, [R1["url"]])
    monkeypatch.setattr(
        llm, "tavily_search",
        lambda *a, **k: {"results": [
            {"title": R1["title"], "url": R1["url"], "content": R1["content"]},
            {"title": R2["title"], "url": R2["url"], "content": R2["content"]},
        ]},
    )
    results, community_count = llm._groq_search("쿼리", "kindness", None)
    assert [r["url"] for r in results] == [R2["url"]]
    assert community_count == 2  # 격차 신호는 dedup 과 무관하게 원본 결과 수
