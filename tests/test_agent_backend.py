# -*- coding: utf-8 -*-
"""AgentBackend contract, LiteLLM parity, and Codex mapping tests."""

from __future__ import annotations

import ast
import json
import threading
import time
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agent.agent_backend import (
    AgentRunRequest,
    AgentRunResult,
    LiteLLMAgentBackend,
    resolve_agent_backend_id,
)
from src.agent.chat_executor import AgentChatExecutor
from src.agent.codex_agent_backend import CodexAgentBackend
from src.agent.codex_app_server_transport import (
    CodexAppServerError,
    ToolCallRecord,
    TurnResult,
    dynamic_tool_specs,
    normalize_token_usage_notification,
    resolve_command,
)
from src.agent.factory import get_tool_registry
from src.agent.llm_adapter import LLMResponse, ToolCall
from src.agent.runner import RunLoopResult, run_agent_loop
from src.agent.stock_scope import StockScope
from src.agent.tool_surface import ToolSurface
from src.agent.tools.execution import ToolAccessContext
from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolPolicy, ToolRegistry
from src.llm.backend_registry import resolve_generation_backend_id


class _FinalAnswerAdapter:
    def __init__(self) -> None:
        self.calls = []

    def call_with_tools(self, messages, tools, timeout=None):
        self.calls.append((messages, tools, timeout))
        return LLMResponse(content="answer", provider="deepseek", model="deepseek/chat")


def _request(**overrides):
    values = {
        "system_prompt": "system",
        "history_messages": [{"role": "assistant", "content": "history"}],
        "user_message": "question",
        "session_id": "session-1",
        "stock_scope": None,
        "max_steps": 3,
        "max_wall_clock_seconds": 30,
        "progress_callback": None,
        "cancel_event": None,
    }
    values.update(overrides)
    return AgentRunRequest(**values)


def _codex_surface() -> ToolSurface:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Cancellation-safe test tool.",
            parameters=[],
            handler=lambda: {"ok": True},
            policy=ToolPolicy.declared(read_only=True, cancellation_safe=True),
        )
    )
    registry.register(
        ToolDefinition(
            name="unsafe_probe",
            description="Test tool without a cancellation contract.",
            parameters=[],
            handler=lambda: {"ok": True},
            policy=ToolPolicy.declared(read_only=True),
        )
    )
    return ToolSurface(registry)


def test_agent_run_request_does_not_carry_tool_dependencies() -> None:
    names = {item.name for item in fields(AgentRunRequest)}
    assert "tool_registry" not in names
    assert "tool_surface" not in names


def test_agent_run_result_contains_only_consumed_terminal_state() -> None:
    names = {item.name for item in fields(AgentRunResult)}
    assert "session_id" not in names
    assert "finish_reason" not in names


def test_codex_backend_and_transport_do_not_import_tool_registry() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative_path in (
        "src/agent/codex_agent_backend.py",
        "src/agent/codex_app_server_transport.py",
    ):
        source = (root / relative_path).read_text(encoding="utf-8")
        imported_modules = {
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        }
        assert "src.agent.tools.registry" not in imported_modules


def test_native_windows_transport_is_rejected_before_executable_lookup(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agent.codex_app_server_transport.is_native_windows",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.agent.codex_app_server_transport.shutil.which",
        lambda _name: (_ for _ in ()).throw(AssertionError("executable lookup must not run")),
    )

    try:
        resolve_command()
    except CodexAppServerError as exc:
        assert exc.code == "capability_unsupported"
    else:
        raise AssertionError("native Windows must be rejected")


def test_auto_agent_backend_remains_litellm() -> None:
    assert resolve_agent_backend_id(SimpleNamespace(agent_backend="auto")) == "litellm"
    assert resolve_agent_backend_id(SimpleNamespace()) == "litellm"


