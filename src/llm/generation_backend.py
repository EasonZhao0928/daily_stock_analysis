# -*- coding: utf-8 -*-
"""Shared generation backend contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Mapping
import re
from typing import Any, Callable, Dict, Optional, Protocol


class GenerationErrorCode(str, Enum):
    """Structured generation backend error codes.

    Shared across LiteLLM and local CLI generation backends.
    """

    BACKEND_NOT_CONFIGURED = "backend_not_configured"
    COMMAND_NOT_FOUND = "command_not_found"
    COMMAND_NOT_EXECUTABLE = "command_not_executable"
    TIMEOUT = "timeout"
    NON_ZERO_EXIT = "non_zero_exit"
    EMPTY_OUTPUT = "empty_output"
    OUTPUT_TOO_LARGE = "output_too_large"
    INVALID_JSON = "invalid_json"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    UNSUPPORTED_TOOL_CALLING = "unsupported_tool_calling"
    INTERACTIVE_PROMPT_REQUIRED = "interactive_prompt_required"
    APPROVAL_REQUIRED = "approval_required"
    LOGIN_REQUIRED = "login_required"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    CANCELLED = "cancelled"
    PROTOCOL_ERROR = "protocol_error"
    PROCESS_FAILED = "process_failed"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    UNSAFE_CONFIG = "unsafe_config"
    UNKNOWN_BACKEND_ERROR = "unknown_backend_error"


@dataclass(frozen=True)
class GenerationCapabilities:
    """Backend capability flags surfaced to resolvers and diagnostics."""

    supports_json: bool
    supports_tools: bool
    supports_stream: bool
    supports_vision: bool
    supports_health_check: bool
    supports_smoke_test: bool


@dataclass
class GenerationResult:
    """Normalized result returned by generation backends."""

    text: str
    model: str
    provider: str
    backend: str
    usage: Dict[str, Any]
    raw: Any = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerationError(Exception):
    """Structured generation backend failure.

    ``stage`` is intentionally descriptive rather than a closed enum. Current
    generation paths use values such as ``generation``, ``configuration``,
    ``execution``, ``validation``, and ``fallback``.
    """

    error_code: GenerationErrorCode
    stage: str
    retryable: bool
    fallbackable: bool
    backend: str
    provider: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider:
            self.provider = self.backend
        Exception.__init__(self, self.message)

    @property
    def message(self) -> str:
        return f"{self.error_code.value} at {self.stage} for backend {self.backend}"

    @property
    def user_message(self) -> str:
        """Return a stable, credential-free message suitable for an API/UI."""
        return _GENERATION_USER_MESSAGES.get(
            self.error_code,
            "模型生成暂时无法完成，请检查生成后端状态后重试。",
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize a safe, stable error envelope.

        Backend details are deliberately filtered here rather than exposing a
        provider exception or raw App Server payload to API callers.  The
        envelope keeps both ``code`` and the historical ``error_code`` key so
        existing clients can migrate without changing failure semantics.
        """
        code = self.error_code.value
        return {
            "code": code,
            "error_code": code,
            "stage": str(self.stage),
            "backend": str(self.backend),
            "provider": str(self.provider or self.backend),
            "retryable": bool(self.retryable),
            "fallbackable": bool(self.fallbackable),
            "message": self.message,
            "user_message": self.user_message,
            "details": _safe_error_details(self.details),
        }


_GENERATION_USER_MESSAGES = {
    GenerationErrorCode.BACKEND_NOT_CONFIGURED: "生成后端尚未正确配置，请检查设置后重试。",
    GenerationErrorCode.COMMAND_NOT_FOUND: "当前设备找不到生成后端程序，请检查安装和 PATH。",
    GenerationErrorCode.COMMAND_NOT_EXECUTABLE: "生成后端程序不可执行，请检查文件权限。",
    GenerationErrorCode.TIMEOUT: "模型生成超时，请稍后重试或调大超时设置。",
    GenerationErrorCode.NON_ZERO_EXIT: "生成后端进程异常退出，请检查运行日志。",
    GenerationErrorCode.EMPTY_OUTPUT: "模型返回了空结果，请稍后重试。",
    GenerationErrorCode.OUTPUT_TOO_LARGE: "模型返回内容超过安全限制，本次请求已停止。",
    GenerationErrorCode.INVALID_JSON: "模型返回的 JSON 无法解析，本次请求已安全停止。",
    GenerationErrorCode.SCHEMA_VALIDATION_FAILED: "模型返回内容不符合业务格式，本次请求已安全停止。",
    GenerationErrorCode.UNSUPPORTED_TOOL_CALLING: "当前生成后端不支持所需工具能力。",
    GenerationErrorCode.INTERACTIVE_PROMPT_REQUIRED: "当前生成后端需要交互式授权，无法在服务任务中继续。",
    GenerationErrorCode.APPROVAL_REQUIRED: "生成后端请求了未允许的授权，本次请求已安全停止。",
    GenerationErrorCode.LOGIN_REQUIRED: "Codex 尚未登录，请先完成登录后重试。",
    GenerationErrorCode.RATE_LIMIT_EXCEEDED: "Codex 订阅额度或速率限制已达到，请稍后重试。",
    GenerationErrorCode.CANCELLED: "本次模型生成已取消。",
    GenerationErrorCode.PROTOCOL_ERROR: "生成后端协议异常，请稍后重试并检查运行日志。",
    GenerationErrorCode.PROCESS_FAILED: "生成后端进程未能安全完成，请检查运行日志。",
    GenerationErrorCode.CAPABILITY_UNSUPPORTED: "当前生成后端或平台不支持该能力。",
    GenerationErrorCode.UNSAFE_CONFIG: "生成后端配置不安全或无效，请检查设置。",
    GenerationErrorCode.UNKNOWN_BACKEND_ERROR: "模型生成暂时无法完成，请检查生成后端状态后重试。",
}

_SENSITIVE_ERROR_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "cookie",
    "prompt",
    "raw_response",
    "raw_rpc",
    "headers",
)
_ERROR_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)\s*[:=]\s*)[^\s,;]+"), r"\1<redacted>"),
    (re.compile(r"(?i)\bBearer\s+[^\s,;]+"), "Bearer <redacted>"),
)


def _safe_error_details(value: Any, *, depth: int = 0) -> Any:
    """Keep error details useful while rejecting secrets/prompts/raw RPC."""
    if depth > 3:
        return "<truncated>"
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 20:
                result["truncated"] = True
                break
            key_text = str(key)
            normalized = key_text.lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_ERROR_KEY_PARTS):
                result[key_text] = "<redacted>"
            else:
                result[key_text] = _safe_error_details(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_error_details(item, depth=depth + 1) for item in list(value)[:8]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 500:
            value = f"{value[:500]}..."
        if isinstance(value, str):
            for pattern, replacement in _ERROR_SECRET_PATTERNS:
                value = pattern.sub(replacement, value)
        return value
    return str(value)[:500]


class GenerationBackend(Protocol):
    """Protocol implemented by generation backends."""

    backend_id: str
    capabilities: GenerationCapabilities

    def generate(
        self,
        prompt: str,
        generation_config: Dict[str, Any],
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> GenerationResult:
        """Generate text with the backend."""
