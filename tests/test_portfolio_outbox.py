# -*- coding: utf-8 -*-
"""Deterministic fake-fill tests for the Portfolio Ledger outbox."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.config import Config
from src.repositories.portfolio_repo import VirtualFillConflictError
from src.services.portfolio_ledger_types import LEDGER_ERROR_OVERSELL
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager


class PortfolioOutboxTestCase(unittest.TestCase):
    """Verify pending/applied recovery without a Paper matching module."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name)
        self.env_path = data_dir / ".env"
        self.db_path = data_dir / "portfolio_outbox_test.db"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.service = PortfolioService()
        account = self.service.create_account(
            name="Paper test",
            broker="Demo",
            market="cn",
            base_currency="CNY",
        )
        self.account_id = int(account["id"])

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def _enqueue(self, *, fill_id: str = "fill-001", side: str = "buy", quantity: float = 10.0):
        return self.service.enqueue_virtual_fill(
            account_id=self.account_id,
            fill_id=fill_id,
            symbol="600519",
            fill_date=date(2026, 1, 2),
            side=side,
            quantity=quantity,
            price=100.0,
            fee=1.0,
            tax=0.0,
            market="cn",
            currency="CNY",
        )

    def test_enqueue_is_pending_and_repeated_fill_is_idempotent(self) -> None:
        first = self._enqueue()
        second = self._enqueue()

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["trade_uid"], "paper:fill-001")
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["attempt_count"], 0)
        self.assertEqual(len(self.service.repo.list_pending_virtual_fills(account_id=self.account_id)), 1)
        self.assertEqual(
            self.service.repo.list_trades(self.account_id, date(2026, 1, 2)),
            [],
        )

        with self.assertRaises(VirtualFillConflictError):
            self._enqueue(fill_id="fill-001", quantity=11.0)

    def test_pending_fill_retries_to_ledger_once(self) -> None:
        row = self._enqueue(fill_id="fill-pending")
        receipt = self.service.apply_virtual_fill(outbox_id=row["id"])

        self.assertTrue(receipt.accepted)
        stored = self.service.repo.get_virtual_fill_outbox(outbox_id=row["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "applied")
        self.assertEqual(stored.ledger_event_id, receipt.event_id)
        trades = self.service.repo.list_trades(self.account_id, date(2026, 1, 2))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].trade_uid, "paper:fill-pending")

        duplicate_receipt = self.service.apply_virtual_fill(outbox_id=row["id"])
        self.assertTrue(duplicate_receipt.accepted)
        self.assertTrue(duplicate_receipt.idempotent)
        self.assertEqual(duplicate_receipt.event_id, receipt.event_id)
        self.assertEqual(
            len(self.service.repo.list_trades(self.account_id, date(2026, 1, 2))),
            1,
        )

    def test_ledger_commit_then_outbox_crash_is_recovered_by_retry(self) -> None:
        row = self._enqueue(fill_id="fill-crash")
        with patch.object(
            self.service.repo,
            "mark_virtual_fill_applied",
            side_effect=RuntimeError("injected outbox crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected outbox crash"):
                self.service.apply_virtual_fill(outbox_id=row["id"])

        pending = self.service.repo.get_virtual_fill_outbox(outbox_id=row["id"])
        self.assertIsNotNone(pending)
        self.assertEqual(pending.status, "pending")
        self.assertEqual(
            len(self.service.repo.list_trades(self.account_id, date(2026, 1, 2))),
            1,
        )

        recovered = self.service.apply_virtual_fill(outbox_id=row["id"])
        self.assertTrue(recovered.accepted)
        self.assertTrue(recovered.idempotent)
        applied = self.service.repo.get_virtual_fill_outbox(outbox_id=row["id"])
        self.assertEqual(applied.status, "applied")
        self.assertEqual(applied.ledger_event_id, recovered.event_id)
        self.assertEqual(
            len(self.service.repo.list_trades(self.account_id, date(2026, 1, 2))),
            1,
        )

    def test_rejected_projection_stays_pending_and_later_retry_applies(self) -> None:
        row = self._enqueue(fill_id="fill-retry", side="sell", quantity=5.0)
        rejected = self.service.apply_virtual_fill(outbox_id=row["id"])

        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.error_code, LEDGER_ERROR_OVERSELL)
        pending = self.service.repo.get_virtual_fill_outbox(outbox_id=row["id"])
        self.assertEqual(pending.status, "pending")
        self.assertEqual(pending.attempt_count, 1)
        self.assertEqual(
            len(self.service.repo.list_trades(self.account_id, date(2026, 1, 2))),
            0,
        )

        self.service.record_trade(
            account_id=self.account_id,
            symbol="600519",
            trade_date=date(2026, 1, 1),
            side="buy",
            quantity=5.0,
            price=90.0,
            market="cn",
            currency="CNY",
        )
        recovered = self.service.retry_pending_virtual_fills(account_id=self.account_id)
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0].accepted)
        applied = self.service.repo.get_virtual_fill_outbox(outbox_id=row["id"])
        self.assertEqual(applied.status, "applied")
        self.assertEqual(applied.attempt_count, 1)
        self.assertEqual(
            [trade.trade_uid for trade in self.service.repo.list_trades(self.account_id, date(2026, 1, 2))],
            [None, "paper:fill-retry"],
        )


if __name__ == "__main__":
    unittest.main()
