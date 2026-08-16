# -*- coding: utf-8 -*-
"""Baseline gate for business-level model-generation bypasses.

This is intentionally a characterization gate for Task 1.  The allowlist
documents the existing direct provider/adapter call sites and prevents a new
one from being added silently.  Migration tasks must shrink this allowlist;
they must not widen it without an explicit design decision.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.generation_route_support import RecordingGenerationBackend


REPO_ROOT = Path(__file__).resolve().parents[1]
BUSINESS_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "bot", REPO_ROOT / "api")


@dataclass(frozen=True)
class RouteFinding:
    path: str
    line: int
    kind: str
    symbol: str


def _attribute_chain(node: ast.AST) -> list[str]:
    chain: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        chain.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        chain.append(current.id)
    return list(reversed(chain))


def _scan_file(path: Path) -> list[RouteFinding]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    relative_path = path.relative_to(REPO_ROOT).as_posix()
    # Keep the canonical name even when a module uses a patchable placeholder
    # and imports the real package lazily inside a function.
    litellm_names: set[str] = {"litellm"}
    router_names: set[str] = set()
    findings: list[RouteFinding] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "litellm" or alias.name.startswith("litellm."):
                    litellm_names.add(alias.asname or alias.name.split(".", 1)[0])
                    findings.append(RouteFinding(relative_path, node.lineno, "litellm_import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "litellm" or module.startswith("litellm."):
                for alias in node.names:
                    imported_name = alias.asname or alias.name
                    if alias.name == "Router":
                        router_names.add(imported_name)
                    findings.append(RouteFinding(relative_path, node.lineno, "litellm_import", f"{module}.{alias.name}"))
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "LLMToolAdapter":
                findings.append(RouteFinding(relative_path, node.lineno, "llm_tool_adapter", "LLMToolAdapter"))
                continue
            if isinstance(node.func, ast.Name) and node.func.id in router_names:
                findings.append(RouteFinding(relative_path, node.lineno, "litellm_router", node.func.id))
                continue
            chain = _attribute_chain(node.func)
            if len(chain) == 2 and chain[1] == "completion" and chain[0] in litellm_names:
                findings.append(RouteFinding(relative_path, node.lineno, "litellm_completion", ".".join(chain)))

    return findings


def _find_business_bypasses() -> list[RouteFinding]:
    findings: list[RouteFinding] = []
    for root in BUSINESS_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            findings.extend(_scan_file(path))
    return findings


# Baseline exceptions are deliberately path/kind scoped.  The exact line is
# not part of the contract, so harmless line movement does not create noise.
# Each entry has an owner and a migration conclusion for review visibility.
BASELINE_ALLOWLIST: dict[tuple[str, str], tuple[str, str]] = {
    ("src/llm/litellm_transport.py", "litellm_import"): (
        "LiteLLM provider adapter",
        "retain as the sole provider SDK seam",
    ),
    ("src/agent/llm_adapter.py", "litellm_import"): ("agent transport", "retain until AgentBackend migration"),
    ("src/agent/llm_adapter.py", "litellm_completion"): ("agent transport", "retain until AgentBackend migration"),
    ("src/agent/llm_adapter.py", "litellm_router"): ("agent transport", "retain until AgentBackend migration"),
    ("src/agent/factory.py", "llm_tool_adapter"): ("AgentBackend", "replace only when Agent path is migrated"),
    ("src/agent/chat_context.py", "litellm_import"): ("context summary", "migrate to GenerationBackend"),
    ("api/v1/endpoints/agent.py", "llm_tool_adapter"): ("Agent API compatibility", "replace when AgentBackend is complete"),
    ("src/services/image_stock_extractor.py", "litellm_import"): ("vision exception", "retain under VISION_* scope"),
    ("src/services/image_stock_extractor.py", "litellm_completion"): ("vision exception", "retain under VISION_* scope"),
    ("src/services/system_config_service.py", "litellm_import"): ("provider diagnostics", "retain as direct provider test"),
    ("src/services/system_config_service.py", "litellm_completion"): ("provider diagnostics", "retain as direct provider test"),
    ("src/config.py", "litellm_import"): ("provider metadata/config parsing", "retain outside business generation"),
    ("src/paper_account/controllers.py", "llm_tool_adapter"): ("Paper Agent compatibility", "replace when AgentBackend is complete"),
    ("bot/commands/ask.py", "llm_tool_adapter"): ("ask Agent compatibility", "classify as Agent or Generation"),
    ("bot/commands/research.py", "llm_tool_adapter"): ("Deep Research exception", "explicit unsupported until Agent parity"),
}


def test_route_inventory_has_no_unregistered_business_bypass() -> None:
    findings = _find_business_bypasses()
    unregistered = sorted(
        {
            (finding.path, finding.kind)
            for finding in findings
            if (finding.path, finding.kind) not in BASELINE_ALLOWLIST
        }
    )
    assert not unregistered, "New business generation bypasses require an explicit allowlist/design decision"


def test_route_inventory_allowlist_has_actionable_metadata() -> None:
    findings = {(finding.path, finding.kind) for finding in _find_business_bypasses()}
    stale = sorted(set(BASELINE_ALLOWLIST) - findings)
    assert not stale, "Remove stale route exceptions after the corresponding call site is migrated"
    assert all(owner and conclusion for owner, conclusion in BASELINE_ALLOWLIST.values())


def test_recording_generation_backend_records_effective_backend_and_call_count() -> None:
    backend = RecordingGenerationBackend(backend_id="codex_app_server", response_text="Codex result")

    result = backend.generate(
        "research prompt",
        {"max_output_tokens": 256},
        system_prompt="research system",
        audit_context={"entrypoint": "stock_analysis"},
    )

    assert result.backend == "codex_app_server"
    assert result.provider == "codex_app_server"
    assert len(backend.calls) == 1
    assert backend.calls[0]["audit_context"] == {"entrypoint": "stock_analysis"}


def test_recording_generation_backend_can_assert_fallback_attempts() -> None:
    primary = RecordingGenerationBackend(backend_id="codex_app_server", error=RuntimeError("primary failed"))
    fallback = RecordingGenerationBackend(backend_id="litellm", response_text="fallback result")

    with pytest.raises(RuntimeError, match="primary failed"):
        primary.generate("prompt", {})
    fallback.generate("prompt", {})

    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1
