# -*- coding: utf-8 -*-
"""HTTP contract and approval-gate tests for Shadow Research."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from api.deps import get_database_manager
from api.v1.endpoints.shadow import router
from src.config import Config
from src.storage import DatabaseManager


def _observations() -> list[dict]:
    return [
        {"date": "2026-08-10", "features": {"rsi14": 25, "volume_ratio": 2}, "next_return_pct": 3.0},
        {"date": "2026-08-11", "features": {"rsi14": 35, "volume_ratio": 2}, "next_return_pct": -1.0},
        {"date": "2026-08-12", "features": {"rsi14": 20, "volume_ratio": 1.6}, "next_return_pct": 4.0},
        {"date": "2026-08-13", "features": {"rsi14": 28, "volume_ratio": 1.7}, "next_return_pct": -2.0},
    ]


def _client(tmp_path: Path) -> tuple[TestClient, DatabaseManager]:
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'shadow_api.db'}")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/shadow")
    app.dependency_overrides[get_database_manager] = lambda: db
    return TestClient(app), db


def test_shadow_api_schema_and_approval_gate(tmp_path: Path) -> None:
    client, _db = _client(tmp_path)
    try:
        invalid = client.post(
            "/api/v1/shadow/profiles",
            json={"name": "bad", "rule": {"feature": "__import__", "op": "eq", "value": 1}},
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["error"] == "invalid_shadow_profile"

        created = client.post(
            "/api/v1/shadow/profiles",
            json={
                "name": "RSI shadow",
                "description": "research only",
                "rule": {"feature": "rsi14", "op": "lt", "value": 30},
            },
        )
        assert created.status_code == 200, created.text
        payload = created.json()
        profile_id = payload["profile"]["profile_id"]
        assert payload["profile"]["status"] == "draft"
        assert payload["rule"]["feature_names"] == ["rsi14"]

        blocked = client.post(f"/api/v1/shadow/profiles/{profile_id}/approve", json={"approved_by": "tester"})
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["error"] == "shadow_state_error"

        backtest = client.post(
            f"/api/v1/shadow/profiles/{profile_id}/backtest",
            json={"code": "600519", "observations": _observations(), "split_date": "2026-08-12"},
        )
        assert backtest.status_code == 200, backtest.text
        run = backtest.json()["run"]
        assert run["status"] == "completed"
        assert run["metrics"]["out_sample_count"] == 2

        approved = client.post(f"/api/v1/shadow/profiles/{profile_id}/approve", json={"approved_by": "tester"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["profile"]["status"] == "approved"

        scanned = client.post(
            f"/api/v1/shadow/profiles/{profile_id}/scan",
            json={"code": "600519", "run_id": run["run_id"], "observations": [_observations()[2]]},
        )
        assert scanned.status_code == 200, scanned.text
        assert len(scanned.json()["items"]) == 1
        assert scanned.json()["items"][0]["data_cutoff"] == "2026-08-12"

        listed = client.get(f"/api/v1/shadow/profiles/{profile_id}/signals?code=600519")
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 1
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_shadow_tables_are_added_when_an_existing_database_is_opened(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-shadow.db"
    legacy_engine = create_engine(f"sqlite:///{db_path}")
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY)"))
    legacy_engine.dispose()

    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{db_path}")
    try:
        inspector = inspect(db._engine)
        assert inspector.has_table("legacy_marker")
        assert all(inspector.has_table(table) for table in (
            "shadow_profiles",
            "shadow_rules",
            "shadow_backtest_runs",
            "shadow_signals",
        ))
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
