# -*- coding: utf-8 -*-
"""Small internal types for the PortfolioService ledger write seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


LEDGER_EVENT_TRADE = "trade"
LEDGER_EVENT_CASH = "cash_ledger"
LEDGER_EVENT_CORPORATE_ACTION = "corporate_action"

LEDGER_ERROR_INVALID_COMMAND = "invalid_command"
LEDGER_ERROR_VALIDATION = "validation_error"
LEDGER_ERROR_ACCOUNT_INACTIVE = "account_inactive"
LEDGER_ERROR_OVERSELL = "oversell"
LEDGER_ERROR_DUPLICATE_TRADE_UID = "duplicate_trade_uid"
LEDGER_ERROR_DUPLICATE_DEDUP_HASH = "duplicate_dedup_hash"
LEDGER_ERROR_PORTFOLIO_BUSY = "portfolio_busy"


@dataclass(frozen=True)
class LedgerCommand:
    """A normalized envelope for one account-ledger fact submission.

    The payload deliberately stays event-specific for this first seam.  The
    repository schema and the public API remain unchanged; later ledger-cycle
    work can evolve the payload without making the three legacy writer methods
    another write path.
    """

    account_id: int
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", (self.kind or "").strip().lower())
        object.__setattr__(self, "payload", dict(self.payload or {}))

    @property
    def event_type(self) -> str:
        """Alias used by callers that describe the command by event type."""

        return self.kind


@dataclass(frozen=True)
class LedgerReceipt:
    """Result of a ledger command, including stable domain rejection codes."""

    accepted: bool
    event_type: str
    event_id: Optional[int] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    idempotent: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", (self.event_type or "").strip().lower())
        object.__setattr__(self, "details", dict(self.details or {}))

    @property
    def ok(self) -> bool:
        """Convenience alias for callers using success-oriented terminology."""

        return self.accepted

    @property
    def success(self) -> bool:
        return self.accepted

    @property
    def error_message(self) -> Optional[str]:
        return self.message

    @property
    def id(self) -> Optional[int]:
        return self.event_id

    @classmethod
    def accepted_event(cls, *, event_type: str, event_id: int) -> "LedgerReceipt":
        return cls(accepted=True, event_type=event_type, event_id=int(event_id))

    @classmethod
    def rejected(
        cls,
        *,
        event_type: str,
        error_code: str,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
        idempotent: bool = False,
    ) -> "LedgerReceipt":
        return cls(
            accepted=False,
            event_type=event_type,
            error_code=error_code,
            message=message,
            details=details or {},
            idempotent=idempotent,
        )


__all__ = [
    "LEDGER_ERROR_ACCOUNT_INACTIVE",
    "LEDGER_ERROR_DUPLICATE_DEDUP_HASH",
    "LEDGER_ERROR_DUPLICATE_TRADE_UID",
    "LEDGER_ERROR_INVALID_COMMAND",
    "LEDGER_ERROR_OVERSELL",
    "LEDGER_ERROR_PORTFOLIO_BUSY",
    "LEDGER_ERROR_VALIDATION",
    "LEDGER_EVENT_CASH",
    "LEDGER_EVENT_CORPORATE_ACTION",
    "LEDGER_EVENT_TRADE",
    "LedgerCommand",
    "LedgerReceipt",
]
