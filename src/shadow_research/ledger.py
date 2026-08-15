# -*- coding: utf-8 -*-
"""Read-only Account Ledger freeze for Shadow Research.

Shadow Research never creates a second journal. This adapter reads the
existing PortfolioRepository event tables, filters them by the requested
interval, and returns a deterministic, content-addressed snapshot that can be
attached to a backtest's point-in-time evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Dict, Optional

from src.repositories.portfolio_repo import PortfolioRepository
from src.storage import DatabaseManager


class LedgerSnapshotError(ValueError):
    """Account ledger history cannot be frozen safely."""


def _as_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise LedgerSnapshotError(f"{field} must be a valid date") from exc


def _event(row: Any, *, kind: str, event_date: date) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "id": getattr(row, "id", None),
        "kind": kind,
        "date": event_date.isoformat(),
    }
    if kind == "trade":
        fields.update({
            "account_id": row.account_id,
            "trade_uid": row.trade_uid,
            "symbol": row.symbol,
            "market": row.market,
            "currency": row.currency,
            "side": row.side,
            "quantity": row.quantity,
            "price": row.price,
            "fee": row.fee,
            "tax": row.tax,
        })
    elif kind == "cash":
        fields.update({
            "account_id": row.account_id,
            "direction": row.direction,
            "amount": row.amount,
            "currency": row.currency,
        })
    else:
        fields.update({
            "account_id": row.account_id,
            "symbol": row.symbol,
            "market": row.market,
            "currency": row.currency,
            "action_type": row.action_type,
            "cash_dividend_per_share": row.cash_dividend_per_share,
            "split_ratio": row.split_ratio,
        })
    return fields


def freeze_account_ledger(
    *,
    account_id: int,
    start_date: date,
    end_date: date,
    db: Optional[DatabaseManager] = None,
) -> Dict[str, Any]:
    """Freeze one account's immutable event history for an inclusive interval."""

    start = _as_date(start_date, "start_date")
    end = _as_date(end_date, "end_date")
    if start > end:
        raise LedgerSnapshotError("start_date must not be after end_date")

    repository = PortfolioRepository(db or DatabaseManager.get_instance())
    account = repository.get_account(int(account_id), include_inactive=True)
    if account is None:
        raise LedgerSnapshotError(f"account not found: {account_id}")

    events = []
    for row in repository.list_trades(int(account_id), end):
        if row.trade_date >= start:
            events.append(_event(row, kind="trade", event_date=row.trade_date))
    for row in repository.list_cash_ledger(int(account_id), end):
        if row.event_date >= start:
            events.append(_event(row, kind="cash", event_date=row.event_date))
    for row in repository.list_corporate_actions(int(account_id), end):
        if row.effective_date >= start:
            events.append(_event(row, kind="corporate_action", event_date=row.effective_date))

    events.sort(key=lambda item: (item["date"], item["kind"], int(item["id"] or 0)))
    if not events:
        raise LedgerSnapshotError("account ledger has no events in the requested interval")

    payload = {
        "account_id": int(account_id),
        "account_market": account.market,
        "base_currency": account.base_currency,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "events": events,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return {**payload, "snapshot_hash": hashlib.sha256(encoded).hexdigest(), "event_count": len(events)}


__all__ = ["LedgerSnapshotError", "freeze_account_ledger"]
