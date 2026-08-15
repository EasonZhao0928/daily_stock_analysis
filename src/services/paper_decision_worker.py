# -*- coding: utf-8 -*-
"""Scheduler adapter for Paper Account decision cycles.

design 6 puts a Scheduler adapter in front of ``run_cycle``: the adapter only
decides *when* to run, while the Paper Account module owns Observation freezing,
Proposal handling, risk and approval.  This module holds no decision logic.

It mirrors :class:`src.services.alert_worker.AlertWorker`: one exception-contained
``run_once`` that a background scheduler thread can call on an interval, rather
than a second scheduling framework.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


def paper_scheduler_enabled(config: Any = None) -> bool:
    """Whether scheduled Paper decision cycles are explicitly enabled.

    Unattended decision runs are opt-in (R10.4), and a configuration failure is
    treated as "disabled" so the gate fails closed.
    """
    if config is None:
        try:
            from src.config import get_config

            config = get_config()
        except Exception:
            return False
    return bool(getattr(config, "paper_scheduler_enabled", False))


class PaperDecisionWorker:
    """Run one decision cycle per enabled Paper account, on a schedule."""

    def __init__(
        self,
        *,
        config_provider: Optional[Callable[[], Any]] = None,
        service: Optional[Any] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        strategy_version: str = "scheduled",
    ) -> None:
        self.config_provider = config_provider or self._default_config_provider
        self._service = service
        self.now_provider = now_provider or datetime.now
        self.strategy_version = strategy_version

    @staticmethod
    def _default_config_provider():
        from src.config import get_config

        return get_config()

    @property
    def service(self):
        if self._service is None:
            from src.paper_account.service import PaperAccountService

            self._service = PaperAccountService()
        return self._service

    def _eligible_accounts(self) -> List[Dict[str, Any]]:
        """Active paper accounts only; paused/frozen/closed never auto-run."""
        eligible = []
        for account in self.service.list_accounts():
            if account.get("account_kind") != "paper":
                continue
            state = self.service.inspect(int(account["id"]))
            if state is None or state["config"].get("state") != "active":
                continue
            eligible.append(account)
        return eligible

    def run_once(self, *, decision_at: Optional[date | datetime] = None) -> Dict[str, int]:
        """Run one scheduled sweep.

        Exception-contained per account so one bad account cannot stop the
        background thread or block the others.
        """
        stats = {"eligible": 0, "ran": 0, "skipped": 0, "failed": 0}
        config = None
        try:
            config = self.config_provider()
        except Exception:
            logger.warning("[PaperScheduler] config unavailable; skipping sweep")
            return stats
        if not paper_scheduler_enabled(config):
            return stats

        when = decision_at or self.now_provider()
        try:
            accounts = self._eligible_accounts()
        except Exception as exc:
            logger.warning("[PaperScheduler] cannot list paper accounts: %s", exc)
            stats["failed"] += 1
            return stats

        stats["eligible"] = len(accounts)
        for account in accounts:
            account_id = int(account["id"])
            try:
                result = self.service.run_cycle_from_sources(
                    account_id,
                    decision_at=when,
                    strategy_version=self.strategy_version,
                )
            except Exception as exc:
                # A missing symbol universe or unavailable source is a normal
                # skip for a scheduled sweep, not a worker failure.
                logger.info("[PaperScheduler] account %s skipped: %s", account_id, exc)
                stats["skipped"] += 1
                continue
            if result.get("idempotent_replay"):
                stats["skipped"] += 1
            else:
                stats["ran"] += 1
        return stats


__all__ = ["PaperDecisionWorker", "paper_scheduler_enabled"]
