# -*- coding: utf-8 -*-
"""Task 5 characterization tests for the unified Generation route.

These tests deliberately stop at the business seams: no real market provider,
LiteLLM credential, or Codex account is required.  They prove that stock
analysis, market review, and the runtime scheduler carry the same configured
Generation backend and that a single analyzer does not manufacture a new
backend for each prompt.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.generation_route_support import RecordingGenerationBackend


def _codex_config() -> SimpleNamespace:
    return SimpleNamespace(
        generation_backend="codex_app_server",
        generation_fallback_backend="",
        generation_backend_timeout_seconds=30,
        generation_backend_max_output_bytes=4096,
        generation_backend_max_concurrency=1,
        codex_model="gpt-5.5",
        litellm_model="",
        litellm_fallback_models=[],
        llm_model_list=[],
        llm_blocks_legacy_fallback=False,
        llm_channel_config_issues=[],
        report_language="zh",
    )


def test_analyzer_reuses_one_factory_backend_for_stock_and_market_generation() -> None:
    from src.analyzer import GeminiAnalyzer

    config = _codex_config()
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._config_override = config
    analyzer._generation_backend_cache = {}
    analyzer._litellm_available = False
    analyzer._router = None
    backend = RecordingGenerationBackend(
        backend_id="codex_app_server",
        response_text="Codex response",
    )

    with patch("src.analyzer.create_generation_backend", return_value=backend) as factory:
        stock_backend = analyzer._get_generation_backend()
        market_backend = analyzer._get_generation_backend()

    assert stock_backend is backend
    assert market_backend is backend
    factory.assert_called_once_with(
        "codex_app_server",
        config=config,
        litellm_completion_callable=analyzer._call_litellm_impl,
    )


def test_runtime_scheduler_passes_codex_generation_config_to_scheduled_runner() -> None:
    from src.services.runtime_scheduler import RuntimeSchedulerService

    config = _codex_config()
    runner = MagicMock(return_value=True)
    service = RuntimeSchedulerService(
        config_provider=lambda: config,
        task_runner=runner,
        owns_schedule=False,
    )
    # Scheduled runs intentionally reload runtime configuration.  Keep this
    # characterization test deterministic by replacing that reload seam with
    # the explicit Codex draft used by the test.
    service._reload_config = lambda: config

    assert service._run_analysis_once(["600519"])
    runner.assert_called_once()
    effective_config = runner.call_args.args[0]
    assert effective_config.generation_backend == "codex_app_server"
    assert effective_config.generation_fallback_backend == ""
    assert effective_config.litellm_model == ""


def test_market_review_runtime_keeps_codex_analyzer_instead_of_template_only_path() -> None:
    from src.core.market_review_runtime import build_market_review_runtime

    config = _codex_config()
    notifier = MagicMock()
    analyzer = MagicMock()
    analyzer.is_available.return_value = True

    with patch("src.analyzer.GeminiAnalyzer", return_value=analyzer), \
         patch("src.notification.NotificationService", return_value=notifier), \
         patch("src.search_service.SearchService"):
        _notifier, runtime_analyzer, _search = build_market_review_runtime(config)

    assert runtime_analyzer is analyzer
    analyzer.is_available.assert_called_once_with()
