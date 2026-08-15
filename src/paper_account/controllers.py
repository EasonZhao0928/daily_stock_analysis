# -*- coding: utf-8 -*-
"""Deterministic Paper decision controllers and strict model adapter.

Controllers only return a structured Proposal candidate.  They never submit
orders, mutate a portfolio, or receive a database handle.  The service layer
owns mandate evaluation and persistence.
"""

from __future__ import annotations

import inspect
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence

from .mandate import PaperMandateError, PROPOSAL_FIELDS, validate_proposal


class ControllerError(ValueError):
    """A controller could not produce one safe Proposal candidate."""


class StructuredOutputError(ControllerError):
    """Model output was malformed, unsafe, or exceeded the timeout."""


PROPOSAL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["symbol", "market", "side", "order_type", "rationale"],
    "properties": {
        "symbol": {"type": "string"},
        "market": {"type": "string", "enum": ["cn", "hk", "us", "jp", "kr", "tw"]},
        "side": {"type": "string", "enum": ["buy", "sell", "hold", "reduce"]},
        "order_type": {"type": "string", "enum": ["market", "limit", "stop"]},
        "quantity": {"type": ["number", "null"]},
        "target_weight": {"type": ["number", "null"]},
        "limit_price": {"type": ["number", "null"]},
        "stop_price": {"type": ["number", "null"]},
        "rationale": {"type": "string", "maxLength": 4000},
        "evidence_refs": {"type": "array", "items": {"type": "object"}},
    },
}


@dataclass(frozen=True)
class ModelTrace:
    """Safe provider metadata; never contains prompts or hidden reasoning."""

    backend: Optional[str] = None
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    skill_version: Optional[str] = None
    tool_trace: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = None  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "skill_version": self.skill_version,
            "tool_trace": list(self.tool_trace),
            "diagnostics": dict(self.diagnostics or {}),
        }


