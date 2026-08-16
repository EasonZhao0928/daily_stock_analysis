# -*- coding: utf-8 -*-
"""Internal DSA Tool Surface for future external Agent runtimes."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any, Dict, Optional

from src.agent.stock_scope import StockScope
from src.agent.tools.execution import (
    ToolAccessContext,
    ToolExecutionCancelled,
    ToolExecutionDeadlineExceeded,
    _guard_tool_stock_scope,
    bind_tool_execution_context,
    build_tool_audit,
    check_tool_execution,
    redact_external_tool_result,
    redact_diagnostic_value,
    reset_tool_execution_context,
    serialize_tool_result,
)
from src.agent.tools.registry import (
    ExecutionProfile,
    SUPPORTED_TOOL_SURFACE_SCOPE_DIMENSIONS,
    ToolDefinition,
    ToolInvocation,
    ToolParameter,
    ToolRegistry,
    ToolResult,
    check_tool_profile_access,
)


_JSON_TYPE_TO_PYTHON = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class ToolSurface:
    """Internal tool schema and execution surface.

    This is a Python API only.  It intentionally does not expose REST, MCP, or
    provider-specific runtime transport.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        legacy_runner_compat: bool = False,
        default_profile: Optional[Any] = None,
    ) -> None:
        self._registry = registry
        # Legacy ``run_agent_loop(tool_registry=...)`` callers may provide
        # ad-hoc definitions without ToolPolicy metadata.  Keep that public
        # compatibility path permissive while the factory-created surface
        # remains fail-closed for undeclared scope contracts.
        self._legacy_runner_compat = bool(legacy_runner_compat)
        # The Execution Profile belongs to the surface, not to each call site.
        # Carrying it here is what keeps LiteLLM and Codex authorizing against
        # the same profile (R1.3) instead of both silently running unprofiled.
        self._default_profile = (
            ExecutionProfile.coerce(default_profile) if default_profile is not None else None
        )

    @property
    def default_profile(self) -> Optional[ExecutionProfile]:
        """The Execution Profile this surface authorizes against, if any.

        Transports read it from here rather than re-deriving it, so LiteLLM and
        Codex provably authorize against the same profile.
        """
        return self._default_profile

    @classmethod
    def empty(cls) -> "ToolSurface":
        """Return an empty Phase 6a surface for protocol-only preflight work."""
        return cls(ToolRegistry())

    def list_tools(
        self,
        format: str = "public",
        *,
        cancellation_safe_only: bool = False,
        profile: Optional[Any] = None,
    ) -> list[dict]:
        """List tools in a stable schema format."""
        normalized = (format or "public").strip().lower()
        tools = self._registry.list_tools(profile=profile)
        if cancellation_safe_only:
            tools = [tool_def for tool_def in tools if tool_def.policy.cancellation_safe]
        if normalized == "openai":
            return [tool_def.to_openai_tool() for tool_def in tools]
        if normalized == "public":
            return [tool_def.to_public_descriptor() for tool_def in tools]
        if normalized == "mcp_descriptor":
            return [tool_def.to_mcp_descriptor() for tool_def in tools]
        raise ValueError(f"Unsupported tool surface format: {format}")

    def describe(self, profile: Any) -> list[dict]:
        """Describe the tools visible to one Execution Profile."""
        return self.list_tools("public", profile=profile)

    def profile_diagnostics(self, profile: Any) -> list[dict]:
        """Return profile visibility decisions and stable denial reasons."""
        return self._registry.profile_diagnostics(profile)

    diagnose_profile = profile_diagnostics

    def execute(self, invocation: Any) -> ToolResult:
        """Execute a serialized or typed profile-bound tool invocation."""
        try:
            if not isinstance(invocation, ToolInvocation):
                invocation = ToolInvocation.from_dict(invocation)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return ToolResult.from_dict(
                self._error_result(
                    tool_name="",
                    code="invalid_arguments",
                    message="Tool invocation must be a valid object.",
                    started_at=time.time(),
                    context=ToolAccessContext(),
                    retriable=False,
                    details={"reason": str(exc)},
                )
            )

        started_at = time.time()
        try:
            profile = ExecutionProfile.coerce(invocation.profile)
        except ValueError as exc:
            return ToolResult.from_dict(
                self._error_result(
                    tool_name=invocation.name,
                    code="invalid_profile",
                    message="Execution profile is not supported.",
                    started_at=started_at,
                    context=invocation.context or ToolAccessContext(),
                    retriable=False,
                    details={"profile": invocation.profile, "reason": str(exc)},
                    arguments=invocation.arguments,
                )
            )

        context = _context_with_invocation_scope(invocation)
        raw_result = self.execute_tool(
            invocation.name,
            invocation.arguments,
            context,
            profile=profile,
        )
        return ToolResult.from_dict(raw_result)

    def execute_tool(
        self,
        name: Any,
        arguments: Any = None,
        context: Optional[ToolAccessContext] = None,
        *,
        profile: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Execute one registered tool by exact name and return structured output."""
        if isinstance(name, ToolInvocation) and arguments is None and profile is None:
            return self.execute(name)
        ctx = context or ToolAccessContext()
        started_at = time.time()
        tool_name = name if isinstance(name, str) else str(name)
        normalized_profile = None
        if profile is None:
            profile = self._default_profile
        if profile is not None:
            try:
                normalized_profile = ExecutionProfile.coerce(profile)
            except ValueError as exc:
                return self._error_result(
                    tool_name=tool_name,
                    code="invalid_profile",
                    message="Execution profile is not supported.",
                    started_at=started_at,
                    context=ctx,
                    retriable=False,
                    details={"profile": profile, "reason": str(exc)},
                    arguments=arguments,
                )
        tool_def = self._registry.resolve(tool_name) if isinstance(name, str) else None

        if tool_def is None:
            if isinstance(name, str) and (":" in name or "." in name):
                return self._error_result(
                    tool_name=tool_name,
                    code="invalid_tool_name",
                    message="Tool name must exactly match a registered DSA tool.",
                    started_at=started_at,
                    context=ctx,
                    retriable=False,
                    arguments=arguments,
                )
            return self._error_result(
                tool_name=tool_name,
                code="tool_not_found",
                message="Tool not found.",
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )

        if self._legacy_runner_compat:
            # Run the historical stock guard before schema validation.  The
            # old runner normalized numeric and exchange-affixed codes before
            # handing arguments to a handler, and callers rely on a scope
            # denial rather than a generic type error for those attempts.
            legacy_guard_result = _guard_tool_stock_scope(
                self._registry,
                tool_name,
                arguments,
                ctx.stock_scope,
            )
            if legacy_guard_result is not None:
                return self._error_result(
                    tool_name=tool_name,
                    code="stock_scope_violation",
                    message="Tool call is outside the allowed stock scope.",
                    started_at=started_at,
                    context=ctx,
                    retriable=False,
                    details={
                        "expected_stock_code": legacy_guard_result.get("expected_stock_code"),
                        "requested_stock_code": legacy_guard_result.get("requested_stock_code"),
                        "allowed_stock_codes": legacy_guard_result.get("allowed_stock_codes", []),
                    },
                    result_text=serialize_tool_result(legacy_guard_result),
                    arguments=arguments,
                )

        if normalized_profile is not None:
            profile_decision = check_tool_profile_access(tool_def, normalized_profile)
            if not profile_decision["visible"]:
                return self._error_result(
                    tool_name=tool_name,
                    code="tool_not_allowed",
                    message=profile_decision["message"],
                    started_at=started_at,
                    context=ctx,
                    retriable=False,
                    details={
                        "profile": normalized_profile.value,
                        **profile_decision["details"],
                    },
                    arguments=arguments,
                )

        validation_error = _validate_arguments(tool_def, arguments)
        if validation_error is not None:
            return self._error_result(
                tool_name=tool_name,
                code="invalid_arguments",
                message=validation_error,
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )

        scope_contract_error = _validate_scope_contract(tool_def)
        if scope_contract_error is not None and not self._legacy_runner_compat:
            return self._error_result(
                tool_name=tool_name,
                code="scope_contract_violation",
                message=scope_contract_error["message"],
                started_at=started_at,
                context=ctx,
                retriable=False,
                details=scope_contract_error["details"],
                arguments=arguments,
            )

        guard_result = None
        if _requires_stock_scope(tool_def) and not self._legacy_runner_compat:
            if ctx.stock_scope is None:
                return self._error_result(
                    tool_name=tool_name,
                    code="stock_scope_violation",
                    message="Tool call requires an explicit stock scope.",
                    started_at=started_at,
                    context=ctx,
                    retriable=False,
                    details={
                        "reason": "stock_scope_required",
                        "scope_dimensions": list(tool_def.policy.scope_dimensions),
                    },
                    arguments=arguments,
                )
            guard_result = _guard_tool_stock_scope(
                self._registry,
                tool_name,
                arguments,
                ctx.stock_scope,
            )
        if guard_result is not None:
            result_text = serialize_tool_result(guard_result)
            return self._error_result(
                tool_name=tool_name,
                code="stock_scope_violation",
                message="Tool call is outside the allowed stock scope.",
                started_at=started_at,
                context=ctx,
                retriable=False,
                details={
                    "expected_stock_code": guard_result.get("expected_stock_code"),
                    "requested_stock_code": guard_result.get("requested_stock_code"),
                    "allowed_stock_codes": guard_result.get("allowed_stock_codes", []),
                },
                result_text=result_text,
                arguments=arguments,
            )

        if normalized_profile is not None:
            scope_error = _validate_profile_scope(tool_def, arguments, ctx, normalized_profile)
            if scope_error is not None:
                return self._error_result(
                    tool_name=tool_name,
                    code=scope_error["code"],
                    message=scope_error["message"],
                    started_at=started_at,
                    context=ctx,
                    retriable=False,
                    details=scope_error["details"],
                    arguments=arguments,
                )

        timeout = ctx.timeout_seconds
        if (
            ctx.deadline is None
            and timeout is not None
            and timeout > 0
        ):
            ctx = replace(ctx, deadline=time.monotonic() + float(timeout))
        controlled_execution = ctx.cancel_event is not None or ctx.deadline is not None
        if controlled_execution and not tool_def.policy.cancellation_safe:
            return self._error_result(
                tool_name=tool_name,
                code="cancellation_unsupported",
                message="Tool is not available to runtimes that require bounded cancellation.",
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )

        try:
            if controlled_execution:
                result = _execute_with_control(tool_def, arguments, ctx)
            else:
                result = tool_def.handler(**arguments)
        except ToolExecutionCancelled:
            return self._error_result(
                tool_name=tool_name,
                code="cancelled",
                message="Tool execution was cancelled.",
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )
        except ToolExecutionDeadlineExceeded:
            return self._error_result(
                tool_name=tool_name,
                code="timeout",
                message="Tool execution exceeded the Agent deadline.",
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )
        except Exception:
            return self._error_result(
                tool_name=tool_name,
                code="handler_error",
                message="Tool handler failed.",
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )

        try:
            result_text = (
                redact_external_tool_result(result)
                if ctx.redact_result
                else serialize_tool_result(result)
            )
        except Exception:
            return self._error_result(
                tool_name=tool_name,
                code="serialization_error",
                message="Tool result could not be serialized.",
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )

        public_result = _public_payload_from_result_text(result_text)
        result_truncated = False
        if ctx.max_result_bytes is not None and ctx.max_result_bytes >= 0:
            result_text, result_truncated = _truncate_text_bytes(result_text, int(ctx.max_result_bytes))
            public_result = None if result_truncated else _public_payload_from_result_text(result_text)

        duration = time.time() - started_at
        return {
            "ok": True,
            "tool_name": tool_name,
            "result": public_result,
            "result_text": result_text,
            "error": None,
            "audit": build_tool_audit(
                tool_name=tool_name,
                arguments=arguments,
                result=result_text,
                duration=duration,
                context=ctx,
            ),
            "diagnostics": {
                "redacted": True,
                "result_length": len(result_text.encode("utf-8")),
                "result_truncated": result_truncated,
                "preview": redact_diagnostic_value(result_text),
            },
        }

    def _error_result(
        self,
        *,
        tool_name: str,
        code: str,
        message: str,
        started_at: float,
        context: ToolAccessContext,
        retriable: bool,
        details: Optional[Dict[str, Any]] = None,
        result_text: Optional[str] = None,
        arguments: Any = None,
    ) -> Dict[str, Any]:
        duration = time.time() - started_at
        safe_text = result_text or json.dumps(
            {"error": message, "code": code, "retriable": retriable},
            ensure_ascii=False,
        )
        result_truncated = False
        if context.max_result_bytes is not None and context.max_result_bytes >= 0:
            safe_text, result_truncated = _truncate_text_bytes(safe_text, int(context.max_result_bytes))
        return {
            "ok": False,
            "tool_name": tool_name,
            "result": None,
            "result_text": safe_text,
            "error": {
                "code": code,
                "message": message,
                "retriable": retriable,
                "details": details or {},
            },
            "audit": build_tool_audit(
                tool_name=tool_name,
                arguments=arguments if arguments is not None else {},
                result=safe_text,
                error_code=code,
                duration=duration,
                context=context,
            ),
            "diagnostics": {
                "redacted": True,
                "result_length": len(safe_text.encode("utf-8")),
                "result_truncated": result_truncated,
                "preview": redact_diagnostic_value(safe_text),
            },
        }


def _execute_with_control(
    tool_def: ToolDefinition,
    arguments: Dict[str, Any],
    context: ToolAccessContext,
) -> Any:
    token = bind_tool_execution_context(context)
    try:
        check_tool_execution()
        result = tool_def.handler(**arguments)
        check_tool_execution()
        return result
    finally:
        reset_tool_execution_context(token)


def _context_with_invocation_scope(invocation: ToolInvocation) -> ToolAccessContext:
    """Merge serializable invocation scopes into the runtime context."""
    context = invocation.context or ToolAccessContext()
    updates: Dict[str, Any] = {}
    if invocation.account_scope is not None:
        updates["account_scope"] = invocation.account_scope
    if invocation.symbol_scope is not None:
        updates["symbol_scope"] = invocation.symbol_scope
        if context.stock_scope is None:
            values = _scope_values(invocation.symbol_scope)
            if values:
                ordered = sorted(values)
                updates["stock_scope"] = StockScope(
                    expected_stock_code=ordered[0],
                    allowed_stock_codes=set(ordered),
                    mode="profile",
                )
    return replace(context, **updates) if updates else context


def _scope_values(value: Any) -> Optional[set]:
    if value is None:
        return None
    if hasattr(value, "allowed_stock_codes"):
        values = getattr(value, "allowed_stock_codes", None) or set()
        expected = getattr(value, "expected_stock_code", None)
        if expected:
            values = set(values) | {expected}
    elif isinstance(value, dict):
        values = value.values()
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = [value]
    return {_normalize_scope_token(item) for item in values if item is not None}


def _normalize_scope_token(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip().upper()


def _requested_scope_argument(tool_def: ToolDefinition, arguments: Dict[str, Any], names: tuple) -> Any:
    for name in names:
        if any(param.name == name for param in tool_def.parameters):
            return arguments.get(name)
    return None


def _scope_error(code: str, dimension: str, reason: str, requested: Any = None, allowed: Any = None) -> Dict[str, Any]:
    details: Dict[str, Any] = {
        "reason": reason,
        "scope_dimension": dimension,
    }
    if requested is not None:
        details["requested"] = requested
    if allowed is not None:
        details["allowed"] = sorted(_scope_values(allowed) or set())
    return {
        "code": code,
        "message": f"Tool call is outside the allowed {dimension} scope.",
        "details": details,
    }


def _validate_profile_scope(
    tool_def: ToolDefinition,
    arguments: Dict[str, Any],
    context: ToolAccessContext,
    profile: ExecutionProfile,
) -> Optional[Dict[str, Any]]:
    """Enforce profile-bound account and symbol scope contracts."""
    dimensions = set(tool_def.policy.scope_dimensions)
    account_param = _requested_scope_argument(tool_def, arguments, ("account_id",))
    requires_account = "account" in dimensions
    if not requires_account and account_param is not None:
        requires_account = any(
            permission.startswith(("portfolio:", "paper:"))
            for permission in tool_def.policy.permissions
        ) and profile in {
            ExecutionProfile.PORTFOLIO_READONLY,
            ExecutionProfile.PAPER_PROPOSAL,
            ExecutionProfile.PAPER_APPROVAL,
        }
    if requires_account:
        allowed_accounts = _scope_values(getattr(context, "account_scope", None))
        if not allowed_accounts:
            return _scope_error(
                "account_scope_violation",
                "account",
                "account_scope_required",
                requested=account_param,
            )
        if account_param is None or _normalize_scope_token(account_param) not in allowed_accounts:
            return _scope_error(
                "account_scope_violation",
                "account",
                "account_scope_mismatch",
                requested=account_param,
                allowed=allowed_accounts,
            )

    symbol_param = _requested_scope_argument(
        tool_def,
        arguments,
        ("symbol", "symbol_code", "stock_code"),
    )
    if "symbol" in dimensions:
        allowed_symbols = _scope_values(
            getattr(context, "symbol_scope", None) or getattr(context, "stock_scope", None)
        )
        if not allowed_symbols:
            return _scope_error(
                "symbol_scope_violation",
                "symbol",
                "symbol_scope_required",
                requested=symbol_param,
            )
        if symbol_param is None or _normalize_scope_token(symbol_param) not in allowed_symbols:
            return _scope_error(
                "symbol_scope_violation",
                "symbol",
                "symbol_scope_mismatch",
                requested=symbol_param,
                allowed=allowed_symbols,
            )
    return None


def _validate_arguments(tool_def: ToolDefinition, arguments: Any) -> Optional[str]:
    if not isinstance(arguments, dict):
        return "arguments must be an object"

    params = {param.name: param for param in tool_def.parameters}
    for param in tool_def.parameters:
        if param.required and param.name not in arguments:
            return f"missing required argument: {param.name}"

    accepts_extra = _handler_accepts_extra_kwargs(tool_def)
    for key in arguments:
        if key not in params and not accepts_extra:
            return f"unexpected argument: {key}"

    for key, value in arguments.items():
        param = params.get(key)
        if param is None:
            continue
        error = _validate_parameter_value(param, value)
        if error:
            return error
    return None


def _handler_accepts_extra_kwargs(tool_def: ToolDefinition) -> bool:
    return tool_def.accepts_extra_arguments()


def _requires_stock_scope(tool_def: ToolDefinition) -> bool:
    return "stock" in tool_def.policy.scope_dimensions


def _validate_scope_contract(tool_def: ToolDefinition) -> Optional[Dict[str, Any]]:
    dimensions = list(tool_def.policy.scope_dimensions)
    has_stock_param = any(param.name == "stock_code" for param in tool_def.parameters)
    declares_stock_scope = "stock" in dimensions
    unsupported = [
        dimension
        for dimension in dimensions
        if dimension not in SUPPORTED_TOOL_SURFACE_SCOPE_DIMENSIONS
    ]
    if unsupported:
        return {
            "message": "Tool declares scope dimensions that Phase 6a cannot enforce.",
            "details": {
                "scope_dimensions": dimensions,
                "unsupported_scope_dimensions": unsupported,
                "supported_scope_dimensions": sorted(SUPPORTED_TOOL_SURFACE_SCOPE_DIMENSIONS),
            },
        }
    if has_stock_param and not declares_stock_scope:
        return {
            "message": "Tool has stock_code parameter but does not declare stock scope.",
            "details": {
                "scope_dimensions": dimensions,
                "missing_scope_dimension": "stock",
            },
        }
    if declares_stock_scope and not has_stock_param:
        return {
            "message": "Tool declares stock scope but has no stock_code parameter.",
            "details": {
                "scope_dimensions": dimensions,
                "missing_parameter": "stock_code",
            },
        }
    if "account" in dimensions and not any(
        param.name == "account_id" for param in tool_def.parameters
    ):
        return {
            "message": "Tool declares account scope but has no account_id parameter.",
            "details": {
                "scope_dimensions": dimensions,
                "missing_parameter": "account_id",
            },
        }
    if "symbol" in dimensions and not any(
        param.name in {"symbol", "symbol_code", "stock_code"}
        for param in tool_def.parameters
    ):
        return {
            "message": "Tool declares symbol scope but has no symbol parameter.",
            "details": {
                "scope_dimensions": dimensions,
                "missing_parameter": "symbol",
            },
        }
    return None


def _validate_parameter_value(param: ToolParameter, value: Any) -> Optional[str]:
    if value is None:
        return f"argument {param.name} must not be null"
    if param.enum and value not in param.enum:
        return f"argument {param.name} must be one of: {', '.join(map(str, param.enum))}"
    expected = _JSON_TYPE_TO_PYTHON.get(param.type)
    if not expected:
        return None
    if param.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"argument {param.name} must be integer"
        return None
    if param.type == "number":
        if isinstance(value, bool) or not isinstance(value, expected):
            return f"argument {param.name} must be number"
        return None
    if not isinstance(value, expected):
        return f"argument {param.name} must be {param.type}"
    return None


def _truncate_text_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    if max_bytes <= 0:
        return "", True
    marker = "<truncated>"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return raw[:max_bytes].decode("utf-8", errors="ignore"), True
    prefix = raw[: max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return f"{prefix}{marker}", True


def _public_payload_from_result_text(result_text: str) -> Any:
    try:
        return json.loads(result_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return result_text
