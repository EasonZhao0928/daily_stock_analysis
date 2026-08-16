# -*- coding: utf-8 -*-
"""Portfolio service for P0 account/events/snapshot workflow."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from data_provider.base import canonical_stock_code, normalize_stock_code
from src.config import get_config
from src.repositories.portfolio_repo import (
    DuplicateTradeDedupHashError,
    DuplicateTradeUidError,
    PortfolioBusyError as RepoPortfolioBusyError,
    PortfolioRepository,
)
from src.services.portfolio_ledger_types import (
    LEDGER_ERROR_ACCOUNT_INACTIVE,
    LEDGER_ERROR_DUPLICATE_DEDUP_HASH,
    LEDGER_ERROR_DUPLICATE_TRADE_UID,
    LEDGER_ERROR_INVALID_COMMAND,
    LEDGER_ERROR_OVERSELL,
    LEDGER_ERROR_PORTFOLIO_BUSY,
    LEDGER_ERROR_VALIDATION,
    LEDGER_EVENT_CASH,
    LEDGER_EVENT_CORPORATE_ACTION,
    LEDGER_EVENT_TRADE,
    LedgerCommand,
    LedgerReceipt,
)
from src.storage import PortfolioLedgerOutbox

logger = logging.getLogger(__name__)

PortfolioBusyError = RepoPortfolioBusyError

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional dependency path
    yf = None

EPS = 1e-8
VALID_MARKETS = {"cn", "hk", "us", "jp", "kr", "tw"}
PARTIAL_VALUATION_MARKETS = {"jp", "kr", "tw"}
VALID_COST_METHODS = {"fifo", "avg"}
VALID_SIDES = {"buy", "sell"}
VALID_CASH_DIRECTIONS = {"in", "out"}
VALID_CORPORATE_ACTIONS = {"cash_dividend", "split_adjustment"}
PORTFOLIO_FX_REFRESH_DISABLED_REASON = "portfolio_fx_update_disabled"
PORTFOLIO_REALTIME_QUOTE_MAX_WORKERS = 4


def _portfolio_limitations_for_market(market: str) -> List[str]:
    """Return explicit snapshot limitations for markets with partial valuation semantics."""

    if market not in PARTIAL_VALUATION_MARKETS:
        return []
    return [
        "realtime_quote_best_effort",
        "fx_and_cost_basis_partial",
        "sector_and_risk_metrics_limited",
    ]


def _merge_portfolio_limitations(*groups: Iterable[str]) -> List[str]:
    merged: List[str] = []
    seen: Set[str] = set()
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


class PortfolioConflictError(Exception):
    """Raised when request conflicts with existing portfolio state."""

    def __init__(self, message: str, *, code: str = "conflict") -> None:
        self.code = code
        super().__init__(message)


class PortfolioOversellError(ValueError):
    """Raised when a sell would exceed the available position quantity."""

    def __init__(
        self,
        *,
        symbol: str,
        trade_date: Optional[date],
        requested_quantity: float,
        available_quantity: float,
    ) -> None:
        self.symbol = symbol
        self.trade_date = trade_date
        self.requested_quantity = float(requested_quantity)
        self.available_quantity = max(0.0, float(available_quantity))
        self.code = LEDGER_ERROR_OVERSELL
        date_hint = f" on {trade_date.isoformat()}" if trade_date is not None else ""
        super().__init__(
            "Oversell detected for "
            f"{symbol}{date_hint}: requested={round(self.requested_quantity, 8)}, "
            f"available={round(self.available_quantity, 8)}"
        )


@dataclass
class _AvgState:
    quantity: float = 0.0
    total_cost: float = 0.0


@dataclass(frozen=True)
class _ResolvedPositionPrice:
    price: float
    source: str
    price_date: Optional[date]
    is_stale: bool
    is_available: bool
    provider: Optional[str] = None


class PortfolioService:
    """Business logic for account CRUD, event writes, and snapshot replay."""

    def __init__(self, repo: Optional[PortfolioRepository] = None):
        self.repo = repo or PortfolioRepository()

    # ------------------------------------------------------------------
    # Account CRUD
    # ------------------------------------------------------------------
    def create_account(
        self,
        *,
        name: str,
        broker: Optional[str],
        market: str,
        base_currency: str,
        owner_id: Optional[str] = None,
        account_kind: str = "manual",
        controller_kind: str = "manual",
        external_execution_enabled: bool = False,
    ) -> Dict[str, Any]:
        name_norm = (name or "").strip()
        if not name_norm:
            raise ValueError("name is required")
        market_norm = self._normalize_market(market)
        base_currency_norm = self._normalize_currency(base_currency)
        row = self.repo.create_account(
            name=name_norm,
            broker=(broker or "").strip() or None,
            market=market_norm,
            base_currency=base_currency_norm,
            owner_id=(owner_id or "").strip() or None,
            account_kind=account_kind,
            controller_kind=controller_kind,
            external_execution_enabled=external_execution_enabled,
        )
        return self._account_to_dict(row)

    def list_accounts(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        rows = self.repo.list_accounts(include_inactive=include_inactive)
        return [self._account_to_dict(r) for r in rows]

    def update_account(
        self,
        account_id: int,
        *,
        name: Optional[str] = None,
        broker: Optional[str] = None,
        market: Optional[str] = None,
        base_currency: Optional[str] = None,
        owner_id: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        fields: Dict[str, Any] = {}
        if name is not None:
            name_norm = name.strip()
            if not name_norm:
                raise ValueError("name is required")
            fields["name"] = name_norm
        if broker is not None:
            fields["broker"] = broker.strip() or None
        if market is not None:
            fields["market"] = self._normalize_market(market)
        if base_currency is not None:
            fields["base_currency"] = self._normalize_currency(base_currency)
        if owner_id is not None:
            fields["owner_id"] = owner_id.strip() or None
        if is_active is not None:
            fields["is_active"] = bool(is_active)
        if not fields:
            raise ValueError("No fields provided for update")

        row = self.repo.update_account(account_id, fields)
        if row is None:
            return None
        return self._account_to_dict(row)

    def deactivate_account(self, account_id: int) -> bool:
        return self.repo.deactivate_account(account_id)

    # ------------------------------------------------------------------
    # Event writes
    # ------------------------------------------------------------------
    def submit(self, command: LedgerCommand) -> LedgerReceipt:
        """Submit one account fact through the single portfolio write seam.

        The repository write session remains the transaction boundary for this
        task.  Domain rejections are converted to receipts only after the
        session has rolled back, so callers can inspect stable error codes
        without creating a second write path.
        """

        if not isinstance(command, LedgerCommand):
            return LedgerReceipt.rejected(
                event_type="",
                error_code=LEDGER_ERROR_INVALID_COMMAND,
                message="command must be a LedgerCommand",
            )

        event_type = self._canonical_ledger_event_type(command.kind)
        if event_type is None:
            return LedgerReceipt.rejected(
                event_type=command.kind,
                error_code=LEDGER_ERROR_INVALID_COMMAND,
                message=f"Unsupported ledger command kind: {command.kind}",
            )

        try:
            with self.repo.ledger_cycle() as session:
                account = self._require_active_account_in_session(
                    session=session,
                    account_id=command.account_id,
                )
                if event_type == LEDGER_EVENT_TRADE:
                    event_id = self._submit_trade_command(
                        session=session,
                        account=account,
                        account_id=command.account_id,
                        payload=command.payload,
                    )
                elif event_type == LEDGER_EVENT_CASH:
                    event_id = self._submit_cash_command(
                        session=session,
                        account=account,
                        account_id=command.account_id,
                        payload=command.payload,
                    )
                else:
                    event_id = self._submit_corporate_action_command(
                        session=session,
                        account=account,
                        account_id=command.account_id,
                        payload=command.payload,
                    )
            return LedgerReceipt.accepted_event(event_type=event_type, event_id=event_id)
        except RepoPortfolioBusyError as exc:
            return LedgerReceipt.rejected(
                event_type=event_type,
                error_code=LEDGER_ERROR_PORTFOLIO_BUSY,
                message=str(exc),
            )
        except DuplicateTradeUidError as exc:
            return LedgerReceipt.rejected(
                event_type=event_type,
                error_code=LEDGER_ERROR_DUPLICATE_TRADE_UID,
                message=str(exc),
                idempotent=True,
            )
        except DuplicateTradeDedupHashError as exc:
            return LedgerReceipt.rejected(
                event_type=event_type,
                error_code=LEDGER_ERROR_DUPLICATE_DEDUP_HASH,
                message=str(exc),
                idempotent=True,
            )
        except PortfolioOversellError as exc:
            return LedgerReceipt.rejected(
                event_type=event_type,
                error_code=LEDGER_ERROR_OVERSELL,
                message=str(exc),
                details={
                    "symbol": exc.symbol,
                    "trade_date": exc.trade_date,
                    "requested_quantity": exc.requested_quantity,
                    "available_quantity": exc.available_quantity,
                },
            )
        except PortfolioConflictError as exc:
            return LedgerReceipt.rejected(
                event_type=event_type,
                error_code=getattr(exc, "code", "conflict"),
                message=str(exc),
                idempotent=getattr(exc, "code", "") in {
                    LEDGER_ERROR_DUPLICATE_TRADE_UID,
                    LEDGER_ERROR_DUPLICATE_DEDUP_HASH,
                },
            )
        except ValueError as exc:
            message = str(exc)
            error_code = (
                LEDGER_ERROR_ACCOUNT_INACTIVE
                if message.startswith("Active account not found:")
                else LEDGER_ERROR_VALIDATION
            )
            return LedgerReceipt.rejected(
                event_type=event_type,
                error_code=error_code,
                message=message,
            )

    # ------------------------------------------------------------------
    # Virtual-fill ledger outbox
    # ------------------------------------------------------------------
    def enqueue_virtual_fill(
        self,
        *,
        account_id: int,
        fill_id: str,
        payload: Optional[Mapping[str, Any]] = None,
        symbol: Optional[str] = None,
        fill_date: Any = None,
        trade_date: Any = None,
        side: Optional[str] = None,
        quantity: Any = None,
        price: Any = None,
        fee: Any = None,
        tax: Any = None,
        market: Optional[str] = None,
        currency: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store a fake fill until the Account Ledger accepts its trade.

        This is intentionally only an outbox intake seam; it does not create
        a trade or run matching. The immutable identity is always owned by
        this service as ``paper:<fill_id>`` and is not accepted from payload.
        ``payload`` is supported for the future Paper Account adapter while
        explicit keyword fields keep fake-fill tests and callers readable.
        """
        if payload is not None and not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        fill_id_norm = str(fill_id or "").strip()
        if not fill_id_norm:
            raise ValueError("fill_id is required")

        account = self._require_active_account(account_id)
        fill_payload: Dict[str, Any] = dict(payload or {})
        overrides = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "fee": fee,
            "tax": tax,
            "market": market,
            "currency": currency,
            "note": note,
        }
        for key, value in overrides.items():
            if value is not None:
                fill_payload[key] = value
        if fill_date is not None:
            fill_payload["trade_date"] = fill_date
        elif trade_date is not None:
            fill_payload["trade_date"] = trade_date
        # A caller cannot smuggle a second identity into the reserved paper
        # namespace. The repository keeps the canonical UID separately.
        fill_payload.pop("trade_uid", None)

        normalized = self._normalize_virtual_fill_payload(fill_payload, account=account)
        row = self.repo.enqueue_virtual_fill(
            account_id=account_id,
            fill_id=fill_id_norm,
            payload=normalized,
        )
        return self._virtual_fill_row_to_dict(row)

    def apply_virtual_fill(
        self,
        *,
        outbox_id: Optional[int] = None,
        account_id: Optional[int] = None,
        fill_id: Optional[str] = None,
    ) -> LedgerReceipt:
        """Project one pending fill through ``submit`` and advance its state.

        Ledger insertion and outbox advancement are separate transactions by
        design. If the process dies after Ledger commit, retry sees the
        duplicate paper UID, resolves the original trade id, and marks the
        same outbox row applied without creating another trade.
        """
        row = self.repo.get_virtual_fill_outbox(
            outbox_id=outbox_id,
            account_id=account_id,
            fill_id=fill_id,
        )
        if row is None:
            raise ValueError("Virtual fill outbox row not found")
        if row.status == "applied":
            return LedgerReceipt(
                accepted=True,
                event_type=LEDGER_EVENT_TRADE,
                event_id=row.ledger_event_id,
                idempotent=True,
            )

        try:
            payload = json.loads(row.payload)
        except (TypeError, ValueError) as exc:
            message = f"Invalid virtual fill payload: {exc}"
            self.repo.record_virtual_fill_failure(outbox_id=row.id, error=message)
            return LedgerReceipt.rejected(
                event_type=LEDGER_EVENT_TRADE,
                error_code=LEDGER_ERROR_VALIDATION,
                message=message,
            )

        if not isinstance(payload, dict):
            message = "Virtual fill payload must be an object"
            self.repo.record_virtual_fill_failure(outbox_id=row.id, error=message)
            return LedgerReceipt.rejected(
                event_type=LEDGER_EVENT_TRADE,
                error_code=LEDGER_ERROR_VALIDATION,
                message=message,
            )
        payload["trade_uid"] = row.trade_uid
        receipt = self.submit(
            LedgerCommand(
                account_id=int(row.account_id),
                kind=LEDGER_EVENT_TRADE,
                payload=payload,
            )
        )

        if receipt.accepted:
            # If this call fails after submit committed, the row stays pending
            # and the next invocation follows the duplicate recovery branch.
            self.repo.mark_virtual_fill_applied(
                outbox_id=int(row.id),
                ledger_event_id=receipt.event_id,
            )
            return receipt

        if receipt.error_code in {
            LEDGER_ERROR_DUPLICATE_TRADE_UID,
            LEDGER_ERROR_DUPLICATE_DEDUP_HASH,
        }:
            existing_id = self.repo.get_trade_id_by_uid(
                account_id=int(row.account_id),
                trade_uid=row.trade_uid,
            )
            if existing_id is not None:
                self.repo.mark_virtual_fill_applied(
                    outbox_id=int(row.id),
                    ledger_event_id=existing_id,
                )
                return LedgerReceipt(
                    accepted=True,
                    event_type=LEDGER_EVENT_TRADE,
                    event_id=int(existing_id),
                    idempotent=True,
                )

        self.repo.record_virtual_fill_failure(
            outbox_id=int(row.id),
            error=receipt.message or receipt.error_code or "virtual fill projection rejected",
        )
        return receipt

    def retry_pending_virtual_fills(self, *, account_id: Optional[int] = None) -> List[LedgerReceipt]:
        """Retry only durable pending rows, never manufacture a new fill."""
        return [
            self.apply_virtual_fill(outbox_id=int(row.id))
            for row in self.repo.list_pending_virtual_fills(account_id=account_id)
        ]

    @classmethod
    def _normalize_virtual_fill_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        account: Any,
    ) -> Dict[str, Any]:
        """Validate and canonicalize fake-fill facts before durable enqueue."""
        normalized: Dict[str, Any] = dict(payload)
        symbol = cls._normalize_symbol_for_storage(str(normalized.get("symbol") or ""))
        if not symbol:
            raise ValueError("symbol is required")
        side = str(normalized.get("side") or "").strip().lower()
        if side not in VALID_SIDES:
            raise ValueError("side must be buy or sell")
        try:
            quantity = float(normalized.get("quantity"))
            price = float(normalized.get("price"))
            fee = float(normalized.get("fee", 0.0) or 0.0)
            tax = float(normalized.get("tax", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError("quantity and price must be > 0") from exc
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be > 0")
        if fee < 0 or tax < 0:
            raise ValueError("fee and tax must be >= 0")

        trade_date_value = normalized.get("trade_date")
        if isinstance(trade_date_value, datetime):
            trade_date_value = trade_date_value.date()
        trade_date = cls._coerce_ledger_date(trade_date_value, "trade_date")
        market = cls._normalize_market(normalized.get("market") or account.market)
        currency = cls._normalize_currency(
            normalized.get("currency") or cls._default_currency_for_market(market)
        )
        normalized.update(
            {
                "symbol": symbol,
                "trade_date": trade_date.isoformat(),
                "side": side,
                "quantity": quantity,
                "price": price,
                "fee": fee,
                "tax": tax,
                "market": market,
                "currency": currency,
                "note": str(normalized.get("note") or "").strip() or None,
            }
        )
        return normalized

    @staticmethod
    def _virtual_fill_row_to_dict(row: PortfolioLedgerOutbox) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "account_id": int(row.account_id),
            "fill_id": row.fill_id,
            "trade_uid": row.trade_uid,
            "payload": json.loads(row.payload),
            "status": row.status,
            "attempt_count": int(row.attempt_count or 0),
            "last_error": row.last_error,
            "ledger_event_id": row.ledger_event_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "applied_at": row.applied_at.isoformat() if row.applied_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def _submit_trade_command(
        self,
        *,
        session: Any,
        account: Any,
        account_id: int,
        payload: Dict[str, Any],
    ) -> int:
        side_norm = str(payload.get("side") or "").strip().lower()
        if side_norm not in VALID_SIDES:
            raise ValueError("side must be buy or sell")

        try:
            quantity = float(payload.get("quantity"))
            price = float(payload.get("price"))
        except (TypeError, ValueError) as exc:
            raise ValueError("quantity and price must be > 0") from exc
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be > 0")

        try:
            fee = float(payload.get("fee", 0.0) or 0.0)
            tax = float(payload.get("tax", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError("fee and tax must be >= 0") from exc
        if fee < 0 or tax < 0:
            raise ValueError("fee and tax must be >= 0")

        symbol = str(payload.get("symbol") or "")
        symbol_norm = self._normalize_symbol_for_storage(symbol)
        if not symbol_norm:
            raise ValueError("symbol is required")

        trade_date = self._coerce_ledger_date(payload.get("trade_date"), "trade_date")
        trade_uid_norm = str(payload.get("trade_uid") or "").strip() or None
        dedup_hash_norm = str(payload.get("dedup_hash") or "").strip() or None
        market_norm = self._normalize_market(payload.get("market") or account.market)
        currency_norm = self._normalize_currency(
            payload.get("currency") or self._default_currency_for_market(market_norm)
        )

        self._validate_trade_identity(
            account_id=account_id,
            trade_uid=trade_uid_norm,
            dedup_hash=dedup_hash_norm,
            session=session,
        )
        if side_norm == "sell":
            self._validate_sell_quantity(
                account_id=account_id,
                symbol=symbol,
                market=market_norm,
                currency=currency_norm,
                trade_date=trade_date,
                quantity=quantity,
                session=session,
            )

        row = self.repo.add_trade_in_session(
            session=session,
            account_id=account_id,
            trade_uid=trade_uid_norm,
            symbol=symbol_norm,
            market=market_norm,
            currency=currency_norm,
            trade_date=trade_date,
            side=side_norm,
            quantity=quantity,
            price=price,
            fee=fee,
            tax=tax,
            note=str(payload.get("note") or "").strip() or None,
            dedup_hash=dedup_hash_norm,
        )
        return int(row.id)

    def _submit_cash_command(
        self,
        *,
        session: Any,
        account: Any,
        account_id: int,
        payload: Dict[str, Any],
    ) -> int:
        direction_norm = str(payload.get("direction") or "").strip().lower()
        if direction_norm not in VALID_CASH_DIRECTIONS:
            raise ValueError("direction must be in or out")
        try:
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise ValueError("amount must be > 0") from exc
        if amount <= 0:
            raise ValueError("amount must be > 0")

        event_date = self._coerce_ledger_date(payload.get("event_date"), "event_date")
        currency_norm = self._normalize_currency(payload.get("currency") or account.base_currency)
        row = self.repo.add_cash_ledger_in_session(
            session=session,
            account_id=account_id,
            event_date=event_date,
            direction=direction_norm,
            amount=amount,
            currency=currency_norm,
            note=str(payload.get("note") or "").strip() or None,
        )
        return int(row.id)

    def _submit_corporate_action_command(
        self,
        *,
        session: Any,
        account: Any,
        account_id: int,
        payload: Dict[str, Any],
    ) -> int:
        action_type_norm = str(payload.get("action_type") or "").strip().lower()
        if action_type_norm not in VALID_CORPORATE_ACTIONS:
            raise ValueError("action_type must be cash_dividend or split_adjustment")

        cash_dividend_per_share = payload.get("cash_dividend_per_share")
        split_ratio = payload.get("split_ratio")
        if action_type_norm == "cash_dividend":
            try:
                cash_dividend_per_share = float(cash_dividend_per_share)
            except (TypeError, ValueError) as exc:
                raise ValueError("cash_dividend_per_share must be >= 0 for cash_dividend") from exc
            if cash_dividend_per_share < 0:
                raise ValueError("cash_dividend_per_share must be >= 0 for cash_dividend")
        if action_type_norm == "split_adjustment":
            try:
                split_ratio = float(split_ratio)
            except (TypeError, ValueError) as exc:
                raise ValueError("split_ratio must be > 0 for split_adjustment") from exc
            if split_ratio <= 0:
                raise ValueError("split_ratio must be > 0 for split_adjustment")

        symbol_norm = self._normalize_symbol_for_storage(str(payload.get("symbol") or ""))
        if not symbol_norm:
            raise ValueError("symbol is required")
        effective_date = self._coerce_ledger_date(payload.get("effective_date"), "effective_date")
        market_norm = self._normalize_market(payload.get("market") or account.market)
        currency_norm = self._normalize_currency(
            payload.get("currency") or self._default_currency_for_market(market_norm)
        )
        row = self.repo.add_corporate_action_in_session(
            session=session,
            account_id=account_id,
            symbol=symbol_norm,
            market=market_norm,
            currency=currency_norm,
            effective_date=effective_date,
            action_type=action_type_norm,
            cash_dividend_per_share=cash_dividend_per_share,
            split_ratio=split_ratio,
            note=str(payload.get("note") or "").strip() or None,
        )
        return int(row.id)

    @staticmethod
    def _canonical_ledger_event_type(kind: str) -> Optional[str]:
        normalized = (kind or "").strip().lower()
        if normalized == LEDGER_EVENT_TRADE:
            return LEDGER_EVENT_TRADE
        if normalized in {"cash", LEDGER_EVENT_CASH}:
            return LEDGER_EVENT_CASH
        if normalized == LEDGER_EVENT_CORPORATE_ACTION:
            return LEDGER_EVENT_CORPORATE_ACTION
        return None

    @staticmethod
    def _coerce_ledger_date(value: Any, field_name: str) -> date:
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        raise ValueError(f"{field_name} is required")

    def _receipt_to_event_dict(self, receipt: LedgerReceipt) -> Dict[str, Any]:
        if receipt.accepted and receipt.event_id is not None:
            return {"id": int(receipt.event_id)}
        self._raise_for_ledger_receipt(receipt)
        raise AssertionError("unreachable")

    @staticmethod
    def _raise_for_ledger_receipt(receipt: LedgerReceipt) -> None:
        message = receipt.message or receipt.error_code or "Ledger command rejected"
        if receipt.error_code == LEDGER_ERROR_OVERSELL:
            details = receipt.details
            raise PortfolioOversellError(
                symbol=str(details.get("symbol") or ""),
                trade_date=details.get("trade_date"),
                requested_quantity=float(details.get("requested_quantity", 0.0)),
                available_quantity=float(details.get("available_quantity", 0.0)),
            )
        if receipt.error_code in {
            LEDGER_ERROR_DUPLICATE_TRADE_UID,
            LEDGER_ERROR_DUPLICATE_DEDUP_HASH,
        }:
            raise PortfolioConflictError(message, code=receipt.error_code)
        if receipt.error_code == LEDGER_ERROR_PORTFOLIO_BUSY:
            raise PortfolioBusyError(message)
        raise ValueError(message)

    def record_trade(
        self,
        *,
        account_id: int,
        symbol: str,
        trade_date: date,
        side: str,
        quantity: float,
        price: float,
        fee: float = 0.0,
        tax: float = 0.0,
        market: Optional[str] = None,
        currency: Optional[str] = None,
        trade_uid: Optional[str] = None,
        dedup_hash: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._receipt_to_event_dict(
            self.submit(
                LedgerCommand(
                    account_id=account_id,
                    kind=LEDGER_EVENT_TRADE,
                    payload={
                        "symbol": symbol,
                        "trade_date": trade_date,
                        "side": side,
                        "quantity": quantity,
                        "price": price,
                        "fee": fee,
                        "tax": tax,
                        "market": market,
                        "currency": currency,
                        "trade_uid": trade_uid,
                        "dedup_hash": dedup_hash,
                        "note": note,
                    },
                )
            )
        )

    def record_cash_ledger(
        self,
        *,
        account_id: int,
        event_date: date,
        direction: str,
        amount: float,
        currency: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._receipt_to_event_dict(
            self.submit(
                LedgerCommand(
                    account_id=account_id,
                    kind=LEDGER_EVENT_CASH,
                    payload={
                        "event_date": event_date,
                        "direction": direction,
                        "amount": amount,
                        "currency": currency,
                        "note": note,
                    },
                )
            )
        )

    def record_corporate_action(
        self,
        *,
        account_id: int,
        symbol: str,
        effective_date: date,
        action_type: str,
        market: Optional[str] = None,
        currency: Optional[str] = None,
        cash_dividend_per_share: Optional[float] = None,
        split_ratio: Optional[float] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._receipt_to_event_dict(
            self.submit(
                LedgerCommand(
                    account_id=account_id,
                    kind=LEDGER_EVENT_CORPORATE_ACTION,
                    payload={
                        "symbol": symbol,
                        "effective_date": effective_date,
                        "action_type": action_type,
                        "market": market,
                        "currency": currency,
                        "cash_dividend_per_share": cash_dividend_per_share,
                        "split_ratio": split_ratio,
                        "note": note,
                    },
                )
            )
        )

    def delete_trade_event(self, trade_id: int) -> bool:
        with self.repo.ledger_cycle() as session:
            return self.repo.delete_trade_in_session(session=session, trade_id=trade_id)

    def delete_cash_ledger_event(self, entry_id: int) -> bool:
        with self.repo.ledger_cycle() as session:
            return self.repo.delete_cash_ledger_in_session(session=session, entry_id=entry_id)

    def delete_corporate_action_event(self, action_id: int) -> bool:
        with self.repo.ledger_cycle() as session:
            return self.repo.delete_corporate_action_in_session(session=session, action_id=action_id)

    def list_trade_events(
        self,
        *,
        account_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        if account_id is not None:
            self._require_active_account(account_id)
        page, page_size = self._validate_paging(page=page, page_size=page_size)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("date_from must be <= date_to")

        symbol_filters: Optional[List[str]] = None
        if symbol is not None and symbol.strip():
            symbol_filters = self._build_symbol_filter_values(symbol)
            if not symbol_filters:
                raise ValueError("symbol is invalid")

        side_norm: Optional[str] = None
        if side is not None and side.strip():
            side_norm = side.strip().lower()
            if side_norm not in VALID_SIDES:
                raise ValueError("side must be buy or sell")

        rows, total = self.repo.query_trades(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            symbols=symbol_filters,
            side=side_norm,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [self._trade_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def list_cash_ledger_events(
        self,
        *,
        account_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        direction: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        if account_id is not None:
            self._require_active_account(account_id)
        page, page_size = self._validate_paging(page=page, page_size=page_size)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("date_from must be <= date_to")

        direction_norm: Optional[str] = None
        if direction is not None and direction.strip():
            direction_norm = direction.strip().lower()
            if direction_norm not in VALID_CASH_DIRECTIONS:
                raise ValueError("direction must be in or out")

        rows, total = self.repo.query_cash_ledger(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            direction=direction_norm,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [self._cash_ledger_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def list_corporate_action_events(
        self,
        *,
        account_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        symbol: Optional[str] = None,
        action_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        if account_id is not None:
            self._require_active_account(account_id)
        page, page_size = self._validate_paging(page=page, page_size=page_size)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("date_from must be <= date_to")

        symbol_filters: Optional[List[str]] = None
        if symbol is not None and symbol.strip():
            symbol_filters = self._build_symbol_filter_values(symbol)
            if not symbol_filters:
                raise ValueError("symbol is invalid")

        action_norm: Optional[str] = None
        if action_type is not None and action_type.strip():
            action_norm = action_type.strip().lower()
            if action_norm not in VALID_CORPORATE_ACTIONS:
                raise ValueError("action_type must be cash_dividend or split_adjustment")

        rows, total = self.repo.query_corporate_actions(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            symbols=symbol_filters,
            action_type=action_norm,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [self._corporate_action_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    # ------------------------------------------------------------------
    # Snapshot replay
    # ------------------------------------------------------------------
    def get_portfolio_snapshot(
        self,
        *,
        account_id: Optional[int] = None,
        as_of: Optional[date] = None,
        cost_method: str = "fifo",
        include_realtime: bool = True,
    ) -> Dict[str, Any]:
        as_of_date = as_of or date.today()
        method = self._normalize_cost_method(cost_method)

        if account_id is not None:
            account = self._require_active_account(account_id)
            account_rows = [account]
        else:
            account_rows = self.repo.list_accounts(include_inactive=False)

        accounts_payload: List[Dict[str, Any]] = []
        processed_account_count = 0
        aggregate_currency = "CNY"
        aggregate = {
            "total_cash": 0.0,
            "total_market_value": 0.0,
            "total_equity": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "fee_total": 0.0,
            "tax_total": 0.0,
            "fx_stale": False,
            "limitations": [],
        }

        for account in account_rows:
            account_id_value = int(account.id)
            realtime_prices: Optional[Dict[str, Tuple[Optional[float], Optional[str]]]] = None
            if include_realtime and as_of_date == date.today():
                # First replay is read-only and deliberately excludes live
                # prices.  It provides the active symbol set while the ledger
                # lock is held for only the DB read.
                with self.repo.ledger_cycle() as session:
                    current_account = self.repo.get_account_in_session(
                        session=session,
                        account_id=account_id_value,
                        include_inactive=False,
                    )
                    if current_account is None:
                        continue
                    account = current_account
                    stable_snapshot = self._replay_account(
                        account=account,
                        as_of_date=as_of_date,
                        cost_method=method,
                        include_realtime=False,
                        session=session,
                    )
                active_symbols = [
                    str(position.get("symbol"))
                    for position in stable_snapshot.get("positions_cache", [])
                    if position.get("symbol") and float(position.get("quantity") or 0.0) > EPS
                ]
                # Network/provider work happens after the read transaction has
                # closed.  The second replay consumes this frozen quote map and
                # never performs a live call while replacing projections.
                realtime_prices = self._prefetch_realtime_position_prices(active_symbols)

            # Re-read source events in a short transaction immediately before
            # replacing projections.  This is the only transaction that writes
            # derived state; no provider/network call is reachable from it.
            with self.repo.ledger_cycle() as session:
                current_account = self.repo.get_account_in_session(
                    session=session,
                    account_id=account_id_value,
                    include_inactive=False,
                )
                if current_account is None:
                    continue
                account = current_account
                account_base_currency = account.base_currency
                account_snapshot = self._replay_account(
                    account=account,
                    as_of_date=as_of_date,
                    cost_method=method,
                    include_realtime=include_realtime,
                    realtime_prices=realtime_prices,
                    session=session,
                )

                self.repo.replace_positions_lots_and_snapshot_in_session(
                    session=session,
                    account_id=account_id_value,
                    snapshot_date=as_of_date,
                    cost_method=method,
                    base_currency=account_base_currency,
                    total_cash=account_snapshot["total_cash"],
                    total_market_value=account_snapshot["total_market_value"],
                    total_equity=account_snapshot["total_equity"],
                    unrealized_pnl=account_snapshot["unrealized_pnl"],
                    realized_pnl=account_snapshot["realized_pnl"],
                    fee_total=account_snapshot["fee_total"],
                    tax_total=account_snapshot["tax_total"],
                    fx_stale=account_snapshot["fx_stale"],
                    payload=json.dumps(account_snapshot["payload"], ensure_ascii=False),
                    positions=account_snapshot["positions_cache"],
                    lots=account_snapshot["lots_cache"],
                    valuation_currency=account_base_currency,
                )

            processed_account_count += 1

            accounts_payload.append(account_snapshot["public"])
            aggregate["limitations"] = _merge_portfolio_limitations(
                aggregate["limitations"],
                account_snapshot["public"].get("limitations", []),
            )

            cash_cny, stale_cash, _ = self._convert_amount(
                amount=account_snapshot["total_cash"],
                from_currency=account_base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            mv_cny, stale_mv, _ = self._convert_amount(
                amount=account_snapshot["total_market_value"],
                from_currency=account_base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            eq_cny, stale_eq, _ = self._convert_amount(
                amount=account_snapshot["total_equity"],
                from_currency=account_base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            realized_cny, stale_realized, _ = self._convert_amount(
                amount=account_snapshot["realized_pnl"],
                from_currency=account_base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            unrealized_cny, stale_unrealized, _ = self._convert_amount(
                amount=account_snapshot["unrealized_pnl"],
                from_currency=account_base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            fee_cny, stale_fee, _ = self._convert_amount(
                amount=account_snapshot["fee_total"],
                from_currency=account_base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            tax_cny, stale_tax, _ = self._convert_amount(
                amount=account_snapshot["tax_total"],
                from_currency=account_base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )

            aggregate["total_cash"] += cash_cny
            aggregate["total_market_value"] += mv_cny
            aggregate["total_equity"] += eq_cny
            aggregate["realized_pnl"] += realized_cny
            aggregate["unrealized_pnl"] += unrealized_cny
            aggregate["fee_total"] += fee_cny
            aggregate["tax_total"] += tax_cny
            aggregate["fx_stale"] = aggregate["fx_stale"] or any(
                [
                    stale_cash,
                    stale_mv,
                    stale_eq,
                    stale_realized,
                    stale_unrealized,
                    stale_fee,
                    stale_tax,
                ]
            )

        return {
            "as_of": as_of_date.isoformat(),
            "cost_method": method,
            "currency": aggregate_currency,
            "account_count": processed_account_count,
            "total_cash": round(aggregate["total_cash"], 6),
            "total_market_value": round(aggregate["total_market_value"], 6),
            "total_equity": round(aggregate["total_equity"], 6),
            "realized_pnl": round(aggregate["realized_pnl"], 6),
            "unrealized_pnl": round(aggregate["unrealized_pnl"], 6),
            "fee_total": round(aggregate["fee_total"], 6),
            "tax_total": round(aggregate["tax_total"], 6),
            "fx_stale": aggregate["fx_stale"],
            "data_quality": "partial" if aggregate["limitations"] else "ok",
            "limitations": aggregate["limitations"],
            "accounts": accounts_payload,
        }

    def refresh_fx_rates(
        self,
        *,
        account_id: Optional[int] = None,
        as_of: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Refresh account FX pairs online with stale fallback when fetch fails."""
        as_of_date = as_of or date.today()
        config = get_config()
        refresh_enabled = bool(getattr(config, "portfolio_fx_update_enabled", True))
        if account_id is not None:
            account_rows = [self._require_active_account(account_id)]
        else:
            account_rows = self.repo.list_accounts(include_inactive=False)

        summary = {
            "as_of": as_of_date.isoformat(),
            "account_count": len(account_rows),
            "refresh_enabled": refresh_enabled,
            "disabled_reason": None if refresh_enabled else PORTFOLIO_FX_REFRESH_DISABLED_REASON,
            "pair_count": 0,
            "updated_count": 0,
            "stale_count": 0,
            "error_count": 0,
        }
        for account in account_rows:
            item = self._refresh_account_fx_rates(
                account=account,
                as_of_date=as_of_date,
                refresh_enabled=refresh_enabled,
            )
            summary["pair_count"] += item["pair_count"]
            summary["updated_count"] += item["updated_count"]
            summary["stale_count"] += item["stale_count"]
            summary["error_count"] += item["error_count"]
        return summary

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _validate_trade_identity(
        self,
        *,
        account_id: int,
        trade_uid: Optional[str],
        dedup_hash: Optional[str],
        session: Optional[Any] = None,
    ) -> None:
        if trade_uid and self._has_trade_uid(account_id=account_id, trade_uid=trade_uid, session=session):
            raise PortfolioConflictError(
                f"Duplicate trade_uid for account_id={account_id}: {trade_uid}",
                code=LEDGER_ERROR_DUPLICATE_TRADE_UID,
            )
        if dedup_hash and self._has_trade_dedup_hash(account_id=account_id, dedup_hash=dedup_hash, session=session):
            raise PortfolioConflictError(
                f"Duplicate dedup_hash for account_id={account_id}: {dedup_hash}",
                code=LEDGER_ERROR_DUPLICATE_DEDUP_HASH,
            )

    def _validate_sell_quantity(
        self,
        *,
        account_id: int,
        symbol: str,
        market: str,
        currency: str,
        trade_date: date,
        quantity: float,
        session: Optional[Any] = None,
    ) -> None:
        key = (
            self._normalize_symbol_for_position(symbol),
            self._normalize_market(market),
            self._normalize_currency(currency),
        )
        available_quantity = self._calculate_available_quantity(
            account_id=account_id,
            key=key,
            as_of_date=trade_date,
            session=session,
        )
        if available_quantity + EPS < quantity:
            raise PortfolioOversellError(
                symbol=key[0],
                trade_date=trade_date,
                requested_quantity=quantity,
                available_quantity=available_quantity,
            )

    def _calculate_available_quantity(
        self,
        *,
        account_id: int,
        key: Tuple[str, str, str],
        as_of_date: date,
        session: Optional[Any] = None,
    ) -> float:
        if session is None:
            trades = self.repo.list_trades(account_id, as_of=as_of_date)
            corporate_actions = self.repo.list_corporate_actions(account_id, as_of=as_of_date)
        else:
            trades = self.repo.list_trades_in_session(session=session, account_id=account_id, as_of=as_of_date)
            corporate_actions = self.repo.list_corporate_actions_in_session(
                session=session,
                account_id=account_id,
                as_of=as_of_date,
            )

        events = []
        for row in corporate_actions:
            event_key = (
                self._normalize_symbol_for_position(row.symbol),
                self._normalize_market(row.market),
                self._normalize_currency(row.currency),
            )
            if event_key == key:
                events.append(("corp", row.effective_date, row.id, row))
        for row in trades:
            event_key = (
                self._normalize_symbol_for_position(row.symbol),
                self._normalize_market(row.market),
                self._normalize_currency(row.currency),
            )
            if event_key == key:
                events.append(("trade", row.trade_date, row.id, row))

        # Quantity validation only depends on position-changing events for one symbol.
        # Cash ledger entries do not affect shares held, so we keep the same corp->trade
        # ordering as full replay without pulling unrelated cash events into this path.
        event_priority = {"corp": 1, "trade": 2}
        events.sort(key=lambda item: (item[1], event_priority[item[0]], item[2]))

        quantity_held = 0.0
        for event_type, event_date, _, event in events:
            if event_type == "corp":
                action_type = (event.action_type or "").strip().lower()
                if action_type != "split_adjustment":
                    continue
                split_ratio = float(event.split_ratio or 0.0)
                if split_ratio <= 0:
                    raise ValueError(f"Invalid split_ratio for {key[0]}")
                if abs(split_ratio - 1.0) <= EPS:
                    continue
                quantity_held *= split_ratio
                continue

            qty = float(event.quantity or 0.0)
            if qty <= 0:
                raise ValueError(f"Invalid trade quantity for {key[0]}")
            side = (event.side or "").strip().lower()
            if side == "buy":
                quantity_held += qty
                continue
            if side != "sell":
                raise ValueError(f"Unsupported trade side: {event.side}")
            if quantity_held + EPS < qty:
                raise PortfolioOversellError(
                    symbol=key[0],
                    trade_date=event_date,
                    requested_quantity=qty,
                    available_quantity=quantity_held,
                )
            quantity_held -= qty
            if quantity_held <= EPS:
                quantity_held = 0.0

        return quantity_held

    def _replay_account(
        self,
        *,
        account: Any,
        as_of_date: date,
        cost_method: str,
        include_realtime: bool,
        realtime_prices: Optional[Dict[str, Tuple[Optional[float], Optional[str]]]] = None,
        session: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if session is None:
            trades = self.repo.list_trades(account.id, as_of=as_of_date)
            cash_ledger = self.repo.list_cash_ledger(account.id, as_of=as_of_date)
            corporate_actions = self.repo.list_corporate_actions(account.id, as_of=as_of_date)
        else:
            trades = self.repo.list_trades_in_session(
                session=session,
                account_id=account.id,
                as_of=as_of_date,
            )
            cash_ledger = self.repo.list_cash_ledger_in_session(
                session=session,
                account_id=account.id,
                as_of=as_of_date,
            )
            corporate_actions = self.repo.list_corporate_actions_in_session(
                session=session,
                account_id=account.id,
                as_of=as_of_date,
            )

        events = []
        for row in cash_ledger:
            events.append(("cash", row.event_date, row.id, row))
        for row in trades:
            events.append(("trade", row.trade_date, row.id, row))
        for row in corporate_actions:
            events.append(("corp", row.effective_date, row.id, row))

        # Same-day deterministic ordering: cash -> corporate action -> trade.
        event_priority = {"cash": 0, "corp": 1, "trade": 2}
        events.sort(key=lambda item: (item[1], event_priority[item[0]], item[2]))

        cash_balances: Dict[str, float] = defaultdict(float)
        fees_total_base = 0.0
        taxes_total_base = 0.0
        realized_pnl_base = 0.0
        fx_stale = False

        fifo_lots: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
        avg_state: Dict[Tuple[str, str, str], _AvgState] = defaultdict(_AvgState)

        for event_type, event_date, _, event in events:
            if event_type == "cash":
                currency = self._normalize_currency(event.currency)
                amount = float(event.amount or 0.0)
                if event.direction == "in":
                    cash_balances[currency] += amount
                elif event.direction == "out":
                    cash_balances[currency] -= amount
                else:
                    raise ValueError(f"Unsupported cash direction: {event.direction}")
                continue

            if event_type == "trade":
                key = (
                    self._normalize_symbol_for_position(event.symbol),
                    self._normalize_market(event.market),
                    self._normalize_currency(event.currency),
                )
                qty = float(event.quantity or 0.0)
                price = float(event.price or 0.0)
                fee = float(event.fee or 0.0)
                tax = float(event.tax or 0.0)
                if qty <= 0 or price <= 0:
                    raise ValueError(f"Invalid trade quantity or price for {event.symbol}")

                gross = qty * price
                side = (event.side or "").lower().strip()
                if side == "buy":
                    cash_balances[key[2]] -= (gross + fee + tax)
                    if cost_method == "fifo":
                        unit_cost = (gross + fee + tax) / qty
                        fifo_lots[key].append(
                            {
                                "symbol": key[0],
                                "market": key[1],
                                "currency": key[2],
                                "open_date": event_date,
                                "remaining_quantity": qty,
                                "unit_cost": unit_cost,
                                "source_trade_id": event.id,
                            }
                        )
                    else:
                        state = avg_state[key]
                        state.quantity += qty
                        state.total_cost += (gross + fee + tax)
                elif side == "sell":
                    cash_balances[key[2]] += (gross - fee - tax)
                    proceeds_net = gross - fee - tax
                    if cost_method == "fifo":
                        cost_basis = self._consume_fifo_lots(
                            fifo_lots[key],
                            qty,
                            key[0],
                            event_date,
                        )
                    else:
                        cost_basis = self._consume_avg_position(
                            avg_state[key],
                            qty,
                            key[0],
                            event_date,
                        )
                    realized_local = proceeds_net - cost_basis
                    realized_base, stale_realized, _ = self._convert_amount(
                        amount=realized_local,
                        from_currency=key[2],
                        to_currency=account.base_currency,
                        as_of_date=event_date,
                    )
                    realized_pnl_base += realized_base
                    fx_stale = fx_stale or stale_realized
                else:
                    raise ValueError(f"Unsupported trade side: {event.side}")

                fee_base, stale_fee, _ = self._convert_amount(
                    amount=fee,
                    from_currency=key[2],
                    to_currency=account.base_currency,
                    as_of_date=event_date,
                )
                tax_base, stale_tax, _ = self._convert_amount(
                    amount=tax,
                    from_currency=key[2],
                    to_currency=account.base_currency,
                    as_of_date=event_date,
                )
                fees_total_base += fee_base
                taxes_total_base += tax_base
                fx_stale = fx_stale or stale_fee or stale_tax
                continue

            if event_type == "corp":
                key = (
                    self._normalize_symbol_for_position(event.symbol),
                    self._normalize_market(event.market),
                    self._normalize_currency(event.currency),
                )
                action_type = (event.action_type or "").strip().lower()
                if action_type == "cash_dividend":
                    per_share = float(event.cash_dividend_per_share or 0.0)
                    if per_share <= 0:
                        continue
                    qty_held = self._held_quantity(
                        key=key,
                        cost_method=cost_method,
                        fifo_lots=fifo_lots,
                        avg_state=avg_state,
                    )
                    if qty_held > EPS:
                        cash_balances[key[2]] += qty_held * per_share
                elif action_type == "split_adjustment":
                    split_ratio = float(event.split_ratio or 0.0)
                    if split_ratio <= 0:
                        raise ValueError(f"Invalid split_ratio for {event.symbol}")
                    if abs(split_ratio - 1.0) <= EPS:
                        continue
                    if cost_method == "fifo":
                        for lot in fifo_lots[key]:
                            lot["remaining_quantity"] *= split_ratio
                            lot["unit_cost"] /= split_ratio
                    else:
                        state = avg_state[key]
                        state.quantity *= split_ratio
                else:
                    raise ValueError(f"Unsupported corporate action type: {event.action_type}")

        position_rows, lot_rows, market_value_base, total_cost_base, stale_pos = self._build_positions(
            account=account,
            as_of_date=as_of_date,
            cost_method=cost_method,
            fifo_lots=fifo_lots,
            avg_state=avg_state,
            include_realtime=include_realtime,
            realtime_prices=realtime_prices,
        )
        fx_stale = fx_stale or stale_pos

        total_cash_base = 0.0
        for currency, amount in cash_balances.items():
            converted, stale, _ = self._convert_amount(
                amount=amount,
                from_currency=currency,
                to_currency=account.base_currency,
                as_of_date=as_of_date,
            )
            total_cash_base += converted
            fx_stale = fx_stale or stale

        unrealized_pnl_base = market_value_base - total_cost_base
        total_equity_base = total_cash_base + market_value_base
        position_limitations = [
            limitation
            for position in position_rows
            for limitation in position.get("limitations", [])
        ]
        limitations = _merge_portfolio_limitations(
            _portfolio_limitations_for_market(account.market),
            position_limitations,
        )

        account_payload = {
            "account_id": account.id,
            "account_name": account.name,
            "owner_id": account.owner_id,
            "broker": account.broker,
            "market": account.market,
            "base_currency": account.base_currency,
            "as_of": as_of_date.isoformat(),
            "cost_method": cost_method,
            "total_cash": round(total_cash_base, 6),
            "total_market_value": round(market_value_base, 6),
            "total_equity": round(total_equity_base, 6),
            "realized_pnl": round(realized_pnl_base, 6),
            "unrealized_pnl": round(unrealized_pnl_base, 6),
            "fee_total": round(fees_total_base, 6),
            "tax_total": round(taxes_total_base, 6),
            "fx_stale": fx_stale,
            "data_quality": "partial" if limitations else "ok",
            "limitations": limitations,
            "positions": position_rows,
        }

        return {
            "public": account_payload,
            "payload": account_payload,
            "positions_cache": position_rows,
            "lots_cache": lot_rows,
            "total_cash": float(total_cash_base),
            "total_market_value": float(market_value_base),
            "total_equity": float(total_equity_base),
            "realized_pnl": float(realized_pnl_base),
            "unrealized_pnl": float(unrealized_pnl_base),
            "fee_total": float(fees_total_base),
            "tax_total": float(taxes_total_base),
            "fx_stale": fx_stale,
        }

    def _build_positions(
        self,
        *,
        account: Any,
        as_of_date: date,
        cost_method: str,
        fifo_lots: Dict[Tuple[str, str, str], List[Dict[str, Any]]],
        avg_state: Dict[Tuple[str, str, str], _AvgState],
        include_realtime: bool = True,
        realtime_prices: Optional[Dict[str, Tuple[Optional[float], Optional[str]]]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, float, bool]:
        position_rows: List[Dict[str, Any]] = []
        lot_rows: List[Dict[str, Any]] = []
        market_value_base = 0.0
        total_cost_base = 0.0
        fx_stale = False

        keys: Iterable[Tuple[str, str, str]]
        if cost_method == "fifo":
            keys = list(fifo_lots.keys())
        else:
            keys = list(avg_state.keys())

        active_symbols: List[str] = []
        if include_realtime and as_of_date == date.today():
            for key in sorted(keys):
                symbol, _, _ = key
                if cost_method == "fifo":
                    qty = sum(
                        float(lot["remaining_quantity"])
                        for lot in fifo_lots[key]
                        if lot["remaining_quantity"] > EPS
                    )
                else:
                    qty = float(avg_state[key].quantity)
                if qty > EPS:
                    active_symbols.append(symbol)
        if realtime_prices is None and active_symbols:
            realtime_prices = self._prefetch_realtime_position_prices(active_symbols)

        for key in sorted(keys):
            symbol, market, currency = key

            if cost_method == "fifo":
                active_lots = [lot for lot in fifo_lots[key] if lot["remaining_quantity"] > EPS]
                qty = sum(float(lot["remaining_quantity"]) for lot in active_lots)
                if qty <= EPS:
                    continue
                total_cost = sum(float(lot["remaining_quantity"]) * float(lot["unit_cost"]) for lot in active_lots)
                avg_cost = total_cost / qty
                lot_rows.extend(active_lots)
            else:
                state = avg_state[key]
                qty = float(state.quantity)
                total_cost = float(state.total_cost)
                if qty <= EPS:
                    continue
                avg_cost = total_cost / qty
                lot_rows.append(
                    {
                        "symbol": symbol,
                        "market": market,
                        "currency": currency,
                        "open_date": as_of_date,
                        "remaining_quantity": qty,
                        "unit_cost": avg_cost,
                        "source_trade_id": None,
                    }
                )

            price_info = self._resolve_position_price(
                symbol=symbol,
                as_of_date=as_of_date,
                realtime_prices=realtime_prices,
                include_realtime=include_realtime,
            )
            last_price = price_info.price
            limitations = _portfolio_limitations_for_market(market)

            if price_info.is_available:
                local_market_value = qty * float(last_price)
                market_base, stale_market, _ = self._convert_amount(
                    amount=local_market_value,
                    from_currency=currency,
                    to_currency=account.base_currency,
                    as_of_date=as_of_date,
                )
                cost_base, stale_cost, _ = self._convert_amount(
                    amount=total_cost,
                    from_currency=currency,
                    to_currency=account.base_currency,
                    as_of_date=as_of_date,
                )
                unrealized_base = market_base - cost_base
                fx_stale = fx_stale or stale_market or stale_cost
            else:
                market_base = 0.0
                cost_base = 0.0
                unrealized_base = 0.0

            unrealized_pct = None
            if abs(cost_base) > EPS:
                unrealized_pct = unrealized_base / cost_base * 100.0

            position_rows.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "currency": currency,
                    "quantity": round(qty, 8),
                    "avg_cost": round(avg_cost, 8),
                    "total_cost": round(total_cost, 8),
                    "last_price": round(float(last_price), 8),
                    "market_value_base": round(market_base, 8),
                    "unrealized_pnl_base": round(unrealized_base, 8),
                    "unrealized_pnl_pct": round(unrealized_pct, 8) if unrealized_pct is not None else None,
                    "valuation_currency": account.base_currency,
                    "price_source": price_info.source,
                    "price_provider": price_info.provider,
                    "price_date": price_info.price_date.isoformat() if price_info.price_date else None,
                    "price_stale": price_info.is_stale,
                    "price_available": price_info.is_available,
                    "data_quality": "partial" if limitations else "ok",
                    "limitations": limitations,
                }
            )

            market_value_base += market_base
            total_cost_base += cost_base

        return position_rows, lot_rows, market_value_base, total_cost_base, fx_stale

    def _resolve_position_price(
        self,
        *,
        symbol: str,
        as_of_date: date,
        realtime_prices: Optional[Dict[str, Tuple[Optional[float], Optional[str]]]] = None,
        include_realtime: bool = True,
    ) -> _ResolvedPositionPrice:
        today = date.today()

        if include_realtime and as_of_date == today:
            if realtime_prices is None:
                realtime_price, provider = self._fetch_realtime_position_price(symbol)
            else:
                realtime_price, provider = realtime_prices.get(symbol, (None, None))
            if realtime_price is not None and realtime_price > 0:
                return _ResolvedPositionPrice(
                    price=float(realtime_price),
                    source="realtime_quote",
                    price_date=today,
                    is_stale=False,
                    is_available=True,
                    provider=provider,
                )

        close = self.repo.get_latest_close_with_date(symbol=symbol, as_of=as_of_date)
        if close is not None:
            close_price, close_date = close
            if close_price > 0:
                return _ResolvedPositionPrice(
                    price=float(close_price),
                    source="history_close",
                    price_date=close_date,
                    is_stale=close_date < as_of_date,
                    is_available=True,
                )

        return _ResolvedPositionPrice(
            price=0.0,
            source="missing",
            price_date=None,
            is_stale=True,
            is_available=False,
        )

    def _prefetch_realtime_position_prices(
        self,
        symbols: Iterable[str],
    ) -> Dict[str, Tuple[Optional[float], Optional[str]]]:
        unique_symbols = sorted({symbol for symbol in symbols if symbol})
        if not unique_symbols:
            return {}

        # Bulk prefetch (when applicable) only warms the fetcher-module-level realtime cache;
        # the manager itself is discarded so per-symbol workers cannot serialize through its
        # per-fetcher call locks when individual reads still need a live fetch (e.g. mixed
        # markets, cache miss, or bulk source returning fewer rows than requested).
        if len(unique_symbols) >= 5:
            try:
                from data_provider.runtime import get_market_data_manager

                get_market_data_manager().prefetch_realtime_quotes(unique_symbols)
            except Exception as exc:
                logger.warning("Failed to prefetch realtime portfolio quotes: %s", exc)

        if len(unique_symbols) == 1:
            symbol = unique_symbols[0]
            return {symbol: self._fetch_realtime_position_price(symbol)}

        results: Dict[str, Tuple[Optional[float], Optional[str]]] = {}
        max_workers = min(PORTFOLIO_REALTIME_QUOTE_MAX_WORKERS, len(unique_symbols))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="portfolio-quote") as executor:
            futures = {
                executor.submit(self._fetch_realtime_position_price, symbol): symbol
                for symbol in unique_symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    results[symbol] = future.result()
                except Exception as exc:  # pragma: no cover - defensive guard for patched fetchers
                    logger.warning("Failed to prefetch realtime portfolio price for %s: %s", symbol, exc)
                    results[symbol] = (None, None)

        return results

    @staticmethod
    def _fetch_realtime_position_price(symbol: str) -> Tuple[Optional[float], Optional[str]]:
        try:
            from data_provider.runtime import get_market_data_manager

            fetcher_manager = get_market_data_manager()
            try:
                quote = fetcher_manager.get_realtime_quote(
                    symbol,
                    log_final_failure=False,
                    concurrent=True,
                )
            except TypeError:
                # Keep compatibility with narrow manager doubles used by
                # integrations while production managers use the shared
                # runtime gate with symbol-level fan-out.
                quote = fetcher_manager.get_realtime_quote(symbol, log_final_failure=False)
        except Exception as exc:
            logger.warning("Failed to fetch realtime portfolio price for %s: %s", symbol, exc)
            return None, None

        if quote is None:
            return None, None

        price = getattr(quote, "price", None)
        try:
            numeric_price = float(price)
        except (TypeError, ValueError):
            return None, None

        if numeric_price <= 0:
            return None, None

        source = getattr(quote, "source", None)
        provider = getattr(source, "value", None) or (str(source) if source is not None else None)
        return numeric_price, provider

    @staticmethod
    def _normalize_symbol_for_storage(symbol: str) -> str:
        return canonical_stock_code(symbol)

    @staticmethod
    def _normalize_symbol_for_position(symbol: str) -> str:
        if not (symbol or "").strip():
            return ""

        raw = canonical_stock_code(symbol)
        if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"} and raw[2:].isdigit():
            return raw

        if "." in raw:
            base, suffix = raw.rsplit(".", 1)
            if base.isdigit() and suffix in {"SH", "SS", "SZ", "BJ"}:
                exchange = "SH" if suffix == "SS" else suffix
                return f"{exchange}{base}"

        return canonical_stock_code(normalize_stock_code(symbol))

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """
        Canonicalization for symbol filtering with exchange-qualified input preservation.

        Keep explicit A-share exchange annotations (SH/SZ/BJ) intact to avoid collapsing
        different exchange variants of the same 6-digit core code.
        """
        raw = canonical_stock_code(symbol)
        if not raw:
            return ""

        if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"} and raw[2:].isdigit():
            return raw

        if "." in raw:
            base, suffix = raw.rsplit(".", 1)
            if base.isdigit() and suffix in {"SH", "SS", "SZ", "BJ"}:
                exchange = "SH" if suffix == "SS" else suffix
                return f"{exchange}{base}"

        return canonical_stock_code(normalize_stock_code(symbol))

    @classmethod
    def _build_symbol_filter_values(cls, symbol: str) -> List[str]:
        original = (symbol or "").strip().upper()
        normalized = cls._normalize_symbol(original)
        if not normalized:
            return []

        seen: Set[str] = set()
        values: List[str] = []

        def _add(value: Optional[str]) -> None:
            candidate = (value or "").strip().upper()
            if candidate and candidate not in seen:
                seen.add(candidate)
                values.append(candidate)

        _add(original)
        _add(normalized)

        if normalized.startswith("HK"):
            hk_digits = normalized[2:]
            if hk_digits.isdigit() and len(hk_digits) == 5:
                legacy_hk_digits = str(int(hk_digits))
                _add(f"HK{hk_digits}")
                _add(f"HK{legacy_hk_digits}")
                _add(f"{hk_digits}.HK")
                _add(f"{legacy_hk_digits}.HK")
            return values

        explicit_exchange: Optional[str] = None
        if len(original) >= 8 and original[:2] in {"SH", "SZ", "BJ"} and original[2:].isdigit():
            explicit_exchange = original[:2]
            explicit_code = original[2:]
        elif "." in original:
            base, suffix = original.rsplit(".", 1)
            if base.isdigit() and suffix in {"SH", "SS", "SZ", "BJ"}:
                explicit_exchange = "SH" if suffix == "SS" else suffix
                explicit_code = base
            else:
                explicit_code = None
        else:
            explicit_code = None

        if normalized.isdigit():
            if len(normalized) == 6:
                exchanges = [explicit_exchange] if explicit_exchange else ["SH", "SZ", "BJ"]
                for exchange in exchanges:
                    if exchange is None:
                        continue
                    _add(f"{exchange}{normalized}")
                    _add(f"{normalized}.{'SS' if exchange == 'SH' else exchange}")
                    if exchange == "SH":
                        _add(f"{normalized}.SH")
            return values

        if explicit_exchange is not None and explicit_code is not None and explicit_code.isdigit():
            if len(explicit_code) == 6:
                _add(f"{explicit_exchange}{explicit_code}")
                _add(f"{explicit_code}.{'SS' if explicit_exchange == 'SH' else explicit_exchange}")
                if explicit_exchange == "SH":
                    _add(f"{explicit_code}.SH")
            elif len(normalized) == 5:
                _add(f"HK{normalized}")
                _add(f"{normalized}.HK")

        return values

    @staticmethod
    def _consume_fifo_lots(
        lots: List[Dict[str, Any]],
        quantity: float,
        symbol: str,
        trade_date: Optional[date] = None,
    ) -> float:
        remaining = quantity
        cost_basis = 0.0
        while remaining > EPS:
            if not lots:
                raise PortfolioOversellError(
                    symbol=symbol,
                    trade_date=trade_date,
                    requested_quantity=quantity,
                    available_quantity=quantity - remaining,
                )
            head = lots[0]
            take = min(remaining, float(head["remaining_quantity"]))
            cost_basis += take * float(head["unit_cost"])
            head["remaining_quantity"] = float(head["remaining_quantity"]) - take
            remaining -= take
            if head["remaining_quantity"] <= EPS:
                lots.pop(0)
        return cost_basis

    @staticmethod
    def _consume_avg_position(
        state: _AvgState,
        quantity: float,
        symbol: str,
        trade_date: Optional[date] = None,
    ) -> float:
        if state.quantity + EPS < quantity:
            raise PortfolioOversellError(
                symbol=symbol,
                trade_date=trade_date,
                requested_quantity=quantity,
                available_quantity=state.quantity,
            )
        if state.quantity <= EPS:
            raise PortfolioOversellError(
                symbol=symbol,
                trade_date=trade_date,
                requested_quantity=quantity,
                available_quantity=0.0,
            )
        avg_cost = state.total_cost / state.quantity
        cost_basis = avg_cost * quantity
        state.quantity -= quantity
        state.total_cost -= cost_basis
        if state.quantity <= EPS:
            state.quantity = 0.0
            state.total_cost = 0.0
        return cost_basis

    @staticmethod
    def _held_quantity(
        *,
        key: Tuple[str, str, str],
        cost_method: str,
        fifo_lots: Dict[Tuple[str, str, str], List[Dict[str, Any]]],
        avg_state: Dict[Tuple[str, str, str], _AvgState],
    ) -> float:
        if cost_method == "fifo":
            return sum(float(lot["remaining_quantity"]) for lot in fifo_lots.get(key, []))
        return float(avg_state.get(key, _AvgState()).quantity)

    def _convert_amount(
        self,
        *,
        amount: float,
        from_currency: str,
        to_currency: str,
        as_of_date: date,
    ) -> Tuple[float, bool, str]:
        from_norm = self._normalize_currency(from_currency)
        to_norm = self._normalize_currency(to_currency)
        if abs(amount) <= EPS:
            return 0.0, False, "zero"
        if from_norm == to_norm:
            return float(amount), False, "identity"

        direct = self.repo.get_latest_fx_rate(
            from_currency=from_norm,
            to_currency=to_norm,
            as_of=as_of_date,
        )
        if direct is not None and direct.rate > 0:
            return float(amount) * float(direct.rate), bool(direct.is_stale), "direct_rate"

        inverse = self.repo.get_latest_fx_rate(
            from_currency=to_norm,
            to_currency=from_norm,
            as_of=as_of_date,
        )
        if inverse is not None and inverse.rate > 0:
            return float(amount) / float(inverse.rate), bool(inverse.is_stale), "inverse_rate"

        # P0 fallback: keep pipeline available even when FX cache is missing.
        return float(amount), True, "fallback_1_to_1"

    def convert_amount(
        self,
        *,
        amount: float,
        from_currency: str,
        to_currency: str,
        as_of_date: date,
    ) -> Tuple[float, bool, str]:
        """Public conversion entry for cross-service consumers."""
        return self._convert_amount(
            amount=amount,
            from_currency=from_currency,
            to_currency=to_currency,
            as_of_date=as_of_date,
        )

    def _list_account_refresh_fx_currencies(
        self,
        *,
        account: Any,
        as_of_date: date,
        strict: bool = True,
    ) -> List[str]:
        """Return distinct non-base currencies participating in refresh for one account."""
        base_currency = self._normalize_currency(account.base_currency)
        currencies: Set[str] = set()
        rows = list(self.repo.list_trades(account.id, as_of=as_of_date))
        rows.extend(self.repo.list_cash_ledger(account.id, as_of=as_of_date))
        for row in rows:
            try:
                currency = self._normalize_currency(row.currency)
            except ValueError:
                if strict:
                    raise
                logger.warning(
                    "Skip invalid FX refresh currency for account %s on %s: %r",
                    account.id,
                    as_of_date.isoformat(),
                    getattr(row, "currency", None),
                )
                continue
            if currency != base_currency:
                currencies.add(currency)
        return sorted(currencies)

    def _refresh_account_fx_rates(
        self,
        *,
        account: Any,
        as_of_date: date,
        refresh_enabled: bool,
    ) -> Dict[str, int]:
        """Refresh FX pairs for one account and keep stale fallback on failures."""
        refresh_currencies = self._list_account_refresh_fx_currencies(
            account=account,
            as_of_date=as_of_date,
            strict=refresh_enabled,
        )
        if not refresh_enabled:
            return {
                "pair_count": len(refresh_currencies),
                "updated_count": 0,
                "stale_count": 0,
                "error_count": 0,
            }

        base_currency = self._normalize_currency(account.base_currency)
        summary = {
            "pair_count": len(refresh_currencies),
            "updated_count": 0,
            "stale_count": 0,
            "error_count": 0,
        }
        for from_currency in refresh_currencies:
            try:
                rate = self._fetch_fx_rate_from_yfinance(
                    from_currency=from_currency,
                    to_currency=base_currency,
                    as_of_date=as_of_date,
                )
                if rate is not None and rate > 0:
                    self.repo.save_fx_rate(
                        from_currency=from_currency,
                        to_currency=base_currency,
                        rate_date=as_of_date,
                        rate=rate,
                        source="yfinance",
                        is_stale=False,
                    )
                    summary["updated_count"] += 1
                    continue
            except Exception as exc:
                logger.warning(
                    "FX online fetch failed for %s/%s on %s: %s",
                    from_currency,
                    base_currency,
                    as_of_date.isoformat(),
                    exc,
                )

            fallback = self.repo.get_latest_fx_rate(
                from_currency=from_currency,
                to_currency=base_currency,
                as_of=as_of_date,
            )
            if fallback is not None and float(fallback.rate or 0.0) > 0:
                self.repo.save_fx_rate(
                    from_currency=from_currency,
                    to_currency=base_currency,
                    rate_date=as_of_date,
                    rate=float(fallback.rate),
                    source=(fallback.source or "cache_fallback"),
                    is_stale=True,
                )
                summary["stale_count"] += 1
            else:
                summary["error_count"] += 1
        return summary

    @staticmethod
    def _fetch_fx_rate_from_yfinance(
        *,
        from_currency: str,
        to_currency: str,
        as_of_date: date,
    ) -> Optional[float]:
        """Fetch latest available FX close rate around as_of date."""
        if yf is None:
            return None
        symbol = f"{from_currency}{to_currency}=X"
        ticker = yf.Ticker(symbol)
        history = ticker.history(
            start=(as_of_date - timedelta(days=7)).isoformat(),
            end=(as_of_date + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
        )
        if history is None or history.empty or "Close" not in history:
            return None
        close = history["Close"].dropna()
        if close.empty:
            return None
        value = float(close.iloc[-1])
        if value <= 0:
            return None
        return value

    def _require_active_account(self, account_id: int) -> Any:
        account = self.repo.get_account(account_id, include_inactive=False)
        if account is None:
            raise ValueError(f"Active account not found: {account_id}")
        return account

    def _require_active_account_in_session(self, *, session: Any, account_id: int) -> Any:
        account = self.repo.get_account_in_session(
            session=session,
            account_id=account_id,
            include_inactive=False,
        )
        if account is None:
            raise ValueError(f"Active account not found: {account_id}")
        return account

    def _has_trade_uid(self, *, account_id: int, trade_uid: str, session: Optional[Any] = None) -> bool:
        if session is None:
            return self.repo.has_trade_uid(account_id, trade_uid)
        return self.repo.has_trade_uid_in_session(session=session, account_id=account_id, trade_uid=trade_uid)

    def _has_trade_dedup_hash(
        self,
        *,
        account_id: int,
        dedup_hash: str,
        session: Optional[Any] = None,
    ) -> bool:
        if session is None:
            return self.repo.has_trade_dedup_hash(account_id, dedup_hash)
        return self.repo.has_trade_dedup_hash_in_session(
            session=session,
            account_id=account_id,
            dedup_hash=dedup_hash,
        )

    @staticmethod
    def _account_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": row.id,
            "owner_id": row.owner_id,
            "name": row.name,
            "broker": row.broker,
            "market": row.market,
            "base_currency": row.base_currency,
            "is_active": bool(row.is_active),
            "account_kind": getattr(row, "account_kind", "manual") or "manual",
            "controller_kind": getattr(row, "controller_kind", "manual") or "manual",
            "external_execution_enabled": bool(getattr(row, "external_execution_enabled", False)),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _trade_row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "account_id": int(row.account_id),
            "trade_uid": row.trade_uid,
            "symbol": row.symbol,
            "market": row.market,
            "currency": row.currency,
            "trade_date": row.trade_date.isoformat() if row.trade_date else "",
            "side": row.side,
            "quantity": float(row.quantity),
            "price": float(row.price),
            "fee": float(row.fee),
            "tax": float(row.tax),
            "note": row.note,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _cash_ledger_row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "account_id": int(row.account_id),
            "event_date": row.event_date.isoformat() if row.event_date else "",
            "direction": row.direction,
            "amount": float(row.amount),
            "currency": row.currency,
            "note": row.note,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _corporate_action_row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "account_id": int(row.account_id),
            "symbol": row.symbol,
            "market": row.market,
            "currency": row.currency,
            "effective_date": row.effective_date.isoformat() if row.effective_date else "",
            "action_type": row.action_type,
            "cash_dividend_per_share": (
                float(row.cash_dividend_per_share) if row.cash_dividend_per_share is not None else None
            ),
            "split_ratio": float(row.split_ratio) if row.split_ratio is not None else None,
            "note": row.note,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _validate_paging(*, page: int, page_size: int) -> Tuple[int, int]:
        if page < 1:
            raise ValueError("page must be >= 1")
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be in [1, 100]")
        return page, page_size

    @staticmethod
    def _normalize_market(value: str) -> str:
        market = (value or "").strip().lower()
        if market not in VALID_MARKETS:
            raise ValueError("market must be one of: cn, hk, us, jp, kr, tw")
        return market

    @staticmethod
    def _normalize_currency(value: str) -> str:
        currency = (value or "").strip().upper()
        if not currency:
            raise ValueError("currency is required")
        return currency

    @staticmethod
    def _normalize_cost_method(value: str) -> str:
        method = (value or "").strip().lower()
        if method not in VALID_COST_METHODS:
            raise ValueError("cost_method must be fifo or avg")
        return method

    @staticmethod
    def _default_currency_for_market(market: str) -> str:
        if market == "hk":
            return "HKD"
        if market == "us":
            return "USD"
        return "CNY"