def test_codex_chat_factory_does_not_construct_litellm_context_adapter() -> None:
    prompt_state = SimpleNamespace(
        skill_instructions="",
        default_skill_policy="",
        use_legacy_default_prompt=False,
    )
    config = SimpleNamespace(
        agent_backend="codex_app_server",
        agent_arch="single",
        agent_context_compression_enabled=True,
        agent_context_compression_trigger_tokens=1,
        agent_litellm_model="must-not-be-used",
        agent_max_steps=10,
        agent_orchestrator_timeout_s=600,
    )

    with patch("src.agent.factory.resolve_skill_prompt_state", return_value=prompt_state), \
         patch("src.agent.factory.get_tool_registry", return_value=ToolRegistry()), \
         patch("src.agent.llm_adapter.LLMToolAdapter", side_effect=AssertionError("LiteLLM must not be constructed")):
        from src.agent.factory import build_agent_chat_executor

        executor = build_agent_chat_executor(config)

    assert executor.context_llm_adapter is None


def test_generation_codex_cli_and_agent_codex_app_server_routes_remain_independent() -> None:
    config = SimpleNamespace(
        generation_backend="codex_cli",
        agent_backend="codex_app_server",
    )

    assert resolve_generation_backend_id(config) == "codex_cli"
    assert resolve_agent_backend_id(config) == "codex_app_server"


def test_litellm_multi_chat_keeps_existing_orchestrator_factory() -> None:
    sentinel = object()
    with patch("src.agent.factory.build_agent_executor", return_value=sentinel) as build_existing:
        from src.agent.factory import build_agent_chat_executor

        result = build_agent_chat_executor(
            SimpleNamespace(agent_backend="auto", agent_arch="multi"),
            skills=["bull_trend"],
        )

    assert result is sentinel
    build_existing.assert_called_once()


def test_litellm_backend_matches_existing_runner_result() -> None:
    registry = ToolRegistry()
    direct_events = []
    wrapped_events = []
    scope = StockScope(expected_stock_code="600519", allowed_stock_codes={"600519"})
    direct = run_agent_loop(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "history"},
            {"role": "user", "content": "question"},
        ],
        tool_registry=registry,
        llm_adapter=_FinalAnswerAdapter(),
        max_steps=3,
        progress_callback=direct_events.append,
        max_wall_clock_seconds=30,
        stock_scope=scope,
    )
    wrapped = LiteLLMAgentBackend(registry, _FinalAnswerAdapter()).run(
        _request(progress_callback=wrapped_events.append, stock_scope=scope)
    )

    assert wrapped.success == direct.success
    assert wrapped.final_answer == direct.content
    assert wrapped.tool_calls_log == direct.tool_calls_log
    assert wrapped.total_steps == direct.total_steps
    assert wrapped.model == direct.model
    assert wrapped.diagnostics["provider"] == direct.provider
    assert wrapped.error_message == direct.error
    assert wrapped.messages == direct.messages
    assert wrapped.usage == (
        {"total_tokens": direct.total_tokens} if direct.total_tokens > 0 else None
    )
    assert wrapped_events == direct_events
    assert wrapped.backend == "litellm"


def test_litellm_backend_passes_only_tool_surface_to_shared_runner() -> None:
    surface = _codex_surface()
    loop_result = RunLoopResult(
        success=True,
        content="answer",
        provider="fake",
        models_used=["fake/model"],
        total_steps=1,
    )

    with patch("src.agent.agent_backend.run_agent_loop", return_value=loop_result) as run_loop:
        result = LiteLLMAgentBackend(surface, _FinalAnswerAdapter()).run(_request())

    assert result.success is True
    kwargs = run_loop.call_args.kwargs
    assert kwargs["tool_surface"] is surface
    assert "tool_registry" not in kwargs


