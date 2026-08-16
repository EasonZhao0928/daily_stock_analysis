# -*- coding: utf-8 -*-
"""SQLite migration, deduplication, TTL and source precedence tests."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import inspect

from src.config import Config
from src.storage import DatabaseManager


def _evidence(**overrides):
    value = {
        "evidence_id": "evidence-001",
        "kind": "announcement",
        "code": "600519",
        "title": "重大事项公告",
        "published_at": "2026-08-14",
        "source": "cninfo",
        "source_tier": "official",
        "url": "https://example.invalid/a.pdf",
        "artifact_ref": "cninfo:anno-001",
        "summary": "短摘要",
        "content_hash": "a" * 64,
        "schema_fingerprint": "b" * 64,
        "payload": {"small": True},
        "quality_flags": ["artifact_only"],
    }
    value.update(overrides)
    return value


def test_research_evidence_table_is_created_for_existing_empty_database(tmp_path) -> None:
    db_path = tmp_path / "evidence-migration.db"
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{db_path}")
    try:
        assert inspect(db._engine).has_table("research_evidence")
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_hash_dedup_and_official_source_precedence(tmp_path) -> None:
    db_path = tmp_path / "evidence.db"
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{db_path}")
    try:
        first_id = db.save_research_evidence(_evidence(source="backup", source_tier="backup"))
        second_id = db.save_research_evidence(_evidence(source="cninfo", source_tier="official"))
        assert first_id == second_id
        stored = db.get_research_evidence("a" * 64)
        assert stored is not None
        assert stored["source"] == "cninfo"
        assert stored["source_tier"] == "official"
        assert len(db.list_research_evidence(code="600519")) == 1
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_ttl_marks_stale_and_purge_is_explicit(tmp_path) -> None:
    db_path = tmp_path / "evidence-ttl.db"
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{db_path}")
    try:
        db.save_research_evidence(
            _evidence(content_hash="c" * 64, evidence_id="evidence-ttl"),
            expires_at=datetime.now() - timedelta(days=1),
        )
        stored = db.get_research_evidence("c" * 64)
        assert stored is not None and stored["stale"] is True
        assert db.list_research_evidence(code="600519", include_stale=False) == []
        assert db.purge_research_evidence(datetime.now() + timedelta(seconds=1)) == 1
        assert db.get_research_evidence("c" * 64) is None
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_large_payload_is_not_stored_inline(tmp_path) -> None:
    db_path = tmp_path / "evidence-large.db"
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{db_path}")
    try:
        db.save_research_evidence(_evidence(content_hash="d" * 64, evidence_id="evidence-large", payload={"text": "x" * 20000}))
        stored = db.get_research_evidence("d" * 64)
        assert stored is not None
        assert stored["payload"] is None
        assert "payload_truncated" in stored["quality_flags"]
        assert stored["artifact_ref"]
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
