# -*- coding: utf-8 -*-
"""Immutable Paper decision observation builder."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo


class ObservationError(ValueError):
    """Observation is not deterministic or violates point-in-time visibility."""


_MARKET_TIME_KEYS = ("as_of", "timestamp", "datetime", "bar_timestamp", "date")
_EVIDENCE_TIME_KEYS = ("as_of", "published_at", "date")
_SIGNAL_TIME_KEYS = ("data_cutoff", "signal_date", "date")
_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _as_temporal(value: Any) -> Optional[date | datetime]:
    """Parse a date or timestamp while retaining whether time was supplied.

    Paper observations historically accepted date-only source fields and naive
    ``datetime`` values.  Date-only values keep their daily-bar semantics;
    timestamp values are normalized to UTC for exact point-in-time comparison.
    Naive timestamps are interpreted in the DSA market timezone rather than
    the host process timezone, so a server's local timezone cannot change the
    decision boundary.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    else:
        text = str(value).strip() if value is not None else ""
        if not text:
            return None
        # A bare ISO date is deliberately a date, not midnight on that date.
        if len(text) == 10:
            try:
                return date.fromisoformat(text)
            except ValueError:
                return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_LOCAL_TIMEZONE)
    return parsed.astimezone(ZoneInfo("UTC"))


def _temporal_is_after(item: date | datetime, cutoff: date | datetime) -> bool:
    """Compare exact timestamps, falling back to daily semantics for dates."""
    if isinstance(item, datetime) and isinstance(cutoff, datetime):
        return item > cutoff
    if isinstance(item, datetime):
        return item.date() > cutoff
    if isinstance(cutoff, datetime):
        return item > cutoff.date()
    return item > cutoff


def _canonical_cutoff(value: date | datetime) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return value.isoformat()


def _first_present(value: Mapping[str, Any], keys: Sequence[str]) -> tuple[bool, Any]:
    """Return ``(found, raw)`` for the first key that is present and non-null."""
    for key in keys:
        if value.get(key) is not None:
            return True, value.get(key)
    return False, None


def _assert_not_after_cutoff(
    value: Mapping[str, Any],
    keys: Sequence[str],
    cutoff: date | datetime,
    *,
    label: str,
    require_timestamp: bool,
) -> None:
    """Reject anything visible past the cutoff, and anything undatable.

    An unparseable or missing timestamp used to skip the check entirely, which
    turned a point-in-time guarantee into an unenforced comment.  Undatable
    inputs now fail closed.
    """
    found, raw = _first_present(value, keys)
    if not found:
        if require_timestamp:
            raise ObservationError(f"{label} has no timestamp to compare against the cutoff")
        return
    item_temporal = _as_temporal(raw)
    if item_temporal is None:
        raise ObservationError(f"{label} has an unparseable timestamp: {raw!r}")
    if _temporal_is_after(item_temporal, cutoff):
        raise ObservationError(f"{label} is newer than the observation cutoff")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ObservationError("NaN/Infinity is not allowed in an observation")
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise ObservationError(f"unsupported observation value: {type(value).__name__}")


def build_paper_observation(
    *,
    account_snapshot: Mapping[str, Any],
    market_data: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]] = (),
    shadow_signals: Sequence[Mapping[str, Any]] = (),
    market_source_refs: Sequence[Mapping[str, Any]] = (),
    cutoff: date | datetime,
) -> dict[str, Any]:
    """Freeze account/market/evidence/signal inputs at one cutoff.

    ``market_data`` is symbol-keyed and holds nothing else; provenance travels
    in ``market_source_refs`` so a consumer iterating symbols can never mistake
    a metadata key for a security.
    """

    cutoff_temporal = _as_temporal(cutoff)
    if cutoff_temporal is None:
        raise ObservationError("cutoff must be a valid date")
    # A cutoff is part of the frozen identity.  A date-only cutoff remains a
    # date; a timestamp cutoff is normalized to a fixed timezone so equivalent
    # instants hash identically across hosts.
    cutoff_value: date | datetime = cutoff_temporal
    if isinstance(cutoff_value, datetime):
        cutoff_value = cutoff_value.astimezone(ZoneInfo("UTC"))
    if not isinstance(account_snapshot, Mapping) or not isinstance(market_data, Mapping):
        raise ObservationError("account_snapshot and market_data must be objects")

    normalized_evidence = []
    for item in evidence or ():
        if not isinstance(item, Mapping):
            raise ObservationError("evidence must contain objects")
        value = _jsonable(item)
        _assert_not_after_cutoff(
            value, _EVIDENCE_TIME_KEYS, cutoff_value, label="evidence", require_timestamp=True
        )
        normalized_evidence.append(value)

    normalized_signals = []
    for item in shadow_signals or ():
        if not isinstance(item, Mapping):
            raise ObservationError("shadow_signals must contain objects")
        value = _jsonable(item)
        _assert_not_after_cutoff(
            value, _SIGNAL_TIME_KEYS, cutoff_value, label="shadow signal", require_timestamp=True
        )
        normalized_signals.append(value)

    # Market and account inputs were previously copied in unchecked, which left
    # the single largest look-ahead vector open.  Every dated market row is now
    # held to the same cutoff as Evidence and Shadow Signals.
    normalized_market = _jsonable(market_data)
    for symbol, row in sorted(normalized_market.items()):
        if not isinstance(row, Mapping):
            raise ObservationError(f"market data for {symbol} must be an object")
        _assert_not_after_cutoff(
            row,
            _MARKET_TIME_KEYS,
            cutoff_value,
            label=f"market data for {symbol}",
            require_timestamp=True,
        )

    normalized_account = _jsonable(account_snapshot)
    _assert_not_after_cutoff(
        normalized_account,
        ("as_of", "date"),
        cutoff_value,
        label="account snapshot",
        require_timestamp=False,
    )

    payload = {
        "cutoff": _canonical_cutoff(cutoff_value),
        "account": normalized_account,
        "market": normalized_market,
        "market_source_refs": sorted(
            (_jsonable(item) for item in market_source_refs or () if isinstance(item, Mapping)),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        ),
        "evidence": sorted(normalized_evidence, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)),
        "shadow_signals": sorted(normalized_signals, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    content_hash = hashlib.sha256(encoded).hexdigest()
    return {
        **payload,
        "observation_hash": content_hash,
        "observation_id": "paper_obs_" + content_hash[:28],
    }


__all__ = ["ObservationError", "build_paper_observation"]
