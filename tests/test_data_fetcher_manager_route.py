# -*- coding: utf-8 -*-
"""Contract tests for DataFetcherManager's capability route."""

from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import BoundedSemaphore
from zoneinfo import ZoneInfo

import pytest

from data_provider.base import DataFetcherManager
from data_provider.market_data_types import DataEnvelope, DataQuery, DataStatus, SourcePolicy
from data_provider.security_id import parse_security_id
from data_provider.supplier_runtime import SupplierPolicy, SupplierRuntimeRegistry


FIXED_TIME = datetime(2026, 1, 5, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Fetcher:
    def __init__(self, name, priority, daily=None, realtime=None, error=None, delay=0):
        self.name = name
        self.priority = priority
        self.daily = daily
        self.realtime = realtime
        self.error = error
        self.delay = delay
        self.calls = []

    def get_daily_data(self, stock_code):
        self.calls.append(("daily_data", stock_code))
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.daily

    def get_realtime_quote(self, stock_code):
        self.calls.append(("realtime_quote", stock_code))
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.realtime


def _manager(*fetchers):
    manager = DataFetcherManager.__new__(DataFetcherManager)
    manager._fetchers = list(fetchers)
    manager._fundamental_timeout_slots = BoundedSemaphore(8)
    return manager


def _query(capability="daily_data", code="600519"):
    return DataQuery(capability=capability, security_id=parse_security_id(code), as_of=FIXED_TIME)


def test_fetch_uses_source_policy_order_even_when_fetchers_have_other_priorities():
    low_priority = _Fetcher("low", priority=20, daily={"source": "low"})
    high_priority = _Fetcher("high", priority=1, daily={"source": "high"})
    manager = _manager(low_priority, high_priority)

    result = manager.fetch(
        _query(),
        SourcePolicy(primary_sources=("low",), fallback_sources=("high",)),
    )

    assert result.status is DataStatus.OK
    assert result.source == "low"
    assert result.fallback_chain == ("low",)
    assert low_priority.calls == [("daily_data", "600519")]
    assert high_priority.calls == []


def test_realtime_concurrent_flag_allows_symbol_fanout_with_shared_runtime(monkeypatch):
    """B3 regression: shared supplier gates remain, adapter calls may fan out."""
    state = {"active": 0, "max_active": 0}
    lock = threading.Lock()
    release = threading.Event()

    class _RealtimeFetcher(_Fetcher):
        def __init__(self):
            super().__init__("EfinanceFetcher", priority=1)

        def get_realtime_quote(self, stock_code):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                if state["active"] >= 2:
                    release.set()
            release.wait(timeout=1.0)
            with lock:
                state["active"] -= 1
            return SimpleNamespace(
                price=100.0,
                has_basic_data=lambda: True,
            )

    from types import SimpleNamespace

    runtime = SupplierRuntimeRegistry(
        policies={"eastmoney": SupplierPolicy(max_concurrency=8, min_interval_seconds=0, jitter_seconds=0)}
    )
    manager = DataFetcherManager(fetchers=[_RealtimeFetcher()], supplier_runtime=runtime)
    monkeypatch.setattr(
        "src.config.get_config",
        lambda: SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="efinance",
            realtime_cache_ttl=600,
        ),
    )
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(
                lambda symbol: manager.get_realtime_quote(symbol, log_final_failure=False, concurrent=True),
                ["600519", "000001", "300750"],
            )
        )
    assert all(result is not None for result in results)
    assert state["max_active"] >= 2


def test_fetch_falls_back_after_provider_error_and_records_chain():
    primary = _Fetcher("primary", priority=1, error=RuntimeError("blocked"))
    backup = _Fetcher("backup", priority=2, daily={"price": 1600})
    manager = _manager(primary, backup)

    result = manager.fetch(
        _query(),
        SourcePolicy(primary_sources=("primary",), fallback_sources=("backup",)),
    )

    assert result.status is DataStatus.OK
    assert result.source == "backup"
    assert result.source_tier == "fallback"
    assert result.fallback_chain == ("primary", "backup")


def test_fetch_timeout_moves_to_independent_backup_without_waiting_for_primary():
    primary = _Fetcher("primary", priority=1, delay=0.08, daily={"late": True})
    backup = _Fetcher("backup", priority=2, daily={"price": 1600})
    manager = _manager(primary, backup)
    started = time.monotonic()

    result = manager.fetch(
        _query(),
        SourcePolicy(primary_sources=("primary",), fallback_sources=("backup",), timeout_seconds=0.01),
    )

    assert result.status is DataStatus.OK
    assert result.source == "backup"
    assert result.fallback_chain == ("primary", "backup")
    assert time.monotonic() - started < 0.07


