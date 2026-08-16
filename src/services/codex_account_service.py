# -*- coding: utf-8 -*-
"""Process-scoped Codex App Server account control for the local DSA UI.

Chat turns intentionally use ephemeral transports.  Login is different: the
App Server must remain alive after ``account/login/start`` while the browser
or device-code flow completes.  This service therefore owns one lazy,
process-scoped account transport and exposes only the typed, token-free
projection from :mod:`src.agent.codex_account`.
"""

from __future__ import annotations

import atexit
import threading
from typing import Any, Callable, Dict, Optional

from src.agent.codex_app_server_transport import (
    PERMISSION_PROFILE,
    CodexAppServerError,
    CodexAppServerTransport,
    build_hardened_command,
)
from src.agent.codex_account import CodexAccountProtocolError
from src.agent.tool_surface import ToolSurface
from src.agent.tools.execution import ToolAccessContext


class CodexAccountServiceError(RuntimeError):
    """Stable service-layer error that API handlers can map to HTTP."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CodexAccountService:
    """Keep one App Server alive for account login and status requests."""

    _instance: Optional["CodexAccountService"] = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        config: Any = None,
        *,
        transport_factory: Callable[..., CodexAppServerTransport] = CodexAppServerTransport,
        command_factory: Callable[..., list[str]] = build_hardened_command,
    ) -> None:
        self.config = config
        self.transport_factory = transport_factory
        self.command_factory = command_factory
        self._lock = threading.RLock()
        self._transport: Optional[CodexAppServerTransport] = None
        self._closed = False

    @classmethod
    def get_instance(cls, config: Any = None) -> "CodexAccountService":
        with cls._instance_lock:
            if cls._instance is None or cls._instance._closed:
                cls._instance = cls(config=config)
                atexit.register(cls._instance.close)
            elif config is not None:
                cls._instance.config = config
            return cls._instance

    @property
    def transport(self) -> Optional[CodexAppServerTransport]:
        with self._lock:
            return self._transport

    def status(self) -> Dict[str, Any]:
        with self._lock:
            transport = self._ensure_transport()
            account = self._call(transport.read_account)
            rate_limits = None
            rate_limit_error_code = None
            try:
                rate_limits = transport.read_account_rate_limits()
            except CodexAppServerError as exc:
                # A signed-out account commonly has no rate-limit snapshot.
                # Keep the account state useful while exposing a stable reason
                # for the UI instead of turning it into an opaque 500.
                if exc.code in {"login_required", "rate_limit_exceeded", "protocol_error"}:
                    rate_limit_error_code = exc.code
                else:
                    raise self._service_error(exc) from exc
            return {
                "account": account.to_dict(),
                "rate_limits": rate_limits.to_dict() if rate_limits is not None else None,
                "rate_limit_error_code": rate_limit_error_code,
                "notifications": [item.to_dict() for item in transport.account_notifications],
            }

    def start_login(self, mode: str = "browser") -> Dict[str, Any]:
        with self._lock:
            try:
                return self._ensure_transport().start_account_login(mode).to_dict()
            except CodexAccountProtocolError as exc:
                raise CodexAccountServiceError("protocol_error", str(exc)) from exc

    def cancel_login(self, login_id: str) -> Dict[str, Any]:
        with self._lock:
            try:
                return self._ensure_transport().cancel_account_login(login_id).to_dict()
            except CodexAccountProtocolError as exc:
                raise CodexAccountServiceError("protocol_error", str(exc)) from exc

    def logout(self) -> Dict[str, Any]:
        with self._lock:
            return self._ensure_transport().logout_account().to_dict()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            transport = self._transport
            self._transport = None
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    # Process teardown must not turn into an atexit traceback.
                    pass

    def _ensure_transport(self) -> CodexAppServerTransport:
        if self._closed:
            raise CodexAccountServiceError("service_closed", "Codex account service is closed")
        if self._transport is not None:
            process = getattr(self._transport, "process", None)
            if process is None or process.poll() is None:
                return self._transport
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None

        timeout = self._timeout_seconds()
        try:
            command = self.command_factory(timeout=timeout)
            transport = self.transport_factory(
                command,
                tool_surface=ToolSurface.empty(),
                tool_context=ToolAccessContext(backend="codex_app_server"),
                request_timeout=timeout,
                tool_profile=PERMISSION_PROFILE,
            )
            transport.start()
        except CodexAppServerError as exc:
            raise self._service_error(exc) from exc
        except (OSError, RuntimeError, TypeError) as exc:
            raise CodexAccountServiceError(
                "capability_unsupported",
                "Codex account App Server could not be started",
            ) from exc
        self._transport = transport
        return transport

    def _timeout_seconds(self) -> float:
        value = getattr(self.config, "agent_orchestrator_timeout_s", 120) if self.config is not None else 120
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            timeout = 120.0
        return max(5.0, min(timeout, 120.0))

    @staticmethod
    def _service_error(exc: CodexAppServerError) -> CodexAccountServiceError:
        return CodexAccountServiceError(exc.code, str(exc))

    @staticmethod
    def _call(callback: Callable[[], Any]) -> Any:
        try:
            return callback()
        except CodexAppServerError as exc:
            raise CodexAccountService._service_error(exc) from exc


__all__ = ["CodexAccountService", "CodexAccountServiceError"]
