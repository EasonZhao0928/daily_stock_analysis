# -*- coding: utf-8 -*-
"""Screening L2 routing tests for the shared GenerationBackend seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

from src.llm.generation_backend import (
    GenerationCapabilities,
    GenerationError,
    GenerationErrorCode,
    GenerationResult,
)
from src.services.screening.models import Pick
from src.services.screening.ranker import rank_candidates_with_metadata


@dataclass
class _RecordingBackend:
    backend_id: str = "codex_app_server"
    response: str = '{"ranked":[{"code":"600519","llm_score":90,"confidence":0.8}]}'
    error: GenerationError | None = None
    calls: list[dict[str, object]] = field(default_factory=list)
    capabilities: GenerationCapabilities = GenerationCapabilities(
        supports_json=True,
        supports_tools=False,
        supports_stream=False,
        supports_vision=False,
        supports_health_check=True,
        supports_smoke_test=True,
    )

    def generate(self, prompt, generation_config, **kwargs):
        self.calls.append({"prompt": prompt, "generation_config": dict(generation_config), "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return GenerationResult(
            text=self.response,
            model=str(generation_config.get("model") or "codex"),
            provider="codex",
            backend=self.backend_id,
            usage={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        )


def _candidate() -> list[Pick]:
    return [Pick(rank=1, code="600519", name="贵州茅台", screen_score=88.0, final_score=88.0)]


def test_screening_ranker_uses_injected_codex_backend_without_direct_litellm_call() -> None:
    backend = _RecordingBackend()
    with patch("src.services.screening.ranker._call_llm", side_effect=AssertionError("LiteLLM bypass")):
        result = rank_candidates_with_metadata(
            _candidate(),
            "trend",
            "",
            "gpt-5.5",
            min_coverage=1.0,
            max_retries=0,
            generation_backend=backend,
        )

    assert result.ranked is True
    assert result.model_used == "gpt-5.5"
    assert len(backend.calls) == 1
    assert backend.calls[0]["generation_config"]["response_format"] == {"type": "json_object"}


def test_screening_ranker_only_uses_explicit_fallback_backend_for_fallbackable_error() -> None:
    primary = _RecordingBackend(
        error=GenerationError(
            error_code=GenerationErrorCode.LOGIN_REQUIRED,
            stage="execution",
            retryable=False,
            fallbackable=True,
            backend="codex_app_server",
            provider="codex",
        )
    )
    fallback = _RecordingBackend(
        backend_id="litellm",
        response='{"ranked":[{"code":"600519","llm_score":86,"confidence":0.7}]}',
    )

    with patch("src.services.screening.ranker._call_llm", side_effect=AssertionError("implicit fallback")):
        result = rank_candidates_with_metadata(
            _candidate(),
            "trend",
            "",
            "gpt-5.5",
            min_coverage=1.0,
            max_retries=0,
            generation_backend=primary,
            generation_fallback_backend=fallback,
        )

    assert result.ranked is True
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1


def test_screening_ranker_fail_closes_without_fallback_for_cancelled_generation() -> None:
    cancelled = _RecordingBackend(
        error=GenerationError(
            error_code=GenerationErrorCode.CANCELLED,
            stage="execution",
            retryable=False,
            fallbackable=False,
            backend="codex_app_server",
            provider="codex",
        )
    )
    fallback = _RecordingBackend(backend_id="litellm")

    with patch("src.services.screening.ranker._call_llm", side_effect=AssertionError("implicit fallback")):
        result = rank_candidates_with_metadata(
            _candidate(),
            "trend",
            "",
            "gpt-5.5",
            min_coverage=1.0,
            max_retries=0,
            generation_backend=cancelled,
            generation_fallback_backend=fallback,
        )

    assert result.ranked is False
    assert result.failure_reason == "call_failed"
    assert len(fallback.calls) == 0


def test_research_command_reports_unsupported_capability_for_codex_agent() -> None:
    from bot.commands.research import ResearchCommand
    from bot.models import BotMessage, ChatType

    config = SimpleNamespace(agent_mode=True, agent_backend="codex_app_server")
    message = BotMessage(
        platform="feishu",
        message_id="m1",
        user_id="u1",
        user_name="tester",
        chat_id="c1",
        chat_type=ChatType.PRIVATE,
        content="/research 600519",
        raw_content="/research 600519",
    )
    with patch("bot.commands.research.get_config", return_value=config):
        response = ResearchCommand().execute(message, ["600519"])

    assert "unsupported_capability" in response.text