def test_litellm_tool_surface_scope_result_matches_codex_surface_contract() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="scoped_echo",
            description="Scoped parity test tool.",
            parameters=[
                ToolParameter(name="stock_code", type="string", description="Stock code"),
            ],
            handler=lambda stock_code: {"stock_code": stock_code},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["market_data:read"],
                scope_dimensions=["stock"],
                cancellation_safe=True,
            ),
        )
    )
    surface = ToolSurface(registry)
    scope = StockScope(expected_stock_code="600519", allowed_stock_codes={"600519"})

    litellm_ok = surface.execute_tool(
        "scoped_echo",
        {"stock_code": "600519"},
        ToolAccessContext(stock_scope=scope, backend="litellm"),
    )
    codex_ok = surface.execute_tool(
        "scoped_echo",
        {"stock_code": "600519"},
        ToolAccessContext(stock_scope=scope, backend="codex_app_server"),
    )
    litellm_denied = surface.execute_tool(
        "scoped_echo",
        {"stock_code": "AAPL"},
        ToolAccessContext(stock_scope=scope, backend="litellm"),
    )
    codex_denied = surface.execute_tool(
        "scoped_echo",
        {"stock_code": "AAPL"},
        ToolAccessContext(stock_scope=scope, backend="codex_app_server"),
    )

    assert set(litellm_ok) == set(codex_ok)
    assert litellm_ok["ok"] is codex_ok["ok"] is True
    assert litellm_ok["result"] == codex_ok["result"] == {"stock_code": "600519"}
    assert (
        litellm_denied["error"]["code"]
        == codex_denied["error"]["code"]
        == "stock_scope_violation"
    )
    assert litellm_denied["error"]["details"] == codex_denied["error"]["details"]


class _FakeTransport:
    last = None

    def __init__(self, command, **kwargs) -> None:
        type(self).last = self
        self.command = command
        self.kwargs = kwargs
        self.injected = None
        self.stderr_preview = "redacted"
        self.tool_calls = (
            ToolCallRecord(
                thread_id="thread-1",
                turn_id="turn-1",
                tool_name="echo",
                arguments={"value": "x"},
                success=True,
                started_at=1.0,
                finished_at=1.2,
            ),
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def start_thread(self, **kwargs):
        self.thread_kwargs = kwargs
        return "thread-1"

    def inspect_external_tool_isolation(self, thread_id):
        return {"passed": True, "apps_disabled": True, "plugins_disabled": True}

    def inject_history(self, thread_id, messages):
        self.injected = (thread_id, messages)

    def run_turn(self, thread_id, text, timeout=None, cancel_event=None):
        return TurnResult(
            turn_id="turn-1",
            final_text="codex answer",
        )

    def thread_metadata(self, thread_id):
        return {"active_permission_profile": {"id": "dsa_gate_a"}}


def _parity_artifact_handler(stock_code: str, mode: str = "read") -> dict:
    return {
        "stock_code": stock_code,
        "mode": mode,
        "artifact_ref": {
            "id": f"artifact-{stock_code}",
            "uri": f"artifact://recorded/{stock_code}",
            "content_hash": f"hash-{stock_code}",
        },
    }


def _parity_surface() -> ToolSurface:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="parity_artifact_read",
            description="Read one recorded artifact for parity tests.",
            parameters=[
                ToolParameter(
                    name="stock_code",
                    type="string",
                    description="Exact stock code.",
                ),
                ToolParameter(
                    name="mode",
                    type="string",
                    description="Recorded artifact mode.",
                    required=False,
                    enum=["read", "summary"],
                    default="read",
                ),
            ],
            handler=_parity_artifact_handler,
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["research:read"],
                scope_dimensions=["stock"],
                cancellation_safe=True,
            ),
        )
    )
    return ToolSurface(registry)


class _ParityLiteLLMAdapter:
    def __init__(self, tool_call: ToolCall) -> None:
        self.tool_call = tool_call
        self.descriptor_batches = []
        self.recorded_result = None

    def call_with_tools(self, messages, tools, timeout=None):
        self.descriptor_batches.append(tools)
        if len(self.descriptor_batches) == 1:
            return LLMResponse(
                content=None,
                tool_calls=[self.tool_call],
                provider="parity-litellm",
                model="parity-model",
            )
        tool_message = next(message for message in messages if message.get("role") == "tool")
        self.recorded_result = json.loads(tool_message["content"])
        return LLMResponse(
            content="recorded-final",
            provider="parity-litellm",
            model="parity-model",
        )