class ProposalController(Protocol):
    def generate(self, observation: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        ...

    def trace(self) -> Mapping[str, Any]:
        ...


def _call_model(model: Callable[..., Any], observation: Mapping[str, Any]) -> Any:
    """Call a fake/real model without allowing arbitrary tool arguments."""

    try:
        signature = inspect.signature(model)
        parameters = signature.parameters
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            return model(observation=observation, schema=PROPOSAL_SCHEMA)
        if "observation" in parameters:
            kwargs: Dict[str, Any] = {"observation": observation}
            if "schema" in parameters:
                kwargs["schema"] = PROPOSAL_SCHEMA
            return model(**kwargs)
        if len(parameters) >= 2:
            return model(observation, PROPOSAL_SCHEMA)
    except (TypeError, ValueError):
        # Some provider callables do not expose a Python signature.  The
        # narrow one-argument fallback still keeps the output boundary strict.
        pass
    return model(observation)


class StructuredProposalAdapter:
    """Adapt a model transport to exactly one validated Proposal object."""

    def __init__(
        self,
        model: Callable[..., Any],
        *,
        backend: Optional[str] = None,
        model_name: Optional[str] = None,
        prompt_version: Optional[str] = None,
        skill_version: Optional[str] = None,
        tool_trace: Sequence[str] = (),
        timeout_seconds: float = 30.0,
    ) -> None:
        if not callable(model):
            raise TypeError("model must be callable")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.model = model
        self._trace = ModelTrace(
            backend=backend,
            model=model_name,
            prompt_version=prompt_version,
            skill_version=skill_version,
            tool_trace=tuple(str(item) for item in tool_trace),
            diagnostics={},
        )
        self.timeout_seconds = float(timeout_seconds)

    def trace(self) -> Mapping[str, Any]:
        return self._trace.to_dict()

    def generate(self, observation: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(observation, Mapping):
            raise StructuredOutputError("model observation must be an object")
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_call_model, self.model, observation)
        try:
            raw = future.result(timeout=self.timeout_seconds)
        except FutureTimeout as exc:
            future.cancel()
            raise StructuredOutputError("model_timeout") from exc
        except Exception as exc:
            raise StructuredOutputError(f"model_call_failed: {type(exc).__name__}") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if not isinstance(raw, Mapping):
            raise StructuredOutputError("model output must be one Proposal object")
        unknown = sorted(set(raw) - PROPOSAL_FIELDS)
        if unknown:
            raise StructuredOutputError(f"model output contains unknown fields: {', '.join(unknown)}")
        # A list, wrapper ({proposal: ...}), tool call, or free-text response
        # never reaches validate_proposal and therefore cannot be interpreted.
        try:
            return validate_proposal(raw)
        except PaperMandateError as exc:
            raise StructuredOutputError(str(exc)) from exc


class ShadowController:
    """Use a deterministic Shadow signal/proposal function."""

    def __init__(self, proposal_fn: Optional[Callable[[Mapping[str, Any]], Any]] = None) -> None:
        self.proposal_fn = proposal_fn

    def generate(self, observation: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        candidate: Any = self.proposal_fn(observation) if self.proposal_fn else None
        if candidate is None:
            for signal in observation.get("shadow_signals", ()) or ():
                if not isinstance(signal, Mapping) or signal.get("status") not in {"eligible", "triggered", "active"}:
                    continue
                candidate = signal.get("proposal") or signal.get("paper_proposal")
                if candidate is not None:
                    break
        if candidate is None:
            return None
        try:
            return validate_proposal(candidate)
        except PaperMandateError as exc:
            raise ControllerError(str(exc)) from exc

    def trace(self) -> Mapping[str, Any]:
        return {"controller": "shadow", "tool_trace": []}


class LLMController:
    """Controller backed by a strict structured-output adapter."""

    def __init__(self, adapter: StructuredProposalAdapter) -> None:
        self.adapter = adapter

    def generate(self, observation: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        return self.adapter.generate(observation)

    def trace(self) -> Mapping[str, Any]:
        payload = dict(self.adapter.trace())
        payload["controller"] = "llm"
        return payload


def _decode_structured_proposal(raw: Any) -> Mapping[str, Any]:
    """Decode the one JSON Proposal object returned by a text backend."""
    if isinstance(raw, Mapping):
        return raw
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise StructuredOutputError("model output must be a JSON Proposal object") from exc
    if not isinstance(value, Mapping):
        raise StructuredOutputError("model output must be one Proposal object")
    return value


def _paper_prompt(observation: Mapping[str, Any]) -> str:
    encoded = json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        "Produce exactly one JSON object matching this schema and no markdown or commentary: "
        f"{json.dumps(PROPOSAL_SCHEMA, ensure_ascii=False, sort_keys=True)}\n"
        "Use only the frozen observation below; do not call live market/news tools.\n"
        f"FROZEN_OBSERVATION={encoded}"
    )


def build_paper_controller(
    controller_kind: str,
    *,
    config: Any = None,
    timeout_seconds: float = 30.0,
) -> ProposalController:
    """Build the production controller used by API and scheduler cycles.

    The factory stays behind the service seam so tests and offline deployments
    can inject a deterministic controller. ``llm`` uses the configured DSA
    backend (LiteLLM or Codex App Server); ``hybrid`` additionally requires the
    deterministic Shadow controller to agree with it.
    """
    normalized = str(controller_kind or "").strip().lower()
    if normalized == "shadow":
        return ShadowController()
    if normalized not in {"llm", "hybrid"}:
        raise ControllerError(f"unsupported controller_kind: {controller_kind}")

    if config is None:
        from src.config import get_config

        config = get_config()
    from src.agent.agent_backend import AgentRunRequest, resolve_agent_backend_id

    backend_id = resolve_agent_backend_id(config)
    model_name = str(
        getattr(config, "agent_litellm_model", "")
        or getattr(config, "litellm_model", "")
        or ""
    )
    if backend_id == "litellm":
        from src.agent.llm_adapter import LLMToolAdapter

        transport = LLMToolAdapter(config)

        def model(observation: Mapping[str, Any], **_kwargs: Any) -> Mapping[str, Any]:
            response = transport.call_text(
                [
                    {
                        "role": "system",
                        "content": "You are the DSA Paper Account proposal controller. Return strict JSON only.",
                    },
                    {"role": "user", "content": _paper_prompt(observation)},
                ],
                temperature=0,
                max_tokens=1200,
                timeout=timeout_seconds,
            )
            return _decode_structured_proposal(getattr(response, "content", response))

        adapter = StructuredProposalAdapter(
            model,
            backend="litellm",
            model_name=model_name or None,
            prompt_version="paper-proposal-v1",
            timeout_seconds=timeout_seconds,
        )
    else:
        from src.agent.codex_agent_backend import CodexAgentBackend
        from src.agent.factory import get_paper_tool_registry
        from src.agent.tool_surface import ToolSurface
        from src.agent.tools.registry import ExecutionProfile

        surface = ToolSurface(
            get_paper_tool_registry(),
            default_profile=ExecutionProfile.PAPER_PROPOSAL,
        )
        backend = CodexAgentBackend(surface, config)

        def model(observation: Mapping[str, Any], **_kwargs: Any) -> Mapping[str, Any]:
            result = backend.run(
                AgentRunRequest(
                    system_prompt=(
                        "You are the DSA Paper Account proposal controller. "
                        "Return exactly one JSON Proposal object and do not execute orders."
                    ),
                    history_messages=[],
                    user_message=_paper_prompt(observation),
                    session_id="paper-proposal",
                    stock_scope=None,
                    max_steps=max(1, int(getattr(config, "agent_max_steps", 8) or 8)),
                    max_wall_clock_seconds=timeout_seconds,
                )
            )
            if not result.success:
                raise StructuredOutputError(result.error_code or "codex_paper_controller_failed")
            return _decode_structured_proposal(result.final_answer)

        adapter = StructuredProposalAdapter(
            model,
            backend="codex_app_server",
            model_name=model_name or "Codex",
            prompt_version="paper-proposal-v1",
            timeout_seconds=timeout_seconds,
        )

    llm = LLMController(adapter)
    if normalized == "llm":
        return llm
    return HybridController(ShadowController(), llm)


def _intersection(left: Mapping[str, Any], right: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a conservative intersection, never the wider candidate."""

    left_normalized = validate_proposal(left)
    right_normalized = validate_proposal(right)
    for key in ("symbol", "market", "side", "order_type"):
        if left_normalized[key] != right_normalized[key]:
            return None
    quantity = left_normalized["quantity"]
    if quantity is not None and right_normalized["quantity"] is not None:
        quantity = min(float(quantity), float(right_normalized["quantity"]))
    elif quantity is not None or right_normalized["quantity"] is not None:
        return None
    target_weight = left_normalized["target_weight"]
    if target_weight is not None and right_normalized["target_weight"] is not None:
        target_weight = min(float(target_weight), float(right_normalized["target_weight"]))
    elif target_weight is not None or right_normalized["target_weight"] is not None:
        return None
    result = dict(left_normalized)
    result["quantity"] = quantity
    result["target_weight"] = target_weight
    result["evidence_refs"] = sorted(
        left_normalized["evidence_refs"] + right_normalized["evidence_refs"],
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
    )
    result["rationale"] = (
        f"Shadow/LLM intersection: {left_normalized['rationale']} | {right_normalized['rationale']}"
    )[:4000]
    return validate_proposal(result)


class HybridController:
    """Require both controllers to agree; the narrower size wins."""

    def __init__(self, shadow: ProposalController, llm: ProposalController) -> None:
        self.shadow = shadow
        self.llm = llm

    def generate(self, observation: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        shadow_candidate = self.shadow.generate(observation)
        llm_candidate = self.llm.generate(observation)
        if shadow_candidate is None or llm_candidate is None:
            return None
        return _intersection(shadow_candidate, llm_candidate)

    def trace(self) -> Mapping[str, Any]:
        return {
            "controller": "hybrid",
            "shadow": dict(self.shadow.trace()),
            "llm": dict(self.llm.trace()),
        }


__all__ = [
    "ControllerError",
    "HybridController",
    "LLMController",
    "ModelTrace",
    "PROPOSAL_SCHEMA",
    "ProposalController",
    "ShadowController",
    "StructuredOutputError",
    "StructuredProposalAdapter",
    "build_paper_controller",
]
