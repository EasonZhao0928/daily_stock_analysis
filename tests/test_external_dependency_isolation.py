"""Deterministic boundaries when one external integration is unavailable."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time

from bot.platforms.wechat_ilink import FakeILinkServer, WeChatChannelAdapter, WeChatILinkClient
from data_provider.market_data_types import DataQuery, SourcePolicy
from src.notification import ChannelAttemptResult, NotificationDispatchResult
from src.paper_account import PaperAccountService
from src.paper_account.repository import PaperAccountRepository
from src.services.alert_service import AlertService
from src.services.portfolio_ledger_types import LEDGER_EVENT_CASH, LedgerCommand
from src.storage import DatabaseManager


class _UnavailableCodexController:
    def trace(self):
        return {"backend": "codex_app_server", "status": "unavailable"}

    def generate(self, _observation):
        raise RuntimeError("codex unavailable")


class _FailedNotificationAdapter:
    def send_with_results(self, *_args, **_kwargs):
        return NotificationDispatchResult(
            dispatched=True,
            success=False,
            status="all_failed",
            channel_results=[ChannelAttemptResult(
                channel="pushplus",
                success=False,
                error_code="provider_unavailable",
                retryable=True,
            )],
        )


def test_codex_and_wechat_failure_do_not_poison_account_ledger(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'isolation.db'}")
    try:
        paper = PaperAccountService(PaperAccountRepository(db))
        account = paper.create_account(name="isolated", initial_cash=1000)
        account_id = int(account["account"]["id"])
        try:
            paper.run_cycle(
                account_id,
                decision_at=datetime(2026, 8, 14, 10, 0),
                strategy_version="codex-outage",
                account_snapshot={"cash": 1000, "positions": []},
                market_data={"600519": {"close": 100, "is_stale": False, "as_of": "2026-08-14"}},
                controller=_UnavailableCodexController(),
            )
        except RuntimeError as exc:
            assert "codex unavailable" in str(exc)
        else:
            raise AssertionError("unavailable Codex controller must fail the cycle")

        server = FakeILinkServer(token_expired=True)
        adapter = WeChatChannelAdapter(
            WeChatILinkClient(base_url="https://fake.invalid", token="token", transport=server),
            enabled=True,
            poll_timeout_ms=1000,
            idle_sleep_seconds=0.01,
        )
        adapter.start(lambda _message: True)
        deadline = time.time() + 1
        while adapter.health().auth_state != "login_lost" and time.time() < deadline:
            time.sleep(0.01)
        adapter.stop()
        assert adapter.health().auth_state == "login_lost"

        receipt = paper.portfolio.submit(LedgerCommand(
            account_id=account_id,
            kind=LEDGER_EVENT_CASH,
            payload={"event_date": "2026-08-14", "direction": "in", "amount": 500, "currency": "CNY"},
        ))
        assert receipt.accepted is True
    finally:
        DatabaseManager.reset_instance()


def test_notification_provider_failure_keeps_alert_trigger_and_attempt(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'alert-isolation.db'}")
    try:
        service = AlertService(db)
        service.create_rule({
            "name": "paper fill outage",
            "target_scope": "paper_account",
            "target": "1",
            "alert_type": "paper_fill",
            "parameters": {"active": True, "message": "fill", "event_id": "fill-isolation"},
        })
        cycle = service.run_cycle(notifier=_FailedNotificationAdapter())
        assert cycle["triggered"] == 1
        assert cycle["notification_attempts"] == 1
        assert service.list_triggers(status="triggered", page_size=20)["total"] == 1
        assert service.list_notifications(page_size=20)["total"] == 1
    finally:
        DatabaseManager.reset_instance()