class _ParityCodexTransport(_FakeTransport):
    def __init__(
        self,
        command,
        *,
        parity_call: ToolCall,
        cancel_before_execute: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(command, **kwargs)
        self.parity_call = parity_call
        self.cancel_before_execute = cancel_before_execute
        self.tool_surface = kwargs["tool_surface"]
        self.tool_context = kwargs["tool_context"]
        self.descriptor_specs = []
        self.recorded_call = None
        self.recorded_result = None

    def start_thread(self, **kwargs):
        self.thread_kwargs = kwargs
        self.descriptor_specs = dynamic_tool_specs(
            self.tool_surface,
            kwargs["tool_names"],
        )
        return "thread-1"

    def run_turn(self, thread_id, text, timeout=None, cancel_event=None):
        if self.cancel_before_execute and cancel_event is not None:
            cancel_event.set()
        self.recorded_call = {
            "name": self.parity_call.name,
            "arguments": dict(self.parity_call.arguments),
        }
        started_at = time.monotonic()
        self.recorded_result = self.tool_surface.execute_tool(
            self.parity_call.name,
            self.parity_call.arguments,
            self.tool_context,
        )
        finished_at = time.monotonic()
        self.tool_calls = (
            ToolCallRecord(
                thread_id=thread_id,
                turn_id="turn-1",
                tool_name=self.parity_call.name,
                arguments=self.parity_call.arguments,
                success=bool(self.recorded_result["ok"]),
                started_at=started_at,
                finished_at=finished_at,
            ),
        )
        return TurnResult(turn_id="turn-1", final_text="recorded-final")


def _stable_tool_result(result: dict) -> dict:
    return {
        key: result.get(key)
        for key in ("ok", "tool_name", "result", "result_text", "error")
    }


def _run_parity_litellm(
    surface: ToolSurface,
    tool_call: ToolCall,
    stock_scope: StockScope,
) -> dict:
    adapter = _ParityLiteLLMAdapter(tool_call)
    result = LiteLLMAgentBackend(surface, adapter).run(
        _request(stock_scope=stock_scope, max_steps=2)
    )
    assert adapter.recorded_result is not None
    assert len(result.tool_calls_log) == 1
    return {
        "result": adapter.recorded_result,
        "descriptors": adapter.descriptor_batches[0],
        "call": {
            "name": tool_call.name,
            "arguments": dict(tool_call.arguments),
        },
        "success": result.tool_calls_log[0]["success"],
    }


def _run_parity_codex(
    surface: ToolSurface,
    tool_call: ToolCall,
    stock_scope: StockScope,
    *,
    cancel_before_execute: bool = False,
    cancel_event: threading.Event | None = None,
) -> dict:
    holder = {}

    def transport_factory(command, **kwargs):
        client = _ParityCodexTransport(
            command,
            parity_call=tool_call,
            cancel_before_execute=cancel_before_execute,
            **kwargs,
        )
        holder["client"] = client
        return client

    backend = CodexAgentBackend(
        surface,
        SimpleNamespace(agent_orchestrator_timeout_s=30),
        transport_factory,
    )
    with patch(
        "src.agent.codex_agent_backend.build_hardened_command",
        return_value=["codex", "app-server", "--stdio"],
    ):
        result = backend.run(
            _request(stock_scope=stock_scope, cancel_event=cancel_event)
        )

    client = holder["client"]
    assert client.recorded_result is not None
    assert len(result.tool_calls_log) == 1
    return {
        "result": client.recorded_result,
        "descriptors": client.descriptor_specs,
        "call": client.recorded_call,
        "success": result.tool_calls_log[0]["success"],
    }


def _normalize_litellm_descriptors(descriptors: list[dict]) -> dict:
    def normalize_schema(schema: dict) -> dict:
        return {
            key: value
            for key, value in schema.items()
            if key != "additionalProperties" or value is not False
        }

    return {
        item["function"]["name"]: {
            "description": item["function"]["description"],
            "inputSchema": normalize_schema(item["function"]["parameters"]),
        }
        for item in descriptors
    }


def _normalize_codex_descriptors(descriptors: list[dict]) -> dict:
    def normalize_schema(schema: dict) -> dict:
        return {
            key: value
            for key, value in schema.items()
            if key != "additionalProperties" or value is not False
        }

    return {
        item["name"]: {
            "description": item["description"],
            "inputSchema": normalize_schema(item["inputSchema"]),
        }
        for item in descriptors
    }


@pytest.mark.parametrize(
    ("case_name", "arguments", "expected_ok", "expected_error"),
    [
        (
            "success_with_artifact",
            {"stock_code": "600519", "mode": "summary"},
            True,
            None,
        ),
        (
            "scope_violation",
            {"stock_code": "000001", "mode": "read"},
            False,
            "stock_scope_violation",
        ),
        (
            "invalid_arguments",
            {"mode": "read"},
            False,
            "invalid_arguments",
        ),
    ],
)
def test_litellm_and_codex_parity_matrix(
    case_name: str,
    arguments: dict,
    expected_ok: bool,
    expected_error: str | None,
) -> None:
    del case_name
    surface = _parity_surface()
    stock_scope = StockScope(
        expected_stock_code="600519",
        allowed_stock_codes={"600519"},
    )
    tool_call = ToolCall(
        id="parity-call-1",
        name="parity_artifact_read",
        arguments=arguments,
    )

    litellm = _run_parity_litellm(surface, tool_call, stock_scope)
    codex = _run_parity_codex(surface, tool_call, stock_scope)
    litellm_envelope = surface.execute_tool(
        tool_call.name,
        tool_call.arguments,
        ToolAccessContext(
            stock_scope=stock_scope,
            backend="litellm",
            redact_result=True,
        ),
    )

    assert _normalize_litellm_descriptors(litellm["descriptors"])["parity_artifact_read"] == _normalize_codex_descriptors(
        codex["descriptors"]
    )["parity_artifact_read"]
    assert set(litellm_envelope) == set(codex["result"])
    assert _stable_tool_result(litellm_envelope) == _stable_tool_result(codex["result"])
    assert litellm["call"] == codex["call"]
    assert litellm["success"] is codex["success"] is expected_ok
    assert litellm["result"] == json.loads(codex["result"]["result_text"])
    assert _stable_tool_result(codex["result"])["ok"] is expected_ok
    actual_error = (codex["result"].get("error") or {}).get("code")
    payload_error = codex["result"]["result_text"]
    if expected_error is None:
        assert actual_error is None
        assert "artifact_ref" in litellm["result"]
        assert litellm["result"]["artifact_ref"] == {
            "id": "artifact-600519",
            "uri": "artifact://recorded/600519",
            "content_hash": "hash-600519",
        }
    else:
        assert actual_error == expected_error
        payload = json.loads(payload_error)
        assert payload.get("code", payload.get("error")) == expected_error


def test_litellm_and_codex_cancellation_result_parity() -> None:
    surface = _parity_surface()
    scope = StockScope(expected_stock_code="600519", allowed_stock_codes={"600519"})
    call = ToolCall(
        id="parity-cancel-1",
        name="parity_artifact_read",
        arguments={"stock_code": "600519"},
    )

    litellm_cancel = threading.Event()
    litellm_cancel.set()
    litellm_result = surface.execute_tool(
        call.name,
        call.arguments,
        ToolAccessContext(
            stock_scope=scope,
            backend="litellm",
            cancel_event=litellm_cancel,
            redact_result=True,
        ),
    )

    codex_cancel = threading.Event()
    codex = _run_parity_codex(
        surface,
        call,
        scope,
        cancel_before_execute=True,
        cancel_event=codex_cancel,
    )
    codex_result = codex["result"]

    assert _stable_tool_result(litellm_result) == _stable_tool_result(codex_result)
    assert litellm_result["error"]["code"] == codex_result["error"]["code"] == "cancelled"
    assert codex_cancel.is_set() is True


def test_codex_backend_uses_tool_surface_and_ephemeral_transport(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agent.codex_agent_backend.build_hardened_command",
        lambda **kwargs: ["codex", "app-server", "--stdio"],
    )
    surface = _codex_surface()
    backend = CodexAgentBackend(surface, SimpleNamespace(agent_orchestrator_timeout_s=30), _FakeTransport)
    result = backend.run(_request())

    assert result.success is True
    assert result.final_answer == "codex answer"
    assert result.backend == "codex_app_server"
    assert result.model == "Codex"
    assert result.total_steps == 1
    assert result.tool_calls_log[0]["tool"] == "echo"
    assert _FakeTransport.last.kwargs["tool_surface"] is surface
    assert _FakeTransport.last.kwargs["tool_profile"] == "dsa_gate_a"
    assert _FakeTransport.last.kwargs["max_tool_calls"] == 3
    assert _FakeTransport.last.injected == (
        "thread-1",
        [{"role": "assistant", "content": "history"}],
    )
    assert _FakeTransport.last.thread_kwargs["developer_instructions"].startswith("system")
    assert _FakeTransport.last.thread_kwargs["tool_names"] == ["echo"]
    assert "provide or select an exact stock code" in _FakeTransport.last.thread_kwargs[
        "developer_instructions"
    ]


def test_production_codex_preparation_matches_the_cancellation_safe_tool_surface(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agent.codex_agent_backend.build_hardened_command",
        lambda **kwargs: ["codex", "app-server", "--stdio"],
    )
    monkeypatch.setattr(
        "src.agent.executor.build_visible_chat_history",
        lambda *_args, **_kwargs: [],
    )
    config = SimpleNamespace(agent_orchestrator_timeout_s=30)
    backend = CodexAgentBackend(
        ToolSurface(get_tool_registry()),
        config,
        _FakeTransport,
    )
    executor = AgentChatExecutor(
        backend=backend,
        config=config,
        context_llm_adapter=None,
        skill_instructions="不得进入 Codex Prompt：新闻、热点、持仓",
        default_skill_policy="不得进入 Codex Prompt：K线、技术指标、筹码",
        max_steps=3,
        timeout_seconds=30,
    )

    with patch("src.agent.chat_executor.conversation_manager.get_or_create"), patch(
        "src.agent.chat_executor.conversation_manager.add_message",
        side_effect=[1, 2],
    ):
        result = executor.chat(
            "分析 AAPL",
            "session-1",
            context={"stock_code": "AAPL", "stock_name": "Apple"},
        )

    assert result.success is True
    assert _FakeTransport.last.thread_kwargs["tool_names"] == [
        "get_realtime_quote",
        "get_daily_history",
        "get_chip_distribution",
        "get_analysis_context",
        "get_stock_info",
        "get_portfolio_snapshot",
        "get_capital_flow",
        "analyze_trend",
        "calculate_ma",
        "get_volume_analysis",
        "analyze_pattern",
        "search_stock_news",
        "search_comprehensive_intel",
        "get_market_indices",
        "get_sector_rankings",
        "get_skill_backtest_summary",
        "get_strategy_backtest_summary",
        "get_stock_backtest_summary",
        # Task 5.2-5.4 capability tools reach Codex on the same terms.
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
    ]
    instructions = _FakeTransport.last.thread_kwargs["developer_instructions"]
    # The transport receives the complete cancellation-safe, profile-bound
    # surface.  The prompt documents the same read-only capability families;
    # forbidden execution tools remain absent.
    assert "get_analysis_context" in instructions
    assert "回测" in instructions
    assert "个股明细" in instructions
    for unavailable_tool in (
        "execute_broker_order",
        "submit_paper_order",
    ):
        assert unavailable_tool not in instructions
    for unavailable_capability in (
        "真实交易",
        "Shell",
        "文件",
        "MCP",
        "插件",
    ):
        assert unavailable_capability in instructions


def test_litellm_preparation_keeps_the_existing_chat_workflow() -> None:
    backend = LiteLLMAgentBackend(ToolRegistry(), _FinalAnswerAdapter())
    executor = AgentChatExecutor(
        backend=backend,
        config=SimpleNamespace(),
        context_llm_adapter=object(),
    )

    with patch("src.agent.executor.build_agent_chat_context_bundle") as build_context, patch(
        "src.agent.chat_executor.conversation_manager.get_or_create"
    ), patch(
        "src.agent.chat_executor.conversation_manager.add_message",
        return_value=1,
    ):
        build_context.return_value.context_messages = []
        turn = executor.prepare_turn(
            message="分析 AAPL",
            session_id="session-1",
            context={"stock_code": "AAPL"},
        )

    assert "get_realtime_quote" in turn.prepared.system_prompt
    assert "get_daily_history" in turn.prepared.system_prompt
    assert "analyze_trend" in turn.prepared.system_prompt
    assert "get_chip_distribution" in turn.prepared.system_prompt
    assert "search_stock_news" in turn.prepared.system_prompt


def test_codex_backend_rejects_disabled_timeout_without_starting_transport(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agent.codex_agent_backend.build_hardened_command",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("transport must not start")),
    )
    backend = CodexAgentBackend(
        _codex_surface(),
        SimpleNamespace(agent_orchestrator_timeout_s=0),
        _FakeTransport,
    )

    result = backend.run(_request(max_wall_clock_seconds=0))

    assert result.success is False
    assert result.error_code == "invalid_timeout"
    assert result.total_steps == 0


def test_codex_backend_does_not_add_unscoped_instruction_when_scope_exists(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agent.codex_agent_backend.build_hardened_command",
        lambda **kwargs: ["codex", "app-server", "--stdio"],
    )
    backend = CodexAgentBackend(
        _codex_surface(),
        SimpleNamespace(agent_orchestrator_timeout_s=30),
        _FakeTransport,
    )

    result = backend.run(
        _request(stock_scope=StockScope(expected_stock_code="AAPL", allowed_stock_codes={"AAPL"}))
    )

    assert result.success is True
    assert _FakeTransport.last.thread_kwargs["developer_instructions"] == "system"


class _TurnFailureTransport(_FakeTransport):
    def run_turn(self, thread_id, text, timeout=None, cancel_event=None):
        raise CodexAppServerError("timeout", "turn timed out", turn_started=True)


class _EmptyFinalTransport(_FakeTransport):
    def run_turn(self, thread_id, text, timeout=None, cancel_event=None):
        return TurnResult(turn_id="turn-1", final_text="")


def test_codex_backend_does_not_mark_empty_terminal_answer_as_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agent.codex_agent_backend.build_hardened_command",
        lambda **kwargs: ["codex", "app-server", "--stdio"],
    )
    backend = CodexAgentBackend(
        _codex_surface(),
        SimpleNamespace(agent_orchestrator_timeout_s=30),
        _EmptyFinalTransport,
    )

    result = backend.run(_request())

    assert result.success is False
    assert result.error_code == "unknown_backend_error"
    assert result.final_answer == ""


