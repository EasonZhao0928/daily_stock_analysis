# -*- coding: utf-8 -*-
"""Offline production-route tests for extended Market Data capabilities."""

from datetime import date

import pytest

from data_provider.base import DataFetcherManager
from data_provider.extended_capabilities import ExtendedCapabilityAdapter
from data_provider.market_data_types import DataQuery, DataStatus, SourcePolicy


class FakeSource:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def fetch(self, query, *, statement_type="", **_kwargs):
        self.calls.append((query.capability, statement_type))
        payload = self.payloads[query.capability]
        if isinstance(payload, Exception):
            raise payload
        return payload


@pytest.fixture(autouse=True)
def _enable_extended_capabilities(monkeypatch):
    """These tests exercise the extended route, which is opt-in (R10.4)."""
    import data_provider.base as base_module

    monkeypatch.setattr(base_module, "extended_market_data_enabled", lambda config=None: True)


def _manager(*sources):
    adapter = ExtendedCapabilityAdapter(dict(sources))
    return DataFetcherManager(fetchers=[], extended_capability_adapter=adapter)


def test_extended_capabilities_are_blocked_when_the_flag_is_off(monkeypatch) -> None:
    """N1 regression: the flag must actually gate the extended route."""
    import data_provider.base as base_module

    monkeypatch.setattr(base_module, "extended_market_data_enabled", lambda config=None: False)
    source = FakeSource({"financial_statement": [{"报告期": "2025-12-31"}]})
    manager = _manager(("primary", source))

    result = manager.fetch(
        DataQuery("financial_statement", "600519.SH", fields=("income_statement",)),
        SourcePolicy(primary_sources=("primary",)),
    )

    assert result.status is DataStatus.UPSTREAM_BLOCKED
    assert source.calls == []


def test_financial_statement_uses_manager_route_and_fallback() -> None:
    primary = FakeSource({"financial_statement": RuntimeError("blocked")})
    backup = FakeSource({"financial_statement": [{"报告期": "2025-12-31", "营业收入": "10亿元"}]})
    manager = _manager(("primary", primary), ("backup", backup))

    result = manager.fetch(
        DataQuery("financial_statement", "600519.SH", fields=("income_statement",)),
        SourcePolicy(primary_sources=("primary",), fallback_sources=("backup",)),
    )

    assert result.status is DataStatus.OK
    assert result.source == "backup"
    assert result.fallback_chain == ("primary",)
    assert result.data["statement_type"] == "income_statement"
    assert result.data["periods"][0]["period"] == "2025-12-31"


@pytest.mark.parametrize(
    ("capability", "payload"),
    [
        ("consensus_estimate", [{"预测年度": "2026", "均值": "12.5", "预测机构数": 8}]),
        ("capital_flow", [{"日期": "2026-08-14", "主力净流入": "1.2亿元"}]),
        ("dragon_tiger", []),
        ("margin", [{"日期": "2026-08-14", "融资余额": "3亿元"}]),
        ("block_trade", [{"交易日期": "2026-08-14", "成交量": "100万股"}]),
        ("holders", [{"报告期": "2026-06-30", "股东户数": 1000}]),
        ("unlock", [{"解禁日期": "2026-08-14", "解禁股数": "20万股"}]),
        ("dividend", [{"除权除息日": "2026-08-14", "派息": "1元"}]),
    ],
)
def test_extended_capabilities_have_distinct_empty_and_failure_states(capability, payload) -> None:
    manager = _manager(("fake", FakeSource({capability: payload})))
    result = manager.fetch(DataQuery(capability, "600519.SH"))
    assert result.status is (DataStatus.VALID_EMPTY if payload == [] else DataStatus.OK)
    assert result.capability == capability


def test_announcement_and_report_are_bounded_evidence_metadata() -> None:
    payloads = {
        "announcement": [
            {"公告标题": "年度报告", "公告日期": "2026-08-14", "link": "https://example.invalid/a.pdf"},
            {"公告标题": "未来公告", "公告日期": "2026-08-16"},
        ],
        "research_report": [{"报告标题": "研究报告", "报告日期": "2026-08-13", "infoCode": "R1"}],
    }
    manager = _manager(("official", FakeSource(payloads)))

    announcement = manager.fetch(DataQuery("announcement", "600519.SH", as_of=date(2026, 8, 15)))
    report = manager.fetch(DataQuery("research_report", "600519.SH"))

    assert len(announcement.data) == 1
    assert announcement.data[0]["artifact_ref"].endswith(".pdf")
    assert "content" not in announcement.data[0]
    assert report.data[0]["artifact_ref"] == "eastmoney-report:R1"


def test_schema_failure_is_not_reported_as_legal_empty() -> None:
    manager = _manager(("fake", FakeSource({"financial_statement": [{"unknown": 1}]})))
    result = manager.fetch(DataQuery("financial_statement", "600519.SH", fields=("balance_sheet",)))
    assert result.status is DataStatus.SCHEMA_CHANGED

