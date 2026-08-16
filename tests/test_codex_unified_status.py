"""Task 9 contracts for unified Codex status, quick checks and smoke gates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.services.generation_backend_status_service import GenerationBackendStatusService


def _codex_map() -> dict[str, str]:
    return {
        "GENERATION_BACKEND": "codex_app_server",
        "GENERATION_FALLBACK_BACKEND": "",
        "AGENT_BACKEND": "codex_app_server",
        "AGENT_ARCH": "single",
        "AGENT_MODE": "true",
        "CODEX_MODEL": "gpt-codex-test",
    }


def _capability_payload() -> dict:
    return {
        "backend": "codex_app_server",
        "available": True,
        "experimental": True,
        "version": "codex-cli test",
        "error_code": None,
        "message": None,
        "platform": {"system": "darwin", "native_windows": False},
        "binary": {"name": "codex", "available": True},
        "protocol": {"status": "passed"},
    }


def test_unified_status_reports_effective_codex_account_and_rate_limits(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.services.agent_backend_status_service.AgentBackendStatusService.codex_capability_status",
        lambda self: _capability_payload(),
    )

    account_probe_calls = []

    def account_probe(config):
        account_probe_calls.append(config)
        return {
            "status": "checked",
            "account": {"status": "authenticated", "auth_method": "chatgpt", "plan_type": "plus"},
            "rate_limits": {"snapshots": [{"limit_id": "codex"}]},
            "rate_limit_error_code": None,
        }

    payload = GenerationBackendStatusService(
        effective_map=_codex_map(),
        account_probe=account_probe,
    ).quick_check()

    assert payload["generation"]["primary"] == "codex_app_server"
    assert payload["agent"]["backend"] == "codex_app_server"
    assert payload["unified_codex_effective"] is True
    assert payload["codex_model"] == "gpt-codex-test"
    assert payload["codex"]["account_status"] == "authenticated"
    assert payload["codex"]["rate_limits"]["snapshots"][0]["limit_id"] == "codex"
    assert payload["capability_exceptions"]["vision"]["supported"] is False
    assert payload["capability_exceptions"]["deep_research"]["error_code"] == "unsupported_capability"
    assert len(account_probe_calls) == 1


def test_status_without_quick_check_does_not_start_account_probe() -> None:
    called = []
    service = GenerationBackendStatusService(
        effective_map=_codex_map(),
        account_probe=lambda _config: called.append(True),
    )

    payload = service.get_status()

    assert called == []
    assert payload["codex"]["account_status"] is None
    assert payload["codex"]["selected"] is True


def test_codex_smoke_returns_preflight_quota_warning_without_generation() -> None:
    generated = []

    class _Analyzer:
        def __init__(self, _config):
            generated.append(True)

    service = GenerationBackendStatusService(
        effective_map=_codex_map(),
        analyzer_factory=_Analyzer,
    )

    payload = service.smoke_test(
        backend_id="codex_app_server",
        mode="json",
        confirm_quota_risk=False,
    )

    assert payload["success"] is False
    assert payload["requires_confirmation"] is True
    assert payload["quota_risk"]["may_consume_subscription_quota"] is True
    assert payload["scope"] == {
        "tools_enabled": False,
        "market_data_access": False,
        "persist_report": False,
    }
    assert generated == []


def test_generation_status_schema_accepts_codex_backend_and_additive_projection() -> None:
    from api.v1.schemas.system_config import GenerationBackendStatusResponse

    payload = GenerationBackendStatusService(
        effective_map={
            "GENERATION_BACKEND": "codex_app_server",
            "GENERATION_FALLBACK_BACKEND": "",
        }
    ).get_status()
    response = GenerationBackendStatusResponse.model_validate(payload)

    assert response.primary.backend_type == "codex_app_server"
    assert response.codex["selected"] is True


def test_quota_probe_csrf_rejects_cross_origin_and_missing_origin(monkeypatch) -> None:
    from api.v1.endpoints.system_config import _require_admin_csrf

    monkeypatch.setattr("api.v1.endpoints.system_config.is_auth_enabled", lambda: True)
    monkeypatch.setattr("api.v1.endpoints.system_config.refresh_auth_state", lambda: None)
    monkeypatch.setattr("api.v1.endpoints.system_config.verify_session", lambda _value: True)

    def request(*, headers):
        return SimpleNamespace(
            cookies={"dsa_session": "valid"},
            headers=headers,
            url=SimpleNamespace(netloc="dsa.example.test"),
        )

    with pytest.raises(Exception) as cross_origin:
        _require_admin_csrf(
            request(headers={"host": "dsa.example.test", "origin": "https://evil.example"})
        )
    assert getattr(cross_origin.value, "status_code", None) == 403

    with pytest.raises(Exception) as missing_origin:
        _require_admin_csrf(request(headers={"host": "dsa.example.test"}))
    assert getattr(missing_origin.value, "status_code", None) == 403

    _require_admin_csrf(
        request(headers={"host": "dsa.example.test", "origin": "https://dsa.example.test"})
    )
