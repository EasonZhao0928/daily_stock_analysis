# -*- coding: utf-8 -*-
"""Financial statement and consensus contracts are parser-only/offline."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from data_provider.financial_types import (
    FinancialStatementAdapter,
    align_statement_periods,
    normalize_consensus_estimates,
    normalize_financial_statement,
)
from data_provider.market_data_types import DataStatus
from data_provider.provider_fixtures import load_fixture
from data_provider.fundamental_adapter import AkshareFundamentalAdapter


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "provider_contract"
RETRIEVED = datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc)


def test_sina_three_statement_shape_is_normalized_without_zero_fill() -> None:
    rows = load_fixture(FIXTURE_DIR / "sina_lrb.json").payload
    envelope = normalize_financial_statement(
        rows,
        statement_type="income_statement",
        security_id="600519.SH",
        source="sina",
        source_tier="backup",
        retrieved_at=RETRIEVED,
    )
    assert envelope.status is DataStatus.OK
    assert envelope.unit == "CNY"
    assert envelope.currency == "CNY"
    assert envelope.provenance.source_tier == "backup"
    assert envelope.data["periods"][0]["period"] == "2026-06-30"
    metrics = envelope.data["periods"][0]["metrics"]
    assert metrics["净利润"] == 42300000000.0
    assert metrics["净利润_同比"] == 8.2
    assert "资产总计" not in metrics  # absent fields are not invented as zero


def test_consensus_estimate_normalizes_period_and_mean() -> None:
    rows = load_fixture(FIXTURE_DIR / "ths_eps.json").payload
    envelope = normalize_consensus_estimates(
        rows,
        security_id="600519.SH",
        source="ths",
        source_tier="primary",
        retrieved_at=RETRIEVED,
    )
    assert envelope.status is DataStatus.OK
    assert envelope.unit == "CNY_PER_SHARE"
    assert envelope.data["estimates"][0]["period"] == "2026E"
    assert envelope.data["estimates"][0]["mean"] == 2.18


def test_missing_period_is_schema_drift_and_not_valid_empty() -> None:
    envelope = normalize_financial_statement(
        [{"营业收入": "10"}],
        statement_type="income_statement",
        security_id="600519.SH",
        source="sina",
        source_tier="backup",
        retrieved_at=RETRIEVED,
    )
    assert envelope.status is DataStatus.SCHEMA_CHANGED
    assert envelope.data["periods"] == []
    assert envelope.data["periods"] == []


def test_primary_failure_uses_independent_backup_and_records_chain() -> None:
    calls: list[str] = []

    def primary(**_kwargs):
        calls.append("primary")
        return [{"unexpected_period": "2026-06-30", "净利润": "10"}]

    def backup(**_kwargs):
        calls.append("backup")
        return [{"报告期": "2026-06-30", "净利润": "10"}]

    adapter = FinancialStatementAdapter({"sina": primary, "exchange": backup})
    envelope = adapter.fetch(
        "600519.SH",
        statement_type="income_statement",
        source_order=("sina", "exchange"),
        source_tiers={"sina": "backup", "exchange": "official"},
    )
    assert calls == ["primary", "backup"]
    assert envelope.status is DataStatus.OK
    assert envelope.source == "exchange"
    assert envelope.fallback_chain == ("sina",)
    assert envelope.source_tier == "official"


def test_valid_empty_is_distinct_from_upstream_failure() -> None:
    empty = normalize_financial_statement(
        [],
        statement_type="cash_flow_statement",
        security_id="600519.SH",
        source="sina",
        source_tier="backup",
        retrieved_at=RETRIEVED,
    )
    blocked = normalize_financial_statement(
        None,
        statement_type="cash_flow_statement",
        security_id="600519.SH",
        source="sina",
        source_tier="backup",
        retrieved_at=RETRIEVED,
    )
    assert empty.status is DataStatus.VALID_EMPTY
    assert blocked.status is DataStatus.UPSTREAM_BLOCKED


def test_cross_statement_period_alignment() -> None:
    income = normalize_financial_statement(
        [{"报告期": "2026-06-30", "净利润": "10"}, {"报告期": "2025-12-31", "净利润": "8"}],
        statement_type="income_statement",
        security_id="600519.SH",
        source="sina",
        source_tier="backup",
        retrieved_at=RETRIEVED,
    )
    cash = normalize_financial_statement(
        [{"报告期": "2026-06-30", "经营现金流": "12"}],
        statement_type="cash_flow_statement",
        security_id="600519.SH",
        source="sina",
        source_tier="backup",
        retrieved_at=RETRIEVED,
    )
    assert align_statement_periods(income, cash) == ["2026-06-30"]
    assert align_statement_periods(income, cash, mode="union") == ["2026-06-30", "2025-12-31"]


def test_fundamental_adapter_facade_uses_same_financial_contract() -> None:
    adapter = AkshareFundamentalAdapter()
    envelope = adapter.normalize_consensus_estimate(
        [{"年度": "2026E", "均值": "2.18"}],
        stock_code="600519.SH",
        source="ths",
        source_tier="primary",
        retrieved_at=RETRIEVED,
    )
    assert envelope.status is DataStatus.OK
    assert envelope.data["estimates"][0]["mean"] == 2.18
