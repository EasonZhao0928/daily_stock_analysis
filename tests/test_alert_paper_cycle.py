# -*- coding: utf-8 -*-
"""Task 12 public Alert cycle and Paper/Shadow event contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Config
from src.services.alert_service import AlertService, AlertServiceError
from src.services.portfolio_alerts import (
    PaperEventAlert,
    evaluate_paper_event_alert,
    normalize_paper_alert_parameters,
)
from src.storage import DatabaseManager


def test_paper_event_parameter_and_evaluation_contract() -> None:
    parameters = normalize_paper_alert_parameters(
        "paper_fill", {"active": True, "message": "paper fill", "event_id": "fill-1"}
    )
    result = evaluate_paper_event_alert(
        PaperEventAlert(
            target_scope="paper_account",
            target="1",
            alert_type="paper_fill",
            parameters=parameters,
            metadata={"persisted_rule_id": 7, "effective_target": "1"},
        )
    )
    assert result["triggered"] is True
    assert result["data_source"] == "paper_account"
    with pytest.raises(ValueError, match="boolean"):
        normalize_paper_alert_parameters("paper_fill", {"active": "yes"})


def test_public_alert_run_cycle_handles_paper_event_and_shadow_degraded(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'alert-paper.db'}")
    service = AlertService(db)
    try:
        service.create_rule({
            "name": "paper fill",
            "target_scope": "paper_account",
            "target": "1",
            "alert_type": "paper_fill",
            "parameters": {"active": True, "message": "virtual fill", "event_id": "fill-1"},
        })
        service.create_rule({
            "name": "shadow degraded",
            "target_scope": "paper_account",
            "target": "1",
            "alert_type": "shadow_degraded",
            "parameters": {"active": False, "message": "stale shadow data"},
        })
        cycle = service.run_cycle()
        assert cycle["loaded"] == 2
        assert {item["status"] for item in cycle["items"]} == {"triggered", "not_triggered"}
        with pytest.raises(AlertServiceError, match="paper/shadow"):
            service.create_rule({
                "target_scope": "single_symbol",
                "target": "600519",
                "alert_type": "paper_fill",
                "parameters": {"active": True},
            })
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_paper_event_dedup_uses_lossless_event_id(tmp_path: Path) -> None:
    """N5 regression: opaque event ids must not collide via timestamp hashes."""
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'alert-event-identity.db'}")
    service = AlertService(db)
    try:
        rule = service.create_rule({
            "name": "paper fill identity",
            "target_scope": "paper_account",
            "target": "1",
            "alert_type": "paper_fill",
            "parameters": {"active": True, "message": "fill", "event_id": "paper-event-13658"},
        })
        first = service.run_cycle()
        assert first["recorded"] == 1
        service.update_rule(
            int(rule["id"]),
            {"parameters": {"active": True, "message": "fill", "event_id": "paper-event-17814"}},
        )
        second = service.run_cycle()
        assert second["recorded"] == 1
        triggers = service.list_triggers(page_size=20)["items"]
        assert {item["source_event_id"] for item in triggers} == {
            "paper-event-13658",
            "paper-event-17814",
        }
        assert all(item["data_timestamp"] is None for item in triggers)
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
