# -*- coding: utf-8 -*-
"""Shared contract for a-stock-data flow/ownership/event capabilities."""

from __future__ import annotations

from datetime import datetime, timezone

from data_provider.market_data_types import DataStatus
from data_provider.market_evidence import MarketEvidenceAdapter, normalize_market_evidence


RETRIEVED = datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc)


def test_capability_rows_keep_units_and_numeric_semantics() -> None:
    envelope = normalize_market_evidence(
        [{"日期": "20260813", "主力净流入": "1,234.5", "净流入占比": "2.1%"}],
        capability="capital_flow",
        security_id="600519.SH",
        source="eastmoney",
        source_tier="primary",
        retrieved_at=RETRIEVED,
    )
    assert envelope.status is DataStatus.OK
    assert envelope.unit == "CNY"
    assert envelope.currency == "CNY"
    record = envelope.data["records"][0]
    assert record["日期"] == "2026-08-13"
    assert record["主力净流入"] == 1234.5
    assert record["净流入占比"] == 2.1


def test_legal_empty_is_not_upstream_blocked() -> None:
    empty = normalize_market_evidence(
        [], capability="dragon_tiger", security_id="600519.SH", source="eastmoney", source_tier="primary", retrieved_at=RETRIEVED
    )
    blocked = normalize_market_evidence(
        None, capability="dragon_tiger", security_id="600519.SH", source="eastmoney", source_tier="primary", retrieved_at=RETRIEVED
    )
    assert empty.status is DataStatus.VALID_EMPTY
    assert blocked.status is DataStatus.UPSTREAM_BLOCKED


def test_date_required_capabilities_report_schema_drift() -> None:
    envelope = normalize_market_evidence(
        [{"解禁类型": "首发原股东"}],
        capability="unlock",
        security_id="600519.SH",
        source="eastmoney",
        source_tier="primary",
        retrieved_at=RETRIEVED,
    )
    assert envelope.status is DataStatus.SCHEMA_CHANGED
    assert "missing:date" in envelope.quality_flags


def test_primary_block_uses_independent_fallback() -> None:
    calls: list[str] = []

    def primary(**_kwargs):
        calls.append("eastmoney")
        return None

    def backup(**_kwargs):
        calls.append("sina")
        return [{"日期": "20260813", "净流入": "12"}]

    adapter = MarketEvidenceAdapter({"eastmoney": primary, "sina": backup})
    envelope = adapter.fetch(
        "600519.SH",
        capability="capital_flow",
        source_order=("eastmoney", "sina"),
        source_tiers={"eastmoney": "primary", "sina": "backup"},
    )
    assert calls == ["eastmoney", "sina"]
    assert envelope.status is DataStatus.OK
    assert envelope.source == "sina"
    assert envelope.fallback_chain == ("eastmoney",)
