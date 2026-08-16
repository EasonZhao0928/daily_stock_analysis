# -*- coding: utf-8 -*-
"""普通文本/JSON Generation adapter backed by the official Codex App Server."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from src.agent.codex_app_server_runtime import CodexAppServerRuntimeFactory
from src.agent.codex_app_server_transport import (
    MAX_TURN_OUTPUT_BYTES,
    CodexAppServerError,
)
from src.agent.tools.execution import redact_diagnostic_value
from src.llm.generation_backend import (
    GenerationBackend,
    GenerationCapabilities,
    GenerationError,
    GenerationErrorCode,
    GenerationResult,
)


CODEX_APP_SERVER_BACKEND_ID = "codex_app_server"
DEFAULT_CODEX_TIMEOUT_SECONDS = 300
DEFAULT_CODEX_MAX_OUTPUT_BYTES = 1024 * 1024
MAX_CODEX_TIMEOUT_SECONDS = 3600

_BASE_INSTRUCTIONS = (
    "You are the DSA ordinary-generation runtime. Return only the requested final answer. "
    "Do not use tools, modify files, request approval, access MCP servers, or reveal hidden reasoning."
)

_ERROR_CODE_MAP = {
    "command_not_found": GenerationErrorCode.COMMAND_NOT_FOUND,
    "command_not_executable": GenerationErrorCode.COMMAND_NOT_EXECUTABLE,
    "login_required": GenerationErrorCode.LOGIN_REQUIRED,
    "rate_limit_exceeded": GenerationErrorCode.RATE_LIMIT_EXCEEDED,
    "cancelled": GenerationErrorCode.CANCELLED,
    "timeout": GenerationErrorCode.TIMEOUT,
    "output_too_large": GenerationErrorCode.OUTPUT_TOO_LARGE,
    "protocol_error": GenerationErrorCode.PROTOCOL_ERROR,
    "process_failed": GenerationErrorCode.PROCESS_FAILED,
    "process_exit": GenerationErrorCode.PROCESS_FAILED,
    "process_exited": GenerationErrorCode.PROCESS_FAILED,
    "capability_unsupported": GenerationErrorCode.CAPABILITY_UNSUPPORTED,
    "unsupported_platform": GenerationErrorCode.CAPABILITY_UNSUPPORTED,
    "platform_unsupported": GenerationErrorCode.CAPABILITY_UNSUPPORTED,
    "resource_cleanup_failed": GenerationErrorCode.PROCESS_FAILED,
}


def _positive_int(value: Any, default: int, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed <= 0:
        parsed = default
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


class CodexAppServerGenerationBackend(GenerationBackend):
    """Run ordinary text/JSON generation in a zero-tool ephemeral thread."""

    backend_id = CODEX_APP_SERVER_BACKEND_ID
    capabilities = GenerationCapabilities(
        supports_json=True,
        supports_tools=False,
        supports_stream=True,
        supports_vision=False,
        supports_health_check=True,
        supports_smoke_test=True,
    )

    def __init__(
        self,
        config: Any,
        *,
        runtime_factory: Optional[CodexAppServerRuntimeFactory] = None,
    ) -> None:
        self._config = config
        max_concurrency = _positive_int(
            getattr(config, "generation_backend_max_concurrency", 1),
            1,
            maximum=16,
        )
        self._runtime_factory = runtime_factory or CodexAppServerRuntimeFactory(
            max_concurrency=max_concurrency,
        )

    def get_config_error(self) -> Optional[GenerationError]:
        timeout = getattr(self._config, "generation_backend_timeout_seconds", DEFAULT_CODEX_TIMEOUT_SECONDS)
        try:
            timeout_value = int(timeout)
        except (TypeError, ValueError):
            timeout_value = 0
        if timeout_value <= 0 or timeout_value > MAX_CODEX_TIMEOUT_SECONDS:
            return self._error(
                GenerationErrorCode.UNSAFE_CONFIG,
                stage="configuration",
                retryable=False,
                fallbackable=False,
                details={"reason": "invalid_timeout", "timeout_seconds": timeout},
            )
        return None

    def generate(
        self,
        prompt: str,
        generation_config: Dict[str, Any],
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> GenerationResult:
        del audit_context
        timeout_seconds = _positive_int(
            getattr(self._config, "generation_backend_timeout_seconds", DEFAULT_CODEX_TIMEOUT_SECONDS),
            DEFAULT_CODEX_TIMEOUT_SECONDS,
            maximum=MAX_CODEX_TIMEOUT_SECONDS,
        )
        max_output_bytes = _positive_int(
            getattr(self._config, "generation_backend_max_output_bytes", DEFAULT_CODEX_MAX_OUTPUT_BYTES),
            DEFAULT_CODEX_MAX_OUTPUT_BYTES,
            maximum=MAX_TURN_OUTPUT_BYTES,
        )
        model = str(
            generation_config.get("model")
            or getattr(self._config, "codex_model", "")
            or ""
        ).strip() or None
        deadline = time.monotonic() + timeout_seconds
        self._emit_progress(stream_progress_callback, 0)

        try:
            with self._runtime_factory.session(
                mode="generation",
                request_timeout=timeout_seconds,
                deadline=deadline,
            ) as client:
                developer_instructions = str(system_prompt or "").strip()
                thread_id = client.start_thread(
                    tool_names=[],
                    base_instructions=_BASE_INSTRUCTIONS,
                    developer_instructions=developer_instructions,
                    model=model,
                )
                turn = client.run_turn(
                    thread_id,
                    str(prompt or ""),
                    timeout=max(0.001, deadline - time.monotonic()),
                )
        except CodexAppServerError as exc:
            raise self._from_transport_error(exc) from exc
        except OSError as exc:
            raise self._error(
                GenerationErrorCode.PROCESS_FAILED,
                stage="execution",
                retryable=False,
                fallbackable=True,
                details={"reason": "process_start_failed", "error": redact_diagnostic_value(str(exc), limit=200)},
            ) from exc

        text = str(turn.final_text or "").strip()
        diagnostics = {
            "thread_id": thread_id,
            "turn_id": turn.turn_id,
            "tools_enabled": False,
            "stream_degraded": bool(stream),
            "max_output_bytes": max_output_bytes,
        }
        if len(text.encode("utf-8")) > max_output_bytes:
            raise self._error(
                GenerationErrorCode.OUTPUT_TOO_LARGE,
                stage="validation",
                retryable=False,
                fallbackable=True,
                details={**diagnostics, "reason": "final_output_too_large"},
            )
        if not text:
            raise self._error(
                GenerationErrorCode.EMPTY_OUTPUT,
                stage="execution",
                retryable=True,
                fallbackable=True,
                details={**diagnostics, "reason": "empty_final_answer"},
            )

        if response_validator is not None:
            try:
                response_validator(text)
            except GenerationError as exc:
                # A validator failure belongs to the current prompt/response
                # contract.  It must not silently switch providers, even when
                # an older validator marks the error as retryable for an
                # in-provider model retry.
                raise self._error(
                    exc.error_code,
                    stage="validation",
                    retryable=exc.retryable,
                    fallbackable=False,
                    details={
                        **diagnostics,
                        "reason": "response_validator_failed",
                        "validator_error": exc.error_code.value,
                    },
                ) from exc
            except Exception as exc:
                raise self._error(
                    GenerationErrorCode.INVALID_JSON,
                    stage="validation",
                    retryable=True,
                    fallbackable=False,
                    details={
                        **diagnostics,
                        "reason": redact_diagnostic_value(str(exc), limit=300) or "invalid_output",
                    },
                ) from exc

        self._emit_progress(stream_progress_callback, len(text))
        usage = dict(turn.usage or {})
        return GenerationResult(
            text=text,
            model=str(turn.model or model or "codex"),
            provider="codex",
            backend=self.backend_id,
            usage=usage,
            diagnostics=diagnostics,
        )

    @classmethod
    def _from_transport_error(cls, error: CodexAppServerError) -> GenerationError:
        code = _ERROR_CODE_MAP.get(error.code, GenerationErrorCode.UNKNOWN_BACKEND_ERROR)
        non_fallbackable = {
            GenerationErrorCode.CANCELLED,
            GenerationErrorCode.CAPABILITY_UNSUPPORTED,
            GenerationErrorCode.INVALID_JSON,
            GenerationErrorCode.SCHEMA_VALIDATION_FAILED,
            GenerationErrorCode.UNSAFE_CONFIG,
        }
        return cls._error_static(
            code,
            stage="execution",
            retryable=code in {GenerationErrorCode.TIMEOUT, GenerationErrorCode.PROTOCOL_ERROR},
            fallbackable=code not in non_fallbackable,
            details={
                "transport_code": error.code,
                "turn_started": bool(error.turn_started),
                "message": redact_diagnostic_value(str(error), limit=500),
            },
        )

    @staticmethod
    def _error_static(
        error_code: GenerationErrorCode,
        *,
        stage: str,
        retryable: bool,
        fallbackable: bool,
        details: Dict[str, Any],
    ) -> GenerationError:
        return GenerationError(
            error_code=error_code,
            stage=stage,
            retryable=retryable,
            fallbackable=fallbackable,
            backend=CODEX_APP_SERVER_BACKEND_ID,
            provider="codex",
            details=details,
        )

    def _error(
        self,
        error_code: GenerationErrorCode,
        *,
        stage: str,
        retryable: bool,
        fallbackable: bool,
        details: Dict[str, Any],
    ) -> GenerationError:
        return self._error_static(
            error_code,
            stage=stage,
            retryable=retryable,
            fallbackable=fallbackable,
            details=details,
        )

    @staticmethod
    def _emit_progress(callback: Optional[Callable[[int], None]], value: int) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception:
            return
