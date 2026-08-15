# -*- coding: utf-8 -*-
"""Virtual Order lifecycle and deterministic bar matching.

This module stops at the Paper Account boundary.  A fill is first persisted as
an immutable ``PaperFill`` and a durable Account Ledger outbox fact; it is
never sent to a broker or an external execution API.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from .service import PaperStateError


ORDER_STATES = frozenset({
    "staged",
    "approved",
    "open",
    "partially_filled",
    "filled",
    "cancelled",
    "expired",
    "rejected",
})
ORDER_TRANSITIONS = {
    "staged": frozenset({"approved", "cancelled", "rejected"}),
    "approved": frozenset({"open", "cancelled", "expired", "rejected"}),
    "open": frozenset({"partially_filled", "filled", "cancelled", "expired"}),
    "partially_filled": frozenset({"partially_filled", "filled", "cancelled", "expired"}),
    "filled": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
    "rejected": frozenset(),
}
EXECUTION_POLICIES = frozenset({"next_open", "limit", "stop"})
_EPS = 1e-9


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dt(value: Any, field: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), time.min)
        except ValueError:
            raise ValueError(f"{field} must be an ISO date/time") from exc
    # The project stores naive local exchange timestamps.  Keep a timezone
    # aware input deterministic by removing its offset after conversion.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _bar_timestamp(bar: Mapping[str, Any]) -> datetime:
    return _dt(bar.get("timestamp") or bar.get("datetime") or bar.get("date"))


def _number(value: Any, field: str, *, default: Optional[float] = None, minimum: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{field} is invalid")
    return result


def _is_halted(bar: Mapping[str, Any]) -> bool:
    return any(bool(bar.get(key)) for key in ("halted", "suspended", "is_halted", "is_suspended"))


def _is_price_limited(bar: Mapping[str, Any], side: str) -> bool:
    if bool(bar.get("price_limited") or bar.get("limit_locked")):
        return True
    if side == "buy":
        return bool(bar.get("limit_up") or bar.get("at_limit_up"))
    return bool(bar.get("limit_down") or bar.get("at_limit_down"))


class PaperOrderService:
    """Order/matching facade backed by a PaperAccountService instance."""

    def __init__(self, paper_service: Any) -> None:
        self.paper = paper_service
        self.repository = paper_service.repository

    def stage_order(
        self,
        account_id: int,
        proposal_id: str,
        *,
        execution_policy: Optional[str] = None,
        observation_id: Optional[str] = None,
        resolved_quantity: Optional[float] = None,
        expires_at: Optional[date | datetime] = None,
    ) -> Dict[str, Any]:
        state = self.paper.inspect(account_id)
        proposal = self.repository.get_proposal(proposal_id)
        if state is None or proposal is None or int(proposal["account_id"]) != int(account_id):
            raise PaperStateError("paper proposal not found")
        if state["config"]["state"] != "active":
            raise PaperStateError("paper account must be active to stage an order")
        if proposal["status"] != "approved":
            raise PaperStateError("only approved proposals can be staged")
        if proposal["side"] not in {"buy", "sell"}:
            raise PaperStateError("hold/reduce proposals cannot be staged without an order side")

        quantity = proposal.get("quantity") if proposal.get("quantity") is not None else resolved_quantity
        quantity = _number(quantity, "quantity", minimum=_EPS)
        if quantity is None:
            raise PaperStateError("target_weight proposals require resolved_quantity")
        policy = str(execution_policy or proposal["order_type"] or "next_open").strip().lower()
        if policy == "market":
            policy = "next_open"
        if policy not in EXECUTION_POLICIES:
            raise PaperStateError(f"unsupported execution_policy: {policy}")
        observation = None
        if observation_id:
            observation = self.repository.get_observation(observation_id)
        if observation is None:
            run = self.repository.get_run(proposal["run_id"])
            observation = self.repository.get_observation_for_run(proposal["run_id"]) if run else None
        if observation is None:
            raise PaperStateError("staging requires a frozen observation")
        cutoff = _dt(observation["cutoff_at"], "observation_cutoff")
        expires = _dt(expires_at, "expires_at") if expires_at is not None else None
        if expires is not None and expires <= cutoff:
            raise PaperStateError("expires_at must be after observation cutoff")
        order_hash = _hash({
            "proposal_id": proposal_id,
            "quantity": quantity,
            "policy": policy,
            "observation_id": observation["observation_id"],
            "expires_at": expires.isoformat() if expires else None,
        })
        order_id = "paper_order_" + order_hash[:28]
        return self.repository.create_order({
            "order_id": order_id,
            "proposal_id": proposal_id,
            "account_id": int(account_id),
            "symbol": proposal["symbol"],
            "market": proposal["market"],
            "side": proposal["side"],
            "order_type": proposal["order_type"],
            "quantity": float(quantity),
            "limit_price": proposal.get("limit_price"),
            "stop_price": proposal.get("stop_price"),
            "status": "staged",
            "version": 1,
            "observation_id": observation["observation_id"],
            "observation_cutoff": cutoff,
            "execution_policy": policy,
            "expires_at": expires,
            "filled_quantity": 0.0,
            "avg_fill_price": None,
        })

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        return self.repository.get_order(order_id)

    def list_orders(self, account_id: int, *, limit: int = 100) -> Sequence[Dict[str, Any]]:
        return self.repository.list_orders(account_id, limit=limit)

    def transition_order(self, order_id: str, target: str, *, expected_version: Optional[int] = None) -> Dict[str, Any]:
        order = self.repository.get_order(order_id)
        if order is None:
            raise PaperStateError("paper order not found")
        target = str(target or "").strip().lower()
        if target not in ORDER_STATES:
            raise PaperStateError(f"unsupported order state: {target}")
        current = str(order["status"])
        if target == current:
            return order
        if target not in ORDER_TRANSITIONS.get(current, frozenset()):
            raise PaperStateError(f"invalid order transition: {current} -> {target}")
        account = self.paper.inspect(int(order["account_id"]))
        if account is None:
            raise PaperStateError("paper account not found")
        account_state = account["config"]["state"]
        if account_state in {"frozen", "closed"} and target not in {"cancelled", "expired", "rejected"}:
            raise PaperStateError("frozen/closed paper account cannot advance an order")
        version = int(order["version"] if expected_version is None else expected_version)
        try:
            return self.repository.update_order_state(order_id, status=target, expected_version=version)
        except ValueError as exc:
            raise PaperStateError(str(exc)) from exc

    def cancel_orders_on_freeze(self, account_id: int) -> Sequence[Dict[str, Any]]:
        cancelled = []
        for order in self.repository.list_orders(account_id):
            if order["status"] in {"staged", "approved", "open", "partially_filled"}:
                cancelled.append(self.transition_order(order["order_id"], "cancelled", expected_version=order["version"]))
        return cancelled

    def match_order(
        self,
        order_id: str,
        bars: Iterable[Mapping[str, Any]],
        *,
        fee_bps: float = 5.0,
        tax_bps: float = 0.0,
        slippage_bps: float = 5.0,
        participation_rate: float = 1.0,
        project_ledger: bool = False,
    ) -> Dict[str, Any]:
        order = self.repository.get_order(order_id)
        if order is None:
            raise PaperStateError("paper order not found")
        if order["status"] in {"filled", "cancelled", "expired", "rejected"}:
            return {"order": order, "fills": [], "remaining_quantity": max(0.0, float(order["quantity"]) - float(order.get("filled_quantity") or 0.0))}
        if order["status"] not in {"approved", "open", "partially_filled"}:
            raise PaperStateError("order is not matchable")
        fee_bps = float(fee_bps)
        tax_bps = float(tax_bps)
        slippage_bps = float(slippage_bps)
        participation_rate = float(participation_rate)
        if min(fee_bps, tax_bps, slippage_bps) < 0 or not 0 < participation_rate <= 1:
            raise ValueError("invalid matching cost or participation parameters")
        cutoff = _dt(order["observation_cutoff"], "observation_cutoff")
        normalized_bars = []
        for raw in bars:
            if not isinstance(raw, Mapping):
                raise ValueError("bars must contain objects")
            timestamp = _bar_timestamp(raw)
            if timestamp <= cutoff:
                continue
            normalized_bars.append((timestamp, dict(raw)))
        normalized_bars.sort(key=lambda item: item[0])
        if order["status"] == "approved":
            order = self.transition_order(order_id, "open", expected_version=order["version"])

        fills = []
        remaining = max(0.0, float(order["quantity"]) - float(order.get("filled_quantity") or 0.0))
        running_value = float(order.get("avg_fill_price") or 0.0) * float(order.get("filled_quantity") or 0.0)
        for timestamp, bar in normalized_bars:
            order = self.repository.get_order(order_id) or order
            if order["status"] in {"cancelled", "expired", "filled", "rejected"}:
                break
            if order.get("expires_at") and timestamp >= _dt(order["expires_at"], "expires_at"):
                order = self.transition_order(order_id, "expired", expected_version=order["version"])
                break
            if _is_halted(bar) or _is_price_limited(bar, order["side"]):
                continue
            base_price = self._eligible_price(order, bar)
            if base_price is None:
                continue
            available = _number(bar.get("available_quantity"), "available_quantity", minimum=0.0)
            if available is None:
                volume = _number(bar.get("volume"), "volume", minimum=0.0)
                available = volume * participation_rate if volume is not None else remaining
            qty = min(remaining, float(available))
            if qty <= _EPS:
                continue
            slippage = float(slippage_bps) / 10000.0
            price = base_price * (1.0 + slippage if order["side"] == "buy" else 1.0 - slippage)
            notional = qty * price
            fee = notional * fee_bps / 10000.0
            tax = notional * tax_bps / 10000.0 if order["side"] == "sell" else 0.0
            fill_hash = _hash({
                "order_id": order_id,
                "bar_timestamp": timestamp.isoformat(),
                "quantity": round(qty, 10),
                "price": round(price, 10),
                "fee": round(fee, 10),
                "tax": round(tax, 10),
            })
            fill_id = "paper_fill_" + fill_hash[:28]
            existing = self.repository.get_fill(fill_id)
            if existing is not None:
                continue
            outbox = self.paper.portfolio.enqueue_virtual_fill(
                account_id=int(order["account_id"]),
                fill_id=fill_id,
                symbol=order["symbol"],
                fill_date=timestamp.date(),
                side=order["side"],
                quantity=qty,
                price=price,
                fee=fee,
                tax=tax,
                market=order["market"],
                currency="CNY" if order["market"] == "cn" else None,
                note=f"paper_order:{order_id}",
            )
            fill = self.repository.save_fill({
                "fill_id": fill_id,
                "order_id": order_id,
                "account_id": int(order["account_id"]),
                "fill_date": timestamp.date(),
                "quantity": qty,
                "price": price,
                "fee": fee,
                "tax": tax,
                "fill_hash": fill_hash,
                "status": "pending",
                "ledger_outbox_id": outbox["id"],
                "bar_timestamp": timestamp,
                "execution_policy": order["execution_policy"],
            })
            fills.append(fill)
            remaining -= qty
            running_value += qty * price
            filled = float(order.get("filled_quantity") or 0.0) + qty
            final_status = "filled" if remaining <= _EPS else "partially_filled"
            order = self.repository.update_order_state(
                order_id,
                status=final_status,
                expected_version=int(order["version"]),
                filled_quantity=filled,
                avg_fill_price=running_value / filled,
            )
            if project_ledger:
                self.apply_fill(fill_id)
            if final_status == "filled":
                break

        return {
            "order": self.repository.get_order(order_id),
            "fills": fills,
            "remaining_quantity": max(0.0, remaining),
        }

    @staticmethod
    def _eligible_price(order: Mapping[str, Any], bar: Mapping[str, Any]) -> Optional[float]:
        policy = str(order["execution_policy"])
        open_price = _number(bar.get("open"), "open", minimum=_EPS)
        high = _number(bar.get("high"), "high", minimum=_EPS)
        low = _number(bar.get("low"), "low", minimum=_EPS)
        if policy == "next_open":
            return open_price
        if policy == "limit":
            limit = _number(order.get("limit_price"), "limit_price", minimum=_EPS)
            if limit is None or high is None or low is None:
                return None
            if order["side"] == "buy" and low <= limit:
                return limit
            if order["side"] == "sell" and high >= limit:
                return limit
            return None
        stop = _number(order.get("stop_price"), "stop_price", minimum=_EPS)
        if stop is None or high is None or low is None:
            return None
        if order["side"] == "buy" and high >= stop:
            return max(open_price or stop, stop)
        if order["side"] == "sell" and low <= stop:
            return min(open_price or stop, stop)
        return None

    def apply_fill(self, fill_id: str) -> Dict[str, Any]:
        fill = self.repository.get_fill(fill_id)
        if fill is None:
            raise PaperStateError("paper fill not found")
        if fill["status"] == "applied":
            return fill
        receipt = self.paper.portfolio.apply_virtual_fill(outbox_id=fill["ledger_outbox_id"])
        if receipt.accepted:
            return self.repository.update_fill_status(fill_id, status="applied")
        return self.repository.update_fill_status(fill_id, status="pending")


__all__ = ["EXECUTION_POLICIES", "ORDER_STATES", "ORDER_TRANSITIONS", "PaperOrderService"]