def test_codex_backend_counts_a_started_failed_turn(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agent.codex_agent_backend.build_hardened_command",
        lambda **kwargs: ["codex", "app-server", "--stdio"],
    )
    backend = CodexAgentBackend(
        _codex_surface(),
        SimpleNamespace(agent_orchestrator_timeout_s=30),
        _TurnFailureTransport,
    )

    result = backend.run(_request())

    assert result.success is False
    assert result.error_code == "timeout"
    assert result.total_steps == 1


class _TurnStartFailureTransport(_FakeTransport):
    def run_turn(self, thread_id, text, timeout=None, cancel_event=None):
        raise CodexAppServerError("protocol_error", "turn/start failed for thread-123")


def test_codex_backend_keeps_pre_turn_failure_at_zero_steps(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agent.codex_agent_backend.build_hardened_command",
        lambda **kwargs: ["codex", "app-server", "--stdio"],
    )
    backend = CodexAgentBackend(
        _codex_surface(),
        SimpleNamespace(agent_orchestrator_timeout_s=30),
        _TurnStartFailureTransport,
    )

    result = backend.run(_request())

    assert result.success is False
    assert result.error_code == "protocol_error"
    assert result.error_message == "Codex Agent 暂时无法完成本次问股，请前往 Agent 设置查看运行状态。"
    assert "thread-123" not in result.error_message
    assert result.diagnostics["internal_error"] == "turn/start failed for thread-123"
    assert result.total_steps == 0


