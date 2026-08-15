# -*- coding: utf-8 -*-
"""API contract tests for Codex account control."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.v1.endpoints import agent as agent_endpoint
from api.v1.schemas.agent import CodexAccountCancelRequest, CodexAccountLoginRequest
from src.services.codex_account_service import CodexAccountService, CodexAccountServiceError


def _codex_config():
    return SimpleNamespace(
        agent_backend="codex_app_server",
        agent_arch="single",
        agent_orchestrator_timeout_s=30,
        _agent_mode_explicit=False,
        agent_mode=True,
    )


class _FakeTransport:
    process = None

    def __init__(self) -> None:
        self.started = False
        self.account_notifications = ()

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.started = False

    def read_account(self):
        from src.agent.codex_account import CodexAccountStatus

        return CodexAccountStatus(
            status="authenticated",
            auth_method="chatgpt",
            email="investor@example.test",
            plan_type="plus",
        )

    def read_account_rate_limits(self):
        from src.agent.codex_account import CodexRateLimitSnapshot, CodexRateLimitWindow, CodexRateLimits

        return CodexRateLimits(
            snapshots=(
                CodexRateLimitSnapshot(
                    limit_id="codex",
                    plan_type="plus",
                    primary=CodexRateLimitWindow(
                        bucket="primary",
                        used_percent=12,
                        window_duration_minutes=300,
                        resets_at=1_700_000_000,
                    ),
                ),
            )
        )

    def start_account_login(self, mode):
        from src.agent.codex_account import CodexLoginStart

        return CodexLoginStart(
            mode=mode,
            status="pending",
            login_id="login-1",
            auth_url="https://auth.example.test" if mode == "browser" else None,
            verification_url="https://auth.example.test/device" if mode == "device_code" else None,
            user_code="ABCD-EFGH" if mode == "device_code" else None,
        )

    def cancel_account_login(self, login_id):
        from src.agent.codex_account import CodexLoginCancellation

        return CodexLoginCancellation("cancelled")

    def logout_account(self):
        from src.agent.codex_account import CodexLogoutResult

        return CodexLogoutResult()


def _service() -> CodexAccountService:
    fake = _FakeTransport()
    return CodexAccountService(
        config=_codex_config(),
        command_factory=lambda **_: ["fake-codex"],
        transport_factory=lambda *args, **kwargs: fake,
    )


def test_account_service_keeps_one_transport_and_returns_safe_status() -> None:
    service = _service()
    first = service.status()
    second = service.status()

    assert first["account"]["plan_type"] == "plus"
    assert first["rate_limits"]["snapshots"][0]["primary"]["resets_at"] == 1_700_000_000
    assert second["account"]["auth_method"] == "chatgpt"
    assert service.transport is not None
    service.close()


def test_account_api_exposes_browser_login_and_logout_actions() -> None:
    service = _service()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(agent_endpoint, "get_config", _codex_config)
        login = asyncio.run(
            agent_endpoint.start_codex_account_login(
                CodexAccountLoginRequest(mode="browser"), service=service
            )
        )
        logout = asyncio.run(agent_endpoint.logout_codex_account(service=service))

    assert login.login_id == "login-1"
    assert login.auth_url == "https://auth.example.test"
    assert logout.status == "signed_out"
    service.close()


def test_account_api_cancel_validates_and_returns_normalized_status() -> None:
    service = _service()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(agent_endpoint, "get_config", _codex_config)
        result = asyncio.run(
            agent_endpoint.cancel_codex_account_login(
                CodexAccountCancelRequest(login_id="login-1"), service=service
            )
        )
    assert result.status == "cancelled"
    service.close()


def test_account_api_maps_service_errors_to_structured_http_error() -> None:
    service = _service()
    service.start_login = lambda mode="browser": (_ for _ in ()).throw(
        CodexAccountServiceError("login_required", "login required")
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(agent_endpoint, "get_config", _codex_config)
        with pytest.raises(HTTPException) as caught:
            asyncio.run(
                agent_endpoint.start_codex_account_login(
                    CodexAccountLoginRequest(), service=service
                )
            )
    assert caught.value.status_code == 409
    assert caught.value.detail == {"error": "login_required", "message": "login required"}
    service.close()
