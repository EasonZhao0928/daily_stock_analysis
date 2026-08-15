# -*- coding: utf-8 -*-
# Reference: a-stock-data SKILL.md (Apache-2.0), independently rewritten for
# DSA; no upstream executable code is copied into this file.
"""Small, shared normalizer for flow/ownership/event capabilities.

The reference data set has separate endpoints for capital flow, dragon-tiger,
margin, block trades, holders, unlocks and dividends.  They differ in columns
but share the same failure semantics.  This adapter intentionally does not
claim that a missing row means zero; it returns DataEnvelope states and keeps
unit/source metadata for the eventual service layer.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from .financial_types import normalize_number, normalize_period
from .market_data_types import DataEnvelope, DataStatus
from .provider_fixtures import schema_fingerprint
from .security_id import SecurityId, parse_security_id


CAPABILITIES = frozenset({"capital_flow", "dragon_tiger", "margin", "block_trade", "holders", "unlock", "dividend"})
_DATE_KEYS = ("date", "日期", "交易日期", "上榜日期", "解禁日期", "除权除息日", "报告期")
_CODE_KEYS = ("code", "股票代码", "证券代码", "symbol", "ts_code")
_UNIT_DEFAULTS = {
    "capital_flow": ("CNY", "CNY"),
    "dragon_tiger": ("CNY", "CNY"),
    "margin": ("CNY", "CNY"),
    "block_trade": ("shares", "CNY"),
    "holders": ("accounts", None),
    "unlock": ("shares", "CNY"),
    "dividend": ("CNY_PER_SHARE", "CNY"),
}


def _first(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if row.get(key) not in (None, "", "-", "--"):
            return row.get(key)
    return None


def normalize_market_evidence(
    rows: Any,
    *,
    capability: str,
    security_id: SecurityId | str,
    source: str,
    source_tier: str,
    unit: Optional[str] = None,
    currency: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
    fallback_chain: Sequence[str] = (),
) -> DataEnvelope:
    """Normalize event/flow rows with explicit legal-empty/degraded states."""

    if capability not in CAPABILITIES:
        raise ValueError(f"unsupported market evidence capability: {capability}")
    sid = security_id if isinstance(security_id, SecurityId) else parse_security_id(str(security_id))
    retrieved = retrieved_at or datetime.now(timezone.utc)
    if retrieved.tzinfo is None or retrieved.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    default_unit, default_currency = _UNIT_DEFAULTS[capability]
    unit = unit or default_unit
    currency = currency if currency is not None else default_currency
    if rows is None:
        status = DataStatus.UPSTREAM_BLOCKED
        normalized: list[dict[str, Any]] = []
        flags = ("upstream_blocked",)
    elif isinstance(rows, Mapping):
        rows = [rows]
        normalized, status, flags = _normalize_rows(rows, capability)
    elif isinstance(rows, (list, tuple)):
        normalized, status, flags = _normalize_rows(rows, capability)
    else:
        status = DataStatus.SCHEMA_CHANGED
        normalized = []
        flags = ("schema_changed", "unsupported_payload")
    data = {
        "records": normalized,
        "schema_fingerprint": schema_fingerprint(rows if rows is not None else []),
    }
    return DataEnvelope(
        capability=capability,
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
        provenance={"adapter": "market_evidence"},
    )


def _normalize_rows(rows: Sequence[Any], capability: str) -> tuple[list[dict[str, Any]], DataStatus, tuple[str, ...]]:
    if not rows:
        return [], DataStatus.VALID_EMPTY, ("valid_empty",)
    normalized: list[dict[str, Any]] = []
    missing_date = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = {}
        for key, value in row.items():
            key_text = str(key).strip()
            if not key_text:
                continue
            parsed_date = normalize_period(value) if key_text in _DATE_KEYS else None
            if parsed_date is not None:
                item[key_text] = parsed_date
                continue
            # Convert numbers only when they are clearly numeric; text labels
            # and provider-specific categories remain unchanged.
            parsed_number = normalize_number(value)
            item[key_text] = parsed_number if parsed_number is not None else value
        if _first(item, _DATE_KEYS) is None and capability in {"dragon_tiger", "unlock", "dividend", "block_trade", "holders"}:
            missing_date += 1
        normalized.append(item)
    if not normalized:
        return [], DataStatus.SCHEMA_CHANGED, ("schema_changed", "invalid_record_rows")
    if missing_date == len(normalized) and capability in {"dragon_tiger", "unlock", "dividend", "block_trade", "holders"}:
        return normalized, DataStatus.SCHEMA_CHANGED, ("schema_changed", "missing:date")
    flags = (f"invalid_date_rows:{missing_date}",) if missing_date else ()
    return normalized, DataStatus.OK, flags


class MarketEvidenceAdapter:
    """Injectable source/fallback facade for the seven event capabilities."""

    def __init__(self, sources: Mapping[str, Callable[..., Any]]) -> None:
        if not sources:
            raise ValueError("at least one source is required")
        self._sources = dict(sources)

    def fetch(
        self,
        security_id: SecurityId | str,
        *,
        capability: str,
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
                payload = source(security_id=security_id, capability=capability, **kwargs)
            except Exception:
                payload = None
            envelope = normalize_market_evidence(
                payload,
                capability=capability,
                security_id=security_id,
                source=name,
                source_tier=tiers.get(name, "primary" if len(attempted) == 1 else "backup"),
                fallback_chain=tuple(attempted[:-1]),
            )
            last = envelope
            if envelope.status in {DataStatus.OK, DataStatus.VALID_EMPTY}:
                return envelope
        return last or normalize_market_evidence(
            None,
            capability=capability,
            security_id=security_id,
            source="unavailable",
            source_tier="derived",
        )


__all__ = ["CAPABILITIES", "MarketEvidenceAdapter", "normalize_market_evidence"]
