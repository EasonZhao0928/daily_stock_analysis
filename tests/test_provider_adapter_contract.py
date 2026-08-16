# -*- coding: utf-8 -*-
"""Offline contract coverage for existing Market Data provider adapters."""

from __future__ import annotations

from datetime import datetime
from math import isclose
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from data_provider.tencent_fetcher import TencentFetcher, _extract_kline_rows
from data_provider.tushare_fetcher import TushareFetcher
from data_provider.market_data_types import DataStatus
from tests.provider_contract import (
    CN_AS_OF,
    CN_RETRIEVED_AT,
    CN_SECURITY,
    DAILY_FIELDS,
    ProviderContractCase,
    assert_daily_contract,
    assert_envelope_contract,
    assert_stale_is_distinct_from_schema_drift,
    envelope_for_payload,
)


def _tencent_case() -> ProviderContractCase:
    # Tencent daily K-line volume is returned in lots.  The adapter parser
    # converts it to shares before the normalizer sees the row.
    rows = _extract_kline_rows(
        {
            "data": {
                "sh600519": {
                    "qfqday": [
                        ["2026-01-04", "99.0", "100.0", "101.0", "98.0", "1234", "6404500"],
                        ["2026-01-05", "100.0", "101.25", "102.0", "99.0", "1234", "6404500"],
                    ]
                }
            }
        },
        symbol="sh600519",
    )
    return ProviderContractCase(
        name="TencentFetcher",
        security_id=CN_SECURITY,
        raw_payload=pd.DataFrame(rows),
        normalize=TencentFetcher()._normalize_data,
        expected_volume_shares=123400,
        expected_amount_cny=6404500.0,
        expected_pct_chg=1.25,
        expected_date="2026-01-05",
    )


def _tushare_case() -> ProviderContractCase:
    # Tushare daily vol is in lots and amount is in thousand CNY.  Both
    # conversions are part of TushareFetcher._normalize_data.
    return ProviderContractCase(
        name="TushareFetcher",
        security_id=CN_SECURITY,
        raw_payload=pd.DataFrame(
            {
                "ts_code": ["600519.SH"],
                "trade_date": ["20260105"],
                "open": [100.0],
                "high": [102.0],
                "low": [99.0],
                "close": [101.0],
                "vol": [1234],
                "amount": [6404.5],
                "pct_chg": [1.25],
            }
        ),
        normalize=TushareFetcher.__new__(TushareFetcher)._normalize_data,
        expected_volume_shares=123400,
        expected_amount_cny=6404500.0,
        expected_pct_chg=1.25,
        expected_date="2026-01-05",
    )


@pytest.fixture(params=(_tencent_case, _tushare_case), ids=("tencent", "tushare"))
def provider_contract(request) -> ProviderContractCase:
    """Return one real adapter parser with a deterministic offline payload."""

    return request.param()


def test_existing_adapters_satisfy_shared_daily_contract(provider_contract):
    frame = provider_contract.normalized()
    assert_daily_contract(
        frame,
        expected_volume_shares=provider_contract.expected_volume_shares,
        expected_amount_cny=provider_contract.expected_amount_cny,
        expected_pct_chg=provider_contract.expected_pct_chg,
        expected_date=provider_contract.expected_date,
    )
    envelope = envelope_for_payload(
        source=provider_contract.name,
        security_id=provider_contract.security_id,
        payload=frame,
        required_fields=DAILY_FIELDS,
        as_of=CN_AS_OF,
        retrieved_at=CN_RETRIEVED_AT,
    )
    assert_envelope_contract(
        envelope,
        security_id=provider_contract.security_id,
        source=provider_contract.name,
    )
    assert envelope.fallback_chain == ()


def test_provider_contract_preserves_common_amount_ratio_and_share_semantics():
    outputs = []
    for factory in (_tencent_case, _tushare_case):
        case = factory()
        frame = case.normalized()
        outputs.append(
            (
                int(frame.iloc[-1]["volume"]),
                float(frame.iloc[-1]["amount"]),
                float(frame.iloc[-1]["pct_chg"]),
            )
        )
    assert [output[0] for output in outputs] == [123400, 123400]
    assert [output[1] for output in outputs] == [6404500.0, 6404500.0]
    assert all(isclose(output[2], 1.25, rel_tol=0, abs_tol=1e-6) for output in outputs)


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ([], DataStatus.VALID_EMPTY),
        (None, DataStatus.UPSTREAM_BLOCKED),
        ({"date": "2026-01-05", "close": 101.0}, DataStatus.SCHEMA_CHANGED),
    ],
    ids=("valid-empty", "upstream-blocked", "schema-drift"),
)
def test_contract_classifies_empty_blocked_and_schema_drift_without_zero_fill(
    payload, expected_status
):
    envelope = envelope_for_payload(
        source="TencentFetcher",
        security_id=CN_SECURITY,
        payload=payload,
        required_fields=DAILY_FIELDS,
    )
    assert_envelope_contract(
        envelope,
        security_id=CN_SECURITY,
        source="TencentFetcher",
        status=expected_status,
    )
    if expected_status is DataStatus.SCHEMA_CHANGED:
        assert envelope.data["close"] == 101.0
        assert "amount" not in envelope.data
        assert envelope.data.get("volume") is None


def test_contract_marks_stale_data_without_relabeling_schema_drift():
    envelope = envelope_for_payload(
        source="TushareFetcher",
        security_id=CN_SECURITY,
        payload={field: 1 for field in DAILY_FIELDS},
        required_fields=DAILY_FIELDS,
        as_of=datetime(2026, 1, 5, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert_stale_is_distinct_from_schema_drift(envelope)
