# -*- coding: utf-8 -*-
"""Build deterministic Paper risk inputs from one frozen Observation.

The model never supplies these values.  Unknown values stay unknown so the
mandate evaluator can fail closed when a configured rule needs them.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Mapping, Optional, Sequence


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _market_item(market: Mapping[str, Any], symbol: str) -> Mapping[str, Any]:
    wanted = symbol.strip().upper()
    for key, value in market.items():
        normalized = str(key).split(".")[-1].strip().upper()
        if normalized == wanted and isinstance(value, Mapping):
            return value
    return {}


def _position_value(position: Mapping[str, Any]) -> Optional[float]:
    for field in ("market_value", "value", "position_value"):
        value = _number(position.get(field))
        if value is not None:
            return value
    quantity = _number(position.get("quantity") or position.get("shares"))
    price = _number(position.get("price") or position.get("current_price"))
    return quantity * price if quantity is not None and price is not None else None


def _holding_days(position: Mapping[str, Any], decision_at: datetime) -> Optional[int]:
    raw = position.get("opened_at") or position.get("acquired_at") or position.get("first_trade_date")
    if raw is None:
        return None
    try:
        opened = raw if isinstance(raw, (date, datetime)) else datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    opened_date = opened.date() if isinstance(opened, datetime) else opened
    return max(0, (decision_at.date() - opened_date).days)


def build_risk_context(
    *,
    observation: Mapping[str, Any],
    proposal: Mapping[str, Any],
    decision_at: datetime,
    open_orders: Optional[int] = None,
    minutes_since_last_decision: Optional[float] = None,
) -> Dict[str, Any]:
    """Derive risk values without guessing absent prices, units or freshness."""

    payload = observation.get("payload") if isinstance(observation.get("payload"), Mapping) else observation
    account = payload.get("account") if isinstance(payload.get("account"), Mapping) else {}
    market = payload.get("market") if isinstance(payload.get("market"), Mapping) else {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
    symbol = str(proposal.get("symbol") or "").strip().upper()
    side = str(proposal.get("side") or "").strip().lower()
    item = _market_item(market, symbol)

    reference_price = next((value for value in (
        _number(item.get("close")), _number(item.get("price")), _number(item.get("open"))
    ) if value is not None), None)
    quantity = _number(proposal.get("quantity"))
    target_weight = _number(proposal.get("target_weight"))

    positions = account.get("positions") if isinstance(account.get("positions"), list) else []
    cash = _number(account.get("cash") or account.get("available_cash"))
    explicit_equity = next((value for value in (
        _number(account.get("total_equity")), _number(account.get("equity")), _number(account.get("net_asset"))
    ) if value is not None), None)
    position_values = [_position_value(item) for item in positions if isinstance(item, Mapping)]
    total_position_value = sum(value for value in position_values if value is not None)
    equity = explicit_equity
    if equity is None and cash is not None and all(value is not None for value in position_values):
        equity = cash + total_position_value

    current_position = next((
        item for item in positions
        if isinstance(item, Mapping) and str(item.get("symbol") or item.get("code") or "").split(".")[-1].upper() == symbol
    ), {})
    current_value = _position_value(current_position) if current_position else 0.0
    order_value = target_weight * equity if target_weight is not None and equity is not None else None
    if order_value is None and quantity is not None and reference_price is not None:
        order_value = quantity * reference_price
    signed_value = None
    if order_value is not None:
        signed_value = -order_value if side in {"sell", "reduce"} else order_value
    projected_value = max(0.0, (current_value or 0.0) + signed_value) if signed_value is not None else None
    projected_position_weight = projected_value / equity if projected_value is not None and equity and equity > 0 else None
    projected_total_exposure = max(0.0, total_position_value + (signed_value or 0.0)) / equity if signed_value is not None and equity and equity > 0 else None
    cash_after = cash - signed_value if cash is not None and signed_value is not None else None
    cash_ratio_after = cash_after / equity if cash_after is not None and equity and equity > 0 else None

    sector = str(item.get("sector") or current_position.get("sector") or "").strip()
    sector_value = sum(
        value for position, value in zip(positions, position_values)
        if value is not None and isinstance(position, Mapping) and str(position.get("sector") or "").strip() == sector
    ) if sector else None
    projected_sector_weight = (
        max(0.0, sector_value + (signed_value or 0.0)) / equity
        if sector_value is not None and signed_value is not None and equity and equity > 0 else None
    )

    stale = item.get("is_stale", item.get("stale"))
    stale_seconds = next((value for value in (
        _number(item.get("stale_seconds")), _number(item.get("delay_seconds")), _number(item.get("data_delay_seconds"))
    ) if value is not None), None)
    refs = proposal.get("evidence_refs") if isinstance(proposal.get("evidence_refs"), list) else []
    available_ids = {
        str(value)
        for entry in evidence if isinstance(entry, Mapping)
        for value in (entry.get("evidence_id"), entry.get("content_hash"), entry.get("artifact_ref"))
        if value
    }
    referenced_ids = {
        str(value)
        for entry in refs if isinstance(entry, Mapping)
        for value in (entry.get("evidence_id"), entry.get("content_hash"), entry.get("artifact_ref"))
        if value
    }

    return {
        "reference_price": reference_price,
        "order_value": order_value,
        "projected_position_weight": projected_position_weight,
        "projected_sector_weight": projected_sector_weight,
        "projected_total_exposure": projected_total_exposure,
        "cash_ratio_after": cash_ratio_after,
        "turnover_pct": _number(account.get("turnover_pct")),
        "daily_loss_pct": _number(account.get("daily_loss_pct")),
        "drawdown_pct": _number(account.get("drawdown_pct")),
        "open_orders": open_orders,
        "holding_days": _holding_days(current_position, decision_at) if current_position else None,
        "data_stale": stale if isinstance(stale, bool) else None,
        "stale_seconds": stale_seconds,
        "evidence_count": len(referenced_ids),
        "evidence_complete": referenced_ids.issubset(available_ids),
        "decision_time": decision_at,
        "minutes_since_last_decision": minutes_since_last_decision,
        "sector": sector or None,
    }


__all__ = ["build_risk_context"]
