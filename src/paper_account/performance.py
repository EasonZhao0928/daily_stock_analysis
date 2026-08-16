# -*- coding: utf-8 -*-
"""Account-level performance comparison for Paper/Shadow/manual accounts."""

from __future__ import annotations

from datetime import date, timedelta
import math
from statistics import pstdev
from typing import Any, Dict, Iterable, List, Optional

from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_service import PortfolioService


class PaperPerformanceService:
    """Read-only comparison built on Account Ledger replay snapshots."""

    def __init__(self, portfolio: Optional[PortfolioService] = None) -> None:
        self.portfolio = portfolio or PortfolioService()
        self.repository = self.portfolio.repo

    def compare(
        self,
        account_ids: Iterable[int],
        *,
        start_date: date,
        end_date: date,
        benchmark: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if start_date > end_date:
            raise ValueError("start_date must be <= end_date")
        items: List[Dict[str, Any]] = []
        for account_id in account_ids:
            account = self.repository.get_account(int(account_id), include_inactive=True)
            if account is None:
                continue
            start = self.portfolio.get_portfolio_snapshot(
                account_id=int(account_id), as_of=start_date, include_realtime=False
            )
            end = self.portfolio.get_portfolio_snapshot(
                account_id=int(account_id), as_of=end_date, include_realtime=False
            )
            start_row = (start.get("accounts") or [{}])[0]
            end_row = (end.get("accounts") or [{}])[0]
            start_equity = float(start_row.get("total_equity") or 0.0)
            end_equity = float(end_row.get("total_equity") or 0.0)
            pnl = end_equity - start_equity
            turnover = 0.0
            trade_count = 0
            trade_events = self.portfolio.list_trade_events(
                account_id=int(account_id),
                date_from=start_date,
                date_to=end_date,
                page=1,
                page_size=100,
            ).get("items", [])
            for trade in trade_events:
                trade_count += 1
                turnover += abs(float(trade.get("quantity") or 0.0) * float(trade.get("price") or 0.0))
            points = self._equity_points(int(account_id), start_date, end_date)
            max_drawdown_pct = self._max_drawdown(points)
            volatility_pct = self._volatility(points)
            win_rate, realized_trade_count = self._win_rate(trade_events)
            account_return = round(pnl / start_equity * 100.0, 6) if start_equity else None
            benchmark_return = self._benchmark_return(benchmark)
            exposure = [
                {
                    "symbol": position.get("symbol"),
                    "market_value": position.get("market_value_base"),
                    "weight": (
                        float(position.get("market_value_base") or 0.0) / end_equity
                        if end_equity > 0 else None
                    ),
                }
                for position in (end_row.get("positions") or [])
            ]
            limitations = list(end.get("limitations") or [])
            if end.get("fx_stale"):
                limitations.append("fx_stale")
            items.append({
                "account_id": int(account_id),
                "account_kind": getattr(account, "account_kind", "manual") or "manual",
                "controller_kind": getattr(account, "controller_kind", "manual") or "manual",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "start_equity": round(start_equity, 6),
                "end_equity": round(end_equity, 6),
                "pnl": round(pnl, 6),
                "return_pct": account_return,
                "max_drawdown_pct": max_drawdown_pct,
                "volatility_pct": volatility_pct,
                "trade_count": trade_count,
                "turnover": round(turnover, 6),
                "fee_total": end_row.get("fee_total", 0.0),
                "tax_total": end_row.get("tax_total", 0.0),
                "win_rate": win_rate,
                "realized_trade_count": realized_trade_count,
                "benchmark_return_pct": benchmark_return,
                "relative_return_pct": (
                    round(account_return - benchmark_return, 6)
                    if account_return is not None and benchmark_return is not None else None
                ),
                "attribution": {
                    "relative_return_pct": (
                        round(account_return - benchmark_return, 6)
                        if account_return is not None and benchmark_return is not None else None
                    ),
                    "cost_drag": round(float(end_row.get("fee_total") or 0.0) + float(end_row.get("tax_total") or 0.0), 6),
                    "controller_kind": getattr(account, "controller_kind", "manual") or "manual",
                },
                "exposure": exposure,
                "data_quality": end.get("data_quality", "unknown"),
                "limitations": sorted(set(limitations)),
            })
        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "benchmark": benchmark or {"available": False},
            "items": items,
        }

    def _equity_points(self, account_id: int, start_date: date, end_date: date) -> List[float]:
        points: List[float] = []
        current = start_date
        # Bound this read-only comparison to one year; callers can still
        # request longer windows but the result is explicitly sampled.
        total_days = (end_date - start_date).days
        step = max(1, (total_days + 364) // 365)
        while current <= end_date:
            snapshot = self.portfolio.get_portfolio_snapshot(
                account_id=account_id, as_of=current, include_realtime=False
            )
            row = (snapshot.get("accounts") or [{}])[0]
            points.append(float(row.get("total_equity") or 0.0))
            current += timedelta(days=step)
        if not points or current - timedelta(days=step) != end_date:
            snapshot = self.portfolio.get_portfolio_snapshot(
                account_id=account_id, as_of=end_date, include_realtime=False
            )
            points.append(float((snapshot.get("accounts") or [{}])[0].get("total_equity") or 0.0))
        return points

    @staticmethod
    def _max_drawdown(points: List[float]) -> Optional[float]:
        peak: Optional[float] = None
        max_dd = 0.0
        for value in points:
            if peak is None or value > peak:
                peak = value
            if peak and peak > 0:
                max_dd = max(max_dd, (peak - value) / peak * 100.0)
        return round(max_dd, 6) if peak is not None else None

    @staticmethod
    def _volatility(points: List[float]) -> Optional[float]:
        returns = [
            (current / previous) - 1.0
            for previous, current in zip(points, points[1:])
            if previous > 0
        ]
        if not returns:
            return None
        return round(pstdev(returns) * math.sqrt(252.0) * 100.0, 6)

    @staticmethod
    def _win_rate(trades: List[Dict[str, Any]]) -> tuple[Optional[float], int]:
        inventory: Dict[str, Dict[str, float]] = {}
        wins = 0
        realized = 0
        ordered = sorted(trades, key=lambda item: str(item.get("trade_date") or item.get("date") or ""))
        for trade in ordered:
            symbol = str(trade.get("symbol") or trade.get("code") or "")
            side = str(trade.get("side") or trade.get("trade_type") or "").lower()
            quantity = abs(float(trade.get("quantity") or 0.0))
            price = float(trade.get("price") or 0.0)
            if not symbol or quantity <= 0 or price <= 0:
                continue
            position = inventory.setdefault(symbol, {"quantity": 0.0, "cost": 0.0})
            if side in {"buy", "b"}:
                position["cost"] += quantity * price
                position["quantity"] += quantity
            elif side in {"sell", "s"} and position["quantity"] > 0:
                average_cost = position["cost"] / position["quantity"]
                sold = min(quantity, position["quantity"])
                realized += 1
                wins += int(price > average_cost)
                position["quantity"] -= sold
                position["cost"] = max(0.0, position["cost"] - average_cost * sold)
        return (round(wins / realized * 100.0, 6) if realized else None, realized)

    @staticmethod
    def _benchmark_return(benchmark: Optional[Dict[str, Any]]) -> Optional[float]:
        if not benchmark:
            return None
        try:
            if benchmark.get("return_pct") is not None:
                return round(float(benchmark["return_pct"]), 6)
            points = [float(value) for value in benchmark.get("points", [])]
            if len(points) >= 2 and points[0] != 0:
                return round((points[-1] / points[0] - 1.0) * 100.0, 6)
        except (TypeError, ValueError):
            return None
        return None


__all__ = ["PaperPerformanceService"]
