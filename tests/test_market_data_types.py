# -*- coding: utf-8 -*-
"""Offline contract tests for the Market Data value objects."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from data_provider.market_data_types import (
    DataEnvelope,
    DataQuery,
    DataStatus,
    Provenance,
    SourcePolicy,
)
from data_provider.market_clock import MarketClock
from data_provider.security_id import parse_security_id


CN_TZ = ZoneInfo("Asia/Shanghai")
FIXED_RETRIEVED_AT = datetime(2026, 1, 5, 10, 0, tzinfo=CN_TZ)


def _query() -> DataQuery:
    return DataQuery(
        capability="realtime_quote",
        security_id=parse_security_id("600519"),
        as_of=FIXED_RETRIEVED_AT,
        fields=("price", "volume"),
    )


def _envelope(*, status: DataStatus = DataStatus.OK, **kwargs) -> DataEnvelope:
    values = {
        "capability": "realtime_quote",
        "security_id": parse_security_id("600519"),
        "data": {"price": 1600.0},
        "source": "fixture",
        "source_tier": "primary",
        "as_of": FIXED_RETRIEVED_AT,
        "retrieved_at": FIXED_RETRIEVED_AT,
        "unit": "CNY/share",
        "currency": "CNY",
        "status": status,
        "quality_flags": ("fixture",),
        "fallback_chain": ("fixture", "backup"),
    }
    values.update(kwargs)
    return DataEnvelope(**values)


def test_data_query_normalizes_security_id_and_serializes_deterministically():
    query = DataQuery(
        capability="realtime_quote",
        security_id="SH600519",
        market="CN",
        fields=["volume", "price", "price"],
    )

    assert query.security_id == parse_security_id("600519")
    assert query.market == "cn"
    assert query.fields == ("volume", "price")
    encoded = json.dumps(query.to_dict(), sort_keys=True)
    assert "realtime_quote" in encoded
    assert "sh600519" in encoded.lower()


@pytest.mark.parametrize(
    "status",
    [
        DataStatus.VALID_EMPTY,
        DataStatus.MARKET_CLOSED,
        DataStatus.SYMBOL_INVALID,
        DataStatus.STALE_SYMBOL,
        DataStatus.UPSTREAM_BLOCKED,
        DataStatus.SCHEMA_CHANGED,
    ],
)
def test_stable_statuses_are_distinct_and_not_collapsed_to_empty(status):
    statuses = {
        DataStatus.VALID_EMPTY.value,
        DataStatus.MARKET_CLOSED.value,
        DataStatus.SYMBOL_INVALID.value,
        DataStatus.STALE_SYMBOL.value,
        DataStatus.UPSTREAM_BLOCKED.value,
        DataStatus.SCHEMA_CHANGED.value,
    }

    assert len(statuses) == 6
    envelope = _envelope(status=status, data=[])
    assert envelope.status is status
    assert envelope.to_dict()["status"] == status.value


def test_envelope_contains_provenance_units_quality_and_fallback_chain():
    envelope = _envelope(
        provenance={"adapter": "fixture_adapter", "request_id": "req-1"},
    )

    assert envelope.market == "cn"
    assert envelope.provenance.source == "fixture"
    assert envelope.provenance.details["adapter"] == "fixture_adapter"
    assert envelope.unit == "CNY/share"
    assert envelope.currency == "CNY"
    assert envelope.quality_flags == ("fixture",)
    assert envelope.fallback_chain == ("fixture", "backup")

    # The contract is safe to hand to an API boundary without a custom encoder.
    encoded = json.dumps(envelope.to_dict(), sort_keys=True)
    assert "schema_changed" not in encoded


def test_stale_status_is_reflected_in_stale_flag_and_clock_can_assess_freshness():
    session_date = date(2026, 1, 5)
    clock = MarketClock(
        "cn",
        stale_threshold=timedelta(minutes=10),
        trading_day_resolver=lambda market, check_date: check_date == session_date,
    )
    stale = _envelope(
        as_of=datetime(2026, 1, 5, 9, 45, tzinfo=CN_TZ),
        status=DataStatus.STALE_SYMBOL,
    )

    assert stale.is_stale is True
    assert stale.is_stale_at(clock, now=FIXED_RETRIEVED_AT) is True
    assessed = _envelope(
        as_of=datetime(2026, 1, 5, 9, 45, tzinfo=CN_TZ),
    ).with_freshness(clock, now=FIXED_RETRIEVED_AT)
    assert assessed.status is DataStatus.STALE_SYMBOL
    assert assessed.is_stale is True


def test_valid_empty_is_successful_absence_and_does_not_request_fallback():
    envelope = _envelope(status=DataStatus.VALID_EMPTY, data=[])

    assert envelope.is_available is True
    assert envelope.should_fallback is False


def test_failure_statuses_request_fallback_except_symbol_and_market_semantics():
    assert _envelope(status=DataStatus.UPSTREAM_BLOCKED).should_fallback is True
    assert _envelope(status=DataStatus.SCHEMA_CHANGED).should_fallback is True
    assert _envelope(status=DataStatus.STALE_SYMBOL).should_fallback is True
    assert _envelope(status=DataStatus.SYMBOL_INVALID).should_fallback is False
    assert _envelope(status=DataStatus.MARKET_CLOSED).should_fallback is False


def test_source_policy_normalizes_order_and_validates_budgets():
    policy = SourcePolicy(
        primary_sources=("eastmoney", "eastmoney"),
        fallback_sources=["tencent", "sina", "tencent"],
        timeout_seconds=2.5,
        max_retries=2,
        cache_ttl=timedelta(minutes=5),
        stale_threshold=timedelta(minutes=10),
        allow_stale=True,
        trading_hours_only=True,
    )

    assert policy.primary_sources == ("eastmoney",)
    assert policy.fallback_sources == ("tencent", "sina")
    assert policy.source_chain == ("eastmoney", "tencent", "sina")
    assert policy.cache_ttl_seconds == 300.0
    assert policy.stale_threshold_seconds == 600.0
    json.dumps(policy.to_dict(), sort_keys=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"max_retries": -1},
        {"cache_ttl": -1},
        {"stale_threshold": -1},
    ],
)
def test_source_policy_rejects_invalid_budgets(kwargs):
    with pytest.raises(ValueError):
        SourcePolicy(**kwargs)


def test_provenance_value_is_immutable_and_serializable():
    provenance = Provenance(
        source="fixture",
        source_tier="backup",
        retrieved_at=FIXED_RETRIEVED_AT,
        details={"adapter": "fixture_adapter"},
    )
    assert provenance.details["adapter"] == "fixture_adapter"
    assert json.dumps(provenance.to_dict(), sort_keys=True)
    with pytest.raises(TypeError):
        provenance.details["adapter"] = "changed"
