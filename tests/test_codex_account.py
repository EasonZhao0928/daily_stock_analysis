# -*- coding: utf-8 -*-
"""Offline contract tests for the token-free Codex account client."""

from __future__ import annotations

import json

from src.agent.codex_account import CodexAccountClient
from src.agent.codex_app_server_transport import CodexAppServerTransport
from src.agent.tool_surface import ToolSurface
from src.agent.tools.execution import ToolAccessContext


class _RecordingRequest:
    def __init__(self, responses: dict[tuple[str, str], dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        key = (method, str(len([item for item in self.calls if item[0] == method])))
        return self.responses[key]


def _assert_token_free(value: object) -> None:
    encoded = json.dumps(value, sort_keys=True)
    for secret in (
        "accessToken",
        "refreshToken",
        "chatgptAuthTokens",
        "access-token-value",
        "refresh-token-value",
    ):
        assert secret not in encoded


def test_account_read_is_typed_and_drops_credential_fields() -> None:
    recorder = _RecordingRequest(
        {
            ("account/read", "1"): {
                "requiresOpenaiAuth": False,
                "account": {
                    "type": "chatgpt",
                    "email": "investor@example.test",
                    "planType": "plus",
                    "accessToken": "access-token-value",
                    "refreshToken": "refresh-token-value",
                },
            }
        }
    )

    account = CodexAccountClient(recorder).read()

    assert account.status == "authenticated"
    assert account.auth_method == "chatgpt"
    assert account.plan_type == "plus"
    assert account.email == "investor@example.test"
    assert recorder.calls == [("account/read", {})]
    _assert_token_free(account.to_dict())


def test_browser_and_device_code_login_use_protocol_discriminators() -> None:
    recorder = _RecordingRequest(
        {
            ("account/login/start", "1"): {
                "type": "chatgpt",
                "loginId": "browser-login-1",
                "authUrl": "https://auth.example.test/browser",
            },
            ("account/login/start", "2"): {
                "type": "chatgptDeviceCode",
                "loginId": "device-login-2",
                "verificationUrl": "https://auth.example.test/device",
                "userCode": "ABCD-EFGH",
            },
        }
    )

    browser = CodexAccountClient(recorder).start_login("browser")
    device = CodexAccountClient(recorder).start_login("device_code")

    assert browser.mode == "browser"
    assert browser.login_id == "browser-login-1"
    assert browser.auth_url == "https://auth.example.test/browser"
    assert device.mode == "device_code"
    assert device.login_id == "device-login-2"
    assert device.verification_url == "https://auth.example.test/device"
    assert device.user_code == "ABCD-EFGH"
    assert recorder.calls == [
        ("account/login/start", {"type": "chatgpt"}),
        ("account/login/start", {"type": "chatgptDeviceCode"}),
    ]
    _assert_token_free(browser.to_dict())
    _assert_token_free(device.to_dict())


def test_cancel_logout_and_rate_limits_preserve_reset_windows() -> None:
    recorder = _RecordingRequest(
        {
            ("account/login/cancel", "1"): {"status": "canceled"},
            ("account/logout", "1"): {},
            ("account/rateLimits/read", "1"): {
                "rateLimits": {
                    "limitId": "codex",
                    "limitName": "Codex",
                    "planType": "plus",
                    "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 1_700_000_000},
                    "secondary": {"usedPercent": 80, "windowDurationMins": 10_080, "resetsAt": 1_700_500_000},
                    "spendControlReached": False,
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 1_700_000_000},
                    }
                },
                "rateLimitResetCredits": {"availableCount": 2, "credits": None},
                "accessToken": "access-token-value",
            },
        }
    )
    client = CodexAccountClient(recorder)

    cancellation = client.cancel_login("login-1")
    logout = client.logout()
    limits = client.read_rate_limits()

    assert cancellation.status == "cancelled"
    assert logout.status == "signed_out"
    assert len(limits.snapshots) == 2
    assert limits.snapshots[0].primary is not None
    assert limits.snapshots[0].primary.resets_at == 1_700_000_000
    assert limits.snapshots[0].secondary is not None
    assert limits.snapshots[0].secondary.window_duration_minutes == 10_080
    assert limits.available_reset_credits == 2
    assert recorder.calls == [
        ("account/login/cancel", {"loginId": "login-1"}),
        ("account/logout", {}),
        ("account/rateLimits/read", {}),
    ]
    _assert_token_free(limits.to_dict())


def test_transport_keeps_redacted_account_notifications_in_arrival_order() -> None:
    class _Runner:
        def close(self) -> bool:
            return True

    transport = CodexAppServerTransport(
        ["fake-codex"],
        tool_surface=ToolSurface.empty(),
        tool_context=ToolAccessContext(),
        tool_runner=_Runner(),
    )
    try:
        transport._route_message(
            {
                "method": "account/login/completed",
                "params": {"success": True, "loginId": "login-1"},
            }
        )
        transport._route_message(
            {
                "method": "account/updated",
                "params": {"authMode": "chatgpt", "planType": "pro", "accessToken": "access-token-value"},
            }
        )
        transport._route_message(
            {
                "method": "account/rateLimits/updated",
                "params": {
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 33, "windowDurationMins": 300, "resetsAt": 1_700_000_001},
                    },
                    "refreshToken": "refresh-token-value",
                },
            }
        )

        notifications = transport.account_notifications
        assert [item.method for item in notifications] == [
            "account/login/completed",
            "account/updated",
            "account/rateLimits/updated",
        ]
        assert notifications[0].login_id == "login-1"
        assert notifications[1].account is not None
        assert notifications[1].account.plan_type == "pro"
        assert notifications[2].rate_limits is not None
        assert notifications[2].rate_limits.snapshots[0].primary is not None
        assert notifications[2].rate_limits.snapshots[0].primary.resets_at == 1_700_000_001
        _assert_token_free([item.to_dict() for item in notifications])
    finally:
        transport.close()
