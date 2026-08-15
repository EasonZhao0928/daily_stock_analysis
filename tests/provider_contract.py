# -*- coding: utf-8 -*-
"""Shared offline contract helpers for Market Data provider adapters.

The provider implementations still expose their legacy DataFrame return shape.
This module keeps the contract assertions in one place while adapting that
shape to the provider-neutral ``DataEnvelope`` used by the route layer.  The
fixtures are deliberately small and deterministic; no provider is contacted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from math import isclose
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from data_provider.market_clock import MarketClock
from data_provider.market_data_types import DataEnvelope, DataStatus
from data_provider.security_id import SecurityId, parse_security_id


CN_AS_OF = datetime(2026, 1, 5, 9, 55, tzinfo=ZoneInfo("Asia/Shanghai"))
CN_RETRIEVED_AT = datetime(2026, 1, 5, 1, 56, tzinfo=ZoneInfo("UTC"))
CN_SECURITY = parse_security_id("600519.SH")

DAILY_FIELDS = ("date", "open", "high", "low", "close", "volume", "amount", "pct_chg")


@dataclass(frozen=True)
class ProviderContractCase:
    """An adapter parser plus one redacted, deterministic provider payload."""

    name: str
    security_id: SecurityId
    raw_payload: Any
    normalize: Callable[[Any, str], pd.DataFrame]
    expected_volume_shares: int
    expected_amount_cny: float
    expected_pct_chg: float
    expected_date: str

    def normalized(self) -> pd.DataFrame:
        return self.normalize(self.raw_payload.copy(), self.security_id.canonical_code)


def _is_empty(payload: Any) -> bool:
    if isinstance(payload, (list, tuple, dict, set, str, bytes)):
        return len(payload) == 0
    empty = getattr(payload, "empty", None)
    if empty is not None:
        try:
            return bool(empty)
        except (TypeError, ValueError):
            return False
    return False


def _missing_fields(payload: Any, required_fields: Sequence[str]) -> tuple[str, ...]:
    if isinstance(payload, pd.DataFrame):
        available = set(payload.columns)
    elif isinstance(payload, Mapping):
        available = set(payload)
    else:
        available = set()
    return tuple(field for field in required_fields if field not in available)


def envelope_for_payload(
    *,
    source: str,
    security_id: SecurityId,
    payload: Any,
    required_fields: Sequence[str] = (),
    capability: str = "daily_data",
    as_of: Optional[datetime] = CN_AS_OF,
    retrieved_at: datetime = CN_RETRIEVED_AT,
    source_tier: str = "primary",
    unit: str = "shares",
    currency: str = "CNY",
    fallback_chain: Sequence[str] = (),
) -> DataEnvelope:
    """Classify a legacy adapter payload without inventing missing values.

    ``None`` means the upstream was unavailable, an empty payload is a valid
    empty result, and a non-empty payload missing required fields is schema
    drift.  The raw payload is retained for diagnostics in the latter case;
    no amount, ratio, or volume is filled with zero.
    """

    quality_flags: list[str] = []
    if payload is None:
        status = DataStatus.UPSTREAM_BLOCKED
    elif _is_empty(payload):
        status = DataStatus.VALID_EMPTY
    else:
        missing = _missing_fields(payload, required_fields)
        status = DataStatus.SCHEMA_CHANGED if missing else DataStatus.OK
        if missing:
            quality_flags.append("missing:" + ",".join(missing))
    quality_flags.append(status.value)
    return DataEnvelope(
        capability=capability,
        security_id=security_id,
        data=payload,
        source=source,
        source_tier=source_tier,
        as_of=as_of,
        retrieved_at=retrieved_at,
        unit=unit,
        currency=currency,
        quality_flags=tuple(quality_flags),
        fallback_chain=tuple(fallback_chain),
        status=status,
        provenance={"adapter_contract": "offline_fixture"},
    )


def assert_envelope_contract(
    envelope: DataEnvelope,
    *,
    security_id: SecurityId,
    source: str,
    status: DataStatus = DataStatus.OK,
    as_of: Optional[datetime] = CN_AS_OF,
    retrieved_at: datetime = CN_RETRIEVED_AT,
) -> None:
    """Assert identity, time, provenance, units, currency and serialization."""

    assert envelope.security_id == security_id
    assert envelope.market == security_id.market == "cn"
    assert envelope.source == source
    assert envelope.status is status
    assert envelope.unit == "shares"
    assert envelope.currency == "CNY"
    assert envelope.as_of == as_of
    assert envelope.as_of is not None and envelope.as_of.tzinfo is not None
    assert envelope.retrieved_at == retrieved_at
    assert envelope.retrieved_at.tzinfo is not None
    assert envelope.provenance.source == source
    assert envelope.provenance.retrieved_at == envelope.retrieved_at
    # DataEnvelope.to_dict must remain safe to persist in JSON logs/artifacts.
    json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True)


def assert_daily_contract(
    frame: pd.DataFrame,
    *,
    expected_volume_shares: int,
    expected_amount_cny: float,
    expected_pct_chg: float,
    expected_date: str,
) -> None:
    """Assert canonical OHLCV columns and their units/ratios."""

    assert set(DAILY_FIELDS).issubset(frame.columns)
    assert not frame.empty
    # Use the latest observation so adapters that derive the first ratio from
    # a prior close (Tencent) are tested with a real ratio rather than a
    # synthetic one-row default.
    row = frame.iloc[-1]
    assert pd.Timestamp(row["date"]).strftime("%Y-%m-%d") == expected_date
    for field in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
        assert pd.notna(row[field]), field
        assert isinstance(row[field], (int, float)) or pd.api.types.is_number(row[field])
    assert int(row["volume"]) == expected_volume_shares
    assert isclose(float(row["amount"]), expected_amount_cny, rel_tol=0, abs_tol=1e-6)
    assert isclose(float(row["pct_chg"]), expected_pct_chg, rel_tol=0, abs_tol=1e-6)


def assert_stale_is_distinct_from_schema_drift(envelope: DataEnvelope) -> None:
    """Use a fixed MarketClock to verify stale is not silently schema drift."""

    clock = MarketClock(
        "cn",
        stale_threshold=300,
        trading_day_resolver=lambda _market, _date: True,
    )
    stale = envelope.with_freshness(
        clock,
        now=datetime(2026, 1, 5, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert stale.status is DataStatus.STALE_SYMBOL
    assert stale.status is not DataStatus.SCHEMA_CHANGED
    assert stale.is_stale is True


__all__ = [
    "CN_AS_OF",
    "CN_RETRIEVED_AT",
    "CN_SECURITY",
    "DAILY_FIELDS",
    "ProviderContractCase",
    "assert_daily_contract",
    "assert_envelope_contract",
    "assert_stale_is_distinct_from_schema_drift",
    "envelope_for_payload",
]
