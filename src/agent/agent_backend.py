# -*- coding: utf-8 -*-
"""AgentBackend contract and the zero-regression LiteLLM wrapper."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.agent.llm_adapter import LLMToolAdapter
from src.agent.runner import run_agent_loop
from src.agent.stock_scope import StockScope
from src.agent.tool_surface import ToolSurface


# Shared by both transports so LiteLLM and Codex explain an unscoped turn the
# same way (R1.3 parity covers behaviour, not just result envelopes).
NO_STOCK_SCOPE_INSTRUCTION = (
    "No stock scope was established for this turn. Do not call any DSA tool that requires a "
    "stock_code. If the user asks about a specific stock, ask them in plain language to provide "
    "or select an exact stock code. Non-stock market tools remain available."
)

AGENT_BACKEND_ERROR_CODES = frozenset(
    {
        "command_not_found",
        "login_required",
        "capability_unsupported",
        "unsupported_agent_arch",
        "approval_required",
        "timeout",
        "cancelled",
        "protocol_error",
        "output_too_large",
        "resource_limit_exceeded",
        "tool_roundtrip_failed",
        "resource_cleanup_failed",
        "invalid_timeout",
        "unknown_backend_error",
    }
)
AGENT_BACKEND_IDS = frozenset({"auto", "litellm", "codex_app_server"})


class AgentBackendConfigError(ValueError):
    """Structured Agent backend selection error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_agent_backend_id(config: Any) -> str:
    """Resolve Chat backend; ``auto`` deliberately remains LiteLLM."""
    requested = str(getattr(config, "agent_backend", "auto") or "auto").strip().lower()
    if requested not in AGENT_BACKEND_IDS:
        raise AgentBackendConfigError(
            "capability_unsupported",
            f"Unsupported AGENT_BACKEND: {requested}",
        )
    return "litellm" if requested == "auto" else requested


@dataclass(frozen=True)
class AgentRunRequest:
    system_prompt: str
    history_messages: List[Dict[str, Any]]
    user_message: str
    session_id: str
    stock_scope: Optional[StockScope]
    max_steps: int
    max_wall_clock_seconds: Optional[float]
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    cancel_event: Optional[threading.Event] = None


@dataclass
class AgentRunResult:
    success: bool = False
    final_answer: str = ""
    tool_calls_log: List[Dict[str, Any]] = field(default_factory=list)
    model: str = ""
    backend: str = ""
    usage: Optional[Dict[str, Any]] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    messages: List[Dict[str, Any]] = field(default_factory=list)
    total_steps: int = 0


class AgentBackend(ABC):
    """One execution backend for Agent Chat."""

    backend_id: str
    runtime_owns_loop: bool

    @abstractmethod
    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Run one Agent turn and return the normalized result."""


class LiteLLMAgentBackend(AgentBackend):
    """Thin wrapper around the existing DSA-owned ``run_agent_loop``."""

    backend_id = "litellm"
    runtime_owns_loop = False

    def __init__(self, tool_surface: Any, llm_adapter: LLMToolAdapter) -> None:
        # Accept the historical registry positional argument, but normalize it
        # immediately so the LiteLLM loop has the same execution seam as Codex.
        self.tool_surface = (
            tool_surface
            if isinstance(tool_surface, ToolSurface)
            else ToolSurface(tool_surface, legacy_runner_compat=True)
        )
        self.llm_adapter = llm_adapter

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        # The factory-built surface is strict (not legacy_runner_compat), so a
        # turn with no resolved stock scope refuses stock-scoped tools just like
        # Codex does.  Tell the model that up front, otherwise it retries and
        # the user only sees an unexplained refusal.
        system_prompt = request.system_prompt
        if request.stock_scope is None:
            system_prompt = f"{system_prompt}\n\n{NO_STOCK_SCOPE_INSTRUCTION}"
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *request.history_messages,
            {"role": "user", "content": request.user_message},
        ]
        loop_result = run_agent_loop(
            messages=messages,
            tool_surface=self.tool_surface,
            llm_adapter=self.llm_adapter,
            max_steps=request.max_steps,
            progress_callback=request.progress_callback,
            max_wall_clock_seconds=request.max_wall_clock_seconds,
            stock_scope=request.stock_scope,
        )
        usage = {"total_tokens": loop_result.total_tokens} if loop_result.total_tokens > 0 else None
        error_code = None
        if not loop_result.success and "timed out" in str(loop_result.error or "").lower():
            error_code = "timeout"
        elif not loop_result.success:
            error_code = "unknown_backend_error"
        return AgentRunResult(
            success=loop_result.success,
            final_answer=loop_result.content,
            tool_calls_log=loop_result.tool_calls_log,
            model=loop_result.model,
            backend=self.backend_id,
            usage=usage,
            diagnostics={"provider": loop_result.provider},
            error_code=error_code,
            error_message=loop_result.error,
            messages=loop_result.messages,
            total_steps=loop_result.total_steps,
        )
