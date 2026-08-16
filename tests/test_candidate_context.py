# -*- coding: utf-8 -*-
"""Contract tests for routed candidate context diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from data_provider.market_data_types import DataEnvelope, DataStatus
from src.services.screening import candidate_context


def _response(*, success: bool, results: list[object], provider: str = "fake-search", error: str | None = None):
    return SimpleNamespace(
        success=success,
        results=results,
        provider=provider,
        error_message=error,
    )


class _Search:
    is_available = True

    def search_stock_news(self, *_args, **_kwargs):
        return _response(
            success=True,
            results=[SimpleNamespace(title="订单落地", snippet="", source="公告源", published_date="2026-08-14")],
        )

    def search_stock_events(self, *_args, **_kwargs):
        return _response(success=True, results=[])


class _Manager:
    def fetch(self, *_args, **_kwargs):
        return DataEnvelope(
            capability="realtime_quote",
            security_id="600519",
            data={"price": 1688.0},
            source="TencentFetcher",
            source_tier="primary",
            as_of=None,
            retrieved_at=datetime.now(timezone.utc),
        )

    def get_capital_flow_context(self, *_args, **_kwargs):
        return {
            "status": "partial",
            "data": {"stock_flow": {"main_net_inflow": 123.0}},
            "source_chain": [{"provider": "AkshareFetcher", "result": "ok"}],
            "errors": [],
        }


def test_candidate_context_keeps_valid_empty_distinct_from_provider_failure(monkeypatch):
    monkeypatch.setattr(candidate_context, "_get_search_service", lambda: _Search())
    monkeypatch.setattr(candidate_context, "_get_market_data_manager", lambda: _Manager())

    empty = candidate_context._fetch_candidate_context_envelope("announcement", "600519")
    assert empty.status is DataStatus.VALID_EMPTY
    assert candidate_context._diagnostic_from_envelope(empty)["errors"] == []

    class Unavailable:
        is_available = False

    monkeypatch.setattr(candidate_context, "_get_search_service", lambda: Unavailable())
    failed = candidate_context._fetch_candidate_context_envelope("news", "600519")
    assert failed.status is DataStatus.UPSTREAM_BLOCKED
    assert candidate_context._diagnostic_from_envelope(failed)["errors"]


def test_collect_candidate_context_preserves_text_facade_and_route_diagnostics(monkeypatch):
    monkeypatch.setattr(candidate_context, "_get_search_service", lambda: _Search())
    monkeypatch.setattr(candidate_context, "_get_market_data_manager", lambda: _Manager())

    rows, errors = candidate_context.collect_candidate_context(
        pd.DataFrame([{"code": "600519", "name": "贵州茅台"}]),
        max_rows=1,
        providers=["news", "announcement", "fund_flow", "quote"],
        cache_dir=None,
        market_data_manager=_Manager(),
    )

    assert errors == []
    assert len(rows) == 1
    row = rows[0]
    assert row["news"] == "2026-08-14 公告源 订单落地"
    assert "quote" in row["source_diagnostics"]
    assert row["source_status"]["announcement"] == "valid_empty"
    assert row["source_count"] == 3
