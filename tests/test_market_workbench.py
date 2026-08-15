# -*- coding: utf-8 -*-
"""Task 13 Candle/annotation/shared snapshot contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import pytest

from src.config import Config
from src.services.market_chart_service import MarketChartError, MarketChartService
from src.services.market_stream_service import MarketSnapshotHub
from src.services.market_annotations_service import MarketAnnotationsService
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager
from data_provider.market_data_types import DataEnvelope


class _FakeStockService:
    def get_history_data(self, stock_code: str, period: str = "daily", days: int = 30):
        return {
            "stock_code": stock_code,
            "source": "fixture_provider",
            "data": [
                {"date": "2026-08-10", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
                {"date": "2026-08-11", "open": 11, "high": 13, "low": 10, "close": 12, "volume": 120},
                {"date": "2026-08-12", "open": 12, "high": 14, "low": 11, "close": 13, "volume": 130},
            ],
        }


def test_candle_service_normalizes_aggregates_and_rejects_invalid_window() -> None:
    service = MarketChartService(_FakeStockService())
    result = service.get_candles("600519", period="weekly", limit=10)
    assert result["source"] == "fixture_provider"
    assert len(result["candles"]) == 1
    assert result["candles"][0]["open"] == 10
    assert result["candles"][0]["high"] == 14
    assert "macd" in result["candles"][0]["indicators"]
    with pytest.raises(MarketChartError, match="start"):
        service.get_candles("600519", start=date(2026, 8, 13), end=date(2026, 8, 1))


def test_candle_service_uses_market_data_capability_and_snapshot_contract() -> None:
    class _Manager:
        query = None

        def fetch(self, query):
            self.query = query
            return DataEnvelope(
                capability="daily_data",
                security_id=query.security_id,
                data=[{"date": "2026-08-14", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100}],
                source="fixture_route",
                source_tier="primary",
                as_of=datetime(2026, 8, 14, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
                retrieved_at=datetime(2026, 8, 14, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

    manager = _Manager()
    service = MarketChartService(
        manager,
        now_fn=lambda: datetime(2026, 8, 14, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    snapshot = service.get_snapshot("600519")
    assert manager.query.capability == "daily_data"
    assert snapshot["source"] == "fixture_route"
    assert snapshot["quote"]["indicators"]["macd"] == 0.0


def test_candle_service_serializes_provider_timestamps_for_api_contract() -> None:
    class _Manager:
        def fetch(self, query):
            # pandas.Timestamp is a common provider output and is a datetime
            # subclass, but FastAPI's public schema requires a string.
            import pandas as pd

            return DataEnvelope(
                capability=query.capability,
                security_id=query.security_id,
                data=[{
                    "date": pd.Timestamp("2026-08-14"),
                    "open": 10,
                    "high": 12,
                    "low": 9,
                    "close": 11,
                }],
                source="fixture_route",
                source_tier="primary",
                as_of=datetime(2026, 8, 14, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
                retrieved_at=datetime(2026, 8, 14, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

    result = MarketChartService(_Manager()).get_candles("600519", limit=1)
    assert result["candles"][0]["timestamp"] == "2026-08-14T00:00:00"


def test_snapshot_hub_shares_cache_and_emits_sse() -> None:
    clock = [0.0]

    def now():
        return clock[0]

    calls = {"count": 0}

    class _Chart:
        def get_candles(self, *args, **kwargs):
            calls["count"] += 1
            return {"candles": [], "stale": False, "data_quality": "ok"}

    hub = MarketSnapshotHub(_Chart(), clock=now)
    assert hub.snapshot("600519", ttl_seconds=10)["cache"] == "miss"
    assert hub.snapshot("600519", ttl_seconds=10)["cache"] == "hit"
    assert calls["count"] == 1
    events = list(hub.stream("600519", max_events=2, interval_seconds=1, sleep_fn=lambda _: clock.__setitem__(0, clock[0] + 11)))
    assert events[0].startswith("event: snapshot")
    assert len(events) == 2
    assert calls["count"] == 2


def test_snapshot_hub_singleflight_and_shared_subscriber_cleanup() -> None:
    calls = {"count": 0}

    class _Chart:
        def get_candles(self, *args, **kwargs):
            calls["count"] += 1
            time.sleep(0.03)
            return {"candles": [], "stale": False, "data_quality": "ok"}

    hub = MarketSnapshotHub(_Chart())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: hub.snapshot("600519", ttl_seconds=10), range(4)))
    assert calls["count"] == 1
    assert {item["cache"] for item in results} == {"miss", "hit"}

    first = hub.stream("600519", max_events=1, interval_seconds=5)
    second = hub.stream("600519", max_events=1, interval_seconds=5)
    assert next(first).startswith("event:")
    assert next(second).startswith("event:")
    first.close()
    second.close()
    assert hub.health()["subscribers"] == 0


def test_annotations_use_account_ledger_and_return_partial_safe_contract(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'annotations.db'}")
    try:
        account = PortfolioService().create_account(name="manual", broker="manual", market="cn", base_currency="CNY")
        service = PortfolioService()
        service.record_trade(
            account_id=account["id"], symbol="600519", trade_date=date(2026, 8, 10),
            side="buy", quantity=2, price=100,
        )
        result = MarketAnnotationsService(db).collect(
            "600519", account_id=account["id"], start=date(2026, 8, 1), end=date(2026, 8, 20)
        )
        assert result["items"][0]["type"] == "trade"
        assert result["items"][0]["source"] == "account_ledger"
        hidden = MarketAnnotationsService(db).collect("600519")
        assert not any(item["type"] == "trade" for item in hidden["items"])
        assert hidden["page"] == 1
        assert hidden["has_more"] is False

        partial_service = MarketAnnotationsService(db)
        partial_service._shadow_items = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
        partial = partial_service.collect("600519", account_id=account["id"], page_size=1)
        assert partial["partial"] is True
        assert "shadow_annotations_unavailable" in partial["limitations"]
        assert partial["total"] >= 1
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_market_stream_slots_are_capped_and_released() -> None:
    """N4 regression: SSE responses were unbounded in both length and count."""
    from src.services.market_stream_service import (
        DEFAULT_MAX_STREAM_EVENTS,
        MarketSnapshotHub,
    )

    hub = MarketSnapshotHub(max_concurrent_streams=2)
    assert hub.acquire_stream_slot() is True
    assert hub.acquire_stream_slot() is True
    # The cap refuses the third rather than pinning another threadpool worker.
    assert hub.acquire_stream_slot() is False
    assert hub.open_streams == 2

    hub.release_stream_slot()
    assert hub.open_streams == 1
    assert hub.acquire_stream_slot() is True

    # Releasing more than acquired must not drive the counter negative.
    for _ in range(5):
        hub.release_stream_slot()
    assert hub.open_streams == 0

    # A stream always terminates so its worker is returned to the pool.
    assert DEFAULT_MAX_STREAM_EVENTS > 0
