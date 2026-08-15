# -*- coding: utf-8 -*-
"""Paper Account Proposal API contracts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_database_manager
from api.v1.endpoints.paper import get_market_chart_service, router
from src.config import Config
from src.paper_account import PaperAccountService
from src.paper_account.controllers import LLMController, StructuredProposalAdapter
from src.paper_account.repository import PaperAccountRepository
from src.storage import DatabaseManager


def _client(tmp_path: Path) -> tuple[TestClient, DatabaseManager]:
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'paper-api.db'}")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/paper")
    app.dependency_overrides[get_database_manager] = lambda: db
    app.dependency_overrides[get_market_chart_service] = lambda: _FakeMarketChartService()
    return TestClient(app), db


class _FakeMarketChartService:
    def get_candles(self, _symbol, **_kwargs):
        return {
            "candles": [{
                "timestamp": datetime(2026, 8, 14, 10, 31),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1,
            }],
            "source": "fixture",
            "as_of": "2026-08-14T10:31:00",
            "stale": False,
            "data_quality": "ok",
        }


def test_paper_proposal_list_detail_approve_reject_and_freeze_conflict(tmp_path: Path) -> None:
    client, db = _client(tmp_path)
    try:
        created = client.post(
            "/api/v1/paper/accounts",
            json={"name": "paper-api", "approval_mode": "human_confirm", "controller_kind": "llm"},
        )
        assert created.status_code == 200, created.text
        account_id = created.json()["account"]["id"]
        service = PaperAccountService(PaperAccountRepository(db))
        cycle = service.run_cycle(
            account_id,
            decision_at=datetime(2026, 8, 14, 9, 30),
            strategy_version="api-v1",
            account_snapshot={"cash": 10000, "positions": []},
            market_data={"600519": {"close": 100, "as_of": "2026-08-14"}},
            controller=LLMController(
                StructuredProposalAdapter(
                    lambda _obs: {
                        "symbol": "600519",
                        "market": "cn",
                        "side": "buy",
                        "order_type": "market",
                        "quantity": 1,
                        "rationale": "api test",
                    }
                )
            ),
            context={"order_value": 100, "data_stale": False},
        )
        proposal_id = cycle["proposal"]["proposal_id"]
        proposal_page = client.get(
            f"/api/v1/paper/accounts/{account_id}/proposals?page=1&page_size=1"
        ).json()
        assert proposal_page["items"]
        assert proposal_page["total"] == 1
        runs = client.get(f"/api/v1/paper/accounts/{account_id}/runs?page=1&page_size=1")
        assert runs.status_code == 200
        run_id = runs.json()["items"][0]["run_id"]
        run_detail = client.get(f"/api/v1/paper/accounts/{account_id}/runs/{run_id}")
        assert run_detail.status_code == 200
        assert run_detail.json()["observation"]["payload_hash"]
        assert client.get(f"/api/v1/paper/accounts/{account_id}/proposals/{proposal_id}").status_code == 200
        approved = client.post(
            f"/api/v1/paper/accounts/{account_id}/proposals/{proposal_id}/approve",
            json={"approved_by": "tester", "expected_version": 1},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["proposal"]["decision_by"] == "local_admin"

        frozen = client.post(f"/api/v1/paper/accounts/{account_id}/freeze", json={})
        assert frozen.status_code == 200
        blocked = client.post(
            f"/api/v1/paper/accounts/{account_id}/proposals/{proposal_id}/approve",
            json={"approved_by": "tester"},
        )
        assert blocked.status_code == 409

        # A separate active account creates an approved proposal/order for matching API.
        created2 = client.post(
            "/api/v1/paper/accounts",
            json={"name": "paper-api-orders", "approval_mode": "human_confirm", "controller_kind": "llm"},
        )
        account2 = created2.json()["account"]["id"]
        cycle2 = service.run_cycle(
            account2,
            decision_at=datetime(2026, 8, 14, 10, 30),
            strategy_version="api-v2",
            account_snapshot={"cash": 10000, "positions": []},
            market_data={"600519": {"close": 100, "as_of": "2026-08-14"}},
            controller=LLMController(
                StructuredProposalAdapter(lambda _obs: {
                    "symbol": "600519", "market": "cn", "side": "buy",
                    "order_type": "market", "quantity": 1, "rationale": "order api",
                })
            ),
            context={"order_value": 100, "data_stale": False},
        )
        proposal2 = service.approve_proposal(account2, cycle2["proposal"]["proposal_id"], approved_by="tester")
        # A4: approval creates the deterministic virtual order; there is no
        # second public staging step that can drive the state machine.
        staged = client.get(f"/api/v1/paper/accounts/{account2}/orders")
        assert staged.status_code == 200, staged.text
        order_id = staged.json()["items"][0]["order_id"]
        assert client.post(
            f"/api/v1/paper/accounts/{account2}/orders",
            json={"proposal_id": proposal2["proposal_id"]},
        ).status_code == 405
        assert client.post(
            f"/api/v1/paper/accounts/{account2}/orders/{order_id}/transition",
            json={"status": "approved"},
        ).status_code == 404
        matched = client.post(
            f"/api/v1/paper/accounts/{account2}/orders/{order_id}/advance",
            json={"as_of": "2026-08-14T10:31:00"},
        )
        assert matched.status_code == 200, matched.text
        assert matched.json()["order"]["status"] == "filled"
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_decision_cycle_is_reachable_over_http(tmp_path: Path) -> None:
    """A5 regression: run_cycle had no production caller at all.

    The endpoint must also refuse to take market or account facts from the
    request body -- inputs come from the owning seams (design 6).
    """
    client, db = _client(tmp_path)
    try:
        service = PaperAccountService(PaperAccountRepository(db))
        account_id = int(service.create_account(
            name="scheduled",
            controller_kind="shadow",
            mandate={"allowed_markets": ["cn"], "allowed_symbols": ["600519"], "max_order_value": 1000},
            initial_cash=10000,
        )["account"]["id"])

        # The route exists and reaches the cycle (a source-backed run may still
        # fail on unavailable market data here; 404 would mean unreachable).
        response = client.post(
            f"/api/v1/paper/accounts/{account_id}/run",
            json={"decision_at": "2026-08-14T15:00:00", "strategy_version": "s1"},
        )
        assert response.status_code != 404, response.text
        assert response.status_code in (200, 409), response.text

        # Unknown account still 404s.
        missing = client.post(
            "/api/v1/paper/accounts/999999/run",
            json={"decision_at": "2026-08-14T15:00:00", "strategy_version": "s1"},
        )
        assert missing.status_code == 404

        # Client-supplied facts are not part of the request contract.
        from api.v1.schemas.paper import PaperRunCycleRequest

        fields = set(PaperRunCycleRequest.model_fields)
        assert not fields & {"market_data", "account_snapshot", "evidence", "shadow_signals"}
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
