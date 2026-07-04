"""cleanup 캡처 승격글 보존 테스트.

계약: from_capture=true(삭제된 원본의 공개 기록)는 어떤 경로(RPC/legacy)로도 정리되지
않는다 — 삭제 시 captured_posts.promoted_story_id 가 dangling 되어 재승격도 막히는
영구 유실이기 때문. 009 미적용(컬럼 부재) 환경에선 필터를 생략해 cleanup 자체가
400 으로 죽지 않는다(그 환경엔 캡처글이 없어 보존 대상도 없음).
"""

from types import SimpleNamespace

import services.cleanup as cleanup
from services.threshold import DEFAULT_THRESHOLD


class FakeQuery:
    """호출된 필터를 기록하는 체이닝 쿼리 목."""
    def __init__(self, db, rows=None, probe_error=None):
        self.db = db
        self._rows = rows or []
        self._probe_error = probe_error
        self.filters = []
        self._selected = None

    def select(self, cols):
        self._selected = cols
        return self

    def delete(self):
        self.filters.append(("delete",))
        return self

    def is_(self, col, val):
        self.filters.append(("is", col, val)); return self

    def lte(self, col, val):
        self.filters.append(("lte", col, val)); return self

    def lt(self, col, val):
        self.filters.append(("lt", col, val)); return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val)); return self

    def in_(self, col, vals):
        self.filters.append(("in", col, list(vals))); return self

    def limit(self, n):
        return self

    def execute(self):
        # from_capture probe(select("from_capture"))는 오류 주입 가능
        if self._selected == "from_capture" and self._probe_error:
            raise self._probe_error
        self.db.executed.append(self)
        return SimpleNamespace(data=self._rows)


class FakeDB:
    def __init__(self, cand_rows=None, probe_error=None):
        self._cand_rows = cand_rows or []
        self._probe_error = probe_error
        self.executed = []

    def table(self, name):
        if name == "stories":
            return FakeQuery(self, rows=self._cand_rows, probe_error=self._probe_error)
        if name == "votes":
            return FakeQuery(self, rows=[])
        return FakeQuery(self)


def _reset_probe(monkeypatch):
    monkeypatch.setattr(cleanup, "_from_capture_supported", None)


def test_default_max_votes_is_threshold_minus_one():
    # 기본 한도 = 임계값-1: 임계값 도달(박제 실패 등) 후보는 보존, 미달 후보만 정리.
    assert cleanup.CLEANUP_MAX_VOTES == max(0, DEFAULT_THRESHOLD - 1)


def test_legacy_applies_capture_filter_when_column_exists(monkeypatch):
    _reset_probe(monkeypatch)
    db = FakeDB(cand_rows=[{"id": "s1"}])
    deleted = cleanup._cleanup_legacy_batched(db, "2026-06-01T00:00:00+00:00")
    # 후보 select 와 delete 양쪽 모두 from_capture=False 필터가 걸려야 한다(이중 안전).
    eq_filters = [q.filters for q in db.executed
                  if ("eq", "from_capture", False) in q.filters]
    assert len(eq_filters) >= 2
    assert deleted == 1


def test_legacy_skips_filter_when_column_missing(monkeypatch):
    _reset_probe(monkeypatch)
    db = FakeDB(cand_rows=[{"id": "s1"}],
                probe_error=RuntimeError("42703 column stories.from_capture does not exist"))
    deleted = cleanup._cleanup_legacy_batched(db, "2026-06-01T00:00:00+00:00")
    # 컬럼 부재(009 미적용): 필터 없이도 cleanup 이 정상 동작(400 사망 방지).
    for q in db.executed:
        assert ("eq", "from_capture", False) not in q.filters
    assert deleted == 1


def test_probe_result_is_cached(monkeypatch):
    _reset_probe(monkeypatch)
    db = FakeDB()
    assert cleanup._has_from_capture(db) is True
    # 캐시 후에는 probe 재실행 없이 같은 답 (모듈 전역 캐시 확인)
    assert cleanup._from_capture_supported is True
    assert cleanup._has_from_capture(db) is True
