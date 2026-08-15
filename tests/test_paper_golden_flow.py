"""Cross-module Paper Account golden flow.

This test intentionally uses one temporary database and deterministic fakes so
the complete virtual path can be replayed without a broker or network.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.config import Config
from src.paper_account import PaperAccountService
from src.paper_account.controllers import LLMController, StructuredProposalAdapter
from src.paper_account.repository import PaperAccountRepository
from src.services.alert_service import AlertService
from src.storage import DatabaseManager
from data_provider.market_data_types import DataEnvelope, DataQuery, DataStatus


class PaperGoldenFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        DatabaseManager.reset_instance()
        Config.reset_instance()
        db = DatabaseManager(db_url=f"sqlite:///{Path(self._tmp.name) / 'golden.db'}")
        self.service = PaperAccountService(PaperAccountRepository(db))

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        self._tmp.cleanup()

    def test_observation_to_alert_is_replayable_and_virtual_only(self) -> None:
        account = self.service.create_account(
            name="golden",
            controller_kind="llm",
            approval_mode="human_confirm",
            initial_cash=10000,
            mandate={"allowed_markets": ["cn"], "allowed_symbols": ["600519"]},
        )
        account_id = int(account["account"]["id"])
        calls = {"model": 0}

        def fake_model(_observation):
            calls["model"] += 1
            return {
                "symbol": "600519",
                "market": "cn",
                "side": "buy",
                "order_type": "limit",
                "quantity": 2,
                "limit_price": 100,
                "rationale": "fixed point-in-time evidence",
                "evidence_refs": [{"evidence_id": "e1"}],
            }

        controller = LLMController(
            StructuredProposalAdapter(
                fake_model,
                backend="fake",
                model_name="golden-model",
                prompt_version="golden-p1",
            )
        )
        cycle_args = dict(
            decision_at=datetime(2026, 8, 14, 9, 30),
            strategy_version="golden-s1",
            account_snapshot={"cash": 10000, "positions": []},
            market_data={"600519": {"close": 100, "as_of": "2026-08-14"}},
            evidence=[{"evidence_id": "e1", "published_at": "2026-08-14", "source": "fixture"}],
            shadow_signals=[{"signal_id": "shadow-1", "data_cutoff": "2026-08-14", "side": "buy"}],
            cutoff=datetime(2026, 8, 14, 9, 30),
            controller=controller,
            context={"order_value": 200, "data_stale": False, "evidence_complete": True},
            idempotency_key="golden-run-1",
        )
        cycle = self.service.run_cycle(account_id, **cycle_args)
        replay = self.service.run_cycle(account_id, **cycle_args)
        self.assertEqual(cycle["run"]["status"], "completed")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(calls["model"], 1)
        self.assertEqual(cycle["risk_decision"]["decision"], "accepted")
        self.assertEqual(cycle["run"]["backend"], "fake")

        approved = self.service.approve_proposal(
            account_id,
            cycle["proposal"]["proposal_id"],
            approved_by="wechat:owner",
        )
        order = self.service.stage_order(account_id, approved["proposal_id"])
        order = self.service.transition_order(order["order_id"], "approved")
        matched = self.service.match_order(
            order["order_id"],
            [{"timestamp": "2026-08-14T09:31:00", "open": 100, "low": 99, "high": 101, "volume": 10}],
            slippage_bps=0,
            project_ledger=True,
        )
        self.assertEqual(matched["order"]["status"], "filled")
        fill_id = matched["fills"][0]["fill_id"]
        applied = self.service.apply_fill(fill_id)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(self.service.apply_fill(fill_id)["status"], "applied")

        outbox = self.service.portfolio.repo.get_virtual_fill_outbox(
            account_id=account_id,
            fill_id=fill_id,
        )
        self.assertEqual(outbox.status, "applied")
        trades = self.service.portfolio.repo.list_trades(account_id, datetime(2026, 8, 14).date())
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].trade_uid, f"paper:{fill_id}")

        alert_service = AlertService(self.service.repository.db)
        alert_service.create_rule({
            "name": "golden fill",
            "target_scope": "paper_account",
            "target": str(account_id),
            "alert_type": "paper_fill",
            "parameters": {"active": True, "message": "paper fill", "event_id": fill_id},
        })
        alert_cycle = alert_service.run_cycle()
        self.assertEqual(alert_cycle["items"][0]["status"], "triggered")

        with self.assertRaises(ValueError):
            self.service.freeze_observation(
                account_id,
                run_id=cycle["run"]["run_id"],
                account_snapshot={},
                market_data={},
                evidence=[{"published_at": "2026-08-15"}],
                cutoff=datetime(2026, 8, 14),
            )
        replayed = self.service.replay_cycle(account_id, cycle["run"]["run_id"])
        self.assertFalse(replayed["model_called"])
        self.assertTrue(replayed["items"][0]["consistent"])

    def test_source_owned_golden_flow_never_accepts_client_price_facts(self) -> None:
        account = self.service.create_account(
            name="source-owned-golden",
            controller_kind="llm",
            approval_mode="human_confirm",
            initial_cash=10000,
            mandate={"allowed_markets": ["cn"], "allowed_symbols": ["600519"]},
        )
        account_id = int(account["account"]["id"])

        class MarketFixture:
            def fetch(self, query: DataQuery, _policy=None):
                self.last_query = query
                return DataEnvelope(
                    capability=query.capability,
                    security_id=query.security_id,
                    data=[
                        {"date": "2026-08-14", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
                        {"date": "2026-08-15", "open": 102, "high": 103, "low": 101, "close": 102, "volume": 12},
                    ],
                    source="fixture-market",
                    source_tier="primary",
                    as_of=datetime(2026, 8, 14, tzinfo=timezone.utc),
                    retrieved_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
                    status=DataStatus.OK,
                )

        class EvidenceFixture:
            def list(self, **_kwargs):
                return [{"evidence_id": "source-e1", "code": "600519", "published_at": "2026-08-14", "source": "fixture"}]

        class ShadowFixture:
            def list_signals(self, _profile_id, **_kwargs):
                return [{"signal_id": "source-s1", "code": "600519", "data_cutoff": "2026-08-14", "status": "eligible"}]

        def fake_model(observation):
            # Account, market and evidence are all generated before this call.
            assert observation["account"]["cash"] == 10000.0
            assert observation["market"]["600519"]["close"] == 100
            assert observation["evidence"][0]["evidence_id"] == "source-e1"
            return {
                "symbol": "600519",
                "market": "cn",
                "side": "buy",
                "order_type": "market",
                "quantity": 2,
                "rationale": "source-owned observation",
                "evidence_refs": [{"evidence_id": "source-e1"}],
            }

        cycle = self.service.run_cycle_from_sources(
            account_id,
            decision_at=datetime(2026, 8, 14, 9, 30),
            strategy_version="source-s1",
            symbols=["600519"],
            market_data_manager=MarketFixture(),
            evidence_service=EvidenceFixture(),
            shadow_repository=ShadowFixture(),
            shadow_profile_id="shadow-profile",
            controller=LLMController(StructuredProposalAdapter(fake_model, backend="fixture")),
            idempotency_key="source-owned-1",
        )
        self.assertEqual(cycle["risk_decision"]["decision"], "accepted")
        approved = self.service.approve_proposal(account_id, cycle["proposal"]["proposal_id"])
        order = self.service.stage_order(account_id, approved["proposal_id"])
        order = self.service.transition_order(order["order_id"], "approved")

        class ChartFixture:
            def get_candles(self, *_args, **_kwargs):
                return {
                    "candles": [{"timestamp": "2026-08-15T09:31:00", "open": 102, "high": 103, "low": 101, "close": 102, "volume": 12}],
                    "source": "fixture-market",
                    "as_of": "2026-08-15T09:31:00",
                    "stale": False,
                    "data_quality": "ok",
                }

        advanced = self.service.advance_order(
            order["order_id"],
            as_of=datetime(2026, 8, 15, 10, 0),
            market_chart_service=ChartFixture(),
        )
        self.assertEqual(advanced["order"]["status"], "filled")
        self.assertEqual(self.service.apply_fill(advanced["fills"][0]["fill_id"])["status"], "applied")


if __name__ == "__main__":
    unittest.main()
