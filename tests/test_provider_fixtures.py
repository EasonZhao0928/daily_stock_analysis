# -*- coding: utf-8 -*-
"""Offline fixture and schema-drift gates for a-stock-data capabilities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_provider.provider_fixtures import (
    FixtureValidationError,
    ProviderFixture,
    fixture_from_dict,
    load_fixture,
    network_probe_enabled,
    require_network_probe,
    schema_fingerprint,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "provider_contract"


def test_checked_in_provider_fixtures_are_offline_and_fingerprinted() -> None:
    paths = sorted(FIXTURE_DIR.glob("*.json"))
    assert {path.stem for path in paths} == {"cninfo_announcements", "eastmoney_reports", "sina_lrb", "ths_eps"}
    for path in paths:
        fixture = load_fixture(path)
        assert fixture.network is False
        assert fixture.schema_fingerprint
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["schema_fingerprint"] == fixture.schema_fingerprint
        assert raw["metadata"]["redacted"] is True


def test_schema_fingerprint_ignores_values_but_detects_field_drift() -> None:
    first = [{"报告期": "2026-06-30", "净利润": "1"}]
    same_shape = [{"报告期": "2025-12-31", "净利润": "999"}]
    changed = [{"报告期": "2025-12-31", "净利润": "999", "货币": "CNY"}]
    assert schema_fingerprint(first) == schema_fingerprint(same_shape)
    assert schema_fingerprint(first) != schema_fingerprint(changed)


def test_secret_like_fixture_fields_are_rejected() -> None:
    with pytest.raises(FixtureValidationError, match="secret-like"):
        fixture_from_dict(
            {
                "provider": "example",
                "capability": "quote",
                "fixture_id": "bad",
                "payload": {"api_key": "sk-not-a-fixture-value"},
            }
        )


def test_network_probe_is_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("DSA_NETWORK_PROBE", raising=False)
    assert network_probe_enabled() is False
    with pytest.raises(RuntimeError, match="network probe disabled"):
        require_network_probe()
    monkeypatch.setenv("DSA_NETWORK_PROBE", "1")
    assert network_probe_enabled() is True


def test_fixture_rejects_declared_schema_drift() -> None:
    with pytest.raises(FixtureValidationError, match="schema fingerprint mismatch"):
        ProviderFixture(
            provider="example",
            capability="quote",
            fixture_id="drift",
            payload={"price": 1},
            expected_schema_fingerprint="0" * 64,
        )
