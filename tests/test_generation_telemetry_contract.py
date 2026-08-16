# -*- coding: utf-8 -*-
"""Stable generation errors and backend attribution in run diagnostics."""

from __future__ import annotations

from src.llm.generation_backend import GenerationError, GenerationErrorCode
from src.services.run_diagnostics import (
    activate_run_diagnostic_context,
    current_diagnostic_snapshot,
    record_llm_run,
    reset_run_diagnostic_context,
)


def test_generation_error_envelope_redacts_sensitive_details() -> None:
    error = GenerationError(
        error_code=GenerationErrorCode.LOGIN_REQUIRED,
        stage="execution",
        retryable=False,
        fallbackable=False,
        backend="codex_app_server",
        details={
            "message": "Authorization: Bearer sk-live-secret",
            "prompt": "never expose this prompt",
            "transport_code": "login_required",
        },
    )

    payload = error.to_dict()

    assert payload["code"] == "login_required"
    assert payload["error_code"] == "login_required"
    assert payload["user_message"]
    assert payload["details"]["message"] == "Authorization: Bearer <redacted>"
    assert payload["details"]["prompt"] == "<redacted>"
    assert "sk-live-secret" not in str(payload)
    assert "never expose this prompt" not in str(payload)


def test_llm_run_records_backend_attempt_and_unknown_codex_cost() -> None:
    token = activate_run_diagnostic_context(trace_id="trace-generation")
    try:
        record_llm_run(
            success=True,
            provider="codex_app_server",
            model="gpt-codex-test",
            call_type="analysis",
            business_entry="stock_analysis",
            primary_backend="codex_app_server",
            effective_backend="codex_app_server",
            attempt=1,
            tokens=42,
            usage_available=True,
            cost_status="unknown",
            duration_ms=123,
        )
        snapshot = current_diagnostic_snapshot()
    finally:
        reset_run_diagnostic_context(token)

    run = snapshot["llm_runs"][0]
    assert run["trace_id"] == "trace-generation"
    assert run["business_entry"] == "stock_analysis"
    assert run["primary_backend"] == "codex_app_server"
    assert run["effective_backend"] == "codex_app_server"
    assert run["attempt"] == 1
    assert run["tokens"] == 42
    assert run["usage_available"] is True
    assert run["cost_status"] == "unknown"
    assert run["duration_ms"] == 123
