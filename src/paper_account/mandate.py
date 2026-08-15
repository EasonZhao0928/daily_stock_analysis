# -*- coding: utf-8 -*-
"""Typed, deterministic Paper Account mandate evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Dict, Mapping, Optional, Tuple


class PaperMandateError(ValueError):
    """Mandate or structured proposal is invalid."""


APPROVAL_MODES = frozenset({"recommend_only", "human_confirm", "auto_paper"})
CONTROLLER_KINDS = frozenset({"shadow", "llm", "hybrid"})
MARKETS = frozenset({"cn", "hk", "us", "jp", "kr", "tw"})
SIDES = frozenset({"buy", "sell", "hold", "reduce"})
ORDER_TYPES = frozenset({"market", "limit", "stop"})
PROPOSAL_FIELDS = frozenset({
    "symbol",
    "market",
    "side",
    "order_type",
    "quantity",
    "target_weight",
    "limit_price",
    "stop_price",
    "rationale",
    "evidence_refs",
})
MANDATE_FIELDS = frozenset({
    "allowed_markets",
    "allowed_symbols",
    "excluded_symbols",
    "allowed_order_types",
    "max_position_weight",
    "max_sector_weight",
    "max_total_exposure",
    "max_order_value",
    "max_turnover_pct",
    "max_daily_loss_pct",
    "max_drawdown_pct",
    "max_open_orders",
    "max_holding_days",
    "min_holding_days",
    "min_cash_ratio",
    "decision_frequency_minutes",
    "decision_windows",
    "min_evidence_count",
    "require_fresh_data",
    "max_stale_seconds",
})

RULE = {
    "market": "MARKET_NOT_ALLOWED",
    "symbol": "SYMBOL_NOT_ALLOWED",
    "blacklist": "SYMBOL_BLACKLISTED",
    "order_type": "ORDER_TYPE_NOT_ALLOWED",
    "order_value": "ORDER_VALUE_LIMIT_EXCEEDED",
    "position": "POSITION_LIMIT_EXCEEDED",
    "sector": "SECTOR_LIMIT_EXCEEDED",
    "exposure": "TOTAL_EXPOSURE_LIMIT_EXCEEDED",
    "turnover": "TURNOVER_LIMIT_EXCEEDED",
    "daily_loss": "DAILY_LOSS_LIMIT_EXCEEDED",
    "drawdown": "DRAWDOWN_HALT",
    "open_orders": "OPEN_ORDER_LIMIT_EXCEEDED",
    "holding": "MINIMUM_HOLDING_PERIOD",
    "max_holding": "MAXIMUM_HOLDING_PERIOD",
    "cash": "CASH_FLOOR_BREACHED",
    "stale": "OBSERVATION_STALE",
    "evidence": "EVIDENCE_INSUFFICIENT",
    "window": "OUTSIDE_DECISION_WINDOW",
    "frequency": "DECISION_FREQUENCY_LIMIT",
    "context": "RISK_CONTEXT_INCOMPLETE",
}


def _finite_number(value: Any, field: str, *, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
    if isinstance(value, bool):
        raise PaperMandateError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PaperMandateError(f"{field} must be numeric") from exc
    if not math.isfinite(result):
        raise PaperMandateError(f"{field} must be finite")
    if minimum is not None and result < minimum:
        raise PaperMandateError(f"{field} must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise PaperMandateError(f"{field} must be <= {maximum}")
    return result


def _optional_number(value: Any, field: str, **kwargs: float) -> Optional[float]:
    return None if value is None else _finite_number(value, field, **kwargs)


def _strict_bool(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PaperMandateError(f"{field} must be boolean")
    return value


def _string_array(value: Any, field: str, *, lower: bool = False) -> Tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple, set)):
        raise PaperMandateError(f"{field} must be an array")
    normalized = {
        (str(item).strip().lower() if lower else str(item).strip().upper())
        for item in value
        if str(item).strip()
    }
    return tuple(sorted(normalized))


def _decision_windows(value: Any) -> Tuple[str, ...]:
    windows = _string_array(value, "decision_windows", lower=True)
    for window in windows:
        parts = window.split("-", 1)
        if len(parts) != 2:
            raise PaperMandateError("decision_windows entries must use HH:MM-HH:MM")
        try:
            time.fromisoformat(parts[0])
            time.fromisoformat(parts[1])
        except ValueError as exc:
            raise PaperMandateError("decision_windows entries must use HH:MM-HH:MM") from exc
    return windows


@dataclass(frozen=True)
class PaperMandate:
    """Versioned risk boundary for a Paper Account."""

    version: str = "1"
    allowed_markets: Tuple[str, ...] = ("cn",)
    allowed_symbols: Tuple[str, ...] = ()
    excluded_symbols: Tuple[str, ...] = ()
    allowed_order_types: Tuple[str, ...] = ("market", "limit", "stop")
    max_position_weight: Optional[float] = None
    max_sector_weight: Optional[float] = None
    max_total_exposure: Optional[float] = None
    max_order_value: Optional[float] = None
    max_turnover_pct: Optional[float] = None
    max_daily_loss_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    max_open_orders: Optional[int] = None
    max_holding_days: Optional[int] = None
    min_holding_days: Optional[int] = None
    min_cash_ratio: Optional[float] = None
    decision_frequency_minutes: Optional[int] = None
    decision_windows: Tuple[str, ...] = ()
    min_evidence_count: int = 0
    require_fresh_data: bool = True
    max_stale_seconds: Optional[int] = None

    @classmethod
    def from_mapping(cls, payload: Optional[Mapping[str, Any]] = None, *, version: str = "1") -> "PaperMandate":
        data = dict(payload or {})
        unknown = sorted(set(data) - MANDATE_FIELDS)
        if unknown:
            raise PaperMandateError(f"unknown mandate fields: {', '.join(unknown)}")

        markets = data.get("allowed_markets", ("cn",))
        if isinstance(markets, str) or not isinstance(markets, (list, tuple, set)):
            raise PaperMandateError("allowed_markets must be an array")
        normalized_markets = tuple(sorted({str(item).strip().lower() for item in markets if str(item).strip()}))
        if not normalized_markets or not set(normalized_markets).issubset(MARKETS):
            raise PaperMandateError("allowed_markets contains an unsupported market")

        normalized_symbols = _string_array(data.get("allowed_symbols", ()), "allowed_symbols")
        excluded_symbols = _string_array(data.get("excluded_symbols", ()), "excluded_symbols")
        allowed_order_types = _string_array(
            data.get("allowed_order_types", tuple(ORDER_TYPES)),
            "allowed_order_types",
            lower=True,
        )
        if not allowed_order_types or not set(allowed_order_types).issubset(ORDER_TYPES):
            raise PaperMandateError("allowed_order_types contains an unsupported order type")

        max_open_orders = data.get("max_open_orders")
        if max_open_orders is not None:
            max_open_orders = int(_finite_number(max_open_orders, "max_open_orders", minimum=0))
        max_holding_days = data.get("max_holding_days")
        if max_holding_days is not None:
            max_holding_days = int(_finite_number(max_holding_days, "max_holding_days", minimum=0))
        min_holding_days = data.get("min_holding_days")
        if min_holding_days is not None:
            min_holding_days = int(_finite_number(min_holding_days, "min_holding_days", minimum=0))
        decision_frequency_minutes = data.get("decision_frequency_minutes")
        if decision_frequency_minutes is not None:
            decision_frequency_minutes = int(_finite_number(
                decision_frequency_minutes, "decision_frequency_minutes", minimum=1
            ))
        min_evidence_count = int(_finite_number(
            data.get("min_evidence_count", 0), "min_evidence_count", minimum=0
        ))
        max_stale_seconds = data.get("max_stale_seconds")
        if max_stale_seconds is not None:
            max_stale_seconds = int(_finite_number(max_stale_seconds, "max_stale_seconds", minimum=0))

        return cls(
            version=str(version or "1"),
            allowed_markets=normalized_markets,
            allowed_symbols=normalized_symbols,
            excluded_symbols=excluded_symbols,
            allowed_order_types=allowed_order_types,
            max_position_weight=_optional_number(data.get("max_position_weight"), "max_position_weight", minimum=0, maximum=1),
            max_sector_weight=_optional_number(data.get("max_sector_weight"), "max_sector_weight", minimum=0, maximum=1),
            max_total_exposure=_optional_number(data.get("max_total_exposure"), "max_total_exposure", minimum=0, maximum=1),
            max_order_value=_optional_number(data.get("max_order_value"), "max_order_value", minimum=0),
            max_turnover_pct=_optional_number(data.get("max_turnover_pct"), "max_turnover_pct", minimum=0, maximum=100),
            max_daily_loss_pct=_optional_number(data.get("max_daily_loss_pct"), "max_daily_loss_pct", minimum=0, maximum=100),
            max_drawdown_pct=_optional_number(data.get("max_drawdown_pct"), "max_drawdown_pct", minimum=0, maximum=100),
            max_open_orders=max_open_orders,
            max_holding_days=max_holding_days,
            min_holding_days=min_holding_days,
            min_cash_ratio=_optional_number(data.get("min_cash_ratio"), "min_cash_ratio", minimum=0, maximum=1),
            decision_frequency_minutes=decision_frequency_minutes,
            decision_windows=_decision_windows(data.get("decision_windows", ())),
            min_evidence_count=min_evidence_count,
            require_fresh_data=_strict_bool(data.get("require_fresh_data"), "require_fresh_data", True),
            max_stale_seconds=max_stale_seconds,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed_markets": list(self.allowed_markets),
            "allowed_symbols": list(self.allowed_symbols),
            "excluded_symbols": list(self.excluded_symbols),
            "allowed_order_types": list(self.allowed_order_types),
            "max_position_weight": self.max_position_weight,
            "max_sector_weight": self.max_sector_weight,
            "max_total_exposure": self.max_total_exposure,
            "max_order_value": self.max_order_value,
            "max_turnover_pct": self.max_turnover_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_open_orders": self.max_open_orders,
            "max_holding_days": self.max_holding_days,
            "min_holding_days": self.min_holding_days,
            "min_cash_ratio": self.min_cash_ratio,
            "decision_frequency_minutes": self.decision_frequency_minutes,
            "decision_windows": list(self.decision_windows),
            "min_evidence_count": self.min_evidence_count,
            "require_fresh_data": self.require_fresh_data,
            "max_stale_seconds": self.max_stale_seconds,
        }


@dataclass(frozen=True)
class MandateDecision:
    accepted: bool
    rule_codes: Tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)


def validate_proposal(proposal: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the structured proposal boundary without guessing fields."""

    if not isinstance(proposal, Mapping):
        raise PaperMandateError("proposal must be an object")
    unknown = sorted(set(proposal) - PROPOSAL_FIELDS)
    if unknown:
        raise PaperMandateError(f"unknown proposal fields: {', '.join(unknown)}")
    required = ("symbol", "market", "side", "order_type", "rationale")
    missing = [field for field in required if not str(proposal.get(field) or "").strip()]
    if missing:
        raise PaperMandateError(f"proposal missing required fields: {', '.join(missing)}")
    symbol = str(proposal["symbol"]).strip().upper()
    market = str(proposal["market"]).strip().lower()
    side = str(proposal["side"]).strip().lower()
    order_type = str(proposal["order_type"]).strip().lower()
    if market not in MARKETS:
        raise PaperMandateError(f"unsupported market: {market}")
    if side not in SIDES:
        raise PaperMandateError(f"unsupported side: {side}")
    if order_type not in ORDER_TYPES:
        raise PaperMandateError(f"unsupported order_type: {order_type}")
    quantity = _optional_number(proposal.get("quantity"), "quantity", minimum=0)
    target_weight = _optional_number(proposal.get("target_weight"), "target_weight", minimum=0, maximum=1)
    if (quantity is None) == (target_weight is None):
        raise PaperMandateError("proposal must contain exactly one of quantity or target_weight")
    limit_price = _optional_number(proposal.get("limit_price"), "limit_price", minimum=0)
    stop_price = _optional_number(proposal.get("stop_price"), "stop_price", minimum=0)
    if order_type == "limit" and limit_price is None:
        raise PaperMandateError("limit order requires limit_price")
    if order_type == "stop" and stop_price is None:
        raise PaperMandateError("stop order requires stop_price")
    evidence_refs = proposal.get("evidence_refs", [])
    if not isinstance(evidence_refs, list) or any(not isinstance(item, Mapping) for item in evidence_refs):
        raise PaperMandateError("evidence_refs must be an array of objects")
    rationale = str(proposal["rationale"]).strip()
    if len(rationale) > 4000:
        raise PaperMandateError("rationale is too long")
    return {
        "symbol": symbol,
        "market": market,
        "side": side,
        "order_type": order_type,
        "quantity": quantity,
        "target_weight": target_weight,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "rationale": rationale,
        "evidence_refs": [dict(item) for item in evidence_refs],
    }


