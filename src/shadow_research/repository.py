# -*- coding: utf-8 -*-
"""Persistence seam for Shadow Research records."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, desc, select

from src.storage import (
    DatabaseManager,
    ShadowBacktestRun,
    ShadowProfile,
    ShadowRule,
    ShadowSignal,
    utc_naive_now,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _as_date(value: Any) -> Any:
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return value
class ShadowResearchRepository:
    """CRUD operations with SQL uniqueness as the replay/idempotency guard."""

    def __init__(self, db: Optional[DatabaseManager] = None) -> None:
        self.db = db or DatabaseManager.get_instance()

    @staticmethod
    def _profile_dict(row: ShadowProfile) -> Dict[str, Any]:
        return {
            "profile_id": row.profile_id,
            "name": row.name,
            "description": row.description,
            "status": row.status,
            "rule_version": row.rule_version,
            "approved_at": row.approved_at,
            "approved_by": row.approved_by,
            "degraded_reason": row.degraded_reason,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _rule_dict(row: ShadowRule) -> Dict[str, Any]:
        return {
            "rule_id": row.rule_id,
            "profile_id": row.profile_id,
            "version": row.version,
            "dsl": _loads(row.dsl, {}),
            "rule_hash": row.rule_hash,
            "feature_names": _loads(row.feature_names, []),
            "status": row.status,
            "created_at": row.created_at,
        }

    @staticmethod
    def _run_dict(row: ShadowBacktestRun) -> Dict[str, Any]:
        return {
            "run_id": row.run_id,
            "profile_id": row.profile_id,
            "rule_id": row.rule_id,
            "code": row.code,
            "split_date": row.split_date,
            "source_snapshot_hash": row.source_snapshot_hash,
            "status": row.status,
            "metrics": _loads(row.metrics, {}),
            "degraded_reason": row.degraded_reason,
            "created_at": row.created_at,
        }

    @staticmethod
    def _signal_dict(row: ShadowSignal) -> Dict[str, Any]:
        return {
            "signal_id": row.signal_id,
            "profile_id": row.profile_id,
            "rule_id": row.rule_id,
            "run_id": row.run_id,
            "code": row.code,
            "signal_date": row.signal_date,
            "status": row.status,
            "feature_snapshot_hash": row.feature_snapshot_hash,
            "data_cutoff": row.data_cutoff,
            "evidence_refs": _loads(row.evidence_refs, []),
            "reason": row.reason,
            "created_at": row.created_at,
        }

    def create_profile(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        values = {
            "profile_id": str(payload["profile_id"]),
            "name": str(payload["name"]).strip(),
            "description": payload.get("description"),
            "status": str(payload.get("status", "draft")),
            "rule_version": str(payload.get("rule_version", "1")),
            "approved_at": payload.get("approved_at"),
            "approved_by": payload.get("approved_by"),
            "degraded_reason": payload.get("degraded_reason"),
            "created_at": payload.get("created_at") or datetime.now(),
            "updated_at": datetime.now(),
        }

        def _write(session):
            row = ShadowProfile(**values)
            session.add(row)
            session.flush()
            return self._profile_dict(row)

        return self.db._run_write_transaction(f"shadow.create_profile[{values['profile_id']}]", _write)

    def get_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(ShadowProfile, str(profile_id))
            return self._profile_dict(row) if row else None

    def list_profiles(self, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            query = select(ShadowProfile).order_by(desc(ShadowProfile.updated_at))
            if status:
                query = query.where(ShadowProfile.status == status)
            return [self._profile_dict(row) for row in session.execute(query).scalars().all()]

    def update_profile_status(
        self,
        profile_id: str,
        *,
        status: str,
        approved_by: Optional[str] = None,
        degraded_reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        def _write(session):
            row = session.get(ShadowProfile, str(profile_id))
            if row is None:
                return None
            row.status = str(status)
            row.approved_by = approved_by or row.approved_by
            row.approved_at = datetime.now() if status == "approved" else row.approved_at
            row.degraded_reason = degraded_reason
            row.updated_at = datetime.now()
            session.flush()
            return self._profile_dict(row)

        return self.db._run_write_transaction(f"shadow.update_profile[{profile_id}]", _write)

    def save_rule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        values = {
            "profile_id": str(payload["profile_id"]),
            "version": str(payload["version"]),
            "dsl": _json(payload.get("dsl") or {}),
            "rule_hash": str(payload["rule_hash"]),
            "feature_names": _json(payload.get("feature_names") or []),
            "status": str(payload.get("status", "draft")),
            "created_at": payload.get("created_at") or datetime.now(),
        }

        def _write(session):
            existing = session.execute(
                select(ShadowRule).where(
                    and_(ShadowRule.profile_id == values["profile_id"], ShadowRule.version == values["version"])
                )
            ).scalar_one_or_none()
            if existing:
                if existing.rule_hash != values["rule_hash"]:
                    raise ValueError("shadow rule version is immutable")
                return self._rule_dict(existing)
            row = ShadowRule(**values)
            session.add(row)
            session.flush()
            return self._rule_dict(row)

        return self.db._run_write_transaction(f"shadow.save_rule[{values['profile_id']}:{values['version']}]", _write)

    def get_rule(self, profile_id: str, version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            query = select(ShadowRule).where(ShadowRule.profile_id == str(profile_id))
            if version is not None:
                query = query.where(ShadowRule.version == str(version))
            query = query.order_by(desc(ShadowRule.created_at)).limit(1)
            row = session.execute(query).scalar_one_or_none()
            return self._rule_dict(row) if row else None

    def save_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        values = {
            "run_id": str(payload["run_id"]),
            "profile_id": str(payload["profile_id"]),
            "rule_id": int(payload["rule_id"]),
            "code": str(payload["code"]),
            "split_date": _as_date(payload["split_date"]),
            "source_snapshot_hash": str(payload["source_snapshot_hash"]),
            "status": str(payload.get("status", "completed")),
            "metrics": _json(payload.get("metrics") or {}),
            "degraded_reason": payload.get("degraded_reason"),
            "created_at": payload.get("created_at") or datetime.now(),
        }

        def _write(session):
            existing = session.get(ShadowBacktestRun, values["run_id"])
            if existing:
                return self._run_dict(existing)
            duplicate = session.execute(
                select(ShadowBacktestRun).where(
                    and_(
                        ShadowBacktestRun.profile_id == values["profile_id"],
                        ShadowBacktestRun.rule_id == values["rule_id"],
                        ShadowBacktestRun.code == values["code"],
                        ShadowBacktestRun.source_snapshot_hash == values["source_snapshot_hash"],
                    )
                )
            ).scalar_one_or_none()
            if duplicate:
                return self._run_dict(duplicate)
            row = ShadowBacktestRun(**values)
            session.add(row)
            session.flush()
            return self._run_dict(row)

        return self.db._run_write_transaction(f"shadow.save_run[{values['run_id']}]", _write)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(ShadowBacktestRun, str(run_id))
            return self._run_dict(row) if row else None

    def get_latest_run(self, profile_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(ShadowBacktestRun)
                .where(ShadowBacktestRun.profile_id == str(profile_id))
                .order_by(desc(ShadowBacktestRun.created_at))
                .limit(1)
            ).scalar_one_or_none()
            return self._run_dict(row) if row else None

    def save_signal(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        values = {
            "signal_id": str(payload["signal_id"]),
            "profile_id": str(payload["profile_id"]),
            "rule_id": int(payload["rule_id"]),
            "run_id": str(payload["run_id"]),
            "code": str(payload["code"]),
            "signal_date": _as_date(payload["signal_date"]),
            "status": str(payload.get("status", "eligible")),
            "feature_snapshot_hash": str(payload["feature_snapshot_hash"]),
            "data_cutoff": _as_date(payload["data_cutoff"]),
            "evidence_refs": _json(payload.get("evidence_refs") or []),
            "reason": payload.get("reason"),
            "created_at": payload.get("created_at") or datetime.now(),
        }

        def _write(session):
            existing = session.get(ShadowSignal, values["signal_id"])
            if existing:
                return self._signal_dict(existing)
            row = ShadowSignal(**values)
            session.add(row)
            session.flush()
            return self._signal_dict(row)

        return self.db._run_write_transaction(f"shadow.save_signal[{values['signal_id']}]", _write)

    def list_signals(self, profile_id: str, *, code: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            query = select(ShadowSignal).where(ShadowSignal.profile_id == str(profile_id)).order_by(desc(ShadowSignal.signal_date), desc(ShadowSignal.created_at)).limit(max(1, min(int(limit), 500)))
            if code:
                query = query.where(ShadowSignal.code == str(code))
            return [self._signal_dict(row) for row in session.execute(query).scalars().all()]


__all__ = ["ShadowResearchRepository"]
