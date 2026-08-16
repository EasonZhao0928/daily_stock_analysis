# -*- coding: utf-8 -*-
"""
Tool Registry for the Agent framework.

Provides:
- ToolParameter / ToolDefinition dataclasses
- ToolRegistry: central tool registry with multi-provider schema generation
- @tool decorator for easy tool registration
"""

import json
import inspect
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SUPPORTED_TOOL_SURFACE_SCOPE_DIMENSIONS = frozenset({"account", "stock", "symbol"})


class ExecutionProfile(str, Enum):
    """The small set of execution capabilities exposed to Agent runtimes."""

    RESEARCH_READONLY = "research_readonly"
    PORTFOLIO_READONLY = "portfolio_readonly"
    PAPER_PROPOSAL = "paper_proposal"
    PAPER_APPROVAL = "paper_approval"

    # Lower-case aliases make the serialized names convenient to use from
    # config/transport code while keeping the enum's canonical members clear.
    research_readonly = RESEARCH_READONLY
    portfolio_readonly = PORTFOLIO_READONLY
    paper_proposal = PAPER_PROPOSAL
    paper_approval = PAPER_APPROVAL

    @classmethod
    def coerce(cls, value: Any) -> "ExecutionProfile":
        """Normalize a profile received from Python or a transport payload."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        raise ValueError(f"Unknown execution profile: {value!r}")

    def __str__(self) -> str:
        return self.value


# Agent Chat is a read-only research session, and R1.8 requires its parity set
# to include portfolio reads.  Both the LiteLLM and Codex transports authorize
# against this profile so R1.3 parity covers permissions, not just envelopes.
AGENT_CHAT_EXECUTION_PROFILE = "portfolio_readonly"

_RESEARCH_PERMISSIONS = frozenset(
    {
        "analysis_context:read",
        "backtest:read",
        "evidence:read",
        "intel:read",
        "market_data:read",
        "news:read",
        "research:read",
    }
)

_PROFILE_PERMISSIONS = {
    ExecutionProfile.RESEARCH_READONLY: _RESEARCH_PERMISSIONS,
    ExecutionProfile.PORTFOLIO_READONLY: _RESEARCH_PERMISSIONS
    | frozenset({"portfolio:read", "risk:read"}),
    # Deliberately NOT _RESEARCH_PERMISSIONS.  A Paper cycle decides against a
    # frozen Observation, so granting live market_data/news reads here would let
    # the model see past its own cutoff (CONTEXT invariant 5).  The frozen
    # context is delivered by ``read_paper_context`` instead.
    ExecutionProfile.PAPER_PROPOSAL: frozenset(
        {"paper:read", "paper:proposal", "paper:proposal:read"}
    ),
    ExecutionProfile.PAPER_APPROVAL: frozenset(
        {"paper:proposal:read", "paper:approval"}
    ),
}

_PROFILE_SIDE_EFFECTS = {
    ExecutionProfile.RESEARCH_READONLY: frozenset(
        {"network_read", "db_read", "db_write_cache"}
    ),
    ExecutionProfile.PORTFOLIO_READONLY: frozenset(
        {"network_read", "db_read", "db_write_cache"}
    ),
    # No ``network_read``: every input for a Paper decision is already frozen.
    ExecutionProfile.PAPER_PROPOSAL: frozenset({"db_read", "paper_proposal"}),
    ExecutionProfile.PAPER_APPROVAL: frozenset({"db_read", "paper_approval"}),
}

_PROFILE_REQUIRES_READ_ONLY = {
    ExecutionProfile.RESEARCH_READONLY: True,
    ExecutionProfile.PORTFOLIO_READONLY: True,
    ExecutionProfile.PAPER_PROPOSAL: False,
    ExecutionProfile.PAPER_APPROVAL: False,
}

# These effect names are deliberately conservative.  Paper proposal/approval
# is the only write-like capability represented by the Phase 1.2 profiles.
_FORBIDDEN_PROFILE_SIDE_EFFECT_PREFIXES = (
    "broker",
    "cash_write",
    "db_write",
    "database_write",
    "external_execution",
    "file",
    "filesystem_write",
    "fill",
    "order",
    "position_write",
    "real_trade",
    "shell",
)

TOOL_ERROR_CODES = frozenset(
    {
        "invalid_tool_name",
        "tool_not_found",
        "invalid_arguments",
        "scope_contract_violation",
        "stock_scope_violation",
        "symbol_scope_violation",
        "account_scope_violation",
        "cancellation_unsupported",
        "cancelled",
        "timeout",
        "handler_error",
        "serialization_error",
        "invalid_profile",
        "tool_not_allowed",
    }
)


# ============================================================
# Data classes
# ============================================================

@dataclass
class ToolParameter:
    """Schema for a single tool parameter."""
    name: str
    type: str  # "string" | "number" | "integer" | "boolean" | "array" | "object"
    description: str
    required: bool = True
    enum: Optional[List[str]] = None
    default: Any = None


def _canonical_scope_value(value: Any) -> Any:
    """Return a JSON-friendly, deterministic scope value."""
    if isinstance(value, dict):
        return {
            str(key): _canonical_scope_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (set, frozenset, tuple, list)):
        items = [_canonical_scope_value(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted(items, key=lambda item: str(item))
        return items
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass(frozen=True, init=False)
class ToolInvocation:
    """Serializable input contract for one profile-bound tool call.

    ``context`` is intentionally runtime-only and is not included in the
    serialized payload.  Account and symbol scopes are transport-safe values
    and are serialized under ``scope``.
    """

    name: str
    arguments: Dict[str, Any]
    profile: Any
    context: Any = field(default=None, compare=False, repr=False)
    scope: Dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        name: Optional[str] = None,
        arguments: Optional[Dict[str, Any]] = None,
        profile: Any = ExecutionProfile.RESEARCH_READONLY.value,
        context: Any = None,
        scope: Optional[Dict[str, Any]] = None,
        *,
        tool_name: Optional[str] = None,
        account_scope: Any = None,
        symbol_scope: Any = None,
    ) -> None:
        resolved_name = tool_name if name is None else name
        if tool_name is not None and name is not None and tool_name != name:
            raise ValueError("name and tool_name must identify the same tool")
        payload = dict(arguments or {})
        normalized_scope = dict(scope or {})
        if account_scope is not None:
            normalized_scope["account_ids"] = _canonical_scope_value(account_scope)
        if symbol_scope is not None:
            normalized_scope["symbols"] = _canonical_scope_value(symbol_scope)
        object.__setattr__(self, "name", resolved_name if resolved_name is not None else "")
        object.__setattr__(self, "arguments", payload)
        object.__setattr__(self, "profile", profile.value if isinstance(profile, ExecutionProfile) else profile)
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "scope", _canonical_scope_value(normalized_scope))

    @property
    def tool_name(self) -> str:
        """Compatibility alias for transports that use ``tool_name``."""
        return self.name

    @property
    def account_scope(self) -> Any:
        return self.scope.get(
            "account_ids",
            self.scope.get("account_id", self.scope.get("account")),
        )

    @property
    def symbol_scope(self) -> Any:
        return self.scope.get(
            "symbols",
            self.scope.get(
                "symbol",
                self.scope.get("stock", self.scope.get("stock_code")),
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the stable transport payload for this invocation."""
        payload = {
            "tool_name": self.name,
            "arguments": _canonical_scope_value(self.arguments),
            "profile": self.profile.value if isinstance(self.profile, ExecutionProfile) else str(self.profile),
        }
        if self.scope:
            payload["scope"] = _canonical_scope_value(self.scope)
        return payload

    def to_json(self) -> str:
        """Serialize the invocation with deterministic key ordering."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, default=str)

    serialize = to_json

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ToolInvocation":
        """Build an invocation from a transport payload."""
        if not isinstance(payload, dict):
            raise TypeError("ToolInvocation payload must be an object")
        return cls(
            name=payload.get("tool_name", payload.get("name")),
            arguments=payload.get("arguments", {}),
            profile=payload.get("profile", ExecutionProfile.RESEARCH_READONLY.value),
            scope=payload.get("scope") or {},
        )

    @classmethod
    def from_json(cls, payload: str) -> "ToolInvocation":
        """Build an invocation from JSON."""
        return cls.from_dict(json.loads(payload))


class ToolError(dict):
    """Dictionary error payload with attribute access for typed callers."""

    def __init__(
        self,
        code: str,
        message: str,
        retriable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            retriable=bool(retriable),
            details=dict(details or {}),
        )

    @property
    def code(self) -> str:
        return self["code"]

    @property
    def message(self) -> str:
        return self["message"]

    @property
    def retriable(self) -> bool:
        return self["retriable"]

    @property
    def details(self) -> Dict[str, Any]:
        return self["details"]


class ToolResult(dict):
    """Unified, JSON-serializable Tool Surface result envelope."""

    def __init__(
        self,
        *,
        ok: bool,
        tool_name: str,
        result: Any = None,
        result_text: str = "",
        error: Optional[Dict[str, Any]] = None,
        audit: Optional[Dict[str, Any]] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        normalized_error = None
        if error is not None:
            normalized_error = ToolError(
                code=str(error.get("code", "unknown_error")),
                message=str(error.get("message", "Tool execution failed.")),
                retriable=bool(error.get("retriable", False)),
                details=error.get("details") or {},
            )
        super().__init__(
            ok=bool(ok),
            tool_name=tool_name,
            result=result,
            result_text=result_text,
            error=normalized_error,
            audit=dict(audit or {}),
            diagnostics=dict(diagnostics or {}),
        )

    @property
    def ok(self) -> bool:
        return self["ok"]

    @property
    def tool_name(self) -> str:
        return self["tool_name"]

    @property
    def result(self) -> Any:
        return self["result"]

    @property
    def result_text(self) -> str:
        return self["result_text"]

    @property
    def error(self) -> Optional[ToolError]:
        return self["error"]

    @property
    def audit(self) -> Dict[str, Any]:
        return self["audit"]

    @property
    def diagnostics(self) -> Dict[str, Any]:
        return self["diagnostics"]

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain dictionary suitable for existing callers."""
        return dict(self)

    def to_json(self) -> str:
        """Serialize the complete result deterministically."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, default=str)

    serialize = to_json

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ToolResult":
        """Wrap an existing Tool Surface envelope without changing its shape."""
        if not isinstance(payload, dict):
            raise TypeError("ToolResult payload must be an object")
        return cls(
            ok=payload.get("ok", False),
            tool_name=str(payload.get("tool_name", "")),
            result=payload.get("result"),
            result_text=str(payload.get("result_text", "")),
            error=payload.get("error"),
            audit=payload.get("audit"),
            diagnostics=payload.get("diagnostics"),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ToolResult":
        """Wrap a JSON result envelope."""
        return cls.from_dict(json.loads(payload))


@dataclass(frozen=True)
class ToolPolicy:
    """Internal policy metadata for DSA Tool Surface descriptors."""

    read_only: Optional[bool] = None
    side_effects: List[str] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)
    policy_status: str = "unknown"
    scope_dimensions: List[str] = field(default_factory=list)
    cancellation_safe: bool = False
    allowed_profiles: Optional[List[str]] = None

    @classmethod
    def unknown(cls) -> "ToolPolicy":
        return cls()

    @classmethod
    def declared(
        cls,
        *,
        read_only: bool,
        side_effects: Optional[List[str]] = None,
        permissions: Optional[List[str]] = None,
        scope_dimensions: Optional[List[str]] = None,
        cancellation_safe: bool = False,
        allowed_profiles: Optional[List[str]] = None,
        execution_profiles: Optional[List[str]] = None,
    ) -> "ToolPolicy":
        if allowed_profiles is not None and execution_profiles is not None:
            if list(allowed_profiles) != list(execution_profiles):
                raise ValueError("allowed_profiles and execution_profiles must match")
        profile_values = allowed_profiles if allowed_profiles is not None else execution_profiles
        return cls(
            read_only=read_only,
            side_effects=list(side_effects or []),
            permissions=list(permissions or []),
            policy_status="declared",
            scope_dimensions=list(scope_dimensions or []),
            cancellation_safe=bool(cancellation_safe),
            allowed_profiles=list(profile_values) if profile_values is not None else None,
        )

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "read_only": self.read_only,
            "side_effects": list(self.side_effects),
            "permissions": list(self.permissions),
            "policy_status": self.policy_status,
            "cancellation_safe": self.cancellation_safe,
        }


@dataclass
class ToolDefinition:
    """Complete definition of an agent-callable tool."""
    name: str
    description: str
    parameters: List[ToolParameter]
    handler: Callable
    category: str = "data"  # data | analysis | search | action
    policy: ToolPolicy = field(default_factory=ToolPolicy.unknown)
    execution_profiles: Optional[List[str]] = None
    allowed_profiles: Optional[List[str]] = None

    def profile_allowlist(self) -> Optional[List[str]]:
        """Return an explicit profile allow-list, if the tool declares one."""
        if self.execution_profiles is not None:
            return list(self.execution_profiles)
        if self.allowed_profiles is not None:
            return list(self.allowed_profiles)
        if self.policy.allowed_profiles is not None:
            return list(self.policy.allowed_profiles)
        return None

    # ----- Multi-provider schema converters -----

    def _params_json_schema(self) -> dict:
        """Convert parameters to JSON Schema (shared by OpenAI/Anthropic)."""
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for p in self.parameters:
            prop: Dict[str, Any] = {"type": p.type, "description": p.description}
            if p.enum:
                prop["enum"] = p.enum
            properties[p.name] = prop
            if p.required:
                required.append(p.name)
        schema: Dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema

    def _descriptor_json_schema(self) -> dict:
        """Return a descriptor schema with explicit empty required list."""
        schema = self._params_json_schema()
        schema.setdefault("required", [])
        schema["additionalProperties"] = self.accepts_extra_arguments()
        return schema

    def accepts_extra_arguments(self) -> bool:
        """Return whether the handler explicitly accepts undeclared kwargs."""
        try:
            sig = inspect.signature(self.handler)
        except (TypeError, ValueError):
            return False
        return any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values())

    def to_openai_tool(self) -> dict:
        """Convert to OpenAI ``tools`` list element format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._params_json_schema(),
            },
        }

    def to_public_descriptor(self) -> dict:
        """Return Tool Surface descriptor without exposing the Python handler."""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "parameters": self._descriptor_json_schema(),
            "policy": self.policy.to_public_dict(),
            "scope": {
                "scope_dimensions": list(self.policy.scope_dimensions),
                "requires_stock_scope": "stock" in self.policy.scope_dimensions,
            },
        }

    def to_mcp_descriptor(self) -> dict:
        """Return an MCP-compatible descriptor only; no server/transport implied."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self._descriptor_json_schema(),
        }


def _profile_permission_matches(permission: str, allowed_permissions: Any) -> bool:
    """Match a declared capability and its narrower sub-capabilities."""
    normalized = str(permission).strip().lower()
    return any(
        normalized == allowed or normalized.startswith(f"{allowed}:")
        for allowed in allowed_permissions
    )


def _profile_side_effect_is_forbidden(effect: str) -> bool:
    normalized = str(effect).strip().lower()
    if normalized == "db_write_cache":
        return False
    return normalized.startswith(_FORBIDDEN_PROFILE_SIDE_EFFECT_PREFIXES)


def _normalize_profile_list(values: Optional[List[str]]) -> Optional[set]:
    if values is None:
        return None
    normalized = set()
    for value in values:
        try:
            normalized.add(ExecutionProfile.coerce(value).value)
        except ValueError:
            normalized.add(str(value).strip().lower())
    return normalized


def check_tool_profile_access(tool_def: ToolDefinition, profile: Any) -> Dict[str, Any]:
    """Return the stable visibility/authorization decision for one tool."""
    normalized_profile = ExecutionProfile.coerce(profile)
    allowed_profiles = _normalize_profile_list(tool_def.profile_allowlist())
    if allowed_profiles is not None and normalized_profile.value not in allowed_profiles:
        return {
            "visible": False,
            "code": "tool_not_allowed",
            "message": "Tool is not declared for this execution profile.",
            "details": {
                "reason": "profile_not_declared",
                "profile": normalized_profile.value,
                "allowed_profiles": sorted(allowed_profiles),
            },
        }

    policy = tool_def.policy
    if policy.policy_status != "declared":
        return {
            "visible": False,
            "code": "tool_not_allowed",
            "message": "Tool policy is not declared for this execution profile.",
            "details": {
                "reason": "policy_not_declared",
                "profile": normalized_profile.value,
            },
        }

    allowed_permissions = _PROFILE_PERMISSIONS[normalized_profile]
    if not any(
        _profile_permission_matches(permission, allowed_permissions)
        for permission in policy.permissions
    ):
        return {
            "visible": False,
            "code": "tool_not_allowed",
            "message": "Tool permission is not granted by the execution profile.",
            "details": {
                "reason": "permission_not_granted",
                "profile": normalized_profile.value,
                "permissions": list(policy.permissions),
            },
        }

    if _PROFILE_REQUIRES_READ_ONLY[normalized_profile] and policy.read_only is not True:
        return {
            "visible": False,
            "code": "tool_not_allowed",
            "message": "The execution profile only permits read-only tools.",
            "details": {
                "reason": "read_only_required",
                "profile": normalized_profile.value,
                "read_only": policy.read_only,
            },
        }

    forbidden_effects = [
        effect
        for effect in policy.side_effects
        if _profile_side_effect_is_forbidden(effect)
    ]
    if forbidden_effects or any(
        effect not in _PROFILE_SIDE_EFFECTS[normalized_profile]
        for effect in policy.side_effects
    ):
        return {
            "visible": False,
            "code": "tool_not_allowed",
            "message": "Tool side effects exceed the execution profile.",
            "details": {
                "reason": "side_effect_not_allowed",
                "profile": normalized_profile.value,
                "side_effects": list(policy.side_effects),
                "forbidden_side_effects": forbidden_effects,
            },
        }

    return {
        "visible": True,
        "code": "allowed",
        "message": "Tool is allowed by the execution profile.",
        "details": {"profile": normalized_profile.value},
    }


# ============================================================
# Tool Registry
# ============================================================

class ToolRegistry:
    """Central registry for all agent-callable tools.

    Usage::

        registry = ToolRegistry()
        registry.register(tool_def)
        registry.execute("get_realtime_quote", stock_code="600519")
    """

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}

    # ----- Registration -----

    def register(self, tool_def: ToolDefinition) -> None:
        """Register a tool definition."""
        if tool_def.name in self._tools:
            logger.warning(f"Tool '{tool_def.name}' already registered, overwriting")
        self._tools[tool_def.name] = tool_def
        logger.debug(f"Registered tool: {tool_def.name} (category={tool_def.category})")

    def unregister(self, name: str) -> None:
        """Remove a registered tool."""
        self._tools.pop(name, None)

    # ----- Query -----

    def get(self, name: str) -> Optional[ToolDefinition]:
        """Return a tool definition by name."""
        return self._tools.get(name)

    def resolve(self, name: str) -> Optional[ToolDefinition]:
        """Return a tool definition by exact registered name."""
        return self._tools.get(name)

    def list_tools(
        self,
        category: Optional[str] = None,
        *,
        profile: Optional[Any] = None,
    ) -> List[ToolDefinition]:
        """List tools, optionally filtered by category and execution profile."""
        tools = list(self._tools.values())
        if category:
            tools = [t for t in tools if t.category == category]
        if profile is not None:
            normalized_profile = ExecutionProfile.coerce(profile)
            tools = [
                tool_def
                for tool_def in tools
                if check_tool_profile_access(tool_def, normalized_profile)["visible"]
            ]
        return tools

    def list_names(self) -> List[str]:
        """Return all registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    # ----- Schema generation -----

    def to_openai_tools(self, *, profile: Optional[Any] = None) -> List[dict]:
        """Generate OpenAI-format tools list (used by litellm for all providers)."""
        tools = self.list_tools(profile=profile)
        return [t.to_openai_tool() for t in tools]

    def profile_diagnostics(self, profile: Any) -> List[Dict[str, Any]]:
        """Return visibility decisions, including reasons for hidden tools."""
        normalized_profile = ExecutionProfile.coerce(profile)
        diagnostics: List[Dict[str, Any]] = []
        for tool_def in self._tools.values():
            decision = check_tool_profile_access(tool_def, normalized_profile)
            diagnostics.append(
                {
                    "tool": tool_def.name,
                    "profile": normalized_profile.value,
                    **decision,
                }
            )
        return diagnostics

    # Alias used by callers that treat profile checks as a diagnostic query.
    diagnose_profile = profile_diagnostics

    def check_tool_access(self, name: str, profile: Any) -> Dict[str, Any]:
        """Return one stable profile authorization decision."""
        tool_def = self.resolve(name)
        if tool_def is None:
            return {
                "visible": False,
                "code": "tool_not_found",
                "message": "Tool not found.",
                "details": {"tool": name},
            }
        return check_tool_profile_access(tool_def, profile)

    def validate_tool_policies(self, *, strict: bool = False) -> List[Dict[str, Any]]:
        """Return policy validation issues for registered tools.

        Ordinary registration intentionally stays permissive.  Strict mode is
        used by Tool Surface checks for production/default registries.
        """
        issues: List[Dict[str, Any]] = []
        for tool_def in self._tools.values():
            policy = tool_def.policy
            if policy.policy_status != "declared":
                if strict:
                    issues.append({
                        "tool": tool_def.name,
                        "code": "policy_unknown",
                        "message": "Tool policy is not declared.",
                    })
                continue
            if strict and policy.read_only is None:
                issues.append({
                    "tool": tool_def.name,
                    "code": "read_only_missing",
                    "message": "Tool policy read_only is not declared.",
                })
            if not strict:
                continue
            unsupported_scopes = [
                dimension
                for dimension in policy.scope_dimensions
                if dimension not in SUPPORTED_TOOL_SURFACE_SCOPE_DIMENSIONS
            ]
            for dimension in unsupported_scopes:
                issues.append({
                    "tool": tool_def.name,
                    "code": "unsupported_scope_dimension",
                    "message": f"Tool declares unsupported scope dimension: {dimension}.",
                    "dimension": dimension,
                })
            has_stock_param = any(param.name == "stock_code" for param in tool_def.parameters)
            declares_stock_scope = "stock" in policy.scope_dimensions
            if has_stock_param and not declares_stock_scope:
                issues.append({
                    "tool": tool_def.name,
                    "code": "stock_scope_missing",
                    "message": "Tool has stock_code parameter but does not declare stock scope.",
                })
            if declares_stock_scope and not has_stock_param:
                issues.append({
                    "tool": tool_def.name,
                    "code": "stock_scope_parameter_missing",
                    "message": "Tool declares stock scope but has no stock_code parameter.",
                })
            has_account_param = any(param.name == "account_id" for param in tool_def.parameters)
            declares_account_scope = "account" in policy.scope_dimensions
            if declares_account_scope and not has_account_param:
                issues.append({
                    "tool": tool_def.name,
                    "code": "account_scope_parameter_missing",
                    "message": "Tool declares account scope but has no account_id parameter.",
                })
            has_symbol_param = any(
                param.name in {"symbol", "symbol_code", "stock_code"}
                for param in tool_def.parameters
            )
            declares_symbol_scope = "symbol" in policy.scope_dimensions
            if declares_symbol_scope and not has_symbol_param:
                issues.append({
                    "tool": tool_def.name,
                    "code": "symbol_scope_parameter_missing",
                    "message": "Tool declares symbol scope but has no symbol parameter.",
                })
        return issues

    # ----- Execution -----

    def execute(self, name: str, **kwargs) -> Any:
        """Execute a registered tool by name.

        Returns the result as a JSON-serializable value.
        Raises ``KeyError`` if tool not found.
        Raises the handler's exception on execution failure.

        Tool names must match the registry exactly.
        """
        tool_def = self.resolve(name)
        if tool_def is None:
            raise KeyError(f"Tool '{name}' not found in registry. Available: {self.list_names()}")

        return tool_def.handler(**kwargs)


