# -*- coding: utf-8 -*-
"""Provider-only LiteLLM transport primitives.

The stock and market analyzers own prompt construction and validation, but
must not import a provider SDK or choose a provider transport themselves.
Keeping these two small primitives here gives the ``LiteLLMGenerationBackend``
and the legacy compatibility path one provider seam.  The module is
deliberately not a generation orchestrator; it only exposes the existing
completion and Router implementations.
"""

from __future__ import annotations

import importlib
from typing import Any

import litellm
from litellm import Router


def completion(**kwargs: Any) -> Any:
    """Dispatch one completion through LiteLLM.

    A wrapper (instead of exposing the SDK call at business call sites) also
    keeps the existing test seam intact: tests that patch the shared LiteLLM
    module continue to intercept this call.
    """

    return provider_module().completion(**kwargs)


def build_router(*, router_cls: Any = None, **kwargs: Any) -> Any:
    """Build LiteLLM's Router using the provider adapter boundary.

    ``router_cls`` exists solely for the long-standing analyzer test seam;
    normal callers use the provider's Router class.
    """

    return (router_cls or Router)(**kwargs)


def provider_module() -> Any:
    """Return the underlying module for legacy test compatibility only."""

    # Resolve lazily so request-scoped test doubles and embedding applications
    # that provide LiteLLM through ``sys.modules`` retain the old seam.
    return importlib.import_module("litellm")


__all__ = ["build_router", "completion", "provider_module"]
