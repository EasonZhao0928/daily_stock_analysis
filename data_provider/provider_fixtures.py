# -*- coding: utf-8 -*-
# Reference: a-stock-data SKILL.md (Apache-2.0), independently rewritten for
# DSA; no upstream executable code is copied into this file.
"""Offline provider fixtures and deterministic schema fingerprints.

The a-stock-data reference describes a number of public endpoints, but its
runtime is intentionally not imported by DSA.  This module provides the small
bridge we do need: redacted payloads can be checked into tests, their *shape*
is fingerprinted without persisting values, and an online probe is explicit
opt-in.  Parsers therefore have a stable contract even when a provider is
unreachable or changes a column name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

try:  # pandas is optional for the fixture helper itself.
    import pandas as pd
except Exception:  # pragma: no cover - exercised only in minimal installs
    pd = None


class FixtureValidationError(ValueError):
    """Raised when a fixture is not safe or does not have a valid envelope."""


_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|cookie|authorization)(?:$|_)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._-]{12,})")


def _json_safe(value: Any) -> Any:
    """Convert common provider values to deterministic JSON-compatible data."""

    if pd is not None:
        if isinstance(value, pd.DataFrame):
            return [_json_safe(row) for row in value.to_dict(orient="records")]
        if isinstance(value, pd.Series):
            return _json_safe(value.to_dict())
        if isinstance(value, (pd.Timestamp,)):
            return value.isoformat()
        if pd.isna(value) if not isinstance(value, (list, tuple, dict, set)) else False:
            return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _assert_no_secrets(value: Any, path: str = "payload") -> None:
    """Reject credentials in fixture keys or values before they reach git."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text):
                raise FixtureValidationError(f"secret-like fixture field: {path}.{key_text}")
            _assert_no_secrets(item, f"{path}.{key_text}")
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _assert_no_secrets(item, f"{path}[{index}]")
        return
    if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise FixtureValidationError(f"secret-like fixture value: {path}")


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "array"
    return type(value).__name__


def _schema_shape(value: Any) -> Any:
    """Return shape/type information only; never include fixture values."""

    if pd is not None and isinstance(value, pd.DataFrame):
        fields = []
        for column in value.columns:
            dtype = str(value[column].dtype)
            fields.append({"name": str(column), "type": dtype, "nullable": bool(value[column].isna().any())})
        return {"kind": "dataframe", "fields": fields}
    if isinstance(value, Mapping):
        return {
            "kind": "object",
            "fields": [
                {"name": str(key), "type": _type_name(item), "shape": _schema_shape(item)}
                for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            ],
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        shapes = [_schema_shape(item) for item in items[:20]]
        type_names = sorted({_type_name(item) for item in items})
        return {"kind": "array", "item_types": type_names, "item_shapes": shapes}
    return {"kind": _type_name(value)}


def schema_descriptor(payload: Any) -> dict[str, Any]:
    """Return a stable, value-free schema descriptor for a provider payload."""

    safe = _json_safe(payload)
    _assert_no_secrets(safe)
    return _schema_shape(safe)


def schema_fingerprint(payload: Any) -> str:
    """Hash a payload's shape, not its contents."""

    descriptor = schema_descriptor(payload)
    encoded = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def content_fingerprint(payload: Any) -> str:
    """Hash redacted fixture content for reproducibility diagnostics."""

    safe = _json_safe(payload)
    _assert_no_secrets(safe)
    encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ProviderFixture:
    """A small, redacted, offline provider payload and its contract metadata."""

    provider: str
    capability: str
    payload: Any
    fixture_id: str
    source_tier: str = "primary"
    captured_at: Optional[str] = None
    network: bool = False
    expected_schema_fingerprint: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        provider = str(self.provider).strip()
        capability = str(self.capability).strip()
        fixture_id = str(self.fixture_id).strip()
        source_tier = str(self.source_tier).strip()
        if not provider or not capability or not fixture_id or not source_tier:
            raise FixtureValidationError("provider, capability, fixture_id and source_tier are required")
        if self.network:
            raise FixtureValidationError("checked-in fixtures must be offline; use an opt-in probe instead")
        safe = _json_safe(self.payload)
        _assert_no_secrets(safe)
        actual = schema_fingerprint(safe)
        if self.expected_schema_fingerprint and self.expected_schema_fingerprint != actual:
            raise FixtureValidationError(
                f"schema fingerprint mismatch for {fixture_id}: expected={self.expected_schema_fingerprint} actual={actual}"
            )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "fixture_id", fixture_id)
        object.__setattr__(self, "source_tier", source_tier)
        object.__setattr__(self, "payload", safe)
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def schema_fingerprint(self) -> str:
        return schema_fingerprint(self.payload)

    @property
    def content_hash(self) -> str:
        return content_fingerprint(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "capability": self.capability,
            "fixture_id": self.fixture_id,
            "source_tier": self.source_tier,
            "captured_at": self.captured_at,
            "network": False,
            "schema_fingerprint": self.schema_fingerprint,
            "content_hash": self.content_hash,
            "metadata": dict(self.metadata),
            "payload": self.payload,
        }


def fixture_from_dict(value: Mapping[str, Any]) -> ProviderFixture:
    if not isinstance(value, Mapping):
        raise FixtureValidationError("fixture must be an object")
    declared = value.get("schema_fingerprint")
    return ProviderFixture(
        provider=value.get("provider", ""),
        capability=value.get("capability", ""),
        fixture_id=value.get("fixture_id", ""),
        source_tier=value.get("source_tier", "primary"),
        captured_at=value.get("captured_at"),
        network=bool(value.get("network", False)),
        expected_schema_fingerprint=declared,
        metadata=value.get("metadata") or {},
        payload=value.get("payload"),
    )


def load_fixture(path: os.PathLike[str] | str) -> ProviderFixture:
    fixture_path = Path(path)
    with fixture_path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return fixture_from_dict(value)


def save_fixture(path: os.PathLike[str] | str, fixture: ProviderFixture) -> None:
    fixture_path = Path(path)
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(json.dumps(fixture.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def network_probe_enabled() -> bool:
    """Return whether an explicitly requested online probe may run."""

    return os.getenv("DSA_NETWORK_PROBE", "").strip().lower() in {"1", "true", "yes", "on"}


def require_network_probe() -> None:
    if not network_probe_enabled():
        raise RuntimeError("network probe disabled; set DSA_NETWORK_PROBE=1 explicitly")


__all__ = [
    "FixtureValidationError",
    "ProviderFixture",
    "content_fingerprint",
    "fixture_from_dict",
    "load_fixture",
    "network_probe_enabled",
    "require_network_probe",
    "save_fixture",
    "schema_descriptor",
    "schema_fingerprint",
]