# ============================================================
# @tool decorator
# ============================================================

# Global default registry (singleton pattern)
_default_registry: Optional[ToolRegistry] = None


def get_default_registry() -> ToolRegistry:
    """Get or create the global default ToolRegistry."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
    return _default_registry


def tool(
    name: str,
    description: str,
    category: str = "data",
    parameters: Optional[List[ToolParameter]] = None,
    registry: Optional[ToolRegistry] = None,
    policy: Optional[ToolPolicy] = None,
    execution_profiles: Optional[List[str]] = None,
    allowed_profiles: Optional[List[str]] = None,
):
    """Decorator to register a function as an agent tool.

    Parameters can be specified explicitly or inferred from type hints.

    Example::

        @tool(name="get_realtime_quote", category="data",
              description="Get real-time stock quote")
        def get_realtime_quote(stock_code: str) -> dict:
            ...
    """
    def decorator(func: Callable) -> Callable:
        # Infer parameters from type hints if not provided
        params = parameters
        if params is None:
            params = _infer_parameters(func)

        tool_def = ToolDefinition(
            name=name,
            description=description,
            parameters=params,
            handler=func,
            category=category,
            policy=policy or ToolPolicy.unknown(),
            execution_profiles=execution_profiles,
            allowed_profiles=allowed_profiles,
        )

        target_registry = registry or get_default_registry()
        target_registry.register(tool_def)

        # Attach metadata to function for introspection
        func._tool_definition = tool_def
        return func

    return decorator


def _infer_parameters(func: Callable) -> List[ToolParameter]:
    """Infer ToolParameter list from function signature and type hints."""
    sig = inspect.signature(func)
    hints = getattr(func, '__annotations__', {})
    params: List[ToolParameter] = []

    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        # Skip return annotation
        hint = hints.get(param_name, str)
        # Handle Optional and other typing constructs
        origin = getattr(hint, '__origin__', None)
        if origin is not None:
            # Optional[X] -> X, List[X] -> array, etc.
            args = getattr(hint, '__args__', ())
            if origin is list or (hasattr(origin, '__name__') and origin.__name__ == 'List'):
                param_type = "array"
            elif origin is dict:
                param_type = "object"
            else:
                # Union/Optional - use first non-None arg
                for a in args:
                    if a is not type(None):
                        param_type = type_map.get(a, "string")
                        break
                else:
                    param_type = "string"
        else:
            param_type = type_map.get(hint, "string")

        has_default = param.default is not inspect.Parameter.empty
        tp = ToolParameter(
            name=param_name,
            type=param_type,
            description=f"Parameter: {param_name}",
            required=not has_default,
            default=param.default if has_default else None,
        )
        params.append(tp)

    return params
