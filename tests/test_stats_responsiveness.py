"""Slow dashboard queries must not hold up homepage/API work on the event loop."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from routers import stats


@pytest.mark.parametrize("endpoint", ["get_stats", "timeseries"])
def test_dashboard_queries_leave_event_loop_responsive_and_share_cache(monkeypatch, endpoint):
    monkeypatch.setattr(stats, "_stats_cache", {"value": None, "expires_at": 0.0})
    monkeypatch.setattr(stats, "_ts_cache", {})
    for name in (
        "get_hunter_status", "get_collector_status", "get_promoter_status",
        "get_wayback_status", "get_proxy_status",
    ):
        monkeypatch.setattr(stats, name, lambda: {})
    monkeypatch.setattr(stats, "get_dynamic_base_threshold", lambda: {
        "threshold": 3, "active_voters": 0, "dynamic": False,
    })
    monkeypatch.setattr("services.tracker._has_deleted_at", lambda db: True)

    async def exercise():
        loop = asyncio.get_running_loop()

        class SlowDB:
            calls = 0
            responsive = False

            def table(self, name):
                return Query(self)

        class Query:
            def __init__(self, db):
                self.db = db

            @property
            def not_(self):
                return self

            def __getattr__(self, name):
                return lambda *args, **kwargs: self

            def execute(self):
                self.db.calls += 1
                if self.db.calls == 1:
                    # The query can finish only after unrelated loop work runs.
                    # A blocking async route would wait here until the timeout.
                    released = threading.Event()
                    loop.call_soon_threadsafe(released.set)
                    self.db.responsive = released.wait(timeout=1)
                return SimpleNamespace(count=3, data=[])

        db = SlowDB()
        monkeypatch.setattr(stats, "get_db", lambda: db)
        handler = getattr(stats, endpoint)
        first = await handler()
        calls_per_refresh = db.calls
        if endpoint == "get_stats":
            stats._stats_cache["expires_at"] = 0.0
        else:
            stats._ts_cache.clear()
        refreshed, concurrent = await asyncio.gather(handler(), handler())
        cached = await handler()

        assert db.responsive, "Dashboard DB work blocked the event loop"
        assert first == refreshed == concurrent == cached
        # One refresh's queries only; concurrent visitors use its cache.
        assert calls_per_refresh > 0
        assert db.calls == calls_per_refresh * 2
        if endpoint == "get_stats":
            assert first["stories"]["total"] == 3
        else:
            assert len(first) == 30

    asyncio.run(exercise())