def test_external_tool_result_redacts_before_roundtrip() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="redaction_probe",
            description="Return a secret-shaped payload.",
            parameters=[],
            handler=lambda: {
                "token": "secret-value",
                "path": "/Users/alice/private.txt",
                "value": "safe",
            },
            category="data",
            policy=ToolPolicy.declared(read_only=True),
        )
    )
    result = ToolSurface(registry).execute_tool(
        "redaction_probe",
        {},
        ToolAccessContext(redact_result=True, max_result_bytes=1024),
    )

    assert result["ok"] is True
    assert "secret-value" not in result["result_text"]
    assert "/Users/alice" not in result["result_text"]
    assert "[REDACTED]" in result["result_text"]
    assert "safe" in result["result_text"]


def test_app_server_usage_uses_documented_last_turn_counts_only() -> None:
    usage = normalize_token_usage_notification(
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": {
                "total": {"totalTokens": 9999},
                "last": {
                    "totalTokens": 120,
                    "inputTokens": 90,
                    "cachedInputTokens": 40,
                    "outputTokens": 30,
                    "reasoningOutputTokens": 12,
                },
                "modelContextWindow": 200000,
            },
        }
    )

    assert usage == {
        "prompt_tokens": 90,
        "completion_tokens": 30,
        "total_tokens": 120,
        "cached_tokens": 40,
        "completion_tokens_details": {"reasoning_tokens": 12},
    }
    assert normalize_token_usage_notification({"tokenUsage": {"last": {}}}) is None


def test_both_transports_explain_a_turn_without_stock_scope() -> None:
    """B5: strict scope is intentional, so both transports must say why.

    The factory-built ToolSurface is strict, so an unscoped turn refuses
    stock-scoped tools.  Without this guidance the model just retries and the
    user sees an unexplained ``stock_scope_violation``.
    """
    from src.agent.agent_backend import NO_STOCK_SCOPE_INSTRUCTION

    captured = {}

    class _CapturingAdapter(_FinalAnswerAdapter):
        def call_with_tools(self, messages, tools, timeout=None):
            captured.setdefault("messages", list(messages))
            return super().call_with_tools(messages, tools, timeout)

    LiteLLMAgentBackend(ToolRegistry(), _CapturingAdapter()).run(_request(stock_scope=None))
    system_message = captured["messages"][0]["content"]
    assert NO_STOCK_SCOPE_INSTRUCTION in system_message

    captured.clear()
    scope = StockScope(expected_stock_code="600519", allowed_stock_codes={"600519"})
    LiteLLMAgentBackend(ToolRegistry(), _CapturingAdapter()).run(_request(stock_scope=scope))
    assert NO_STOCK_SCOPE_INSTRUCTION not in captured["messages"][0]["content"]
