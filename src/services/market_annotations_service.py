# -*- coding: utf-8 -*-
"""Unified chart annotations from Ledger, Shadow, Paper and Alert sources."""

from __future__ import annotations

from datetime import date, datetime, time
import json
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from data_provider.base import normalize_stock_code
from src.repositories.alert_repo import AlertRepository
from src.repositories.portfolio_repo import PortfolioRepository
from src.shadow_research.repository import ShadowResearchRepository
from src.paper_account.repository import PaperAccountRepository
from src.storage import DatabaseManager


class MarketAnnotationsService:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.portfolio = PortfolioRepository(self.db)
        self.paper = PaperAccountRepository(self.db)
        self.shadow = ShadowResearchRepository(self.db)
        self.alerts = AlertRepository(self.db)

    def collect(
        self,
        symbol: str,
        *,
        account_id: Optional[int] = None,
        profile_id: Optional[str] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        target = normalize_stock_code(str(symbol or "").strip())
        if not target:
            raise ValueError("symbol is required")
        if start and end and start > end:
            raise ValueError("start must be <= end")
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 500))
        items: List[Dict[str, Any]] = []
        limitations: List[str] = []

        if account_id is not None:
            account = self.portfolio.get_account(int(account_id), include_inactive=True)
            if account is None:
                raise ValueError("account_id is not accessible")
            try:
                items.extend(self._account_items(target, int(account_id), start, end))
            except Exception:
                limitations.append("account_annotations_unavailable")

        try:
            items.extend(self._shadow_items(target, profile_id, account_id, start, end))
        except Exception:
            limitations.append("shadow_annotations_unavailable")
        try:
            items.extend(self._alert_items(target, account_id, start, end))
        except Exception:
            limitations.append("alert_annotations_unavailable")

        unique: Dict[str, Dict[str, Any]] = {}
        for item in items:
            key = json.dumps({
                "type": item["type"], "source": item["source"], "timestamp": item.get("timestamp"),
                "account_id": item.get("account_id"), "symbol": item["symbol"], "payload": item["payload"],
            }, ensure_ascii=False, sort_keys=True, default=str)
            unique[key] = item
        items = sorted(unique.values(), key=lambda item: (str(item.get("timestamp") or ""), item["type"], str(item["payload"])))
        total = len(items)
        offset = (page - 1) * page_size
        page_items = items[offset:offset + page_size]
        return {
            "symbol": target,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "items": page_items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": offset + len(page_items) < total,
            "partial": bool(limitations),
            "limitations": sorted(set(limitations)),
        }

    def _account_items(self, target: str, account_id: int, start: Optional[date], end: Optional[date]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for row in self.portfolio.list_trades(account_id, as_of=end or date.max):
                if self._symbol(row.symbol) != target or not self._in_window(row.trade_date, start, end):
                    continue
                items.append({
                    "type": "trade",
                    "source": "account_ledger",
                    "timestamp": self._iso(row.trade_date),
                    "account_id": account_id,
                    "symbol": target,
                    "payload": {"side": row.side, "quantity": row.quantity, "price": row.price, "fee": row.fee, "tax": row.tax},
                })
        for order in self.paper.list_orders(account_id, limit=500):
                if self._symbol(order["symbol"]) != target:
                    continue
                if self._in_window(order.get("created_at"), start, end):
                    items.append({
                        "type": "virtual_order",
                        "source": "paper_account",
                        "timestamp": self._iso(order.get("created_at")),
                        "account_id": account_id,
                        "symbol": target,
                        "payload": {"order_id": order["order_id"], "status": order["status"], "side": order["side"], "quantity": order["quantity"]},
                    })
        for fill in self.paper.list_fills(account_id, limit=500):
                order = self.paper.get_order(fill["order_id"])
                if not order or self._symbol(order["symbol"]) != target or not self._in_window(fill.get("bar_timestamp") or fill.get("fill_date"), start, end):
                    continue
                items.append({
                    "type": "virtual_fill",
                    "source": "paper_account",
                    "timestamp": self._iso(fill.get("bar_timestamp") or fill.get("fill_date")),
                    "account_id": account_id,
                    "symbol": target,
                    "payload": {"fill_id": fill["fill_id"], "status": fill["status"], "quantity": fill["quantity"], "price": fill["price"]},
                })

        return items

    def _shadow_items(self, target: str, profile_id: Optional[str], account_id: Optional[int], start: Optional[date], end: Optional[date]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        profiles = [profile_id] if profile_id else [row["profile_id"] for row in self.shadow.list_profiles()]
        for current_profile in profiles:
            for signal in self.shadow.list_signals(str(current_profile), code=target, limit=500):
                signal_date = signal.get("signal_date") or signal.get("data_cutoff")
                if not self._in_window(signal_date, start, end):
                    continue
                items.append({
                    "type": "shadow_signal",
                    "source": "shadow_research",
                    "timestamp": self._iso(signal_date),
                    "account_id": account_id,
                    "symbol": target,
                    "payload": {"signal_id": signal["signal_id"], "profile_id": signal["profile_id"], "status": signal["status"], "reason": signal.get("reason")},
                })

        return items

    def _alert_items(self, target: str, account_id: Optional[int], start: Optional[date], end: Optional[date]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for trigger in self.alerts.list_triggers(target=target, page=1, page_size=500)[0]:
            timestamp = trigger.triggered_at
            if not self._in_window(timestamp, start, end):
                continue
            items.append({
                "type": "alert",
                "source": "alert_service",
                "timestamp": self._iso(timestamp),
                "account_id": account_id,
                "symbol": target,
                "payload": {"trigger_id": trigger.id, "status": trigger.status, "reason": trigger.reason, "data_source": trigger.data_source},
            })

        return items

    @staticmethod
    def _symbol(value: Any) -> str:
        try:
            return normalize_stock_code(str(value or "").strip())
        except Exception:
            return str(value or "").strip().upper()

    @staticmethod
    def _as_date(value: Any) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None

    @classmethod
    def _in_window(cls, value: Any, start: Optional[date], end: Optional[date]) -> bool:
        current = cls._as_date(value)
        if current is None:
            return False
        return not (start and current < start) and not (end and current > end)

    @staticmethod
    def _iso(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            value = datetime.combine(value, time.min)
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return value.isoformat() if hasattr(value, "isoformat") else str(value)


__all__ = ["MarketAnnotationsService"]
