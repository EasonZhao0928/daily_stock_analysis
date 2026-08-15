# -*- coding: utf-8 -*-
"""Shadow Research orchestration and approval state machine."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.shadow_research.backtest import ShadowBacktestRunner
from src.shadow_research.dsl import CompiledShadowRule, ShadowRuleError, compile_shadow_rule
from src.shadow_research.ledger import freeze_account_ledger
from src.shadow_research.repository import ShadowResearchRepository
from src.shadow_research.snapshot import build_feature_snapshot


class ShadowStateError(ValueError):
    """Invalid Shadow profile state transition or approval condition."""


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


class ShadowResearchService:
    """Create, backtest, approve and scan a rule profile deterministically."""

    def __init__(self, repository: Optional[ShadowResearchRepository] = None, runner: Optional[ShadowBacktestRunner] = None) -> None:
        self.repository = repository or ShadowResearchRepository()
        self.runner = runner or ShadowBacktestRunner()

    def create_profile(
        self,
        *,
        name: str,
        rule: Mapping[str, Any],
        description: Optional[str] = None,
        version: str = "1",
    ) -> Dict[str, Any]:
        if not str(name).strip():
            raise ShadowStateError("profile name is required")
        compiled = compile_shadow_rule(rule, version=version)
        profile_id = "shadow_" + _hash({"name": str(name).strip().lower()})[:24]
        existing = self.repository.get_profile(profile_id)
        if existing:
            return {"profile": existing, "rule": self.repository.get_rule(profile_id, version=version)}
        profile = self.repository.create_profile(
            {
                "profile_id": profile_id,
                "name": str(name).strip(),
                "description": description,
                "rule_version": str(version),
                "status": "draft",
            }
        )
        rule_row = self.repository.save_rule(
            {
                "profile_id": profile_id,
                "version": str(version),
                "dsl": dict(rule),
                "rule_hash": compiled.rule_hash,
                "feature_names": list(compiled.features),
                "status": "draft",
            }
        )
        return {"profile": profile, "rule": rule_row}

    def get_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        profile = self.repository.get_profile(profile_id)
        if profile is None:
            return None
        return {"profile": profile, "rule": self.repository.get_rule(profile_id, version=profile["rule_version"]), "latest_run": self.repository.get_latest_run(profile_id)}

    def list_profiles(self, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
        profiles = []
        for item in self.repository.list_profiles(status=status):
            detail = self.get_profile(item["profile_id"])
            if detail is not None:
                profiles.append(detail)
        return profiles

    def freeze_account_ledger(
        self,
        *,
        account_id: int,
        start_date: date,
        end_date: date,
    ) -> Dict[str, Any]:
        """Read the canonical Account Ledger and return a deterministic snapshot."""

        return freeze_account_ledger(
            account_id=account_id,
            start_date=start_date,
            end_date=end_date,
            db=self.repository.db,
        )

    def freeze_snapshot(
        self,
        *,
        code: str,
        cutoff: date | datetime,
        features: Mapping[str, Any],
        source_refs: Sequence[Mapping[str, Any]] = (),
    ) -> Dict[str, Any]:
        return build_feature_snapshot(code=code, cutoff=cutoff, features=features, source_refs=source_refs)

    def run_backtest(
        self,
        profile_id: str,
        *,
        code: str,
        split_date: date,
        observations: Optional[Sequence[Mapping[str, Any]]] = None,
        fee_bps: float = 5.0,
        slippage_bps: float = 5.0,
        source_refs: Sequence[Mapping[str, Any]] = (),
        account_id: Optional[int] = None,
        ledger_start_date: Optional[date] = None,
        ledger_end_date: Optional[date] = None,
        market_data_start: Optional[date] = None,
        market_data_end: Optional[date] = None,
        market_data_manager: Any = None,
    ) -> Dict[str, Any]:
        profile = self.repository.get_profile(profile_id)
        rule_row = self.repository.get_rule(profile_id, version=profile["rule_version"] if profile else None)
        if profile is None or rule_row is None:
            raise ShadowStateError("shadow profile or rule not found")
        compiled = compile_shadow_rule(rule_row["dsl"], version=rule_row["version"])
        effective_refs = list(source_refs)
        if account_id is not None or ledger_start_date is not None or ledger_end_date is not None:
            if account_id is None or ledger_start_date is None or ledger_end_date is None:
                raise ShadowStateError("account_id, ledger_start_date and ledger_end_date are required together")
            ledger_snapshot = self.freeze_account_ledger(
                account_id=account_id,
                start_date=ledger_start_date,
                end_date=ledger_end_date,
            )
            effective_refs.append({
                "kind": "account_ledger",
                "account_id": account_id,
                "as_of": ledger_snapshot["end_date"],
                "snapshot_hash": ledger_snapshot["snapshot_hash"],
                "event_count": ledger_snapshot["event_count"],
            })
        if observations is None:
            if market_data_start is None or market_data_end is None:
                raise ShadowStateError("observations or market_data_start/market_data_end is required")
            if market_data_manager is None:
                from data_provider.runtime import get_market_data_manager

                market_data_manager = get_market_data_manager()
            result = self.runner.run_from_market_data(
                code=code,
                rule=compiled,
                start_date=market_data_start,
                end_date=market_data_end,
                split_date=split_date,
                market_data_manager=market_data_manager,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                source_refs=effective_refs,
            )
        else:
            result = self.runner.run(
                code=code,
                rule=compiled,
                observations=observations,
                split_date=split_date,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                source_refs=effective_refs,
            )
        run_id = "shadow_run_" + _hash({"profile": profile_id, "rule": rule_row["rule_id"], "code": str(code).upper(), "snapshot": result["source_snapshot_hash"]})[:28]
        run = self.repository.save_run(
            {
                "run_id": run_id,
                "profile_id": profile_id,
                "rule_id": rule_row["rule_id"],
                "code": str(code).upper(),
                "split_date": split_date,
                "source_snapshot_hash": result["source_snapshot_hash"],
                "status": result["status"],
                "metrics": result,
                "degraded_reason": None if result["status"] == "completed" else "sample_out_or_executable_return_insufficient",
            }
        )
        if result["status"] == "degraded":
            self.repository.update_profile_status(profile_id, status="degraded", degraded_reason="backtest sample is insufficient or not executable")
        return {"run": run, "metrics": result}

    def approve_profile(self, profile_id: str, *, approved_by: str = "local_user") -> Dict[str, Any]:
        profile = self.repository.get_profile(profile_id)
        latest = self.repository.get_latest_run(profile_id)
        if profile is None:
            raise ShadowStateError("shadow profile not found")
        if profile["status"] in {"disabled", "frozen"}:
            raise ShadowStateError(f"profile cannot be approved from state {profile['status']}")
        if latest is None or latest["status"] != "completed":
            raise ShadowStateError("a completed out-of-sample backtest is required before approval")
        approved = self.repository.update_profile_status(profile_id, status="approved", approved_by=approved_by, degraded_reason=None)
        return self.get_profile(profile_id) or {"profile": approved}

    def disable_profile(self, profile_id: str, *, reason: str = "disabled by user") -> Dict[str, Any]:
        profile = self.repository.get_profile(profile_id)
        if profile is None:
            raise ShadowStateError("shadow profile not found")
        updated = self.repository.update_profile_status(profile_id, status="disabled", degraded_reason=reason)
        return self.get_profile(profile_id) or {"profile": updated}

    def scan_signals(
        self,
        profile_id: str,
        *,
        code: str,
        observations: Sequence[Mapping[str, Any]],
        run_id: Optional[str] = None,
        evidence_refs: Sequence[Mapping[str, Any]] = (),
    ) -> List[Dict[str, Any]]:
        profile = self.repository.get_profile(profile_id)
        if profile is None:
            raise ShadowStateError("shadow profile not found")
        if profile["status"] != "approved":
            raise ShadowStateError("only approved, non-degraded profiles can scan signals")
        rule_row = self.repository.get_rule(profile_id, version=profile["rule_version"])
        run = self.repository.get_run(run_id) if run_id else self.repository.get_latest_run(profile_id)
        if rule_row is None or run is None or run["status"] != "completed":
            raise ShadowStateError("a completed backtest run is required for signal scan")
        compiled = compile_shadow_rule(rule_row["dsl"], version=rule_row["version"])
        output = []
        for observation in observations:
            if not isinstance(observation, Mapping) or not compiled.evaluate(observation.get("features") or {}):
                continue
            current_date = observation.get("date")
            snapshot = build_feature_snapshot(
                code=code,
                cutoff=current_date,
                features=observation.get("features") or {},
                # The frozen feature snapshot is a property of the
                # observation itself.  Caller-supplied evidence refs are
                # audit metadata and must not make a replay produce a new
                # signal hash when omitted on a retry.
                source_refs=observation.get("source_refs") or (),
            )
            signal_id = "shadow_signal_" + _hash({"profile": profile_id, "rule": rule_row["rule_id"], "run": run["run_id"], "code": str(code).upper(), "date": str(current_date), "snapshot": snapshot["snapshot_hash"]})[:28]
            signal = self.repository.save_signal(
                {
                    "signal_id": signal_id,
                    "profile_id": profile_id,
                    "rule_id": rule_row["rule_id"],
                    "run_id": run["run_id"],
                    "code": str(code).upper(),
                    "signal_date": current_date,
                    "status": "eligible",
                    "feature_snapshot_hash": snapshot["snapshot_hash"],
                    "data_cutoff": current_date,
                    "evidence_refs": list(observation.get("evidence_refs") or evidence_refs),
                    "reason": "rule_matched",
                }
            )
            output.append(signal)
        return output


__all__ = ["ShadowResearchService", "ShadowStateError"]
