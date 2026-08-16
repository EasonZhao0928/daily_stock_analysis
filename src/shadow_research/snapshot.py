# -*- coding: utf-8 -*-
"""Point-in-time Shadow feature snapshot construction."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional, Sequence


class SnapshotError(ValueError):
    """Feature snapshot is not deterministic or violates cutoff visibility."""


_DEFAULT_SHADOW_SOURCE_PRIORITY = (
    "efinance",
    "akshare",
    "baostock",
    "tushare",
    "tickflow",
    "yfinance",
)


def _shadow_source_policy() -> Any:
    """Build the bounded source route used by Shadow Research.

    Pytdx is intentionally opt-in here.  Its multi-host retry strategy can
    spend tens of seconds on an unreachable network, while Shadow Research
    only needs deterministic daily bars and already has Baostock/Tushare and
    other fallbacks.  Operators can add it explicitly through
    ``SHADOW_RESEARCH_SOURCE_PRIORITY`` when it is the preferred source.
    """
    from data_provider.market_data_types import SourcePolicy

    configured = os.getenv("SHADOW_RESEARCH_SOURCE_PRIORITY", "")
    source_chain = tuple(dict.fromkeys(
        item.strip().lower()
        for item in configured.split(",")
        if item.strip()
    )) or _DEFAULT_SHADOW_SOURCE_PRIORITY
    try:
        timeout = float(os.getenv("SHADOW_RESEARCH_SOURCE_TIMEOUT_SECONDS", "15"))
    except (TypeError, ValueError):
        timeout = 15.0
    timeout = min(max(timeout, 1.0), 60.0)
    return SourcePolicy(
        primary_sources=source_chain,
        timeout_seconds=timeout,
        allow_stale=False,
    )


def build_market_observations(
    *,
    code: str,
    start_date: date,
    end_date: date,
    market_data_manager: Any,
    source_policy: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build point-in-time features from the canonical Market Data route.

    Indicators are calculated incrementally from bars visible at each date;
    callers cannot provide a precomputed future feature frame. The returned
    source snapshot is content-addressed and can be persisted with a run.
    """
    if start_date > end_date:
        raise SnapshotError("start_date must not be after end_date")
    from data_provider.market_data_types import DataQuery, DataStatus, SourcePolicy

    policy = source_policy or _shadow_source_policy()
    # Forward both bounds to the capability route. Without ``start`` a
    # provider is free to use its own default lookback window, which can omit
    # the user's requested in-sample period and make a valid OOS run appear
    # degraded even though the provider has the data.
    envelope = market_data_manager.fetch(
        DataQuery("daily_data", code, start=start_date, as_of=end_date),
        policy,
    )
    if envelope.status is not DataStatus.OK:
        raise SnapshotError(f"market data unavailable: {envelope.status.value}")
    payload = envelope.data
    if hasattr(payload, "to_dict"):
        rows = payload.to_dict(orient="records")
    elif isinstance(payload, (list, tuple)):
        rows = list(payload)
    else:
        raise SnapshotError("daily market data must be tabular")

    normalized: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        current = _date(raw.get("date") or raw.get("trade_date") or raw.get("日期"))
        if current is None or current < start_date or current > end_date:
            continue
        item: dict[str, Any] = {"date": current.isoformat()}
        for key in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
            value = raw.get(key)
            if value is not None:
                try:
                    item[key] = float(value)
                except (TypeError, ValueError):
                    continue
        if "close" not in item:
            continue
        normalized.append(item)
    normalized.sort(key=lambda item: item["date"])
    if len(normalized) < 2:
        raise SnapshotError("market data has fewer than two visible bars")

    closes = [float(item["close"]) for item in normalized]
    observations: list[dict[str, Any]] = []
    for index, item in enumerate(normalized[:-1]):
        visible_closes = closes[: index + 1]
        features = dict(item)
        features["ma5"] = sum(visible_closes[-5:]) / min(5, len(visible_closes))
        features["ma10"] = sum(visible_closes[-10:]) / min(10, len(visible_closes))
        features["ma20"] = sum(visible_closes[-20:]) / min(20, len(visible_closes))
        changes = [visible_closes[pos] - visible_closes[pos - 1] for pos in range(1, len(visible_closes))]
        gains = [max(change, 0.0) for change in changes[-14:]]
        losses = [max(-change, 0.0) for change in changes[-14:]]
        avg_gain = sum(gains) / len(gains) if gains else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        features["rsi14"] = 100.0 if avg_loss == 0 and avg_gain > 0 else (50.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
        features["volume_ratio"] = 1.0
        if "volume" in item:
            historical_volume = [float(row["volume"]) for row in normalized[: index + 1] if "volume" in row]
            if historical_volume and sum(historical_volume[-5:]) > 0:
                features["volume_ratio"] = float(item["volume"]) / (sum(historical_volume[-5:]) / min(5, len(historical_volume)))
        next_close = closes[index + 1]
        observations.append(
            {
                "date": item["date"],
                "features": features,
                "features_as_of": item["date"],
                "next_return_pct": (next_close / closes[index] - 1.0) * 100.0 if closes[index] else None,
                "source_refs": [{"source": envelope.source, "as_of": item["date"], "status": envelope.status.value}],
            }
        )
    snapshot = build_feature_snapshot(
        code=code,
        cutoff=end_date,
        features={"observations": observations},
        source_refs=[ref for item in observations for ref in item["source_refs"]],
    )
    return observations, {"snapshot_hash": snapshot["snapshot_hash"], "source": envelope.source, "status": envelope.status.value}


def _date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise SnapshotError(f"unsupported snapshot value: {type(value).__name__}")


def build_feature_snapshot(
    *,
    code: str,
    cutoff: date | datetime,
    features: Mapping[str, Any],
    source_refs: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build an immutable feature snapshot and content hash.

    Source refs may include ``as_of``/``published_at``.  Any source newer than
    the cutoff is rejected to make future-function bugs visible at the seam.
    """

    cutoff_date = _date(cutoff)
    if not str(code).strip() or cutoff_date is None:
        raise SnapshotError("code and a valid cutoff are required")
    normalized_features = _jsonable(features)
    if not isinstance(normalized_features, Mapping):
        raise SnapshotError("features must be an object")
    normalized_refs = []
    for ref in source_refs or ():
        if not isinstance(ref, Mapping):
            raise SnapshotError("source_refs must contain objects")
        ref_copy = _jsonable(ref)
        ref_date = _date(ref_copy.get("as_of") or ref_copy.get("published_at") or ref_copy.get("date"))
        if ref_date and ref_date > cutoff_date:
            raise SnapshotError(f"source ref is newer than cutoff: {ref_date.isoformat()}")
        normalized_refs.append(ref_copy)
    payload = {
        "code": str(code).strip().upper(),
        "cutoff": cutoff_date.isoformat(),
        "features": normalized_features,
        "source_refs": sorted(normalized_refs, key=lambda ref: json.dumps(ref, ensure_ascii=False, sort_keys=True)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**payload, "snapshot_hash": hashlib.sha256(encoded).hexdigest()}


__all__ = ["SnapshotError", "build_feature_snapshot", "build_market_observations"]
