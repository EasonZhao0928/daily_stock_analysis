# -*- coding: utf-8 -*-
"""Paper Account state, mandate, observation and proposal contracts."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from src.config import Config
from src.paper_account import PaperAccountService, PaperStateError
from src.paper_account.mandate import PaperMandate, PaperMandateError, evaluate_proposal
from src.paper_account.observation import ObservationError, build_paper_observation
from src.paper_account.repository import PaperAccountRepository
from src.paper_account.controllers import LLMController, StructuredProposalAdapter
from src.storage import DatabaseManager


class _AutoPaperConfig:
    """Explicitly opt in to auto_paper, which is refused by default."""

    paper_auto_mode_enabled = True


def _service(tmp_path: Path, *, auto_paper: bool = False) -> PaperAccountService:
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'paper.db'}")
    return PaperAccountService(
        PaperAccountRepository(db),
        config=_AutoPaperConfig() if auto_paper else None,
    )


def test_mandate_is_typed_and_rejects_unsafe_values() -> None:
    mandate = PaperMandate.from_mapping({
        "allowed_markets": ["cn"],
        "max_order_value": 1000,
        "max_position_weight": 0.3,
        "require_fresh_data": True,
    }, version="v2")
    accepted = evaluate_proposal(
        mandate,
        {
            "symbol": "600519",
            "market": "cn",
            "side": "buy",
            "order_type": "market",
            "quantity": 2,
            "rationale": "结构化研究建议",
        },
        {"order_value": 200, "projected_position_weight": 0.2, "data_stale": False},
    )
    assert accepted.accepted is True
    rejected = evaluate_proposal(
        mandate,
        {
            "symbol": "600519",
            "market": "cn",
            "side": "buy",
            "order_type": "market",
            "quantity": 2,
            "rationale": "结构化研究建议",
        },
        {"order_value": 2000, "data_stale": True},
    )
    assert rejected.accepted is False
    assert rejected.rule_codes == (
        "ORDER_VALUE_LIMIT_EXCEEDED",
        "RISK_CONTEXT_INCOMPLETE",
        "OBSERVATION_STALE",
    )
    assert rejected.details["missing_context"] == ["projected_position_weight"]
    with pytest.raises(PaperMandateError, match="exactly one"):
        evaluate_proposal(mandate, {
            "symbol": "600519", "market": "cn", "side": "buy", "order_type": "market",
            "quantity": 1, "target_weight": 0.1, "rationale": "ambiguous",
        })


def test_mandate_covers_full_risk_surface_with_stable_codes() -> None:
    mandate = PaperMandate.from_mapping({
        "allowed_markets": ["cn"],
        "allowed_symbols": ["600519", "000001"],
        "excluded_symbols": ["000001"],
        "allowed_order_types": ["limit"],
        "max_order_value": 100,
        "max_position_weight": 0.1,
        "max_sector_weight": 0.2,
        "max_total_exposure": 0.5,
        "max_turnover_pct": 10,
        "max_daily_loss_pct": 2,
        "max_drawdown_pct": 5,
        "max_open_orders": 1,
        "min_cash_ratio": 0.2,
        "decision_frequency_minutes": 60,
        "decision_windows": ["09:30-15:00"],
        "min_evidence_count": 2,
        "max_stale_seconds": 30,
    })
    decision = evaluate_proposal(mandate, {
        "symbol": "000001", "market": "cn", "side": "buy", "order_type": "market",
        "quantity": 10, "rationale": "boundary", "evidence_refs": [],
    }, {
        "order_value": 1000,
        "projected_position_weight": 0.5,
        "projected_sector_weight": 0.5,
        "projected_total_exposure": 0.9,
        "turnover_pct": 20,
        "daily_loss_pct": 3,
        "drawdown_pct": 6,
        "open_orders": 1,
        "cash_ratio_after": 0.1,
        "minutes_since_last_decision": 10,
        "decision_time": datetime(2026, 8, 14, 16, 0),
        "data_stale": False,
        "stale_seconds": 60,
        "evidence_count": 0,
        "evidence_complete": False,
    })
    assert decision.accepted is False
    assert set(decision.rule_codes) == {
        "SYMBOL_BLACKLISTED", "ORDER_TYPE_NOT_ALLOWED", "ORDER_VALUE_LIMIT_EXCEEDED",
        "POSITION_LIMIT_EXCEEDED", "SECTOR_LIMIT_EXCEEDED", "TOTAL_EXPOSURE_LIMIT_EXCEEDED",
        "TURNOVER_LIMIT_EXCEEDED", "DAILY_LOSS_LIMIT_EXCEEDED", "DRAWDOWN_HALT",
        "OPEN_ORDER_LIMIT_EXCEEDED", "CASH_FLOOR_BREACHED", "DECISION_FREQUENCY_LIMIT",
        "OUTSIDE_DECISION_WINDOW", "OBSERVATION_STALE", "EVIDENCE_INSUFFICIENT",
    }


def test_auto_paper_requires_explicit_feature_flag(tmp_path: Path) -> None:
    """N1 regression: auto_paper must not be creatable with the flag off."""
    service = _service(tmp_path)
    with pytest.raises(PaperStateError, match="PAPER_AUTO_MODE_ENABLED"):
        service.create_account(name="auto", controller_kind="llm", approval_mode="auto_paper")


def test_auto_paper_stages_virtual_order_and_missing_freshness_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path, auto_paper=True)
    try:
        account_id = int(service.create_account(
            name="auto", controller_kind="llm", approval_mode="auto_paper",
            mandate={"allowed_markets": ["cn"], "max_order_value": 1000},
        )["account"]["id"])
        proposal = {
            "symbol": "600519", "market": "cn", "side": "buy", "order_type": "market",
            "quantity": 1, "rationale": "auto paper",
        }
        rejected = service.run_cycle(
            account_id,
            decision_at=datetime(2026, 8, 14, 9, 30),
            strategy_version="missing-freshness",
            account_snapshot={"cash": 10000, "positions": []},
            market_data={"600519": {"close": 100, "as_of": "2026-08-14"}},
            controller=LLMController(StructuredProposalAdapter(lambda _obs: proposal)),
        )
        assert rejected["risk_decision"]["decision"] == "rejected"
        assert "RISK_CONTEXT_INCOMPLETE" in rejected["risk_decision"]["rule_codes"]
        assert rejected["staged_order"] is None

        accepted = service.run_cycle(
            account_id,
            decision_at=datetime(2026, 8, 14, 10, 30),
            strategy_version="fresh",
            account_snapshot={"cash": 10000, "positions": []},
            market_data={"600519": {"close": 100, "is_stale": False, "as_of": "2026-08-14"}},
            controller=LLMController(StructuredProposalAdapter(lambda _obs: proposal)),
        )
        assert accepted["proposal"]["status"] == "approved"
        assert accepted["staged_order"]["status"] == "staged"
        assert accepted["staged_order"]["proposal_id"] == accepted["proposal"]["proposal_id"]
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_llm_cycle_resolves_injected_controller_factory_without_caller_controller(tmp_path: Path) -> None:
    """A5 regression: API/scheduler-shaped cycles must not require a hidden controller argument."""
    proposal = {
        "symbol": "600519",
        "market": "cn",
        "side": "buy",
        "order_type": "market",
        "quantity": 1,
        "rationale": "factory proposal",
    }
    calls = []

    def factory(kind, config):
        calls.append((kind, config))
        return LLMController(StructuredProposalAdapter(lambda _obs: proposal))

    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'paper-factory.db'}")
    service = PaperAccountService(PaperAccountRepository(db), controller_factory=factory)
    try:
        account_id = int(service.create_account(
            name="factory-backed",
            controller_kind="llm",
            approval_mode="human_confirm",
            mandate={"allowed_markets": ["cn"], "max_order_value": 1000},
        )["account"]["id"])
        result = service.run_cycle(
            account_id,
            decision_at=datetime(2026, 8, 14, 10, 30),
            strategy_version="factory",
            account_snapshot={"cash": 10000, "positions": []},
            market_data={"600519": {"close": 100, "is_stale": False, "as_of": "2026-08-14"}},
        )
        assert result["proposal"]["status"] == "pending_confirmation"
        assert calls and calls[0][0] == "llm"
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_observation_is_immutable_and_cutoff_bounded() -> None:
    first = build_paper_observation(
        account_snapshot={"cash": 1000, "positions": []},
        market_data={"600519": {"close": 100, "as_of": "2026-08-10"}},
        evidence=[{"source": "cninfo", "published_at": "2026-08-10"}],
        shadow_signals=[{"signal_date": "2026-08-10", "status": "eligible"}],
        cutoff=date(2026, 8, 10),
    )
    second = build_paper_observation(
        account_snapshot={"positions": [], "cash": 1000},
        market_data={"600519": {"close": 100, "as_of": "2026-08-10"}},
        evidence=[{"published_at": "2026-08-10", "source": "cninfo"}],
        shadow_signals=[{"status": "eligible", "signal_date": "2026-08-10"}],
        cutoff=date(2026, 8, 10),
    )
    assert first["observation_hash"] == second["observation_hash"]
    with pytest.raises(ObservationError, match="newer"):
        build_paper_observation(
            account_snapshot={}, market_data={}, evidence=[{"published_at": "2026-08-11"}], cutoff=date(2026, 8, 10)
        )


def test_observation_rejects_market_data_past_the_cutoff() -> None:
    """A3 regression: market rows were previously copied in unchecked."""
    with pytest.raises(ObservationError, match="market data for 600519 is newer"):
        build_paper_observation(
            account_snapshot={"cash": 1000, "positions": []},
            # One day past the cutoff: the single largest look-ahead vector.
            market_data={"600519": {"close": 100, "as_of": "2026-08-11"}},
            cutoff=date(2026, 8, 10),
        )


def test_observation_rejects_same_day_future_timestamp() -> None:
    """A3 regression: date-only comparison must not admit same-day future data."""
    cutoff = datetime(2026, 8, 10, 10, 0)
    with pytest.raises(ObservationError, match="market data for 600519 is newer"):
        build_paper_observation(
            account_snapshot={"cash": 1000, "positions": []},
            market_data={"600519": {"close": 100, "timestamp": "2026-08-10T10:00:01+08:00"}},
            cutoff=cutoff,
        )
    with pytest.raises(ObservationError, match="evidence is newer"):
        build_paper_observation(
            account_snapshot={"cash": 1000, "positions": []},
            market_data={},
            evidence=[{"published_at": "2026-08-10T10:00:01+08:00"}],
            cutoff=cutoff,
        )


def test_observation_rejects_undatable_inputs_instead_of_skipping() -> None:
    """A3 regression: an unparseable/missing timestamp must not skip the check."""
    with pytest.raises(ObservationError, match="no timestamp"):
        build_paper_observation(
            account_snapshot={"cash": 1000, "positions": []},
            market_data={"600519": {"close": 100}},
            cutoff=date(2026, 8, 10),
        )
    with pytest.raises(ObservationError, match="unparseable timestamp"):
        build_paper_observation(
            account_snapshot={"cash": 1000, "positions": []},
            market_data={"600519": {"close": 100, "as_of": "not-a-date"}},
            cutoff=date(2026, 8, 10),
        )
    with pytest.raises(ObservationError, match="evidence has no timestamp"):
        build_paper_observation(
            account_snapshot={},
            market_data={},
            evidence=[{"source": "cninfo"}],
            cutoff=date(2026, 8, 10),
        )


def test_observation_rejects_account_snapshot_past_the_cutoff() -> None:
    """A3 regression: the account side was unchecked too."""
    with pytest.raises(ObservationError, match="account snapshot is newer"):
        build_paper_observation(
            account_snapshot={"cash": 1000, "positions": [], "as_of": "2026-08-11"},
            market_data={},
            cutoff=date(2026, 8, 10),
        )


def test_paper_account_state_priority_idempotency_and_human_proposal(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        created = service.create_account(
            name="LLM paper",
            approval_mode="human_confirm",
            mandate={"allowed_markets": ["cn"], "max_order_value": 1000},
            initial_cash=10000,
        )
        account_id = int(created["account"]["id"])
        assert created["account"]["account_kind"] == "paper"
        assert created["account"]["external_execution_enabled"] is False
        assert created["config"]["state"] == "active"

        run = service.create_run(account_id, decision_at=datetime(2026, 8, 14, 9, 30), strategy_version="s1")
        duplicate_run = service.create_run(account_id, decision_at=datetime(2026, 8, 14, 9, 30), strategy_version="s1")
        assert run["run_id"] == duplicate_run["run_id"]
        observation = service.freeze_observation(
            account_id,
            run_id=run["run_id"],
            account_snapshot={"cash": 10000, "positions": []},
            market_data={"600519": {"close": 100, "as_of": "2026-08-14"}},
            evidence=[{"evidence_id": "e1", "published_at": "2026-08-14"}],
            cutoff=date(2026, 8, 14),
        )
        proposal = service.submit_proposal(
            account_id,
            run_id=run["run_id"],
            observation_id=observation["observation_id"],
            proposal={
                "symbol": "600519",
                "market": "cn",
                "side": "buy",
                "order_type": "limit",
                "quantity": 2,
                "limit_price": 100,
                "rationale": "满足结构化研究条件",
                "evidence_refs": [{"evidence_id": "e1"}],
            },
            context={"order_value": 200, "data_stale": False},
        )
        assert proposal["proposal"]["status"] == "pending_confirmation"
        assert proposal["risk_decision"]["decision"] == "accepted"
        approved = service.approve_proposal(account_id, proposal["proposal"]["proposal_id"])
        assert approved["status"] == "approved"

        config_version = int(service.inspect(account_id)["config"]["config_version"])
        service.pause(account_id, expected_version=config_version)
        with pytest.raises(PaperStateError, match="version conflict"):
            service.resume(account_id, expected_version=config_version)
        current_version = int(service.inspect(account_id)["config"]["config_version"])
        service.freeze(account_id, expected_version=current_version)
        with pytest.raises(PaperStateError, match="invalid transition"):
            service.resume(account_id)
        service.close(account_id)
        assert service.inspect(account_id)["config"]["state"] == "closed"
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_legacy_portfolio_account_gets_paper_columns_with_safe_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-portfolio.db"
    legacy_engine = create_engine(f"sqlite:///{db_path}")
    with legacy_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE portfolio_accounts ("
            "id INTEGER PRIMARY KEY, owner_id VARCHAR(64), name VARCHAR(64) NOT NULL, "
            "broker VARCHAR(64), market VARCHAR(8) NOT NULL, base_currency VARCHAR(8) NOT NULL, "
            "is_active BOOLEAN NOT NULL, created_at DATETIME, updated_at DATETIME)"
        ))
        connection.execute(text(
            "INSERT INTO portfolio_accounts (id, name, market, base_currency, is_active) "
            "VALUES (1, 'legacy', 'cn', 'CNY', 1)"
        ))
    legacy_engine.dispose()

    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{db_path}")
    try:
        columns = {item["name"] for item in inspect(db._engine).get_columns("portfolio_accounts")}
        assert {"account_kind", "controller_kind", "external_execution_enabled"}.issubset(columns)
        with db.get_session() as session:
            row = session.execute(text(
                "SELECT account_kind, controller_kind, external_execution_enabled "
                "FROM portfolio_accounts WHERE id = 1"
            )).one()
            assert tuple(row) == ("manual", "manual", 0)
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_context_vocabulary_aliases_exist(tmp_path: Path) -> None:
    """E2: design 6 / CONTEXT.md names must be callable, not just documented."""
    service = _service(tmp_path)
    try:
        for name in ("run_cycle", "review_proposal", "advance_orders", "control", "inspect"):
            assert callable(getattr(service, name, None)), name

        account_id = int(service.create_account(name="vocab")["account"]["id"])
        assert service.control(account_id, "paused")["config"]["state"] == "paused"
        assert service.control(account_id, "active")["config"]["state"] == "active"
        with pytest.raises(PaperStateError, match="unsupported paper account command"):
            service.control(account_id, "teleported")
        with pytest.raises(PaperStateError, match="unsupported proposal decision"):
            service.review_proposal(account_id, "missing", decision="maybe")
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
