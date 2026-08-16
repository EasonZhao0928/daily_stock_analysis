# -*- coding: utf-8 -*-
"""Provider-neutral contracts for Market Data (no routing or network I/O)."""
from __future__ import annotations
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from .market_clock import MARKET_ALIASES, MarketClock
from .security_id import SecurityId, parse_security_id
Timestamp = Union[date, datetime]


class DataStatus(str, Enum):
    """Stable result states; the six non-success states are distinct."""
    OK = "ok"
    VALID = "ok"
    VALID_EMPTY = "valid_empty"
    MARKET_CLOSED = "market_closed"
    SYMBOL_INVALID = "symbol_invalid"
    STALE_SYMBOL = "stale_symbol"
    UPSTREAM_BLOCKED = "upstream_blocked"
    SCHEMA_CHANGED = "schema_changed"

def _text(value: Any, name: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value

def _texts(values: Optional[Sequence[str]], name: str) -> Tuple[str, ...]:
    values = (values,) if isinstance(values, str) else (values or ())
    result = []
    for value in values:
        value = _text(value, name)
        if value not in result:
            result.append(value)
    return tuple(result)

def _duration(value: Optional[Union[timedelta, int, float]], name: str) -> Optional[timedelta]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative duration")
    result = value if isinstance(value, timedelta) else timedelta(seconds=float(value))
    if result.total_seconds() < 0:
        raise ValueError(f"{name} must be a non-negative duration")
    return result

def _timestamp(value: Optional[Timestamp], name: str) -> Optional[Timestamp]:
    if value is None or (isinstance(value, date) and not isinstance(value, datetime)):
        return value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be date or timezone-aware datetime")
    return value

def _market(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    market = MARKET_ALIASES.get(str(value).strip().lower())
    if market is None:
        raise ValueError(f"unsupported market: {value!r}")
    return market

def _sid(value: Union[SecurityId, str]) -> SecurityId:
    return value if isinstance(value, SecurityId) else parse_security_id(str(value))

@dataclass(frozen=True)
class DataQuery:
    """A deterministic request for one capability and one SecurityId."""
    capability: str
    security_id: Union[SecurityId, str]
    as_of: Optional[Timestamp] = None
    fields: Tuple[str, ...] = ()
    market: Optional[str] = None
    # History window.  Without these the capability route could only ask for a
    # provider's default page, so a 240-bar chart request silently returned ~30
    # days of data.  ``as_of`` remains the upper bound of the window.
    start: Optional[Timestamp] = None
    limit: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", _text(self.capability, "capability"))
        security_id = _sid(self.security_id)
        object.__setattr__(self, "security_id", security_id)
        object.__setattr__(self, "as_of", _timestamp(self.as_of, "as_of"))
        object.__setattr__(self, "fields", _texts(self.fields, "field"))
        object.__setattr__(self, "start", _timestamp(self.start, "start"))
        if self.limit is not None:
            limit = int(self.limit)
            if limit <= 0:
                raise ValueError("limit must be positive")
            object.__setattr__(self, "limit", limit)
        if self.start is not None and self.as_of is not None and self.start > self.as_of:
            raise ValueError("start must be <= as_of")
        market = _market(self.market) or security_id.market
        if market != security_id.market:
            raise ValueError("query market conflicts with security_id")
        object.__setattr__(self, "market", market)
    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "security_id": self.security_id.to_dict(),
                "market": self.market, "as_of": _jsonable(self.as_of), "fields": list(self.fields),
                "start": _jsonable(self.start), "limit": self.limit}

@dataclass(frozen=True)
class Provenance:
    """Immutable source metadata with provider-specific details."""
    source: str
    source_tier: str
    retrieved_at: datetime
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.retrieved_at, datetime) or self.retrieved_at.tzinfo is None:
            raise ValueError("provenance.retrieved_at must be timezone-aware datetime")
        object.__setattr__(self, "source", _text(self.source, "provenance.source"))
        object.__setattr__(self, "source_tier", _text(self.source_tier, "provenance.source_tier"))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details or {})))
    def to_dict(self) -> dict[str, Any]:
        result = {"source": self.source, "source_tier": self.source_tier,
                  "retrieved_at": self.retrieved_at.isoformat()}
        result.update({str(k): _jsonable(v) for k, v in self.details.items()})
        return result

