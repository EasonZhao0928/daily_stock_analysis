# -*- coding: utf-8 -*-
"""Persistence seam for Paper Account state and audit records."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import desc, select

from src.storage import (
    DatabaseManager,
    PaperAccountConfig,
    PaperDecisionRun,
    PaperObservation,
    PaperOrder,
    PaperFill,
    PaperProposal,
    PaperRiskDecision,
    PortfolioAccount,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class PaperAccountRepository:
    """Small ORM repository; state transitions stay in the service facade."""

    def __init__(self, db: Optional[DatabaseManager] = None) -> None:
        self.db = db or DatabaseManager.get_instance()

    @staticmethod
    def account_dict(row: PortfolioAccount) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "name": row.name,
            "broker": row.broker,
            "market": row.market,
            "base_currency": row.base_currency,
            "is_active": bool(row.is_active),
            "account_kind": getattr(row, "account_kind", "manual") or "manual",
            "controller_kind": getattr(row, "controller_kind", "manual") or "manual",
            "external_execution_enabled": bool(getattr(row, "external_execution_enabled", False)),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def config_dict(row: PaperAccountConfig) -> Dict[str, Any]:
        return {
            "config_id": row.config_id,
            "account_id": row.account_id,
            "config_version": row.config_version,
            "controller_kind": row.controller_kind,
            "approval_mode": row.approval_mode,
            "mandate_version": row.mandate_version,
            "mandate": _loads(row.mandate_json, {}),
            "initial_cash": row.initial_cash,
            "state": row.state,
            "enabled": bool(row.enabled),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def run_dict(row: PaperDecisionRun) -> Dict[str, Any]:
        return {
            "run_id": row.run_id,
            "account_id": row.account_id,
            "decision_at": row.decision_at,
            "strategy_version": row.strategy_version,
            "config_version": row.config_version,
            "status": row.status,
            "idempotency_key": row.idempotency_key,
            "backend": getattr(row, "backend", None),
            "model": getattr(row, "model", None),
            "prompt_version": getattr(row, "prompt_version", None),
            "skill_version": getattr(row, "skill_version", None),
            "tool_trace": _loads(getattr(row, "tool_trace_json", None), []),
            "diagnostics": _loads(getattr(row, "diagnostics_json", None), {}),
            "created_at": row.created_at,
        }

    @staticmethod
    def observation_dict(row: PaperObservation) -> Dict[str, Any]:
        return {
            "observation_id": row.observation_id,
            "run_id": row.run_id,
            "account_id": row.account_id,
            "cutoff_at": row.cutoff_at,
            "payload_hash": row.payload_hash,
            "payload": _loads(row.payload_json, {}),
            "status": row.status,
            "created_at": row.created_at,
        }

    @staticmethod
    def proposal_dict(row: PaperProposal) -> Dict[str, Any]:
        return {
            "proposal_id": row.proposal_id,
            "run_id": row.run_id,
            "account_id": row.account_id,
            "proposal_version": row.proposal_version,
            "symbol": row.symbol,
            "market": row.market,
            "side": row.side,
            "order_type": row.order_type,
            "quantity": row.quantity,
            "target_weight": row.target_weight,
            "limit_price": row.limit_price,
            "stop_price": row.stop_price,
            "rationale": row.rationale,
            "evidence_refs": _loads(row.evidence_refs, []),
            "proposal_hash": row.proposal_hash,
            "status": row.status,
            "decision_by": getattr(row, "decision_by", None),
            "decision_at": getattr(row, "decision_at", None),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def order_dict(row: PaperOrder) -> Dict[str, Any]:
        return {
            "order_id": row.order_id,
            "proposal_id": row.proposal_id,
            "account_id": row.account_id,
            "symbol": row.symbol,
            "market": row.market,
            "side": row.side,
            "order_type": row.order_type,
            "quantity": row.quantity,
            "limit_price": row.limit_price,
            "stop_price": getattr(row, "stop_price", None),
            "status": row.status,
            "version": row.version,
            "observation_id": getattr(row, "observation_id", None),
            "observation_cutoff": getattr(row, "observation_cutoff", None),
            "execution_policy": getattr(row, "execution_policy", "next_open"),
            "expires_at": getattr(row, "expires_at", None),
            "filled_quantity": getattr(row, "filled_quantity", 0.0) or 0.0,
            "avg_fill_price": getattr(row, "avg_fill_price", None),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def fill_dict(row: PaperFill) -> Dict[str, Any]:
        return {
            "fill_id": row.fill_id,
            "order_id": row.order_id,
            "account_id": row.account_id,
            "fill_date": row.fill_date,
            "quantity": row.quantity,
            "price": row.price,
            "fee": row.fee,
            "tax": row.tax,
            "fill_hash": row.fill_hash,
            "status": row.status,
            "ledger_outbox_id": row.ledger_outbox_id,
            "bar_timestamp": getattr(row, "bar_timestamp", None),
            "execution_policy": getattr(row, "execution_policy", "next_open"),
            "created_at": row.created_at,
        }

    @staticmethod
    def risk_dict(row: PaperRiskDecision) -> Dict[str, Any]:
        return {
            "risk_decision_id": row.risk_decision_id,
            "proposal_id": row.proposal_id,
            "run_id": row.run_id,
            "decision": row.decision,
            "rule_codes": _loads(row.rule_codes, []),
            "details": _loads(row.details_json, {}),
            "mandate_version": row.mandate_version,
            "created_at": row.created_at,
        }

    def get_account(self, account_id: int, *, include_inactive: bool = True) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(PortfolioAccount, int(account_id))
            if row is None or (not include_inactive and not row.is_active):
                return None
            return self.account_dict(row)

    def list_accounts(self, *, include_inactive: bool = False) -> List[Dict[str, Any]]:
        """List only canonical Paper Account rows with their configs."""
        with self.db.get_session() as session:
            query = select(PortfolioAccount).where(PortfolioAccount.account_kind == "paper")
            if not include_inactive:
                query = query.where(PortfolioAccount.is_active.is_(True))
            rows = session.execute(query.order_by(PortfolioAccount.id.asc())).scalars().all()
            result: List[Dict[str, Any]] = []
            for row in rows:
                config = session.execute(
                    select(PaperAccountConfig).where(PaperAccountConfig.account_id == int(row.id))
                ).scalar_one_or_none()
                if config is None:
                    continue
                result.append({"account": self.account_dict(row), "config": self.config_dict(config)})
            return result

    def get_config(self, account_id: int) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(select(PaperAccountConfig).where(PaperAccountConfig.account_id == int(account_id))).scalar_one_or_none()
            return self.config_dict(row) if row else None

    def set_account_active(self, account_id: int, active: bool) -> Optional[Dict[str, Any]]:
        def _write(session):
            row = session.get(PortfolioAccount, int(account_id))
            if row is None:
                return None
            row.is_active = bool(active)
            row.updated_at = datetime.now()
            session.flush()
            return self.account_dict(row)

        return self.db._run_write_transaction(f"paper.set_account_active[{account_id}:{active}]", _write)

    def create_config(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        values = {
            "config_id": str(payload["config_id"]),
            "account_id": int(payload["account_id"]),
            "config_version": int(payload.get("config_version", 1)),
            "controller_kind": str(payload["controller_kind"]),
            "approval_mode": str(payload["approval_mode"]),
            "mandate_version": str(payload["mandate_version"]),
            "mandate_json": _json(payload.get("mandate") or {}),
            "initial_cash": float(payload.get("initial_cash", 0.0)),
            "state": str(payload.get("state", "active")),
            "enabled": bool(payload.get("enabled", True)),
        }

        def _write(session):
            row = PaperAccountConfig(**values)
            session.add(row)
            session.flush()
            return self.config_dict(row)

        return self.db._run_write_transaction(f"paper.create_config[{values['config_id']}]", _write)

    def update_state(self, account_id: int, *, state: str, expected_version: int) -> Dict[str, Any]:
        def _write(session):
            row = session.execute(select(PaperAccountConfig).where(PaperAccountConfig.account_id == int(account_id))).scalar_one_or_none()
            if row is None:
                raise ValueError("paper account config not found")
            if int(row.config_version) != int(expected_version):
                raise ValueError("paper account version conflict")
            row.config_version = int(row.config_version) + 1
            row.state = str(state)
            row.enabled = state not in {"closed", "frozen"}
            row.updated_at = datetime.now()
            session.flush()
            return self.config_dict(row)

        return self.db._run_write_transaction(f"paper.update_state[{account_id}:{state}]", _write)

    def update_mandate(
        self,
        account_id: int,
        *,
        mandate: Mapping[str, Any],
        mandate_version: str,
        expected_version: int,
    ) -> Dict[str, Any]:
        def _write(session):
            row = session.execute(
                select(PaperAccountConfig).where(PaperAccountConfig.account_id == int(account_id))
            ).scalar_one_or_none()
            if row is None:
                raise ValueError("paper account config not found")
            if int(row.config_version) != int(expected_version):
                raise ValueError("paper account version conflict")
            row.config_version = int(row.config_version) + 1
            row.mandate_version = str(mandate_version)
            row.mandate_json = _json(dict(mandate))
            row.updated_at = datetime.now()
            session.flush()
            return self.config_dict(row)

        return self.db._run_write_transaction(f"paper.update_mandate[{account_id}]", _write)

    def save_run(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        values = dict(payload)
        if "tool_trace" in values:
            values["tool_trace_json"] = _json(values.pop("tool_trace"))
        if "diagnostics" in values:
            values["diagnostics_json"] = _json(values.pop("diagnostics"))

        def _write(session):
            existing = session.get(PaperDecisionRun, values["run_id"])
            if existing:
                return self.run_dict(existing)
            row = PaperDecisionRun(**values)
            session.add(row)
            session.flush()
            return self.run_dict(row)

        return self.db._run_write_transaction(f"paper.save_run[{values['run_id']}]", _write)

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        diagnostics: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Update only the cycle envelope status and sanitized diagnostics."""

        def _write(session):
            row = session.get(PaperDecisionRun, str(run_id))
            if row is None:
                raise ValueError("paper decision run not found")
            row.status = str(status)
            if diagnostics is not None:
                row.diagnostics_json = _json(dict(diagnostics))
            session.flush()
            return self.run_dict(row)

        return self.db._run_write_transaction(f"paper.run_status[{run_id}:{status}]", _write)

    def save_observation(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        values = dict(payload)
        values["payload_json"] = _json(values.pop("payload"))

        def _write(session):
            existing = session.get(PaperObservation, values["observation_id"])
            if existing:
                return self.observation_dict(existing)
            row = PaperObservation(**values)
            session.add(row)
            session.flush()
            return self.observation_dict(row)

        return self.db._run_write_transaction(f"paper.save_observation[{values['observation_id']}]", _write)

    def save_proposal(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        values = dict(payload)
        values["evidence_refs"] = _json(values.pop("evidence_refs", []))

        def _write(session):
            existing = session.get(PaperProposal, values["proposal_id"])
            if existing:
                return self.proposal_dict(existing)
            row = PaperProposal(**values)
            session.add(row)
            session.flush()
            return self.proposal_dict(row)

        return self.db._run_write_transaction(f"paper.save_proposal[{values['proposal_id']}]", _write)

    def save_risk_decision(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        values = dict(payload)
        values["rule_codes"] = _json(values.pop("rule_codes", []))
        values["details_json"] = _json(values.pop("details", {}))

        def _write(session):
            existing = session.get(PaperRiskDecision, values["risk_decision_id"])
            if existing:
                return self.risk_dict(existing)
            row = PaperRiskDecision(**values)
            session.add(row)
            session.flush()
            return self.risk_dict(row)

        return self.db._run_write_transaction(
            f"paper.save_risk[{values['risk_decision_id']}]",
            _write,
        )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(PaperDecisionRun, str(run_id))
            return self.run_dict(row) if row else None

    def list_runs(self, account_id: int, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PaperDecisionRun)
                .where(PaperDecisionRun.account_id == int(account_id))
                .order_by(desc(PaperDecisionRun.decision_at), desc(PaperDecisionRun.created_at))
                .limit(max(1, min(int(limit), 500)))
            ).scalars().all()
            return [self.run_dict(row) for row in rows]

    def get_observation(self, observation_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(PaperObservation, str(observation_id))
            return self.observation_dict(row) if row else None

    def get_observation_for_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(PaperObservation)
                .where(PaperObservation.run_id == str(run_id))
                .order_by(PaperObservation.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            return self.observation_dict(row) if row else None

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(PaperProposal, str(proposal_id))
            return self.proposal_dict(row) if row else None

    def get_risk_decision_for_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(PaperRiskDecision)
                .where(PaperRiskDecision.proposal_id == str(proposal_id))
                .order_by(desc(PaperRiskDecision.created_at))
                .limit(1)
            ).scalar_one_or_none()
            return self.risk_dict(row) if row else None

    def list_proposals(self, account_id: int, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PaperProposal)
                .where(PaperProposal.account_id == int(account_id))
                .order_by(desc(PaperProposal.created_at))
                .limit(max(1, min(int(limit), 500)))
            ).scalars().all()
            return [self.proposal_dict(row) for row in rows]

    def list_run_proposals(self, run_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PaperProposal)
                .where(PaperProposal.run_id == str(run_id))
                .order_by(desc(PaperProposal.created_at))
                .limit(max(1, min(int(limit), 500)))
            ).scalars().all()
            return [self.proposal_dict(row) for row in rows]

    def create_order(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        values = dict(payload)

        def _write(session):
            existing = session.get(PaperOrder, str(values["order_id"]))
            if existing:
                return self.order_dict(existing)
            row = PaperOrder(**values)
            session.add(row)
            session.flush()
            return self.order_dict(row)

        return self.db._run_write_transaction(f"paper.create_order[{values['order_id']}]", _write)

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(PaperOrder, str(order_id))
            return self.order_dict(row) if row else None

    def get_order_for_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(PaperOrder).where(PaperOrder.proposal_id == str(proposal_id)).limit(1)
            ).scalar_one_or_none()
            return self.order_dict(row) if row else None

    def list_orders(self, account_id: int, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PaperOrder)
                .where(PaperOrder.account_id == int(account_id))
                .order_by(desc(PaperOrder.created_at))
                .limit(max(1, min(int(limit), 500)))
            ).scalars().all()
            return [self.order_dict(row) for row in rows]

    def update_order_state(
        self,
        order_id: str,
        *,
        status: str,
        expected_version: int,
        filled_quantity: Optional[float] = None,
        avg_fill_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        def _write(session):
            row = session.get(PaperOrder, str(order_id))
            if row is None:
                raise ValueError("paper order not found")
            if int(row.version) != int(expected_version):
                raise ValueError("paper order version conflict")
            row.status = str(status)
            row.version = int(row.version) + 1
            if filled_quantity is not None:
                row.filled_quantity = float(filled_quantity)
            if avg_fill_price is not None:
                row.avg_fill_price = float(avg_fill_price)
            row.updated_at = datetime.now()
            session.flush()
            return self.order_dict(row)

        return self.db._run_write_transaction(f"paper.order_state[{order_id}:{status}]", _write)

    def save_fill(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        values = dict(payload)

        def _write(session):
            existing = session.get(PaperFill, str(values["fill_id"]))
            if existing:
                return self.fill_dict(existing)
            duplicate = session.execute(
                select(PaperFill).where(
                    PaperFill.order_id == str(values["order_id"]),
                    PaperFill.fill_hash == str(values["fill_hash"]),
                ).limit(1)
            ).scalar_one_or_none()
            if duplicate:
                return self.fill_dict(duplicate)
            row = PaperFill(**values)
            session.add(row)
            session.flush()
            return self.fill_dict(row)

        return self.db._run_write_transaction(f"paper.save_fill[{values['fill_id']}]", _write)

    def get_fill(self, fill_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(PaperFill, str(fill_id))
            return self.fill_dict(row) if row else None

    def list_fills(self, account_id: int, *, order_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            query = select(PaperFill).where(PaperFill.account_id == int(account_id))
            if order_id:
                query = query.where(PaperFill.order_id == str(order_id))
            rows = session.execute(
                query.order_by(desc(PaperFill.created_at)).limit(max(1, min(int(limit), 500)))
            ).scalars().all()
            return [self.fill_dict(row) for row in rows]

    def update_fill_status(
        self,
        fill_id: str,
        *,
        status: str,
        ledger_outbox_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        def _write(session):
            row = session.get(PaperFill, str(fill_id))
            if row is None:
                raise ValueError("paper fill not found")
            row.status = str(status)
            if ledger_outbox_id is not None:
                row.ledger_outbox_id = int(ledger_outbox_id)
            session.flush()
            return self.fill_dict(row)

        return self.db._run_write_transaction(f"paper.fill_status[{fill_id}:{status}]", _write)


__all__ = ["PaperAccountRepository"]
