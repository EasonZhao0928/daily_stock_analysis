# -*- coding: utf-8 -*-
"""Task 10 Paper decision-cycle contracts."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest

from src.agent.factory import get_paper_tool_registry
from src.agent.tool_surface import ToolSurface
from src.agent.tools.execution import ToolAccessContext
from src.agent.tools.registry import ExecutionProfile, ToolInvocation
from src.config import Config
from src.paper_account import PaperAccountService
from src.paper_account.controllers import (
    HybridController,
    LLMController,
    ShadowController,
    StructuredOutputError,
    StructuredProposalAdapter,
)
from src.paper_account.repository import PaperAccountRepository
from src.storage import DatabaseManager


def _service(tmp_path: Path) -> PaperAccountService:
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'cycle.db'}")
    return PaperAccountService(PaperAccountRepository(db))


def _proposal(quantity: float = 2) -> dict:
    return {
        "symbol": "600519",
        "market": "cn",
        "side": "buy",
        "order_type": "market",
        "quantity": quantity,
        "rationale": "由冻结 observation 推导",
        "evidence_refs": [{"evidence_id": "e1"}],
    }


def test_structured_adapter_rejects_malformed_injection_and_timeout() -> None:
    adapter = StructuredProposalAdapter(lambda observation: _proposal(), timeout_seconds=0.2)
    assert adapter.generate({"cutoff": "2026-08-14"})["symbol"] == "600519"

    for raw in (
        "buy 600519",
        [_proposal()],
        {**_proposal(), "tool_calls": [{"name": "submit_order"}]},
        {**_proposal(), "account_id": 99},
    ):
        with pytest.raises(StructuredOutputError):
            StructuredProposalAdapter(lambda observation, raw=raw: raw).generate({})

    def slow(_observation):
        time.sleep(0.2)
        return _proposal()

    with pytest.raises(StructuredOutputError, match="timeout"):
        StructuredProposalAdapter(slow, timeout_seconds=0.01).generate({})


def test_shadow_llm_hybrid_intersection_is_conservative() -> None:
    shadow = ShadowController(lambda _obs: _proposal(10))
    llm = LLMController(StructuredProposalAdapter(lambda _obs: _proposal(4)))
    result = HybridController(shadow, llm).generate({})
    assert result is not None
    assert result["quantity"] == 4
    assert HybridController(
        ShadowController(lambda _obs: _proposal()),
        LLMController(StructuredProposalAdapter(lambda _obs: {**_proposal(), "side": "sell"})),
    ).generate({}) is None


def test_run_cycle_is_idempotent_and_quota_exhaustion_skips(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        created = service.create_account(
            name="cycle",
            controller_kind="llm",
            approval_mode="human_confirm",
            mandate={"allowed_markets": ["cn"]},
        )
        account_id = int(created["account"]["id"])
        calls = {"count": 0}

        def model(_observation):
            calls["count"] += 1
            return _proposal()

        controller = LLMController(
            StructuredProposalAdapter(model, backend="fake", model_name="fake-v1", prompt_version="p1")
        )
        kwargs = dict(
            decision_at=datetime(2026, 8, 14, 9, 30),
            strategy_version="s1",
            account_snapshot={"cash": 10000, "positions": []},
            market_data={"600519": {"close": 100, "as_of": "2026-08-14"}},
            evidence=[{"evidence_id": "e1", "published_at": "2026-08-14"}],
            controller=controller,
            context={"order_value": 200, "data_stale": False},
        )
        first = service.run_cycle(account_id, **kwargs)
        second = service.run_cycle(account_id, **kwargs)
        assert first["run"]["status"] == "completed"
        assert second["idempotent_replay"] is True
        assert first["proposal"]["proposal_id"] == second["proposal"]["proposal_id"]
        assert second["risk_decision"]["decision"] == "accepted"
        assert calls["count"] == 1
        assert first["run"]["backend"] == "fake"

        skipped = service.run_cycle(
            account_id,
            **{**kwargs, "decision_at": datetime(2026, 8, 14, 10, 0), "quota_available": False},
        )
        assert skipped["run"]["status"] == "skipped"
        assert skipped["run"]["diagnostics"]["reason"] == "quota_exhausted"
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_paper_profiles_are_opt_in_and_have_no_execution_tools() -> None:
    registry = get_paper_tool_registry()
    surface = ToolSurface(registry)
    proposal_names = {item["name"] for item in surface.list_tools(profile=ExecutionProfile.PAPER_PROPOSAL)}
    approval_names = {item["name"] for item in surface.list_tools(profile=ExecutionProfile.PAPER_APPROVAL)}
    assert {"read_paper_context", "submit_paper_proposal", "cancel_paper_proposal"}.issubset(proposal_names)
    assert {"approve_paper_proposal", "reject_paper_proposal"}.issubset(approval_names)
    forbidden = ("broker", "fill", "cash", "position", "shell", "file", "database", "order")
    assert not any(any(word in name.lower() for word in forbidden) for name in proposal_names | approval_names)
    assert not ({"submit_paper_proposal", "cancel_paper_proposal"} & approval_names)