@dataclass(frozen=True)
class SourcePolicy:
    """Declarative primary/fallback order and freshness budgets."""
    primary_sources: Tuple[str, ...] = ()
    fallback_sources: Tuple[str, ...] = ()
    timeout_seconds: float = 10.0
    max_retries: int = 0
    cache_ttl: Optional[Union[timedelta, int, float]] = None
    stale_threshold: Optional[Union[timedelta, int, float]] = None
    allow_stale: bool = False
    trading_hours_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "primary_sources", _texts(self.primary_sources, "primary source"))
        object.__setattr__(self, "fallback_sources", _texts(self.fallback_sources, "fallback source"))
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        object.__setattr__(self, "cache_ttl", _duration(self.cache_ttl, "cache_ttl"))
        object.__setattr__(self, "stale_threshold", _duration(self.stale_threshold, "stale_threshold"))
    @property
    def source_chain(self) -> Tuple[str, ...]:
        return _texts(self.primary_sources + self.fallback_sources, "source")

    @property
    def cache_ttl_seconds(self) -> Optional[float]:
        return None if self.cache_ttl is None else self.cache_ttl.total_seconds()

    @property
    def stale_threshold_seconds(self) -> Optional[float]:
        return None if self.stale_threshold is None else self.stale_threshold.total_seconds()

    def to_dict(self) -> dict[str, Any]:
        seconds = lambda value: None if value is None else value.total_seconds()
        return {"primary_sources": list(self.primary_sources), "fallback_sources": list(self.fallback_sources),
                "source_chain": list(self.source_chain), "timeout_seconds": self.timeout_seconds,
                "max_retries": self.max_retries, "cache_ttl_seconds": seconds(self.cache_ttl),
                "stale_threshold_seconds": seconds(self.stale_threshold), "allow_stale": self.allow_stale,
                "trading_hours_only": self.trading_hours_only}

@dataclass(frozen=True)
class DataEnvelope:
    """Normalized data and diagnostics consumed by later adapters."""
    capability: str
    security_id: Union[SecurityId, str]
    data: Any
    source: str
    source_tier: str
    as_of: Optional[Timestamp]
    retrieved_at: datetime
    unit: Optional[str] = None
    currency: Optional[str] = None
    is_stale: bool = False
    quality_flags: Tuple[str, ...] = ()
    fallback_chain: Tuple[str, ...] = ()
    market: Optional[str] = None
    status: DataStatus = DataStatus.OK
    provenance: Optional[Union[Provenance, Mapping[str, Any]]] = None
    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", _text(self.capability, "capability"))
        security_id = _sid(self.security_id)
        object.__setattr__(self, "security_id", security_id)
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(self, "source_tier", _text(self.source_tier, "source_tier"))
        object.__setattr__(self, "as_of", _timestamp(self.as_of, "as_of"))
        if not isinstance(self.retrieved_at, datetime) or self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware datetime")
        market = _market(self.market) or security_id.market
        if market != security_id.market:
            raise ValueError("envelope market conflicts with security_id")
        object.__setattr__(self, "market", market)
        if self.unit is not None:
            object.__setattr__(self, "unit", _text(self.unit, "unit"))
        if self.currency is not None:
            object.__setattr__(self, "currency", _text(self.currency, "currency").upper())
        if not isinstance(self.is_stale, bool):
            raise TypeError("is_stale must be bool")
        status = self.status if isinstance(self.status, DataStatus) else DataStatus(self.status)
        if status is DataStatus.OK and self.is_stale:
            status = DataStatus.STALE_SYMBOL
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "is_stale", self.is_stale or status is DataStatus.STALE_SYMBOL)
        object.__setattr__(self, "quality_flags", _texts(self.quality_flags, "quality flag"))
        object.__setattr__(self, "fallback_chain", _texts(self.fallback_chain, "fallback source"))
        value = self.provenance
        if value is None:
            value = Provenance(self.source, self.source_tier, self.retrieved_at)
        elif isinstance(value, Mapping):
            values = dict(value)
            source = values.pop("source", self.source)
            tier = values.pop("source_tier", self.source_tier)
            retrieved = values.pop("retrieved_at", self.retrieved_at)
            value = Provenance(source, tier, retrieved, values)
        if not isinstance(value, Provenance):
            raise TypeError("provenance must be Provenance, mapping, or None")
        object.__setattr__(self, "provenance", value)
    @property
    def is_available(self) -> bool:
        return self.status in {DataStatus.OK, DataStatus.VALID_EMPTY}
    @property
    def should_fallback(self) -> bool:
        return self.status in {DataStatus.STALE_SYMBOL, DataStatus.UPSTREAM_BLOCKED, DataStatus.SCHEMA_CHANGED}
    def is_stale_at(self, clock: MarketClock, *, now: Optional[datetime] = None) -> bool:
        return self.is_stale or (isinstance(self.as_of, datetime) and self.market == clock.market and clock.is_stale(self.as_of, now=now))
    def with_freshness(self, clock: MarketClock, *, now: Optional[datetime] = None) -> "DataEnvelope":
        return replace(self, status=DataStatus.STALE_SYMBOL, is_stale=True) if self.is_stale_at(clock, now=now) else self
    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "security_id": self.security_id.to_dict(), "data": _jsonable(self.data),
                "source": self.source, "source_tier": self.source_tier, "provenance": self.provenance.to_dict(),
                "as_of": _jsonable(self.as_of), "retrieved_at": self.retrieved_at.isoformat(), "market": self.market,
                "unit": self.unit, "currency": self.currency, "is_stale": self.is_stale, "status": self.status.value,
                "quality_flags": list(self.quality_flags), "fallback_chain": list(self.fallback_chain)}

def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, SecurityId):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value

__all__ = ["DataEnvelope", "DataQuery", "DataStatus", "Provenance", "SourcePolicy"]
