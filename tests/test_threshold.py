"""compute_effective_threshold 경계값 테스트.

검열 신호(삭제/격차)가 강할수록 임계값이 내려가 '사라지기 전에' 박제되도록 한다.
단, 최소 1표(인간 합의)는 항상 유지되어야 한다.
"""

from datetime import datetime, timezone

import services.threshold as th


def _fixed_base(value):
    return lambda: {"threshold": value, "active_voters": 0, "dynamic": True}


# ── 발행량 기반 난이도 보정(비트코인 retarget 풍) ─────────────────────────────
class _IssResp:
    def __init__(self, count):
        self.count = count


class _IssQuery:
    def __init__(self, count):
        self._c = count

    def select(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def execute(self):
        return _IssResp(self._c)


class _IssDB:
    def __init__(self, count):
        self._c = count

    def table(self, name):
        return _IssQuery(self._c)


_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_issuance_target_zero_adjust(monkeypatch):
    # 목표(기본 28) 수준이면 보정 0
    monkeypatch.setattr(th, "ISSUANCE_ADJUST_ENABLED", True)
    count, adj = th._issuance_adjustment(_IssDB(28), _NOW)
    assert count == 28 and adj == 0


def test_issuance_above_target_raises(monkeypatch):
    # 공급 초과(48 = 목표+20, step 10) → +2 (더 어렵게)
    monkeypatch.setattr(th, "ISSUANCE_ADJUST_ENABLED", True)
    _, adj = th._issuance_adjustment(_IssDB(48), _NOW)
    assert adj == 2


def test_issuance_below_target_lowers(monkeypatch):
    # 공급 부족(8 = 목표-20) → -2 (더 쉽게)
    monkeypatch.setattr(th, "ISSUANCE_ADJUST_ENABLED", True)
    _, adj = th._issuance_adjustment(_IssDB(8), _NOW)
    assert adj == -2


def test_issuance_clamped_to_max(monkeypatch):
    # 폭증(100)이어도 ±ISSUANCE_MAX_ADJUST(기본 4)로 클램프
    monkeypatch.setattr(th, "ISSUANCE_ADJUST_ENABLED", True)
    monkeypatch.setattr(th, "ISSUANCE_MAX_ADJUST", 4)
    _, adj = th._issuance_adjustment(_IssDB(100), _NOW)
    assert adj == 4


def test_issuance_disabled_returns_zero(monkeypatch):
    monkeypatch.setattr(th, "ISSUANCE_ADJUST_ENABLED", False)
    count, adj = th._issuance_adjustment(_IssDB(999), _NOW)
    assert count == 0 and adj == 0


def test_high_urgency_minus_two(monkeypatch):
    monkeypatch.setattr(th, "get_dynamic_base_threshold", _fixed_base(5))
    r2 = th.compute_effective_threshold(deleted_count=1)
    assert r2["urgency"] == "high" and r2["threshold"] == 3


def test_medium_urgency_minus_one(monkeypatch):
    monkeypatch.setattr(th, "get_dynamic_base_threshold", _fixed_base(5))
    r2 = th.compute_effective_threshold(blocked_count=1)
    assert r2["urgency"] == "medium" and r2["threshold"] == 4


def test_normal_keeps_base(monkeypatch):
    monkeypatch.setattr(th, "get_dynamic_base_threshold", _fixed_base(5))
    r = th.compute_effective_threshold()
    assert r["urgency"] == "normal" and r["threshold"] == 5


def test_floor_at_one(monkeypatch):
    monkeypatch.setattr(th, "get_dynamic_base_threshold", _fixed_base(1))
    r = th.compute_effective_threshold(deleted_count=2)
    assert r["threshold"] == 1  # max(1, 1-2)
