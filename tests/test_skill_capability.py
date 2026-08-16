# -*- coding: utf-8 -*-
"""Research Capability metadata, discovery and ToolSurface validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agent.skills.base import Skill, SkillManager, load_skill_from_markdown, load_skill_from_yaml
from src.agent.tool_surface import ToolSurface
from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolRegistry, ToolPolicy


def test_legacy_yaml_receives_compatible_capability_defaults(tmp_path: Path) -> None:
    path = tmp_path / "legacy.yaml"
    path.write_text(
        "name: legacy\ndisplay_name: Legacy\ndescription: old\ninstructions: |\n  # Old\n  Keep compatibility\n",
        encoding="utf-8",
    )
    skill = load_skill_from_yaml(path)
    assert skill.version == "1.0.0"
    assert skill.risk_level == "low"
    assert skill.evidence_policy == "optional"
    assert skill.section_index[0]["title"] == "Old"


def test_markdown_metadata_and_section_index_are_loaded(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: finance\ndescription: Financial quality\nversion: 2.1.0\nrequired-capabilities: [market_data:read, evidence:read]\nrisk-level: medium\nevidence-policy: required\n---\n# Summary\nshort\n## Statements\nlong body\n## Risks\nred flags\n",
        encoding="utf-8",
    )
    skill = load_skill_from_markdown(path)
    assert skill.version == "2.1.0"
    assert skill.required_capabilities == ["market_data:read", "evidence:read"]
    assert skill.risk_level == "medium"
    assert skill.evidence_policy == "required"
    assert [item["title"] for item in skill.section_index] == ["Summary", "Statements", "Risks"]
    assert skill.load_instructions(sections=["Statements"]) == "## Statements\nlong body"
    assert len(skill.load_instructions(max_chars=7)) == 7


def test_invalid_capability_metadata_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "name: bad\ndisplay_name: Bad\ndescription: bad\nrisk_level: unsafe\ninstructions: bad\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="risk_level"):
        load_skill_from_yaml(path)


def test_catalog_is_summary_first_and_full_body_is_opt_in() -> None:
    manager = SkillManager()
    manager.register(
        Skill(
            name="finance",
            display_name="Finance",
            description="Financial quality",
            instructions="# Facts\nlong body",
            required_tools=["read_quote"],
            required_capabilities=["market_data:read"],
            evidence_policy="required",
        )
    )
    summary = manager.get_skill_catalog()
    assert "instructions" not in summary[0]
    assert summary[0]["evidence_policy"] == "required"
    full = manager.get_skill_catalog(include_instructions=True)
    assert full[0]["instructions"] == "# Facts\nlong body"


def test_required_tools_and_capabilities_use_tool_surface_profile() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_quote",
            description="read",
            parameters=[ToolParameter(name="stock_code", type="string", description="code")],
            handler=lambda stock_code: {"code": stock_code},
            policy=ToolPolicy.declared(
                read_only=True,
                side_effects=["network_read"],
                permissions=["market_data:read"],
                scope_dimensions=["stock"],
                cancellation_safe=True,
            ),
        )
    )
    manager = SkillManager()
    manager.register(
        Skill(
            name="finance",
            display_name="Finance",
            description="Financial quality",
            instructions="facts",
            required_tools=["read_quote"],
            required_capabilities=["market_data:read"],
        )
    )
    decision = manager.validate_required_tools("finance", ToolSurface(registry), "research_readonly")
    assert decision["available"] is True
    manager.get("finance").required_tools.append("missing")
    assert manager.validate_required_tools("finance", ToolSurface(registry))["available"] is False


def test_skill_validation_reports_profile_and_missing_capability() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_quote",
            description="read",
            parameters=[],
            handler=lambda: {"ok": True},
            policy=ToolPolicy.declared(
                read_only=True,
                side_effects=["network_read"],
                permissions=["market_data:read"],
                cancellation_safe=True,
            ),
        )
    )
    manager = SkillManager()
    manager.register(
        Skill(
            name="paper_only",
            display_name="Paper",
            description="paper",
            instructions="paper",
            required_tools=["read_quote"],
            required_capabilities=["paper:proposal"],
        )
    )
    decision = manager.validate_required_tools("paper_only", ToolSurface(registry), "research_readonly")
    assert decision["available"] is False
    assert decision["reason"] == "missing_capability"
    assert decision["profile"] == "research_readonly"
