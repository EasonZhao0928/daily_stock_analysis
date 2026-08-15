# -*- coding: utf-8 -*-
"""Tests for shared/injectable Market Data manager lifecycle."""

from __future__ import annotations

from data_provider.base import DataFetcherManager
from data_provider.runtime import MarketDataRuntime
from data_provider.supplier_runtime import SupplierPolicy, SupplierRuntimeRegistry


class _Fetcher:
    name = "TencentFetcher"
    priority = 1

    def get_realtime_quote(self, stock_code):
        return {"code": stock_code, "price": 1}


def test_runtime_reuses_one_manager_and_injected_supplier_registry():
    created = []
    registry = SupplierRuntimeRegistry(
        policies={"tencent": SupplierPolicy(max_concurrency=1, min_interval_seconds=0.0)}
    )

    def factory(*, supplier_runtime):
        created.append(supplier_runtime)
        return DataFetcherManager(fetchers=[_Fetcher()], supplier_runtime=supplier_runtime)

    runtime = MarketDataRuntime(manager_factory=factory, supplier_runtime=registry)
    first = runtime.manager()
    second = runtime.manager()

    assert first is second
    assert created == [registry]
    first._call_fetcher_method(first._fetchers[0], "get_realtime_quote", "600519")
    assert registry.health("tencent").in_flight == 0


def test_two_runtime_fixtures_do_not_share_manager_or_supplier_state():
    first = MarketDataRuntime(manager_factory=lambda: object())
    second = MarketDataRuntime(manager_factory=lambda: object())

    assert first.manager() is not second.manager()
    lease = first.supplier_runtime.acquire("eastmoney")
    first.supplier_runtime.release(lease, success=False, status_code=503)
    assert first.supplier_runtime.health("eastmoney").consecutive_failures == 1
    assert second.supplier_runtime.health("eastmoney").consecutive_failures == 0


def test_config_reload_keeps_shared_supplier_sessions_alive():
    """B8 regression: a settings save must not kill long-held sessions.

    Screening/hotspot providers cache the supplier session object, so closing
    it inside reset() left them holding a dead session after any config reload.
    """
    from data_provider.runtime import get_market_data_manager, reset_market_data_runtime
    from data_provider.supplier_runtime import get_supplier_runtime_registry

    registry = get_supplier_runtime_registry()
    get_market_data_manager()
    held_session = registry.get_session("eastmoney")

    reset_market_data_runtime()

    # Same live session object, not a closed one.
    assert registry.get_session("eastmoney") is held_session
    assert getattr(held_session, "get", None) is not None

    # Explicit teardown still works when a caller asks for it.
    reset_market_data_runtime(close_sessions=True)
    assert registry.get_session("eastmoney") is not held_session
