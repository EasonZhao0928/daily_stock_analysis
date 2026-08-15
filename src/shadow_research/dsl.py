# -*- coding: utf-8 -*-
"""Safe, bounded Shadow Rule DSL compiler.

The compiler walks a JSON/YAML-shaped mapping and builds Python callables from
an allowlisted operator table.  It never evaluates user-provided Python,
attribute paths, imports, shell commands or arbitrary expressions.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


class ShadowRuleError(ValueError):
    """Invalid or unsafe Shadow Rule definition."""


FEATURES = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pct_chg",
        "ma5",
        "ma10",
        "ma20",
        "rsi14",
        "macd",
        "volume_ratio",
        "turnover_rate",
        "pe_ratio",
        "pb_ratio",
        "main_net_inflow",
        "holding_change",
        "days_since_signal",
    }
)
OPERATORS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "in", "between", "exists"})
MAX_DEPTH = 6
MAX_NODES = 40


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ShadowRuleError("NaN/Infinity is not allowed in a shadow rule")
        return value
    raise ShadowRuleError(f"unsupported DSL value type: {type(value).__name__}")


def _compare(operator: str, actual: Any, expected: Any) -> bool:
    if operator == "exists":
        return actual is not None
    if actual is None:
        return False
    try:
        if operator == "eq":
            return actual == expected
        if operator == "neq":
            return actual != expected
        if operator == "gt":
            return actual > expected
        if operator == "gte":
            return actual >= expected
        if operator == "lt":
            return actual < expected
        if operator == "lte":
            return actual <= expected
        if operator == "in":
            return actual in expected
        if operator == "between":
            return len(expected) == 2 and expected[0] <= actual <= expected[1]
    except (TypeError, ValueError, IndexError):
        return False
    raise ShadowRuleError(f"unsupported operator: {operator}")


@dataclass(frozen=True)
class CompiledShadowRule:
    """Compiled rule with deterministic identity and feature references."""

    version: str
    spec: Mapping[str, Any]
    features: tuple[str, ...]
    rule_hash: str
    _predicate: Callable[[Mapping[str, Any]], bool]

    def evaluate(self, features: Mapping[str, Any]) -> bool:
        if not isinstance(features, Mapping):
            return False
        return bool(self._predicate(features))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "spec": dict(self.spec),
            "features": list(self.features),
            "rule_hash": self.rule_hash,
        }


def compile_shadow_rule(spec: Mapping[str, Any], *, version: str = "1") -> CompiledShadowRule:
    """Validate and compile a rule spec into a bounded predicate."""

    if not isinstance(spec, Mapping) or not spec:
        raise ShadowRuleError("rule must be a non-empty object")
    canonical = _canonical(spec)
    features: set[str] = set()
    nodes = 0

    def compile_node(node: Mapping[str, Any], depth: int = 0) -> Callable[[Mapping[str, Any]], bool]:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES:
            raise ShadowRuleError(f"rule exceeds max nodes ({MAX_NODES})")
        if depth > MAX_DEPTH:
            raise ShadowRuleError(f"rule exceeds max depth ({MAX_DEPTH})")
        if not isinstance(node, Mapping):
            raise ShadowRuleError("rule nodes must be objects")
        if "all" in node or "any" in node:
            key = "all" if "all" in node else "any"
            children = node.get(key)
            if not isinstance(children, Sequence) or isinstance(children, (str, bytes)) or not children:
                raise ShadowRuleError(f"{key} must be a non-empty array")
            predicates = [compile_node(child, depth + 1) for child in children]
            if key == "all":
                return lambda row: all(predicate(row) for predicate in predicates)
            return lambda row: any(predicate(row) for predicate in predicates)
        if "not" in node:
            child = node.get("not")
            if not isinstance(child, Mapping):
                raise ShadowRuleError("not must contain an object")
            predicate = compile_node(child, depth + 1)
            return lambda row: not predicate(row)

        feature = str(node.get("feature") or "").strip()
        operator = str(node.get("op", node.get("operator", "")) or "").strip().lower()
        if feature not in FEATURES:
            raise ShadowRuleError(f"feature is not allowlisted: {feature}")
        if operator not in OPERATORS:
            raise ShadowRuleError(f"operator is not allowlisted: {operator}")
        if any(token in feature for token in ("__", ".", "[", "]")):
            raise ShadowRuleError("dynamic feature paths are not allowed")
        expected = node.get("value")
        if operator == "between" and (not isinstance(expected, Sequence) or len(expected) != 2):
            raise ShadowRuleError("between requires exactly two values")
        if operator == "in" and (not isinstance(expected, Sequence) or isinstance(expected, (str, bytes))):
            raise ShadowRuleError("in requires an array value")
        if operator != "exists" and "value" not in node:
            raise ShadowRuleError(f"operator {operator} requires value")
        features.add(feature)
        return lambda row: _compare(operator, row.get(feature), expected)

    predicate = compile_node(canonical)
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    rule_hash = hashlib.sha256(encoded).hexdigest()
    return CompiledShadowRule(
        version=str(version),
        spec=canonical,
        features=tuple(sorted(features)),
        rule_hash=rule_hash,
        _predicate=predicate,
    )


__all__ = ["CompiledShadowRule", "FEATURES", "ShadowRuleError", "compile_shadow_rule"]