def test_valid_empty_is_not_treated_as_provider_failure():
    primary = _Fetcher("primary", priority=1, daily=[])
    backup = _Fetcher("backup", priority=2, daily={"price": 1600})
    manager = _manager(primary, backup)

    result = manager.fetch(
        _query(), SourcePolicy(primary_sources=("primary",), fallback_sources=("backup",))
    )

    assert result.status is DataStatus.VALID_EMPTY
    assert result.data == []
    assert result.fallback_chain == ("primary",)
    assert backup.calls == []


def test_stale_envelope_can_trigger_fallback_and_preserve_status_if_all_sources_stale():
    stale = DataEnvelope(
        capability="daily_data",
        security_id=parse_security_id("600519"),
        data={"price": 1500},
        source="primary",
        source_tier="primary",
        as_of=FIXED_TIME,
        retrieved_at=FIXED_TIME,
        status=DataStatus.STALE_SYMBOL,
    )
    primary = _Fetcher("primary", priority=1, daily=stale)
    backup = _Fetcher("backup", priority=2, daily={"price": 1600})
    result = _manager(primary, backup).fetch(
        _query(), SourcePolicy(primary_sources=("primary",), fallback_sources=("backup",))
    )

    assert result.status is DataStatus.OK
    assert result.source == "backup"
    assert result.fallback_chain == ("primary", "backup")


def test_ambiguous_security_identity_returns_symbol_invalid_without_provider_call():
    primary = _Fetcher("primary", priority=1, daily={"price": 1600})
    manager = _manager(primary)

    result = manager.fetch(DataQuery(capability="daily_data", security_id="000001"))

    assert result.status is DataStatus.SYMBOL_INVALID
    assert result.fallback_chain == ()
    assert primary.calls == []


def test_unsupported_capability_returns_stable_upstream_status():
    result = _manager().fetch(DataQuery(capability="not_registered", security_id="600519"))

    assert result.status is DataStatus.UPSTREAM_BLOCKED
    assert result.fallback_chain == ()


def test_trading_hours_policy_returns_market_closed_without_provider_call(monkeypatch):
    class _ClosedClock:
        def __init__(self, market):
            self.market = market

        def is_trading_day(self, value):
            return False

        def is_open(self, value):
            return False

    import data_provider.base as base_module

    monkeypatch.setattr(base_module, "MarketClock", _ClosedClock)
    primary = _Fetcher("primary", priority=1, daily={"price": 1600})
    result = _manager(primary).fetch(
        _query(), SourcePolicy(primary_sources=("primary",), trading_hours_only=True)
    )

    assert result.status is DataStatus.MARKET_CLOSED
    assert primary.calls == []


def test_trading_hours_policy_treats_weekday_after_close_as_market_closed():
    primary = _Fetcher("primary", priority=1, daily={"price": 1600})
    after_close = datetime(2026, 1, 5, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    result = _manager(primary).fetch(
        DataQuery(capability="daily_data", security_id="600519", as_of=after_close),
        SourcePolicy(primary_sources=("primary",), trading_hours_only=True),
    )

    assert result.status is DataStatus.MARKET_CLOSED
    assert primary.calls == []


class _WindowedFetcher(_Fetcher):
    """A fetcher whose daily_data accepts the same window as production."""

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append(("daily_data", stock_code, start_date, end_date, days))
        return self.daily


def test_capability_route_forwards_the_query_history_window():
    """N2 regression: limit/start were dropped, pinning every chart to days=30."""
    fetcher = _WindowedFetcher("primary", priority=1, daily={"price": 1600})
    _manager(fetcher).fetch(
        DataQuery(capability="daily_data", security_id="600519", limit=240),
        SourcePolicy(primary_sources=("primary",)),
    )
    assert fetcher.calls[0][4] == 240, fetcher.calls

    ranged = _WindowedFetcher("primary", priority=1, daily={"price": 1600})
    _manager(ranged).fetch(
        DataQuery(
            capability="daily_data",
            security_id="600519",
            start=datetime(2025, 1, 6, tzinfo=ZoneInfo("Asia/Shanghai")),
            as_of=datetime(2026, 1, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
        SourcePolicy(primary_sources=("primary",)),
    )
    assert ranged.calls[0][2] == "2025-01-06"
    assert ranged.calls[0][3] == "2026-01-05"


def test_capability_route_omits_window_for_narrow_adapters():
    """Adapters without window parameters must keep working unchanged."""
    fetcher = _Fetcher("primary", priority=1, daily={"price": 1600})
    result = _manager(fetcher).fetch(
        DataQuery(capability="daily_data", security_id="600519", limit=240),
        SourcePolicy(primary_sources=("primary",)),
    )
    assert result.status is DataStatus.OK
    assert fetcher.calls == [("daily_data", "600519")]
