# -*- coding: utf-8 -*-
"""Shared lifecycle seam for DSA's official Codex App Server sessions.

The RuntimeFactory owns only process construction, bounded concurrency and
cleanup.  Generation and Agent backends retain separate contracts and choose
their own thread/tool semantics on top of the yielded transport.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional, Sequence

from src.agent.codex_app_server_transport import (
    PERMISSION_PROFILE,
    CodexAppServerError,
    CodexAppServerTransport,
    ToolCallRecord,
    build_hardened_command,
)
from src.agent.codex_tool_process import CodexToolProcessRunner
from src.agent.tool_surface import ToolSurface
from src.agent.tools.execution import ToolAccessContext


TransportFactory = Callable[..., CodexAppServerTransport]
CommandFactory = Callable[..., Sequence[str]]


class CodexAppServerRuntimeFactory:
    """Create one isolated App Server session per business operation.

    The default limit is intentionally one process at a time.  A future pool
    can be added behind this seam after lifecycle and throughput evidence is
    available; callers must not depend on process reuse.
    """

    def __init__(
        self,
        *,
        transport_factory: TransportFactory = CodexAppServerTransport,
        command_factory: CommandFactory = build_hardened_command,
        max_concurrency: int = 1,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self.transport_factory = transport_factory
        self.command_factory = command_factory
        self.max_concurrency = max_concurrency
        self._slots = threading.BoundedSemaphore(max_concurrency)

    def _acquire_slot(
        self,
        *,
        request_timeout: float,
        deadline: Optional[float],
        cancel_event: Optional[threading.Event],
    ) -> None:
        if request_timeout <= 0:
            raise CodexAppServerError("timeout", "Codex App Server request timeout must be positive")
        wait_deadline = time.monotonic() + request_timeout
        if deadline is not None:
            wait_deadline = min(wait_deadline, deadline)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CodexAppServerError("cancelled", "Codex App Server request was cancelled")
            remaining = wait_deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerError("timeout", "Codex App Server concurrency limit timed out")
            if self._slots.acquire(timeout=min(remaining, 0.1)):
                return

    @contextmanager
    def session(
        self,
        *,
        mode: str,
        request_timeout: float,
        deadline: Optional[float] = None,
        cancel_event: Optional[threading.Event] = None,
        tool_surface: Optional[ToolSurface] = None,
        tool_context: Optional[ToolAccessContext] = None,
        tool_event_callback: Optional[Callable[[str, ToolCallRecord], None]] = None,
        tool_profile: Optional[Any] = None,
        execution_profile: Optional[Any] = None,
        max_tool_calls: int = 1,
        environment: Optional[dict[str, str]] = None,
        tool_runner: Optional[CodexToolProcessRunner] = None,
    ) -> Iterator[CodexAppServerTransport]:
        """Yield a started, isolated transport and always release its slot."""

        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"generation", "agent"}:
            raise ValueError("mode must be 'generation' or 'agent'")
        if normalized_mode == "generation":
            # A Generation session must never inherit an Agent ToolSurface.
            effective_surface = ToolSurface.empty()
            effective_profile = None
        else:
            effective_surface = tool_surface or ToolSurface.empty()
            effective_profile = execution_profile
        effective_context = tool_context or ToolAccessContext(
            backend="codex_app_server",
            timeout_seconds=request_timeout,
            deadline=deadline,
            cancel_event=cancel_event,
        )

        self._acquire_slot(
            request_timeout=request_timeout,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        client = None
        try:
            command = list(
                self.command_factory(
                    timeout=request_timeout,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
            )
            if not command:
                raise CodexAppServerError("command_not_found", "Codex App Server command is empty")
            client = self.transport_factory(
                command,
                tool_surface=effective_surface,
                tool_context=effective_context,
                request_timeout=request_timeout,
                environment=environment,
                tool_event_callback=tool_event_callback,
                deadline=deadline,
                cancel_event=cancel_event,
                tool_runner=tool_runner,
                tool_profile=tool_profile if tool_profile is not None else PERMISSION_PROFILE,
                execution_profile=effective_profile,
                max_tool_calls=max(1, int(max_tool_calls)),
            )
            with client:
                yield client
        finally:
            try:
                self._slots.release()
            except ValueError as exc:
                # A double release would indicate a lifecycle bug.  Keep it
                # visible instead of silently corrupting the concurrency cap.
                raise RuntimeError("Codex App Server runtime slot released twice") from exc
