# -*- coding: utf-8 -*-
"""Market Candle/Snapshot service.

Endpoints consume this service instead of reaching provider SDKs directly.
The service returns a stable OHLCV contract with source/as-of/stale metadata.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from data_provider.security_id import SecurityId, SecurityIdError
from data_provider.market_clock import MarketClock
from data_provider.market_data_types import DataQuery, DataStatus
from data_provider.runtime import get_market_data_manager


class MarketChartError(ValueError):
    """Invalid candle query or unavailable normalized market data."""


# Daily bars consumed per output bar, before allowing for non-trading days.
_PERIOD_DAILY_ROWS = {"daily": 1, "weekly": 5, "monthly": 22}
# ~250 trading days per 365 calendar days; ask for calendar coverage.
_TRADING_DAY_RATIO = 1.5


def _daily_rows_needed(period: str, limit: int) -> int:
    """Daily rows required to satisfy ``limit`` bars of ``period``."""
    per_bar = _PERIOD_DAILY_ROWS.get(period, 1)
    return max(1, int(limit * per_bar * _TRADING_DAY_RATIO))


class MarketChartService:
    def __init__(self, market_data_manager: Any = None, *, now_fn=None) -> None:
        self.market_data = market_data_manager or get_market_data_manager()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def get_candles(
        self,
        security_id: str,
        *,
        period: str = "daily",
        start: Optional[date] = None,
        end: Optional[date] = None,
        limit: int = 240,
    ) -> Dict[str, Any]:
        try:
            identity = SecurityId.from_input(security_id, strict=False)
        except SecurityIdError as exc:
            raise MarketChartError(str(exc)) from exc
        period = str(period or "daily").strip().lower()
        if period not in {"daily", "weekly", "monthly"}:
            raise MarketChartError("period must be daily, weekly, or monthly")
        if start and end and start > end:
            raise MarketChartError("start must be <= end")
        limit = max(1, min(int(limit), 2000))
        if hasattr(self.market_data, "fetch"):
            envelope = self.market_data.fetch(DataQuery(
                capability="daily_data",
                security_id=identity,
                as_of=end,
                start=start,
                # Weekly/monthly aggregation consumes several daily bars per
                # output bar, and calendar days exceed trading days, so ask for
                # more than ``limit`` rather than starving the requested window.
                limit=None if start else _daily_rows_needed(period, limit),
            ))
            raw = self._records(envelope.data)
            source = envelope.source
            route_stale = bool(envelope.is_stale)
            status = envelope.status
            quality_flags = list(envelope.quality_flags)
            fallback_chain = list(envelope.fallback_chain)
        elif hasattr(self.market_data, "get_history_data"):
            # Compatibility seam for old test doubles; production uses the
            # capability-routed manager above.
            result = self.market_data.get_history_data(identity.code, period="daily", days=limit)
            raw = list(result.get("data") or []) if isinstance(result, dict) else []
            source = result.get("source") or "legacy_fixture"
            route_stale = False
            status = DataStatus.OK
            quality_flags = []
            fallback_chain = []
        else:
            raise MarketChartError("market data runtime does not support daily_data")
        candles = self._normalize_daily(raw)
        if start:
            candles = [item for item in candles if item["timestamp"].date() >= start]
        if end:
            candles = [item for item in candles if item["timestamp"].date() <= end]
        if period != "daily":
            candles = self._aggregate(candles, period)
        candles = candles[-limit:]
        self._attach_indicators(candles)
        as_of = candles[-1]["timestamp"] if candles else None
        now = self.now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        delay_seconds = None
        stale = route_stale
        if as_of:
            clock = MarketClock(identity.market)
            as_of_aware = as_of.replace(tzinfo=clock.timezone)
            delay_seconds = max(0.0, (now.astimezone(clock.timezone) - as_of_aware).total_seconds())
            stale = stale or clock.is_stale(as_of_aware, now=now)
        limitations = [] if candles else ["market_data_unavailable"]
        limitations.extend(quality_flags)
        if stale:
            limitations.append("candle_stale")
        serialized_candles = [self._serialize_candle(item) for item in candles]
        return {
            "security_id": {
                "market": identity.market,
                "code": identity.code,
                "exchange": identity.exchange,
                "asset_type": identity.asset_type,
                "reliable": identity.is_reliable,
            },
            "period": period,
            "candles": serialized_candles,
            "source": source,
            "source_status": status.value if isinstance(status, DataStatus) else str(status),
            "fallback_chain": fallback_chain,
            "as_of": as_of.isoformat() if as_of else None,
            "delay_seconds": round(delay_seconds, 3) if delay_seconds is not None else None,
            "stale": stale,
            "data_quality": "stale" if stale else ("ok" if candles else "unavailable"),
            "limitations": sorted(set(limitations)),
        }

    def get_snapshot(self, security_id: str) -> Dict[str, Any]:
        result = self.get_candles(security_id, period="daily", limit=60)
        latest = result["candles"][-1] if result["candles"] else None
        return {
            "security_id": result["security_id"],
            "quote": latest,
            "source": result["source"],
            "source_status": result["source_status"],
            "as_of": result["as_of"],
            "delay_seconds": result["delay_seconds"],
            "stale": result["stale"],
            "data_quality": result["data_quality"],
            "limitations": result["limitations"],
        }

    @staticmethod
    def _records(value: Any) -> List[Dict[str, Any]]:
        if value is None:
            return []
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                return list(to_dict(orient="records"))
            except TypeError:
                converted = to_dict()
                if isinstance(converted, list):
                    return converted
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            rows = value.get("data", value.get("items", []))
            return [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
        return []

    @staticmethod
    def _ema(values: List[float], span: int) -> List[float]:
        alpha = 2.0 / (span + 1.0)
        output: List[float] = []
        for value in values:
            output.append(value if not output else alpha * value + (1.0 - alpha) * output[-1])
        return output

    @classmethod
    def _attach_indicators(cls, candles: List[Dict[str, Any]]) -> None:
        closes = [float(item["close"]) for item in candles]
        ema12, ema26 = cls._ema(closes, 12), cls._ema(closes, 26)
        macd = [fast - slow for fast, slow in zip(ema12, ema26)]
        signal = cls._ema(macd, 9)
        for index, candle in enumerate(candles):
            indicators: Dict[str, Optional[float]] = {}
            for window in (5, 10, 20):
                indicators[f"ma{window}"] = (
                    round(sum(closes[index + 1 - window:index + 1]) / window, 6)
                    if index + 1 >= window else None
                )
            if index >= 14:
                changes = [closes[pos] - closes[pos - 1] for pos in range(index - 13, index + 1)]
                gain = sum(max(value, 0.0) for value in changes) / 14.0
                loss = sum(max(-value, 0.0) for value in changes) / 14.0
                indicators["rsi14"] = round(100.0 if loss == 0 and gain > 0 else (50.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)), 6)
            else:
                indicators["rsi14"] = None
            indicators.update({
                "macd": round(macd[index], 6),
                "macd_signal": round(signal[index], 6),
                "macd_histogram": round((macd[index] - signal[index]) * 2.0, 6),
            })
            candle["indicators"] = indicators

    @staticmethod
    def _normalize_daily(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        output = []
        for row in rows:
            try:
                timestamp = MarketChartService._as_datetime(row.get("date") or row.get("timestamp"))
                values = {key: float(row[key]) for key in ("open", "high", "low", "close")}
                if min(values.values()) <= 0 or values["high"] < max(values["open"], values["close"]) or values["low"] > min(values["open"], values["close"]):
                    continue
                volume = row.get("volume")
                amount = row.get("amount")
                output.append({
                    "timestamp": timestamp,
                    **values,
                    "volume": float(volume) if volume is not None else None,
                    "amount": float(amount) if amount is not None else None,
                    "change_percent": row.get("change_percent"),
                })
            except (TypeError, ValueError, KeyError):
                continue
        output.sort(key=lambda item: item["timestamp"])
        return output

    @staticmethod
    def _serialize_candle(item: Dict[str, Any]) -> Dict[str, Any]:
        """Return the public candle contract without pandas/date objects.

        Provider adapters commonly return ``pandas.Timestamp`` values.  Keep
        datetime values internally for filtering/aggregation, but serialize
        them before the response reaches FastAPI's strict ``timestamp: str``
        schema.
        """
        serialized = dict(item)
        timestamp = serialized.get("timestamp")
        if isinstance(timestamp, (datetime, date)):
            serialized["timestamp"] = timestamp.isoformat()
        elif timestamp is not None and not isinstance(timestamp, str):
            serialized["timestamp"] = str(timestamp)
        return serialized

    @staticmethod
    def _aggregate(rows: List[Dict[str, Any]], period: str) -> List[Dict[str, Any]]:
        groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        for row in rows:
            ts = row["timestamp"]
            key = ts.strftime("%G-W%V") if period == "weekly" else ts.strftime("%Y-%m")
            groups.setdefault(key, []).append(row)
        output = []
        for values in groups.values():
            output.append({
                "timestamp": values[0]["timestamp"],
                "open": values[0]["open"],
                "high": max(item["high"] for item in values),
                "low": min(item["low"] for item in values),
                "close": values[-1]["close"],
                "volume": sum(item["volume"] or 0.0 for item in values),
                "amount": sum(item["amount"] or 0.0 for item in values),
                "change_percent": None,
            })
        return output

    @staticmethod
    def _as_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        text = str(value or "").strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


__all__ = ["MarketChartError", "MarketChartService"]
