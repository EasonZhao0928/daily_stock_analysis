# -*- coding: utf-8 -*-
"""C3: task 5.2-5.4 capabilities must be reachable by the Agent."""

from __future__ import annotations

import pytest

from src.agent.factory import get_tool_registry
from src.agent.tools.extended_data_tools import ALL_EXTENDED_DATA_TOOLS
from src.agent.tools.registry import ExecutionProfile, check_tool_profile_access


_EXPECTED = {
    "get_financial_statement",
    "get_consensus_estimate",
    "get_announcements",
    "get_research_reports",
    "get_dragon_tiger",
    "get_margin_trading",
    "get_block_trades",
    "get_shareholder_counts",
    "get_share_unlocks",
    "get_dividends",
}


def test_extended_capabilities_are_registered_tools():
    names = {tool.name for tool in get_tool_registry().list_tools()}
    assert _EXPECTED <= names, sorted(_EXPECTED - names)


def test_every_skill_required_tool_actually_exists():
    """design 3: a skill must map to real tools, never to a promise."""
    import yaml
    from pathlib import Path

    registered = {tool.name for tool in get_tool_registry().list_tools()}
    root = Path(__file__).resolve().parents[1] / "strategies"
    checked = 0
    for path in sorted(root.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        required = data.get("required_tools") or []
        if not required:
            continue
        checked += 1
        missing = [name for name in required if name not in registered]
        assert not missing, f"{path.name} requires unregistered tools: {missing}"
    assert checked > 0


def test_extended_tools_are_research_only_and_not_in_paper_profile():
    """A Paper cycle decides on frozen inputs, so live research stays out."""
    for tool in ALL_EXTENDED_DATA_TOOLS:
        assert tool.policy.read_only is True, tool.name
        assert "network_read" in set(tool.policy.side_effects or ()), tool.name
        assert not check_tool_profile_access(tool, ExecutionProfile.PAPER_PROPOSAL)["visible"], tool.name
        assert check_tool_profile_access(tool, ExecutionProfile.RESEARCH_READONLY)["visible"], tool.name


def test_blocked_capability_reports_unavailable_instead_of_empty(monkeypatch):
    """R10.4/design 3: unavailable must be visible, never silently empty."""
    import data_provider.base as base_module
    from src.agent.tools.extended_data_tools import _fetch_capability

    monkeypatch.setattr(base_module, "extended_market_data_enabled", lambda config=None: False)
    result = _fetch_capability("financial_statement", "600519")

    assert result["status"] == "upstream_blocked"
    assert result["data"] is None
    assert "unavailable" in result["note"].lower()
    # An empty list would read as "this company has no financials".
    assert result["data"] != []
