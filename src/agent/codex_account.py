# -*- coding: utf-8 -*-
"""Typed, token-free client for the Codex App Server account surface.

The App Server owns ChatGPT authentication and token persistence.  DSA only
requests the account state that is safe to display and keeps a small typed
projection of account notifications.  Parsers in this module deliberately
whitelist response fields instead of copying arbitrary JSON so a future
protocol addition cannot accidentally expose access or refresh tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple


AccountRequest = Callable[[str, dict], dict]

ACCOUNT_NOTIFICATION_METHODS = frozenset(
    {
        "account/updated",
        "account/rateLimits/updated",
        "account/login/completed",
    }
)

_PLAN_TYPES = frozenset(
    {
        "free",
        "go",
        "plus",
        "pro",
        "prolite",
        "team",
        "self_serve_business_prolite",
        "self_serve_business_usage_based",
        "business",
        "ent26",
        "enterprise_cbp_automation",
        "enterprise_cbp_usage_based",
        "enterprise",
        "edu",
        "unknown",
    }
)


class CodexAccountProtocolError(ValueError):
    """Raised when an account response cannot be safely projected."""

    def __init__(self, message: str, *, method: Optional[str] = None) -> None:
        super().__init__(message)
        self.method = method


def _string(value: Any, *, field: str, required: bool = False) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    if required:
        raise CodexAccountProtocolError(f"Codex account response missing {field}")
    return None


def _bool(value: Any, *, field: str, required: bool = False) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if required:
        raise CodexAccountProtocolError(f"Codex account response missing {field}")
    return None


def _integer(
    value: Any,
    *,
    field: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    required: bool = False,
) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        if minimum is not None and value < minimum:
            return None
        if maximum is not None and value > maximum:
            return None
        return value
    if required:
        raise CodexAccountProtocolError(f"Codex account response missing {field}")
    return None


def _plan_type(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return "unknown"
    return value if value in _PLAN_TYPES else "unknown"


def _mapping(value: Any, *, field: str, required: bool = False) -> Optional[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return value
    if required:
        raise CodexAccountProtocolError(f"Codex account response missing {field}")
    return None


@dataclass(frozen=True)
class CodexAccountStatus:
    """Safe projection of ``account/read`` and account update state."""

    status: str
    auth_method: Optional[str] = None
    email: Optional[str] = None
    plan_type: Optional[str] = None
    requires_openai_auth: Optional[bool] = None

    @classmethod
    def from_response(cls, payload: Mapping[str, Any]) -> "CodexAccountStatus":
        if not isinstance(payload, Mapping):
            raise CodexAccountProtocolError("Codex account/read result must be an object")
        requires_auth = _bool(payload.get("requiresOpenaiAuth"), field="requiresOpenaiAuth")
        account = payload.get("account")
        if account is None:
            return cls(
                status="signed_out",
                requires_openai_auth=requires_auth,
            )
        account_map = _mapping(account, field="account", required=True)
        account_type = _string(account_map.get("type"), field="account.type", required=True)
        if account_type == "chatgpt":
            return cls(
                status="authenticated",
                auth_method="chatgpt",
                email=_string(account_map.get("email"), field="account.email"),
                plan_type=_plan_type(account_map.get("planType")),
                requires_openai_auth=requires_auth,
            )
        if account_type == "apiKey":
            return cls(
                status="authenticated",
                auth_method="apiKey",
                requires_openai_auth=requires_auth,
            )
        if account_type == "amazonBedrock":
            return cls(
                status="authenticated",
                auth_method="amazonBedrock",
                requires_openai_auth=requires_auth,
            )
        return cls(
            status="protocol_error",
            auth_method=account_type,
            requires_openai_auth=requires_auth,
        )

    @classmethod
    def from_update(cls, payload: Mapping[str, Any]) -> "CodexAccountStatus":
        auth_method = _string(payload.get("authMode"), field="authMode")
        return cls(
            status="authenticated" if auth_method else "signed_out",
            auth_method=auth_method,
            plan_type=_plan_type(payload.get("planType")),
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "auth_method": self.auth_method,
            "email": self.email,
            "plan_type": self.plan_type,
            "requires_openai_auth": self.requires_openai_auth,
        }


@dataclass(frozen=True)
class CodexLoginStart:
    """Browser or device-code login hand-off returned by App Server."""

    mode: str
    status: str
    login_id: str
    auth_url: Optional[str] = None
    verification_url: Optional[str] = None
    user_code: Optional[str] = None

    @classmethod
    def from_response(
        cls,
        payload: Mapping[str, Any],
        *,
        requested_mode: str,
    ) -> "CodexLoginStart":
        if not isinstance(payload, Mapping):
            raise CodexAccountProtocolError("Codex account/login/start result must be an object")
        response_type = _string(payload.get("type"), field="type", required=True)
        if response_type == "chatgpt":
            login_id = _string(payload.get("loginId"), field="loginId", required=True)
            auth_url = _string(payload.get("authUrl"), field="authUrl", required=True)
            return cls(
                mode="browser",
                status="pending",
                login_id=login_id,
                auth_url=auth_url,
            )
        if response_type == "chatgptDeviceCode":
            login_id = _string(payload.get("loginId"), field="loginId", required=True)
            verification_url = _string(
                payload.get("verificationUrl"),
                field="verificationUrl",
                required=True,
            )
            user_code = _string(payload.get("userCode"), field="userCode", required=True)
            return cls(
                mode="device_code",
                status="pending",
                login_id=login_id,
                verification_url=verification_url,
                user_code=user_code,
            )
        raise CodexAccountProtocolError(
            f"Unsupported Codex account login response type: {response_type}"
        )

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "status": self.status,
            "login_id": self.login_id,
            "auth_url": self.auth_url,
            "verification_url": self.verification_url,
            "user_code": self.user_code,
        }


@dataclass(frozen=True)
class CodexLoginCancellation:
    status: str

    @classmethod
    def from_response(cls, payload: Mapping[str, Any]) -> "CodexLoginCancellation":
        status = _string(payload.get("status"), field="status", required=True)
        normalized = {
            "canceled": "cancelled",
            "notFound": "not_found",
        }.get(status, "protocol_error")
        return cls(status=normalized)

    def to_dict(self) -> dict:
        return {"status": self.status}


@dataclass(frozen=True)
class CodexLogoutResult:
    status: str = "signed_out"

    def to_dict(self) -> dict:
        return {"status": self.status}


@dataclass(frozen=True)
class CodexRateLimitWindow:
    bucket: str
    used_percent: int
    window_duration_minutes: Optional[int] = None
    resets_at: Optional[int] = None

    @classmethod
    def from_response(cls, payload: Mapping[str, Any], *, bucket: str) -> Optional["CodexRateLimitWindow"]:
        if not isinstance(payload, Mapping):
            return None
        used_percent = _integer(
            payload.get("usedPercent"),
            field="usedPercent",
            minimum=0,
            maximum=100,
            required=True,
        )
        duration = _integer(
            payload.get("windowDurationMins"),
            field="windowDurationMins",
            minimum=0,
        )
        resets_at = _integer(payload.get("resetsAt"), field="resetsAt", minimum=0)
        return cls(
            bucket=bucket,
            used_percent=used_percent,
            window_duration_minutes=duration,
            resets_at=resets_at,
        )

    def to_dict(self) -> dict:
        return {
            "bucket": self.bucket,
            "used_percent": self.used_percent,
            "window_duration_minutes": self.window_duration_minutes,
            "resets_at": self.resets_at,
        }


@dataclass(frozen=True)
class CodexRateLimitSnapshot:
    limit_id: Optional[str] = None
    limit_name: Optional[str] = None
    plan_type: Optional[str] = None
    primary: Optional[CodexRateLimitWindow] = None
    secondary: Optional[CodexRateLimitWindow] = None
    spend_control_reached: Optional[bool] = None
    rate_limit_reached_type: Optional[str] = None

    @classmethod
    def from_response(cls, payload: Mapping[str, Any]) -> "CodexRateLimitSnapshot":
        if not isinstance(payload, Mapping):
            raise CodexAccountProtocolError("Codex rate-limit snapshot must be an object")
        return cls(
            limit_id=_string(payload.get("limitId"), field="limitId"),
            limit_name=_string(payload.get("limitName"), field="limitName"),
            plan_type=_plan_type(payload.get("planType")),
            primary=CodexRateLimitWindow.from_response(payload.get("primary"), bucket="primary"),
            secondary=CodexRateLimitWindow.from_response(payload.get("secondary"), bucket="secondary"),
            spend_control_reached=_bool(payload.get("spendControlReached"), field="spendControlReached"),
            rate_limit_reached_type=_string(
                payload.get("rateLimitReachedType"),
                field="rateLimitReachedType",
            ),
        )

    def to_dict(self) -> dict:
        return {
            "limit_id": self.limit_id,
            "limit_name": self.limit_name,
            "plan_type": self.plan_type,
            "primary": self.primary.to_dict() if self.primary else None,
            "secondary": self.secondary.to_dict() if self.secondary else None,
            "spend_control_reached": self.spend_control_reached,
            "rate_limit_reached_type": self.rate_limit_reached_type,
        }


@dataclass(frozen=True)
class CodexRateLimits:
    snapshots: Tuple[CodexRateLimitSnapshot, ...]
    available_reset_credits: Optional[int] = None

    @classmethod
    def from_response(cls, payload: Mapping[str, Any]) -> "CodexRateLimits":
        if not isinstance(payload, Mapping):
            raise CodexAccountProtocolError("Codex account/rateLimits/read result must be an object")
        historical = _mapping(payload.get("rateLimits"), field="rateLimits", required=True)
        snapshots = [CodexRateLimitSnapshot.from_response(historical)]
        by_id = payload.get("rateLimitsByLimitId")
        if isinstance(by_id, Mapping):
            for limit_id in sorted(by_id):
                value = by_id.get(limit_id)
                if not isinstance(limit_id, str) or not isinstance(value, Mapping):
                    continue
                snapshot = CodexRateLimitSnapshot.from_response(value)
                if snapshot.limit_id is None:
                    snapshot = CodexRateLimitSnapshot(
                        limit_id=limit_id,
                        limit_name=snapshot.limit_name,
                        plan_type=snapshot.plan_type,
                        primary=snapshot.primary,
                        secondary=snapshot.secondary,
                        spend_control_reached=snapshot.spend_control_reached,
                        rate_limit_reached_type=snapshot.rate_limit_reached_type,
                    )
                snapshots.append(snapshot)
        reset_credits = _mapping(payload.get("rateLimitResetCredits"), field="rateLimitResetCredits")
        available_count = None
        if reset_credits is not None:
            available_count = _integer(
                reset_credits.get("availableCount"),
                field="availableCount",
                minimum=0,
            )
        return cls(snapshots=tuple(snapshots), available_reset_credits=available_count)

    @classmethod
    def from_update(cls, payload: Mapping[str, Any]) -> "CodexRateLimits":
        snapshot = _mapping(payload.get("rateLimits"), field="rateLimits", required=True)
        return cls(snapshots=(CodexRateLimitSnapshot.from_response(snapshot),))

    def to_dict(self) -> dict:
        return {
            "snapshots": [snapshot.to_dict() for snapshot in self.snapshots],
            "available_reset_credits": self.available_reset_credits,
        }


@dataclass(frozen=True)
class CodexAccountNotification:
    """Whitelisted, ordered account event projection."""

    method: str
    status: str
    account: Optional[CodexAccountStatus] = None
    rate_limits: Optional[CodexRateLimits] = None
    login_id: Optional[str] = None
    success: Optional[bool] = None
    error: Optional[str] = None

    @classmethod
    def from_message(cls, method: str, payload: Mapping[str, Any]) -> Optional["CodexAccountNotification"]:
        if method not in ACCOUNT_NOTIFICATION_METHODS:
            return None
        if not isinstance(payload, Mapping):
            raise CodexAccountProtocolError(f"{method} notification must be an object", method=method)
        if method == "account/updated":
            account = CodexAccountStatus.from_update(payload)
            return cls(method=method, status=account.status, account=account)
        if method == "account/rateLimits/updated":
            rate_limits = CodexRateLimits.from_update(payload)
            return cls(method=method, status="updated", rate_limits=rate_limits)
        success = _bool(payload.get("success"), field="success", required=True)
        login_id = _string(payload.get("loginId"), field="loginId")
        return cls(
            method=method,
            status="login_completed" if success else "login_failed",
            login_id=login_id,
            success=success,
            # Do not return backend error text: it is not needed by the UI and
            # could contain credentials or other sensitive diagnostics.
            error="login_failed" if not success and payload.get("error") else None,
        )

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "status": self.status,
            "account": self.account.to_dict() if self.account else None,
            "rate_limits": self.rate_limits.to_dict() if self.rate_limits else None,
            "login_id": self.login_id,
            "success": self.success,
            "error": self.error,
        }


class CodexAccountClient:
    """Small RPC facade that never accepts or returns credential material."""

    def __init__(self, request: AccountRequest) -> None:
        if not callable(request):
            raise TypeError("request must be callable")
        self._request = request

    def read(self, *, refresh_token: bool = False) -> CodexAccountStatus:
        params = {"refreshToken": True} if refresh_token else {}
        return CodexAccountStatus.from_response(self._request("account/read", params))

    def start_login(self, mode: str = "browser") -> CodexLoginStart:
        normalized_mode = {"browser": "browser", "device-code": "device_code", "device_code": "device_code"}.get(
            mode
        )
        if normalized_mode is None:
            raise ValueError("mode must be 'browser' or 'device_code'")
        params = {"type": "chatgpt" if normalized_mode == "browser" else "chatgptDeviceCode"}
        response = self._request("account/login/start", params)
        return CodexLoginStart.from_response(response, requested_mode=normalized_mode)

    def cancel_login(self, login_id: str) -> CodexLoginCancellation:
        if not isinstance(login_id, str) or not login_id:
            raise ValueError("login_id must not be empty")
        response = self._request("account/login/cancel", {"loginId": login_id})
        return CodexLoginCancellation.from_response(response)

    def logout(self) -> CodexLogoutResult:
        self._request("account/logout", {})
        return CodexLogoutResult()

    def read_rate_limits(self) -> CodexRateLimits:
        return CodexRateLimits.from_response(self._request("account/rateLimits/read", {}))

    @staticmethod
    def parse_notification(method: str, payload: Mapping[str, Any]) -> Optional[CodexAccountNotification]:
        return CodexAccountNotification.from_message(method, payload)


__all__ = [
    "ACCOUNT_NOTIFICATION_METHODS",
    "CodexAccountClient",
    "CodexAccountNotification",
    "CodexAccountProtocolError",
    "CodexAccountStatus",
    "CodexLoginCancellation",
    "CodexLoginStart",
    "CodexLogoutResult",
    "CodexRateLimitSnapshot",
    "CodexRateLimitWindow",
    "CodexRateLimits",
]
