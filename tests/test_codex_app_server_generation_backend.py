# -*- coding: utf-8 -*-
"""Contract tests for ordinary Codex App Server generation."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.agent.codex_app_server_transport import CodexAppServerError, TurnResult
from src.llm.codex_app_server_backend import CodexAppServerGenerationBackend
from src.llm.generation_backend import GenerationError, GenerationErrorCode


class _FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result or TurnResult(
            turn_id="turn-1",
            final_text="Codex answer",
            model="gpt-codex-test",
            usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        )
        self.error = error
        self.thread_calls: list[dict] = []
        self.turn_calls: list[dict] = []

    def start_thread(self, **kwargs):
        self.thread_calls.append(kwargs)
        return "thread-1"

    def run_turn(self, thread_id, text, timeout=None, cancel_event=None):
        self.turn_calls.append(
            {
                "thread_id": thread_id,
                "text": text,
                "timeout": timeout,
                "cancel_event": cancel_event,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


class _FakeRuntime:
    def __init__(self, client: _FakeClient):
        self.client = client
        self.session_calls: list[dict] = []

    @contextmanager
    def session(self, **kwargs):
        self.session_calls.append(kwargs)
        yield self.client


def _config(**overrides):
    values = {
        "generation_backend_timeout_seconds": 30,
        "generation_backend_max_output_bytes": 1024,
        "generation_backend_max_concurrency": 1,
        "codex_model": "gpt-codex-configured",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_generation_returns_final_text_model_usage_and_zero_tools() -> None:
    client = _FakeClient()
    runtime = _FakeRuntime(client)
    backend = CodexAppServerGenerationBackend(_config(), runtime_factory=runtime)
    progress: list[int] = []

    result = backend.generate(
        "write a summary",
        {"max_output_tokens": 256},
        system_prompt="Use only supplied evidence",
        stream=True,
        stream_progress_callback=progress.append,
    )

    assert result.text == "Codex answer"
    assert result.model == "gpt-codex-test"
    assert result.provider == "codex"
    assert result.backend == "codex_app_server"
    assert result.usage["total_tokens"] == 5
    assert progress == [0, len("Codex answer")]
    assert runtime.session_calls[0]["mode"] == "generation"
    assert runtime.session_calls[0].get("tool_surface") is None
    assert client.thread_calls[0]["tool_names"] == []
    assert client.thread_calls[0]["model"] == "gpt-codex-configured"
    assert client.thread_calls[0]["developer_instructions"] == "Use only supplied evidence"


def test_generation_honors_request_model_override_and_json_validator() -> None:
    client = _FakeClient(result=TurnResult(turn_id="turn-1", final_text='{"ok": true}'))
    runtime = _FakeRuntime(client)
    backend = CodexAppServerGenerationBackend(_config(codex_model=""), runtime_factory=runtime)

    result = backend.generate(
        "return JSON",
        {"model": "gpt-codex-request", "response_format": {"type": "json_object"}},
        response_validator=lambda text: (_ for _ in ()).throw(ValueError("not json"))
        if text != '{"ok": true}'
        else None,
    )

    assert result.text == '{"ok": true}'
    assert client.thread_calls[0]["model"] == "gpt-codex-request"


def test_generation_maps_validator_failure_to_invalid_json() -> None:
    client = _FakeClient()
    backend = CodexAppServerGenerationBackend(_config(), runtime_factory=_FakeRuntime(client))

    with pytest.raises(GenerationError) as exc_info:
        backend.generate("prompt", {}, response_validator=lambda _text: (_ for _ in ()).throw(ValueError("bad JSON")))

    assert exc_info.value.error_code is GenerationErrorCode.INVALID_JSON
    assert exc_info.value.stage == "validation"
    assert exc_info.value.fallbackable is False


@pytest.mark.parametrize(
    ("transport_code", "expected"),
    [
        ("login_required", GenerationErrorCode.LOGIN_REQUIRED),
        ("cancelled", GenerationErrorCode.CANCELLED),
        ("timeout", GenerationErrorCode.TIMEOUT),
        ("protocol_error", GenerationErrorCode.PROTOCOL_ERROR),
        ("output_too_large", GenerationErrorCode.OUTPUT_TOO_LARGE),
    ],
)
def test_generation_maps_transport_errors(transport_code, expected) -> None:
    client = _FakeClient(error=CodexAppServerError(transport_code, "safe transport message"))
    backend = CodexAppServerGenerationBackend(_config(), runtime_factory=_FakeRuntime(client))

    with pytest.raises(GenerationError) as exc_info:
        backend.generate("prompt", {})

    assert exc_info.value.error_code is expected
    assert exc_info.value.details["transport_code"] == transport_code
    assert "safe transport message" in exc_info.value.details["message"]
    assert "prompt" not in exc_info.value.details


def test_generation_rejects_empty_and_oversized_output() -> None:
    empty_client = _FakeClient(result=TurnResult(turn_id="turn-1", final_text=""))
    with pytest.raises(GenerationError) as empty_error:
        CodexAppServerGenerationBackend(_config(), runtime_factory=_FakeRuntime(empty_client)).generate("prompt", {})
    assert empty_error.value.error_code is GenerationErrorCode.EMPTY_OUTPUT

    oversized_client = _FakeClient(result=TurnResult(turn_id="turn-1", final_text="12345"))
    with pytest.raises(GenerationError) as oversized_error:
        CodexAppServerGenerationBackend(
            _config(generation_backend_max_output_bytes=4),
            runtime_factory=_FakeRuntime(oversized_client),
        ).generate("prompt", {})
    assert oversized_error.value.error_code is GenerationErrorCode.OUTPUT_TOO_LARGE


def test_generation_config_error_rejects_non_positive_timeout() -> None:
    backend = CodexAppServerGenerationBackend(
        _config(generation_backend_timeout_seconds=0),
        runtime_factory=_FakeRuntime(_FakeClient()),
    )
    error = backend.get_config_error()
    assert error is not None
    assert error.error_code is GenerationErrorCode.UNSAFE_CONFIG
