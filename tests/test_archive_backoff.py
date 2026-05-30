"""박제 재시도 백오프 로직 테스트 (DB 없이 순수 함수만)."""

from datetime import datetime, timedelta, timezone

import services.archive as archive


def test_backoff_ready():
    now = datetime.now(timezone.utc)
    # 한 번도 시도 안 함 → 즉시 재시도 가능
    assert archive._backoff_ready(0, None, now) is True
    # 1회 실패 후 10초 (base 300초 필요) → 아직
    assert archive._backoff_ready(1, (now - timedelta(seconds=10)).isoformat(), now) is False
    # 1회 실패 후 400초 → 재시도 가능
    assert archive._backoff_ready(1, (now - timedelta(seconds=400)).isoformat(), now) is True
    # 백오프 상한(6시간) — 9회째여도 22000초 지나면 가능
    assert archive._backoff_ready(9, (now - timedelta(seconds=22000)).isoformat(), now) is True


def test_parse_iso_handles_z_suffix():
    assert archive._parse_iso("2026-05-30T00:00:00Z") is not None
    assert archive._parse_iso("2026-05-30T00:00:00+00:00") is not None
    assert archive._parse_iso(None) is None
    assert archive._parse_iso("garbage") is None
