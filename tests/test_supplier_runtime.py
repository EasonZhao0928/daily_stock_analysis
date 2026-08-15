# -*- coding: utf-8 -*-
"""Contract tests for the process-wide supplier-family runtime."""

from __future__ import annotations

import threading
import time

import pytest

from data_provider.supplier_runtime import (
    SupplierCircuitOpen,
    SupplierPolicy,
    SupplierRuntimeRegistry,
    supplier_family_for_name,
)


def test_same_supplier_family_respects_shared_concurrency_gate() -> None:
    registry = SupplierRuntimeRegistry(
        policies={"eastmoney": SupplierPolicy(max_concurrency=1)},
    )
    active = 0
    max_active = 0
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker() -> None:
        nonlocal active, max_active
        barrier.wait(timeout=2)
        with registry.request("eastmoney"):
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert max_active == 1
    assert registry.health("eastmoney").in_flight == 0


def test_rate_gate_and_independent_families_do_not_share_delay() -> None:
    registry = SupplierRuntimeRegistry(
        policies={
            "eastmoney": SupplierPolicy(max_concurrency=2, min_interval_seconds=0.03),
            "tencent": SupplierPolicy(max_concurrency=2),
        },
    )
    with registry.request("eastmoney"):
        pass
    started = time.monotonic()
    with registry.request("eastmoney"):
        pass
    assert time.monotonic() - started >= 0.025

    # A different family has its own clock and gate.
    started = time.monotonic()
    with registry.request("tencent"):
        pass
    assert time.monotonic() - started < 0.02


def test_retryable_failures_open_circuit_then_allow_one_half_open_probe() -> None:
    current = [100.0]
    registry = SupplierRuntimeRegistry(
        policies={"test": SupplierPolicy(failure_threshold=2, cooldown_seconds=10)},
        clock=lambda: current[0],
        random_source=lambda: 0.0,
    )

    for _ in range(2):
        lease = registry.acquire("test")
        registry.release(lease, success=False, status_code=503)

    assert registry.health("test").state == "open"
    with pytest.raises(SupplierCircuitOpen) as caught:
        registry.acquire("test")
    assert caught.value.retry_after_seconds == 10

    current[0] += 11
    probe = registry.acquire("test")
    assert probe.half_open_probe is True
    registry.release(probe, success=True)
    assert registry.health("test").state == "closed"


def test_sessions_are_shared_by_family_and_adapter_names_map_consistently() -> None:
    registry = SupplierRuntimeRegistry()
    sessions = []

    def factory():
        session = object()
        sessions.append(session)
        return session

    first = registry.get_session("eastmoney", factory=factory)
    second = registry.get_session("eastmoney", factory=factory)

    assert first is second
    assert len(sessions) == 1
    assert supplier_family_for_name("EfinanceFetcher") == "eastmoney"
    assert supplier_family_for_name("TencentFetcher") == "tencent"
    assert supplier_family_for_name("TushareFetcher") == "tushare"


def test_screening_snapshot_and_hotspot_share_the_eastmoney_session(monkeypatch) -> None:
    """The two historical screening bypasses must use one supplier session."""
    from src.services.screening import snapshot
    from src.services import screening_service

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    registry = SupplierRuntimeRegistry(
        policies={"eastmoney": SupplierPolicy(max_concurrency=2, min_interval_seconds=0.0)}
    )
    registry.get_session("eastmoney", factory=lambda: session)
    monkeypatch.setattr(snapshot, "get_supplier_runtime_registry", lambda: registry)
    monkeypatch.setattr(screening_service, "get_supplier_runtime_registry", lambda: registry)

    provider = screening_service.DsaEastMoneyHotspotProvider()
    assert provider._session is session
    snapshot._eastmoney_get("https://data.eastmoney.com/dataapi/xuangu/list", timeout=1)
    provider._eastmoney_get_once("https://push2.eastmoney.com/api/qt/clist/get")

    assert len(session.calls) == 2
    assert registry.health("eastmoney").in_flight == 0


def test_parse_errors_do_not_trip_the_supplier_circuit():
    """B4 regression: a bad payload is not evidence that a supplier is down."""
    from data_provider.supplier_runtime import SupplierRuntimeRegistry, is_transport_failure

    assert is_transport_failure(ValueError("unparseable row")) is False
    assert is_transport_failure(KeyError("missing column")) is False
    assert is_transport_failure(TimeoutError("read timeout")) is True
    assert is_transport_failure(ConnectionError("reset")) is True

    registry = SupplierRuntimeRegistry()
    # Far more consecutive parse errors than any failure threshold.
    for _ in range(10):
        with pytest.raises(ValueError):
            with registry.request("eastmoney"):
                raise ValueError("one bad symbol")

    # The family must still be usable for every other caller in the process.
    with registry.request("eastmoney") as lease:
        assert lease is not None
