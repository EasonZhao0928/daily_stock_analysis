# -*- coding: utf-8 -*-
"""Test-only helpers for generation route characterization.

The helper deliberately records the backend contract rather than constructing a
real provider client.  Later Codex routing tests can reuse it to prove which
backend was selected and whether a fallback was attempted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from src.llm.generation_backend import (
    GenerationBackend,
    GenerationCapabilities,
    GenerationResult,
)


@dataclass
class RecordingGenerationBackend(GenerationBackend):
    """Deterministic GenerationBackend double that records every invocation."""

    backend_id: str = "recording"
    response_text: str = "recorded response"
    response_model: str = "recording-model"
    response_usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[BaseException] = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    response_factory: Optional[Callable[[str, Dict[str, Any]], str]] = None

    capabilities = GenerationCapabilities(
        supports_json=True,
        supports_tools=False,
        supports_stream=True,
        supports_vision=False,
        supports_health_check=True,
        supports_smoke_test=True,
    )

    def generate(
        self,
        prompt: str,
        generation_config: Dict[str, Any],
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback=None,
        response_validator=None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> GenerationResult:
        self.calls.append(
            {
                "prompt": prompt,
                "generation_config": dict(generation_config),
                "system_prompt": system_prompt,
                "stream": stream,
                "stream_progress_callback": stream_progress_callback,
                "response_validator": response_validator,
                "audit_context": dict(audit_context or {}),
            }
        )
        if self.error is not None:
            raise self.error

        text = self.response_text
        if self.response_factory is not None:
            text = self.response_factory(prompt, generation_config)
        if response_validator is not None:
            response_validator(text)
        if stream_progress_callback is not None:
            stream_progress_callback(len(text))
        return GenerationResult(
            text=text,
            model=self.response_model,
            provider=self.backend_id,
            backend=self.backend_id,
            usage=dict(self.response_usage),
        )

