"""compute_effective_threshold 경계값 테스트.

검열 신호(삭제/격차)가 강할수록 임계값이 내려가 '사라지기 전에' 박제되도록 한다.
단, 최소 1표(인간 합의)는 항상 유지되어야 한다.
"""

import services.threshold as th


def _fixed_base(value):
    return lambda: {"threshold": value, "active_voters": 0, "dynamic": True}


def test_high_urgency_minus_two(monkeypatch):
    monkeypatch.setattr(th, "get_dynamic_base_threshold", _fixed_base(5))
    r = th.compute_effective_threshold(gap_score="extreme")
    assert r["urgency"] == "high" and r["threshold"] == 3
    r2 = th.compute_effective_threshold(deleted_count=1)
    assert r2["urgency"] == "high" and r2["threshold"] == 3


def test_medium_urgency_minus_one(monkeypatch):
    monkeypatch.setattr(th, "get_dynamic_base_threshold", _fixed_base(5))
    r = th.compute_effective_threshold(gap_score="high")
    assert r["urgency"] == "medium" and r["threshold"] == 4
    r2 = th.compute_effective_threshold(blocked_count=1)
    assert r2["urgency"] == "medium" and r2["threshold"] == 4


def test_normal_keeps_base(monkeypatch):
    monkeypatch.setattr(th, "get_dynamic_base_threshold", _fixed_base(5))
    r = th.compute_effective_threshold()
    assert r["urgency"] == "normal" and r["threshold"] == 5


def test_floor_at_one(monkeypatch):
    monkeypatch.setattr(th, "get_dynamic_base_threshold", _fixed_base(1))
    r = th.compute_effective_threshold(gap_score="extreme", deleted_count=2)
    assert r["threshold"] == 1  # max(1, 1-2)
