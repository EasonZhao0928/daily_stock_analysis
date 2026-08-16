# -*- coding: utf-8 -*-
"""Profile-bound descriptor regressions for Codex Agent and Paper paths."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent.codex_app_server_transport import (
    CodexAppServerError,
    CodexAppServerTransport,
    dynamic_tool_specs,
)
from src.agent.factory import get_paper_tool_registry, get_tool_registry
from src.agent.tool_surface import ToolSurface
from src.agent.tools.registry import (
    AGENT_CHAT_EXECUTION_PROFILE,
    ExecutionProfile,
    ToolDefinition,
    ToolPolicy,
    ToolRegistry,
)
from src.agent.tools.execution import ToolAccessContext


def _profile_surface() -> ToolSurface:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="research_read",
            description="Research-only read.",
            parameters=[],
            handler=lambda: {"ok": True},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["research:read"],
                cancellation_safe=True,
                allowed_profiles=["research_readonly"],
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="paper_submit",
            description="Paper proposal only.",
            parameters=[],
            handler=lambda: {"ok": True},
            policy=ToolPolicy.declared(
                read_only=False,
                side_effects=["paper_proposal"],
                permissions=["paper:proposal"],
                cancellation_safe=True,
                allowed_profiles=["paper_proposal"],
            ),
        )
    )
    return ToolSurface(registry, default_profile=ExecutionProfile.RESEARCH_READONLY)


def test_dynamic_tool_specs_filters_descriptors_by_execution_profile() -> None:
    surface = _profile_surface()
    specs = dynamic_tool_specs(
        surface,
        ["research_read"],
        profile=ExecutionProfile.RESEARCH_READONLY,
    )
    assert [item["name"] for item in specs] == ["research_read"]

    with pytest.raises(CodexAppServerError) as exc_info:
        dynamic_tool_specs(
            surface,
            ["paper_submit"],
            profile=ExecutionProfile.RESEARCH_READONLY,
        )
    assert exc_info.value.code == "tool_not_found"


def test_tool_surface_default_profile_hides_paper_tool_from_agent_chat() -> None:
    surface = _profile_surface()
    visible = {item["name"] for item in surface.list_tools("public", profile=surface.default_profile)}
    assert visible == {"research_read"}


def test_production_codex_profiles_expose_separate_tool_sets() -> None:
    """Agent Chat and Paper proposal must send different manifests to Codex."""
    portfolio_surface = ToolSurface(
        get_tool_registry(),
        default_profile=AGENT_CHAT_EXECUTION_PROFILE,
    )
    paper_surface = ToolSurface(
        get_paper_tool_registry(),
        default_profile=ExecutionProfile.PAPER_PROPOSAL,
    )

    portfolio_names = {
        item["name"]
        for item in portfolio_surface.list_tools(
            "mcp_descriptor",
            cancellation_safe_only=True,
            profile=portfolio_surface.default_profile,
        )
    }
    paper_names = {
        item["name"]
        for item in paper_surface.list_tools(
            "mcp_descriptor",
            cancellation_safe_only=True,
            profile=paper_surface.default_profile,
        )
    }
    paper_profile_names = {
        item["name"]
        for item in paper_surface.list_tools(
            "mcp_descriptor",
            profile=paper_surface.default_profile,
        )
    }

    assert "get_realtime_quote" in portfolio_names
    assert "read_paper_context" not in portfolio_names
    assert paper_profile_names == {
        "read_paper_context",
        "list_paper_proposals",
        "get_paper_proposal",
        "submit_paper_proposal",
        "cancel_paper_proposal",
    }
    assert paper_names == {
        "read_paper_context",
        "list_paper_proposals",
        "get_paper_proposal",
    }
    assert not paper_names & portfolio_names


def test_transport_rejects_forged_tool_call_outside_thread_manifest(monkeypatch) -> None:
    """A server asking for an undisclosed tool must receive a denial."""
    registry = ToolRegistry()
    calls: list[str] = []

    def handler() -> dict:
        calls.append("executed")
        return {"ok": True}

    registry.register(
        ToolDefinition(
            name="allowed_read",
            description="Allowed read.",
            parameters=[],
            handler=handler,
            policy=ToolPolicy.declared(read_only=True, permissions=["research:read"]),
        )
    )
    surface = ToolSurface(registry, default_profile=ExecutionProfile.RESEARCH_READONLY)
    client = CodexAppServerTransport(
        ["unused"],
        tool_surface=surface,
        tool_context=ToolAccessContext(backend="codex_app_server"),
        execution_profile=ExecutionProfile.RESEARCH_READONLY,
    )
    messages: list[dict] = []
    monkeypatch.setattr(client, "_write_message", lambda message, *, deadline, **_kwargs: messages.append(message))
    try:
        client._thread_tools["thread-1"] = {"allowed_read"}
        client._execute_tool_request(
            {
                "id": 9,
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "tool": "submit_paper_proposal",
                    "arguments": {},
                },
            }
        )
    finally:
        client.close()

    assert calls == []
    assert messages[0]["result"]["success"] is False
    assert "tool_not_allowed" in messages[0]["result"]["contentItems"][0]["text"]
