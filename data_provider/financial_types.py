# -*- coding: utf-8 -*-
# Reference: a-stock-data SKILL.md (Apache-2.0), independently rewritten for
# DSA; no upstream executable code is copied into this file.
"""Normalized financial statement and consensus-estimate contracts.

The reference a-stock-data skill exposes Sina's three statements and
Tonghuashun consensus EPS as dictionaries/DataFrames with provider-specific
Chinese column names.  This module turns those shapes into DataEnvelope data
without filling absent values and with an explicit source tier/fallback chain.
It is deliberately parser-only: callers inject network fetchers so ordinary
tests remain deterministic and offline.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import pandas as pd

from .market_data_types import DataEnvelope, DataStatus
from .provider_fixtures import schema_fingerprint
from .security_id import SecurityId, parse_security_id


STATEMENT_TYPES = frozenset({"balance_sheet", "income_statement", "cash_flow_statement"})
_PERIOD_KEYS = ("报告期", "报告日期", "截止日期", "统计截止日期", "report_period", "period", "date")
_FORECAST_PERIOD_KEYS = ("年度", "预测年度", "年份", "year", "forecast_year", "period")
_SOURCE_TIER_ORDER = {"official": 0, "primary": 1, "backup": 2, "derived": 3}


def _as_records(rows: Any) -> Optional[list[dict[str, Any]]]:
    if rows is None:
        return None
    if isinstance(rows, pd.DataFrame):
        return [dict(row) for row in rows.to_dict(orient="records")]
    if isinstance(rows, Mapping):
        # Sina returns a period-keyed mapping in some deployments.
        if all(isinstance(value, Mapping) for value in rows.values()):
            result = []
            for period, value in rows.items():
                item = dict(value)
                item.setdefault("报告期", period)
                result.append(item)
            return result
        return [dict(rows)]
    if isinstance(rows, (list, tuple)):
        result = []
        for row in rows:
            if isinstance(row, Mapping):
                result.append(dict(row))
            elif isinstance(row, pd.Series):
                result.append(dict(row.to_dict()))
            else:
                return None
        return result
    return None


def _first_value(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, "", "-", "--", "N/A", "nan"):
            return row[key]
    return None


def normalize_period(value: Any) -> Optional[str]:
    """Normalize common provider report-period formats to YYYY-MM-DD."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    quarter = re.fullmatch(r"(\d{4})[- ]?Q([1-4])", text, re.IGNORECASE)
    if quarter:
        return {"1": f"{quarter.group(1)}-03-31", "2": f"{quarter.group(1)}-06-30", "3": f"{quarter.group(1)}-09-30", "4": f"{quarter.group(1)}-12-31"}[quarter.group(2)]
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) == 8:
        try:
            return datetime.strptime(digits, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    if len(digits) == 6:
        try:
            return datetime.strptime(digits + "01", "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    try:
        parsed = pd.to_datetime(text, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date().isoformat()
    except Exception:
        return None


def normalize_number(value: Any, *, percent: bool = False) -> Optional[float]:
    """Parse provider numbers while retaining missing values as ``None``."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text or text in {"-", "--", "—", "N/A", "nan", "None"}:
        return None
    had_percent = text.endswith("%")
    if had_percent:
        text = text[:-1].strip()
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    # Values in Sina/THS tables are already percentage points when a percent
    # sign is present.  Do not divide by 100 and silently change semantics.
    return parsed


def _normalized_metrics(row: Mapping[str, Any], excluded: set[str]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for key, value in row.items():
        name = str(key).strip()
        if not name or name in excluded:
            continue
        is_percent = name.endswith("%") or any(token in name for token in ("同比", "占比", "率", "margin", "ratio"))
        parsed = normalize_number(value, percent=is_percent)
        metrics[name] = parsed if parsed is not None else (str(value).strip() if value is not None else None)
    return metrics


def _retrieved_at(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    return value


def _security(value: SecurityId | str) -> SecurityId:
    return value if isinstance(value, SecurityId) else parse_security_id(str(value))


def normalize_financial_statement(
    rows: Any,
    *,
    statement_type: str,
    security_id: SecurityId | str,
    source: str,
    source_tier: str,
    unit: str = "CNY",
    currency: str = "CNY",
    retrieved_at: Optional[datetime] = None,
    fallback_chain: Sequence[str] = (),
    as_of: Optional[date | datetime] = None,
) -> DataEnvelope:
    """Normalize one of the Sina-style balance/income/cash-flow statements."""

    statement_type = str(statement_type).strip()
    if statement_type not in STATEMENT_TYPES:
        raise ValueError(f"unsupported statement_type: {statement_type}")
    retrieved = _retrieved_at(retrieved_at)
    sid = _security(security_id)
    records = _as_records(rows)
    if records is None:
        status = DataStatus.UPSTREAM_BLOCKED
        data: dict[str, Any] = {"statement_type": statement_type, "periods": []}
        flags = ("upstream_blocked",)
    elif not records:
        status = DataStatus.VALID_EMPTY
        data = {"statement_type": statement_type, "periods": []}
        flags = ("valid_empty",)
    else:
        periods = []
        invalid_periods = 0
        for row in records:
            raw_period = _first_value(row, _PERIOD_KEYS)
            period = normalize_period(raw_period)
            if period is None:
                invalid_periods += 1
                continue
            periods.append(
                {
                    "period": period,
                    "metrics": _normalized_metrics(row, set(_PERIOD_KEYS)),
                }
            )
        if not periods:
            status = DataStatus.SCHEMA_CHANGED
            flags = ("schema_changed", "missing:report_period")
        else:
            status = DataStatus.OK
            flags_list = []
            if invalid_periods:
                flags_list.append(f"invalid_period_rows:{invalid_periods}")
            flags = tuple(flags_list)
        data = {
            "statement_type": statement_type,
            "periods": sorted(periods, key=lambda item: item["period"], reverse=True),
            "schema_fingerprint": schema_fingerprint(records),
        }
    return DataEnvelope(
        capability="financial_statement",
        security_id=sid,
        data=data,
        source=source,
        source_tier=source_tier,
        as_of=as_of,
        retrieved_at=retrieved,
        unit=unit,
        currency=currency,
        quality_flags=flags,
        fallback_chain=tuple(fallback_chain),
        status=status,
        provenance={"adapter": "financial_statement", "statement_type": statement_type},
    )


def normalize_consensus_estimates(
    rows: Any,
    *,
    security_id: SecurityId | str,
    source: str,
    source_tier: str,
    unit: str = "CNY_PER_SHARE",
    currency: str = "CNY",
    retrieved_at: Optional[datetime] = None,
    fallback_chain: Sequence[str] = (),
) -> DataEnvelope:
    """Normalize THS/EastMoney consensus EPS rows without inventing estimates."""

    retrieved = _retrieved_at(retrieved_at)
    sid = _security(security_id)
    records = _as_records(rows)
    if records is None:
        status = DataStatus.UPSTREAM_BLOCKED
        estimates: list[dict[str, Any]] = []
        flags = ("upstream_blocked",)
    elif not records:
        status = DataStatus.VALID_EMPTY
        estimates = []
        flags = ("valid_empty",)
    else:
        estimates = []
        missing = 0
        for row in records:
            period_value = _first_value(row, _FORECAST_PERIOD_KEYS)
            period = str(period_value).strip() if period_value is not None else None
            if not period:
                missing += 1
                continue
            estimates.append(
                {
                    "period": period,
                    "analyst_count": normalize_number(_first_value(row, ("预测机构数", "机构数", "analyst_count"))),
                    "low": normalize_number(_first_value(row, ("最小值", "最低", "low"))),
                    "mean": normalize_number(_first_value(row, ("均值", "平均", "mean", "consensus"))),
                    "high": normalize_number(_first_value(row, ("最大值", "最高", "high"))),
                }
            )
        if not estimates:
            status = DataStatus.SCHEMA_CHANGED
            flags = ("schema_changed", "missing:forecast_period")
        else:
            status = DataStatus.OK
            flags = (f"invalid_period_rows:{missing}",) if missing else ()
    data = {
        "estimates": estimates,
        "schema_fingerprint": schema_fingerprint(records or []),
    }
    return DataEnvelope(
        capability="consensus_estimate",
        security_id=sid,
        data=data,
        source=source,
        source_tier=source_tier,
        as_of=None,
        retrieved_at=retrieved,
        unit=unit,
        currency=currency,
        quality_flags=flags,
        fallback_chain=tuple(fallback_chain),
        status=status,
        provenance={"adapter": "consensus_estimate"},
    )


def align_statement_periods(*statements: DataEnvelope | Mapping[str, Any], mode: str = "intersection") -> list[str]:
    """Align report periods across statements for point-in-time comparisons."""

    if mode not in {"intersection", "union"}:
        raise ValueError("mode must be intersection or union")
    sets: list[set[str]] = []
    for statement in statements:
        data = statement.data if isinstance(statement, DataEnvelope) else statement
        periods = data.get("periods", []) if isinstance(data, Mapping) else []
        sets.append({str(item.get("period")) for item in periods if isinstance(item, Mapping) and item.get("period")})
    if not sets:
        return []
    result = set.intersection(*sets) if mode == "intersection" else set.union(*sets)
    return sorted(result, reverse=True)


class FinancialStatementAdapter:
    """Injectable primary/fallback adapter used by services and offline tests."""

    def __init__(self, sources: Mapping[str, Callable[..., Any]]) -> None:
        if not sources:
            raise ValueError("at least one financial source is required")
        self._sources = dict(sources)

    def fetch(
        self,
        security_id: SecurityId | str,
        *,
        statement_type: str,
        source_order: Optional[Sequence[str]] = None,
        source_tiers: Optional[Mapping[str, str]] = None,
        **kwargs: Any,
    ) -> DataEnvelope:
        order = tuple(source_order or self._sources.keys())
        tiers = dict(source_tiers or {})
        attempted: list[str] = []
        last: Optional[DataEnvelope] = None
        for name in order:
            source = self._sources.get(name)
            if source is None:
                continue
            attempted.append(name)
            try:
                payload = source(security_id=security_id, statement_type=statement_type, **kwargs)
            except Exception as exc:
                last = normalize_financial_statement(
                    None,
                    statement_type=statement_type,
                    security_id=security_id,
                    source=name,
                    source_tier=tiers.get(name, "backup"),
                    fallback_chain=tuple(attempted[:-1]),
                )
                last = DataEnvelope(
                    **{**last.__dict__, "provenance": {"adapter": "financial_statement", "error": type(exc).__name__}}
                )
                continue
            envelope = normalize_financial_statement(
                payload,
                statement_type=statement_type,
                security_id=security_id,
                source=name,
                source_tier=tiers.get(name, "primary" if len(attempted) == 1 else "backup"),
                fallback_chain=tuple(attempted[:-1]),
            )
            last = envelope
            if envelope.status in {DataStatus.OK, DataStatus.VALID_EMPTY}:
                return envelope
        if last is not None:
            return last
        return normalize_financial_statement(
            None,
            statement_type=statement_type,
            security_id=security_id,
            source="unavailable",
            source_tier="derived",
        )


__all__ = [
    "FinancialStatementAdapter",
    "STATEMENT_TYPES",
    "align_statement_periods",
    "normalize_consensus_estimates",
    "normalize_financial_statement",
    "normalize_number",
    "normalize_period",
]
