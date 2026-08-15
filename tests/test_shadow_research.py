# -*- coding: utf-8 -*-
"""Shadow Research rules, snapshots, backtests and approval gates."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from data_provider.market_data_types import DataEnvelope, DataStatus, DataQuery
from src.config import Config
from src.shadow_research.backtest import ShadowBacktestError, ShadowBacktestRunner
from src.shadow_research.dsl import ShadowRuleError, compile_shadow_rule
from src.shadow_research.ledger import LedgerSnapshotError, freeze_account_ledger
from src.shadow_research.repository import ShadowResearchRepository
from src.shadow_research.service import ShadowResearchService, ShadowStateError
from src.shadow_research.snapshot import SnapshotError, build_feature_snapshot, build_market_observations
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager


def test_shadow_dsl_is_allowlisted_and_deterministic() -> None:
    spec = {"all": [{"feature": "rsi14", "op": "lt", "value": 30}, {"feature": "volume_ratio", "op": "gte", "value": 1.5}]}
    rule = compile_shadow_rule(spec, version="7")
    assert rule.evaluate({"rsi14": 25, "volume_ratio": 2}) is True
    assert rule.evaluate({"rsi14": 35, "volume_ratio": 2}) is False
    assert rule.features == ("rsi14", "volume_ratio")
    assert rule.rule_hash == compile_shadow_rule(spec, version="7").rule_hash


@pytest.mark.parametrize(
    "spec",
    [
        {"feature": "__import__", "op": "eq", "value": 1},
        {"feature": "close.price", "op": "gt", "value": 1},
        {"feature": "close", "op": "eval", "value": 1},
        {"feature": "close", "op": "between", "value": [1]},
        {"feature": "close", "op": "eq", "value": float("nan")},
    ],
)
def test_shadow_dsl_rejects_dynamic_or_malformed_input(spec) -> None:
    with pytest.raises(ShadowRuleError):
        compile_shadow_rule(spec)


def test_snapshot_hash_is_stable_and_rejects_future_evidence() -> None:
    payload = {"close": 100.0, "rsi14": 25.0}
    first = build_feature_snapshot(code="600519.SH", cutoff=date(2026, 8, 14), features=payload, source_refs=[{"source": "cninfo", "published_at": "2026-08-13"}])
    second = build_feature_snapshot(code="600519.SH", cutoff=date(2026, 8, 14), features=dict(reversed(list(payload.items()))), source_refs=[{"published_at": "2026-08-13", "source": "cninfo"}])
    assert first["snapshot_hash"] == second["snapshot_hash"]
    with pytest.raises(SnapshotError, match="newer than cutoff"):
        build_feature_snapshot(code="600519", cutoff=date(2026, 8, 14), features=payload, source_refs=[{"published_at": "2026-08-15"}])


def _observations():
    return [
        {"date": "2026-08-10", "features": {"rsi14": 25, "volume_ratio": 2}, "next_return_pct": 3.0},
        {"date": "2026-08-11", "features": {"rsi14": 35, "volume_ratio": 2}, "next_return_pct": -1.0},
        {"date": "2026-08-12", "features": {"rsi14": 20, "volume_ratio": 1.6}, "next_return_pct": 4.0},
        {"date": "2026-08-13", "features": {"rsi14": 28, "volume_ratio": 1.7}, "next_return_pct": -2.0},
    ]


def test_backtest_has_in_sample_out_of_sample_and_costs() -> None:
    rule = compile_shadow_rule({"feature": "rsi14", "op": "lt", "value": 30})
    result = ShadowBacktestRunner().run(code="600519", rule=rule, observations=_observations(), split_date=date(2026, 8, 12), fee_bps=10, slippage_bps=10)
    assert result["status"] == "completed"
    assert result["in_sample_count"] == 2
    assert result["out_sample_count"] == 2
    assert result["out_sample_signal_count"] == 2
    assert result["out_sample_avg_return_pct"] == pytest.approx(0.8)  # (4-0.2 + -2-0.2) / 2
    assert result["source_snapshot_hash"]


def test_backtest_rejects_future_feature_and_duplicate_dates() -> None:
    rule = compile_shadow_rule({"feature": "close", "op": "gt", "value": 1})
    with pytest.raises(ShadowBacktestError, match="future-derived"):
        ShadowBacktestRunner().run(code="600519", rule=rule, observations=[{"date": "2026-08-10", "features": {"future_close": 10}}], split_date=date(2026, 8, 11))
    with pytest.raises(ShadowBacktestError, match="duplicate"):
        ShadowBacktestRunner().run(code="600519", rule=rule, observations=[{"date": "2026-08-10", "features": {"close": 10}}, {"date": "2026-08-10", "features": {"close": 11}}], split_date=date(2026, 8, 11))


def test_market_snapshot_builder_calculates_only_visible_features() -> None:
    class Manager:
        def fetch(self, query, policy):
            assert isinstance(query, DataQuery)
            return DataEnvelope(
                capability="daily_data",
                security_id=query.security_id,
                data=[
                    {"date": "2026-08-10", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100},
                    {"date": "2026-08-11", "open": 11, "high": 12, "low": 10, "close": 11, "volume": 200},
                    {"date": "2026-08-12", "open": 12, "high": 13, "low": 11, "close": 12, "volume": 300},
                ],
                source="fixture",
                source_tier="primary",
                as_of=date(2026, 8, 12),
                retrieved_at=datetime.now(timezone.utc),
                status=DataStatus.OK,
            )

    observations, snapshot = build_market_observations(
        code="600519",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 12),
        market_data_manager=Manager(),
    )
    assert len(observations) == 2
    assert observations[0]["features_as_of"] == "2026-08-10"
    assert observations[0]["features"]["ma20"] == pytest.approx(10)
    assert observations[0]["next_return_pct"] == pytest.approx(10)
    assert snapshot["snapshot_hash"]


def test_backtest_attributes_halted_and_price_limit_failures() -> None:
    rule = compile_shadow_rule({"feature": "close", "op": "gt", "value": 1})
    result = ShadowBacktestRunner().run(
        code="600519",
        rule=rule,
        observations=[
            {"date": "2026-08-10", "features": {"close": 10}, "next_return_pct": 1.0},
            {"date": "2026-08-11", "features": {"close": 11}, "halted": True, "next_return_pct": 2.0},
            {"date": "2026-08-12", "features": {"close": 12}, "limit_up": True, "next_return_pct": 3.0},
        ],
        split_date=date(2026, 8, 10),
    )
    assert result["status"] == "completed"
    assert result["out_sample_signal_count"] == 3
    assert result["out_sample_executed_signal_count"] == 1
    assert result["out_sample_coverage_pct"] == pytest.approx(100 / 3)
    assert result["failure_modes"] == {"halted": 1, "price_limited": 1}


def test_shadow_service_requires_completed_backtest_before_approval(tmp_path: Path) -> None:
    db_path = tmp_path / "shadow.db"
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{db_path}")
    try:
        service = ShadowResearchService(ShadowResearchRepository(db))
        created = service.create_profile(name="RSI shadow", rule={"feature": "rsi14", "op": "lt", "value": 30})
        profile_id = created["profile"]["profile_id"]
        with pytest.raises(ShadowStateError, match="completed"):
            service.approve_profile(profile_id)
        backtest = service.run_backtest(profile_id, code="600519", observations=_observations(), split_date=date(2026, 8, 12))
        assert backtest["run"]["status"] == "completed"
        approved = service.approve_profile(profile_id, approved_by="tester")
        assert approved["profile"]["status"] == "approved"
        signals = service.scan_signals(profile_id, code="600519", observations=[_observations()[2]], run_id=backtest["run"]["run_id"], evidence_refs=[{"evidence_id": "e1"}])
        assert len(signals) == 1
        assert signals[0]["data_cutoff"] == date(2026, 8, 12)
        assert service.scan_signals(profile_id, code="600519", observations=[_observations()[2]], run_id=backtest["run"]["run_id"]) == signals
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_account_ledger_freeze_is_point_in_time_and_content_addressed(tmp_path: Path) -> None:
    db_path = tmp_path / "shadow-ledger.db"
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{db_path}")
    try:
        portfolio = PortfolioService(PortfolioRepository(db))
        account = portfolio.create_account(name="Shadow source", broker="demo", market="cn", base_currency="CNY")
        account_id = account["id"]
        portfolio.record_cash_ledger(account_id=account_id, event_date=date(2026, 8, 1), direction="in", amount=10000)
        portfolio.record_trade(
            account_id=account_id,
            symbol="600519",
            trade_date=date(2026, 8, 10),
            side="buy",
            quantity=1,
            price=100,
            trade_uid="trade-shadow-1",
        )
        first = freeze_account_ledger(
            account_id=account_id,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 10),
            db=db,
        )
        second = freeze_account_ledger(
            account_id=account_id,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 10),
            db=db,
        )
        assert first["snapshot_hash"] == second["snapshot_hash"]
        assert first["event_count"] == 2
        assert {item["kind"] for item in first["events"]} == {"cash", "trade"}
        with pytest.raises(LedgerSnapshotError, match="no events"):
            freeze_account_ledger(
                account_id=account_id,
                start_date=date(2026, 8, 11),
                end_date=date(2026, 8, 12),
                db=db,
            )
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
