# -*- coding: utf-8 -*-
"""A5: the Paper decision cycle must be reachable from a real entry point."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.services.paper_decision_worker import PaperDecisionWorker, paper_scheduler_enabled


class _Cfg:
    def __init__(self, enabled: bool) -> None:
        self.paper_scheduler_enabled = enabled


class _FakeService:
    def __init__(self, accounts, *, raises=None, replay=False) -> None:
        self._accounts = accounts
        self._raises = raises
        self._replay = replay
        self.calls = []

    def list_accounts(self, **_kwargs):
        return self._accounts

    def inspect(self, account_id):
        for account in self._accounts:
            if int(account["id"]) == int(account_id):
                return {"config": {"state": account.get("state", "active")}}
        return None

    def run_cycle_from_sources(self, account_id, **kwargs):
        self.calls.append((account_id, kwargs))
        if self._raises is not None:
            raise self._raises
        return {"idempotent_replay": self._replay}


def _account(account_id, state="active"):
    return {"id": account_id, "account_kind": "paper", "state": state}


def test_scheduler_is_disabled_by_default():
    assert paper_scheduler_enabled(_Cfg(False)) is False
    assert paper_scheduler_enabled(object()) is False
    assert paper_scheduler_enabled(_Cfg(True)) is True


def test_disabled_scheduler_runs_no_cycle():
    service = _FakeService([_account(1)])
    worker = PaperDecisionWorker(config_provider=lambda: _Cfg(False), service=service)
    assert worker.run_once()["ran"] == 0
    assert service.calls == []


def test_enabled_scheduler_runs_active_accounts_only():
    service = _FakeService([_account(1), _account(2, state="frozen"), _account(3, state="paused")])
    worker = PaperDecisionWorker(config_provider=lambda: _Cfg(True), service=service)
    stats = worker.run_once(decision_at=datetime(2026, 8, 14, 15, 0))
    assert stats["eligible"] == 1
    assert stats["ran"] == 1
    assert [call[0] for call in service.calls] == [1]


def test_one_bad_account_does_not_stop_the_sweep():
    service = _FakeService([_account(1)], raises=RuntimeError("no symbol universe"))
    worker = PaperDecisionWorker(config_provider=lambda: _Cfg(True), service=service)
    stats = worker.run_once()
    assert stats["skipped"] == 1 and stats["ran"] == 0


def test_idempotent_replay_is_not_counted_as_a_new_run():
    service = _FakeService([_account(1)], replay=True)
    worker = PaperDecisionWorker(config_provider=lambda: _Cfg(True), service=service)
    stats = worker.run_once()
    assert stats["ran"] == 0 and stats["skipped"] == 1


def test_scheduler_never_supplies_market_or_account_facts():
    """The adapter decides *when*, never *what* the cycle sees."""
    service = _FakeService([_account(1)])
    worker = PaperDecisionWorker(config_provider=lambda: _Cfg(True), service=service)
    worker.run_once()
    _, kwargs = service.calls[0]
    for forbidden in ("market_data", "account_snapshot", "evidence", "shadow_signals"):
        assert forbidden not in kwargs


def test_main_registers_the_scheduler_behind_its_flag():
    """The worker must actually be wired into schedule mode, not just exist."""
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "paper_scheduler_enabled" in source
    assert "PaperDecisionWorker" in source
    assert "paper_decision_cycle" in source