def evaluate_proposal(
    mandate: PaperMandate,
    proposal: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
) -> MandateDecision:
    """Evaluate a normalized proposal against a frozen risk context."""

    normalized = validate_proposal(proposal)
    current = dict(context or {})
    failures = []
    missing_context = []
    details: Dict[str, Any] = {"mandate_version": mandate.version}

    def reject(code: str) -> None:
        if code not in failures:
            failures.append(code)

    def required(field: str, enabled: bool) -> Any:
        value = current.get(field)
        if enabled and value is None:
            missing_context.append(field)
            reject(RULE["context"])
        return value

    if normalized["market"] not in mandate.allowed_markets:
        reject(RULE["market"])
    if mandate.allowed_symbols and normalized["symbol"] not in mandate.allowed_symbols:
        reject(RULE["symbol"])
    if normalized["symbol"] in mandate.excluded_symbols:
        reject(RULE["blacklist"])
    if normalized["order_type"] not in mandate.allowed_order_types:
        reject(RULE["order_type"])

    order_value = current.get("order_value")
    if order_value is None and normalized["quantity"] is not None and current.get("reference_price") is not None:
        order_value = float(normalized["quantity"]) * float(current["reference_price"])
    if mandate.max_order_value is not None:
        required("order_value", True) if order_value is None else None
        if order_value is not None and float(order_value) > mandate.max_order_value:
            reject(RULE["order_value"])

    numeric_rules = (
        ("projected_position_weight", mandate.max_position_weight, RULE["position"], lambda value, limit: value > limit),
        ("projected_sector_weight", mandate.max_sector_weight, RULE["sector"], lambda value, limit: value > limit),
        ("projected_total_exposure", mandate.max_total_exposure, RULE["exposure"], lambda value, limit: value > limit),
        ("turnover_pct", mandate.max_turnover_pct, RULE["turnover"], lambda value, limit: value > limit),
        ("daily_loss_pct", mandate.max_daily_loss_pct, RULE["daily_loss"], lambda value, limit: value > limit),
        ("drawdown_pct", mandate.max_drawdown_pct, RULE["drawdown"], lambda value, limit: value > limit),
        ("open_orders", mandate.max_open_orders, RULE["open_orders"], lambda value, limit: value >= limit),
        ("holding_days", mandate.min_holding_days if normalized["side"] in {"sell", "reduce"} else None, RULE["holding"], lambda value, limit: value < limit),
        ("holding_days", mandate.max_holding_days, RULE["max_holding"], lambda value, limit: value > limit),
        ("cash_ratio_after", mandate.min_cash_ratio, RULE["cash"], lambda value, limit: value < limit),
        ("minutes_since_last_decision", mandate.decision_frequency_minutes, RULE["frequency"], lambda value, limit: value < limit),
    )
    for field, limit, code, predicate in numeric_rules:
        value = required(field, limit is not None)
        if limit is not None and value is not None and predicate(float(value), float(limit)):
            reject(code)

    data_stale = required("data_stale", mandate.require_fresh_data)
    stale_seconds = required("stale_seconds", mandate.max_stale_seconds is not None)
    if mandate.require_fresh_data and data_stale is True:
        reject(RULE["stale"])
    if mandate.max_stale_seconds is not None and stale_seconds is not None:
        if float(stale_seconds) > mandate.max_stale_seconds:
            reject(RULE["stale"])

    evidence_count = required("evidence_count", mandate.min_evidence_count > 0)
    if current.get("evidence_complete") is False:
        reject(RULE["evidence"])
    if mandate.min_evidence_count and evidence_count is not None:
        if int(evidence_count) < mandate.min_evidence_count:
            reject(RULE["evidence"])

    if mandate.decision_windows:
        decision_time = required("decision_time", True)
        if decision_time is not None:
            if not isinstance(decision_time, datetime):
                try:
                    decision_time = datetime.fromisoformat(str(decision_time))
                except ValueError:
                    missing_context.append("decision_time")
                    reject(RULE["context"])
                    decision_time = None
            if decision_time is not None:
                now = decision_time.time()
                inside = False
                for window in mandate.decision_windows:
                    start_text, end_text = window.split("-", 1)
                    start, end = time.fromisoformat(start_text), time.fromisoformat(end_text)
                    inside = inside or (start <= now <= end if start <= end else now >= start or now <= end)
                if not inside:
                    reject(RULE["window"])

    details.update({
        "order_value": order_value,
        "missing_context": sorted(set(missing_context)),
        "failures": list(failures),
    })
    return MandateDecision(accepted=not failures, rule_codes=tuple(failures), details=details)


__all__ = [
    "APPROVAL_MODES",
    "CONTROLLER_KINDS",
    "MandateDecision",
    "PaperMandate",
    "PaperMandateError",
    "PROPOSAL_FIELDS",
    "evaluate_proposal",
    "validate_proposal",
]
