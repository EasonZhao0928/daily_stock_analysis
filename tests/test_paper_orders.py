# -*- coding: utf-8 -*-
"""Virtual Order, matching and Ledger outbox contracts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.config import Config
from src.paper_account import PaperAccountService, PaperStateError
from src.paper_account.controllers import LLMController, StructuredProposalAdapter
from src.paper_account.repository import PaperAccountRepository
from src.storage import DatabaseManager


def _setup(tmp_path: Path) -> tuple[PaperAccountService, int]:
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'paper-orders.db'}")
    service = PaperAccountService(PaperAccountRepository(db))
    account = service.create_account(name="orders", controller_kind="llm", approval_mode="human_confirm")
    return service, int(account["account"]["id"])


def _approved_order(service: PaperAccountService, account_id: int, *, quantity: float = 10) -> dict:
    controller = LLMController(StructuredProposalAdapter(lambda _obs: {
        "symbol": "600519",
        "market": "cn",
        "side": "buy",
        "order_type": "limit",
        "quantity": quantity,
        "limit_price": 100,
        "rationale": "deterministic order",
    }))
    cycle = service.run_cycle(
        account_id,
        decision_at=datetime(2026, 8, 14, 9),
        strategy_version=f"order-test-{quantity}",
        account_snapshot={"cash": 10000},
        market_data={"600519": {"close": 100, "as_of": "2026-08-14"}},
        controller=controller,
        context={"order_value": quantity * 100, "data_stale": False},
    )
    proposal = service.approve_proposal(account_id, cycle["proposal"]["proposal_id"], approved_by="tester")
    return service.stage_order(account_id, proposal["proposal_id"])


def test_virtual_order_state_machine_and_optimistic_version(tmp_path: Path) -> None:
    service, account_id = _setup(tmp_path)
    try:
        order = _approved_order(service, account_id)
        assert order["status"] == "staged"
        order = service.transition_order(order["order_id"], "approved", expected_version=1)
        assert order["status"] == "approved"
        with pytest.raises(PaperStateError, match="version conflict"):
            service.transition_order(order["order_id"], "open", expected_version=1)
        order = service.transition_order(order["order_id"], "open", expected_version=order["version"])
        assert order["status"] == "open"
        service.freeze(account_id)
        cancelled = service.cancel_orders_on_freeze(account_id)
        assert cancelled[0]["status"] == "cancelled"
        with pytest.raises(PaperStateError, match="invalid order transition"):
            service.transition_order(order["order_id"], "open")
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_matching_uses_only_post_cutoff_bars_and_replays_fill_idempotently(tmp_path: Path) -> None:
    service, account_id = _setup(tmp_path)
    try:
        order = _approved_order(service, account_id, quantity=10)
        order = service.transition_order(order["order_id"], "approved")
        result = service.match_order(
            order["order_id"],
            [
                {"timestamp": "2026-08-14T09:00:00", "open": 90, "low": 90, "high": 90, "volume": 10},
                {"timestamp": "2026-08-14T09:31:00", "open": 99, "low": 98, "high": 100, "volume": 4},
                {"timestamp": "2026-08-14T09:32:00", "open": 100, "low": 99, "high": 101, "volume": 10},
            ],
            slippage_bps=0,
        )
        assert result["order"]["status"] == "filled"
        assert [fill["quantity"] for fill in result["fills"]] == [4.0, 6.0]
        assert all(fill["status"] == "pending" for fill in result["fills"])
        replay = service.match_order(
            order["order_id"],
            [
                {"timestamp": "2026-08-14T09:31:00", "open": 99, "low": 98, "high": 100, "volume": 4},
                {"timestamp": "2026-08-14T09:32:00", "open": 100, "low": 99, "high": 101, "volume": 10},
            ],
            slippage_bps=0,
        )
        assert replay["fills"] == []
        first = service.apply_fill(result["fills"][0]["fill_id"])
        second = service.apply_fill(result["fills"][0]["fill_id"])
        assert first["status"] == second["status"] == "applied"
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_matching_skips_halt_price_limit_and_can_expire(tmp_path: Path) -> None:
    service, account_id = _setup(tmp_path)
    try:
        order = _approved_order(service, account_id, quantity=2)
        order = service.transition_order(order["order_id"], "approved")
        result = service.match_order(
            order["order_id"],
            [
                {"timestamp": "2026-08-14T09:31", "open": 100, "low": 100, "high": 100, "halted": True},
                {"timestamp": "2026-08-14T09:32", "open": 100, "low": 100, "high": 100, "limit_up": True},
            ],
        )
        assert result["fills"] == []
        assert result["order"]["status"] == "open"
        expiring = _approved_order(service, account_id, quantity=1)
        expiring = service.transition_order(expiring["order_id"], "approved")
        result = service.match_order(
            expiring["order_id"],
            [{"timestamp": "2026-08-14T10:00", "open": 100, "low": 99, "high": 101}],
        )
        assert result["order"]["status"] == "filled"
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_replay_recomputes_mandate_without_model(tmp_path: Path) -> None:
    service, account_id = _setup(tmp_path)
    try:
        controller = LLMController(StructuredProposalAdapter(lambda _obs: {
            "symbol": "600519", "market": "cn", "side": "buy",
            "order_type": "market", "quantity": 1, "rationale": "replay",
        }))
        cycle = service.run_cycle(
            account_id,
            decision_at=datetime(2026, 8, 15, 9),
            strategy_version="replay",
            account_snapshot={},
            market_data={},
            controller=controller,
            context={"order_value": 100, "data_stale": False},
        )
        replay = service.replay_cycle(
            account_id,
            cycle["run"]["run_id"],
            context={"order_value": 100, "data_stale": False},
        )
        assert replay["model_called"] is False
        assert replay["items"][0]["consistent"] is True
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_performance_compare_includes_risk_benchmark_and_attribution(tmp_path: Path) -> None:
    service, account_id = _setup(tmp_path)
    try:
        result = service.compare_performance(
            [account_id],
            start_date=datetime(2026, 8, 1).date(),
            end_date=datetime(2026, 8, 14).date(),
            benchmark={"return_pct": 2.5, "symbol": "000300"},
        )
        item = result["items"][0]
        assert item["account_id"] == account_id
        assert item["benchmark_return_pct"] == 2.5
        assert "volatility_pct" in item
        assert "realized_trade_count" in item
        assert item["attribution"]["controller_kind"] == "llm"
        assert item["data_quality"] in {"ok", "partial", "unknown"}
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
