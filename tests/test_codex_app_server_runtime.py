# -*- coding: utf-8 -*-
"""Tests for the shared Codex App Server lifecycle seam."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from src.agent.codex_app_server_runtime import CodexAppServerRuntimeFactory
from src.agent.codex_app_server_transport import CodexAppServerError
from src.agent.tool_surface import ToolSurface


class _FakeTransport:
    instances: list["_FakeTransport"] = []

    def __init__(self, command, **kwargs):
        self.command = list(command)
        self.kwargs = kwargs
        self.entered = False
        self.exited = False
        type(self).instances.append(self)

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exited = True
        return False


def _fake_command_factory(**_kwargs):
    return ["codex", "app-server", "--stdio"]


@pytest.fixture(autouse=True)
def _clear_fake_transport_instances():
    _FakeTransport.instances.clear()
    yield
    _FakeTransport.instances.clear()


def test_generation_session_is_toolless_and_releases_process_slot() -> None:
    calls = []

    def command_factory(**kwargs):
        calls.append(kwargs)
        return ["codex", "app-server", "--stdio"]

    surface = ToolSurface.empty()
    factory = CodexAppServerRuntimeFactory(
        transport_factory=_FakeTransport,
        command_factory=command_factory,
    )

    with factory.session(
        mode="generation",
        request_timeout=5,
        tool_surface=surface,
        execution_profile=SimpleNamespace(name="must-not-cross-boundary"),
    ) as client:
        assert client.entered is True
        assert client.kwargs["tool_surface"].list_tools() == []
        assert client.kwargs["execution_profile"] is None

    assert client.exited is True
    assert calls and calls[0]["timeout"] == 5
    assert len(_FakeTransport.instances) == 1


def test_agent_session_preserves_surface_context_and_profile() -> None:
    surface = ToolSurface.empty()
    context = object()
    profile = object()
    factory = CodexAppServerRuntimeFactory(
        transport_factory=_FakeTransport,
        command_factory=_fake_command_factory,
    )

    with factory.session(
        mode="agent",
        request_timeout=5,
        tool_surface=surface,
        tool_context=context,
        execution_profile=profile,
        max_tool_calls=4,
    ) as client:
        assert client.kwargs["tool_surface"] is surface
        assert client.kwargs["tool_context"] is context
        assert client.kwargs["execution_profile"] is profile
        assert client.kwargs["max_tool_calls"] == 4


def test_runtime_slot_timeout_does_not_start_a_second_transport() -> None:
    factory = CodexAppServerRuntimeFactory(
        transport_factory=_FakeTransport,
        command_factory=_fake_command_factory,
        max_concurrency=1,
    )
    entered = threading.Event()
    release = threading.Event()

    def hold_session() -> None:
        with factory.session(mode="generation", request_timeout=5):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_session)
    thread.start()
    assert entered.wait(timeout=1)

    with pytest.raises(CodexAppServerError) as exc_info:
        with factory.session(mode="generation", request_timeout=0.05):
            raise AssertionError("second session should not start")

    assert exc_info.value.code == "timeout"
    assert len(_FakeTransport.instances) == 1
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_runtime_cancellation_before_slot_acquisition_fails_closed() -> None:
    factory = CodexAppServerRuntimeFactory(
        transport_factory=_FakeTransport,
        command_factory=_fake_command_factory,
    )
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(CodexAppServerError) as exc_info:
        with factory.session(mode="generation", request_timeout=1, cancel_event=cancel_event):
            raise AssertionError("cancelled request should not start")

    assert exc_info.value.code == "cancelled"
    assert not _FakeTransport.instances


def test_runtime_rejects_invalid_mode_and_concurrency() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        CodexAppServerRuntimeFactory(max_concurrency=0)

    factory = CodexAppServerRuntimeFactory(
        transport_factory=_FakeTransport,
        command_factory=_fake_command_factory,
    )
    with pytest.raises(ValueError, match="mode"):
        with factory.session(mode="other", request_timeout=1):
            pass
