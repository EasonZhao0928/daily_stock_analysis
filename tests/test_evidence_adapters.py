# -*- coding: utf-8 -*-
"""Announcement/research evidence normalization and provenance tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from data_provider.evidence_adapters import EvidenceAdapter, normalize_evidence_records
from data_provider.provider_fixtures import load_fixture


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "provider_contract"


def test_cninfo_official_announcement_is_structured_and_artifact_only() -> None:
    rows = load_fixture(FIXTURE_DIR / "cninfo_announcements.json").payload
    records = EvidenceAdapter.announcements(rows, code="600519", cutoff=date(2026, 8, 14))
    assert len(records) == 1
    record = records[0]
    assert record.kind == "announcement"
    assert record.source_tier == "official"
    assert record.published_at == "2026-08-14"
    assert record.artifact_ref and record.artifact_ref.endswith(".PDF")
    assert "artifact_only" in record.quality_flags
    assert record.summary is None  # long body is not injected into the result


def test_cutoff_and_duplicate_hash_keep_only_eligible_report() -> None:
    rows = load_fixture(FIXTURE_DIR / "eastmoney_reports.json").payload
    rows = rows + [dict(rows[0], publishDate="2026-08-15T00:00:00", infoCode="later")]
    records = EvidenceAdapter.research_reports(rows, code="600519", cutoff=date(2026, 8, 14))
    assert len(records) == 1
    assert records[0].content_hash
    assert records[0].artifact_ref


def test_official_source_wins_exact_duplicate() -> None:
    row = {
        "title": "重大合同公告",
        "date": "2026-08-14",
        "url": "https://example.invalid/a.pdf",
        "summary": "same metadata",
    }
    official = normalize_evidence_records([row], kind="announcement", code="600519", source="cninfo", source_tier="official")
    backup = normalize_evidence_records([row], kind="announcement", code="600519", source="other", source_tier="backup")
    combined = normalize_evidence_records(
        [
            dict(row, source="cninfo"),
            dict(row, source="other"),
        ],
        kind="announcement",
        code="600519",
        source="cninfo",
        source_tier="official",
    )
    assert official[0].content_hash == backup[0].content_hash
    assert combined[0].source_tier == "official"


def test_conflicting_sources_are_flagged_instead_of_silently_merged() -> None:
    rows = [
        {"title": "业绩预告", "date": "2026-08-14", "summary": "预计增长 10%", "url": "a"},
        {"title": "业绩预告", "date": "2026-08-14", "summary": "预计增长 20%", "url": "b"},
    ]
    records = normalize_evidence_records(rows, kind="announcement", code="600519", source="mixed", source_tier="primary")
    assert len(records) == 2
    assert all(record.conflict_group for record in records)
    assert all("source_conflict" in record.quality_flags for record in records)


def test_missing_title_is_dropped_and_unknown_date_is_diagnostic() -> None:
    rows = [
        {"date": "2026-08-14", "summary": "no title"},
        {"title": "无日期记录", "summary": "metadata"},
    ]
    records = normalize_evidence_records(rows, kind="research_report", source="eastmoney", source_tier="primary")
    assert len(records) == 1
    assert "missing:published_at" in records[0].quality_flags
