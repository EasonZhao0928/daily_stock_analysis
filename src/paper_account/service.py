# -*- coding: utf-8 -*-
"""Paper Account state machine and structured proposal facade."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager

from .mandate import (
    APPROVAL_MODES,
    CONTROLLER_KINDS,
    MandateDecision,
    PaperMandate,
    PaperMandateError,
    evaluate_proposal,
    validate_proposal,
)
from .observation import build_paper_observation
from .repository import PaperAccountRepository
from .risk_context import build_risk_context


class PaperStateError(ValueError):
    """Invalid Paper Account state transition or stale version."""


STATES = frozenset({"active", "paused", "frozen", "closed"})
TRANSITIONS = {
    "active": frozenset({"paused", "frozen", "closed"}),
    "paused": frozenset({"active", "frozen", "closed"}),
    "frozen": frozenset({"closed"}),
    "closed": frozenset(),
}


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = datetime.combine(date.fromisoformat(text[:10]), time.min)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    return datetime.combine(value, time.min)


def auto_paper_mode_enabled(config: Any = None) -> bool:
    """Return whether ``auto_paper`` approval is explicitly enabled.

    ``auto_paper`` is the only approval mode that creates Virtual Orders with no
    human in the loop, so design requires it to be opt-in.  Any failure to read
    configuration is treated as "disabled": this gate fails closed.
    """
    if config is None:
        try:
            from src.config import get_config

            config = get_config()
        except Exception:
            return False
    return bool(getattr(config, "paper_auto_mode_enabled", False))


class PaperAccountService:
    """Deep runtime facade for a personal, local-only Paper Account."""

    def __init__(
        self,
        repository: Optional[PaperAccountRepository] = None,
        *,
        config: Any = None,
        controller_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.repository = repository or PaperAccountRepository()
        self.portfolio = PortfolioService(PortfolioRepository(self.repository.db))
        self._config = config
        self._controller_factory = controller_factory

    def _resolve_controller(self, controller_kind: str) -> Any:
        """Resolve the configured controller for API/scheduler cycles."""
        if self._controller_factory is not None:
            try:
                return self._controller_factory(controller_kind, self._config)
            except TypeError:
                # Keep a small one-argument seam for existing test doubles and
                # integrations while the production factory remains typed.
                return self._controller_factory(controller_kind)
        from .controllers import build_paper_controller

        return build_paper_controller(controller_kind, config=self._config)

    def _account_has_cash_history(self, account_id: int, *, as_of: date) -> bool:
        """Whether the Ledger holds any cash event for this account."""
        try:
            entries = self.portfolio.repo.list_cash_ledger(int(account_id), as_of)
        except Exception:
            # Unknown history must not be read as "empty", which would re-seed
            # the configured initial cash on top of a real balance.
            return True
        return bool(entries)

    def _require_auto_paper_enabled(self, approval_mode: str) -> None:
        """Reject ``auto_paper`` unless PAPER_AUTO_MODE_ENABLED is set."""
        if approval_mode != "auto_paper":
            return
        if not auto_paper_mode_enabled(self._config):
            raise PaperStateError(
                "approval_mode 'auto_paper' requires PAPER_AUTO_MODE_ENABLED=true"
            )

    def create_account(
        self,
        *,
        name: str,
        market: str = "cn",
        base_currency: str = "CNY",
        controller_kind: str = "llm",
        approval_mode: str = "human_confirm",
        mandate: Optional[Mapping[str, Any]] = None,
        mandate_version: str = "1",
        initial_cash: float = 0.0,
    ) -> Dict[str, Any]:
        if controller_kind not in CONTROLLER_KINDS:
            raise PaperStateError(f"unsupported controller_kind: {controller_kind}")
        if approval_mode not in APPROVAL_MODES:
            raise PaperStateError(f"unsupported approval_mode: {approval_mode}")
        self._require_auto_paper_enabled(approval_mode)
        if initial_cash < 0:
            raise PaperStateError("initial_cash must be non-negative")
        normalized_mandate = PaperMandate.from_mapping(mandate, version=mandate_version)
        account = self.portfolio.create_account(
            name=name,
            broker="paper",
            market=market,
            base_currency=base_currency,
            account_kind="paper",
            controller_kind=controller_kind,
            external_execution_enabled=False,
        )
        account_id = int(account["id"])
        config_id = "paper_cfg_" + _hash({"account_id": account_id, "mandate": normalized_mandate.to_dict()})[:28]
        config = self.repository.create_config({
            "config_id": config_id,
            "account_id": account_id,
            "controller_kind": controller_kind,
            "approval_mode": approval_mode,
            "mandate_version": mandate_version,
            "mandate": normalized_mandate.to_dict(),
            "initial_cash": float(initial_cash),
            "state": "active",
        })
        return {"account": self.repository.get_account(account_id), "config": config}

    def inspect(self, account_id: int) -> Optional[Dict[str, Any]]:
        account = self.repository.get_account(account_id)
        config = self.repository.get_config(account_id)
        if account is None or config is None:
            return None
        return {
            "account": account,
            "config": config,
            "proposals": self.repository.list_proposals(account_id),
        }

    def list_accounts(self, *, include_inactive: bool = False) -> Sequence[Dict[str, Any]]:
        return self.repository.list_accounts(include_inactive=include_inactive)

    def get_risk_decision(self, account_id: int, proposal_id: str) -> Optional[Dict[str, Any]]:
        proposal = self.get_proposal(account_id, proposal_id)
        if proposal is None:
            return None
        return self.repository.get_risk_decision_for_proposal(proposal_id)

    def update_mandate(
        self,
        account_id: int,
        *,
        mandate: Mapping[str, Any],
        mandate_version: str,
        expected_version: int,
    ) -> Dict[str, Any]:
        state = self.inspect(account_id)
        if state is None:
            raise PaperStateError("paper account not found")
        if state["config"]["state"] not in {"paused", "frozen"}:
            raise PaperStateError("pause or freeze the paper account before editing its mandate")
        try:
            normalized = PaperMandate.from_mapping(mandate, version=mandate_version)
            config = self.repository.update_mandate(
                account_id,
                mandate=normalized.to_dict(),
                mandate_version=mandate_version,
                expected_version=expected_version,
            )
        except (PaperMandateError, ValueError) as exc:
            raise PaperStateError(str(exc)) from exc
        return {**(self.inspect(account_id) or {}), "config": config}

    def transition(self, account_id: int, target: str, *, expected_version: Optional[int] = None) -> Dict[str, Any]:
        if target not in STATES:
            raise PaperStateError(f"unsupported paper state: {target}")
        account = self.repository.get_account(account_id)
        config = self.repository.get_config(account_id)
        if account is None or config is None or account.get("account_kind") != "paper":
            raise PaperStateError("paper account not found")
        current = str(config["state"])
        if target == current:
            return self.inspect(account_id) or {}
        if target not in TRANSITIONS.get(current, frozenset()):
            raise PaperStateError(f"invalid transition: {current} -> {target}")
        version = int(config["config_version"] if expected_version is None else expected_version)
        try:
            updated = self.repository.update_state(account_id, state=target, expected_version=version)
        except ValueError as exc:
            raise PaperStateError(str(exc)) from exc
        if target == "closed":
            self.repository.set_account_active(account_id, False)
        return self.inspect(account_id) or {"config": updated}

    def pause(self, account_id: int, *, expected_version: Optional[int] = None) -> Dict[str, Any]:
        return self.transition(account_id, "paused", expected_version=expected_version)

    def resume(self, account_id: int, *, expected_version: Optional[int] = None) -> Dict[str, Any]:
        return self.transition(account_id, "active", expected_version=expected_version)

    def freeze(self, account_id: int, *, expected_version: Optional[int] = None) -> Dict[str, Any]:
        return self.transition(account_id, "frozen", expected_version=expected_version)

    def close(self, account_id: int, *, expected_version: Optional[int] = None) -> Dict[str, Any]:
        return self.transition(account_id, "closed", expected_version=expected_version)

    def create_run(
        self,
        account_id: int,
        *,
        decision_at: date | datetime,
        strategy_version: str,
        idempotency_key: Optional[str] = None,
        trace: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        state = self.inspect(account_id)
        if state is None:
            raise PaperStateError("paper account not found")
        if state["config"]["state"] in {"frozen", "closed"}:
            raise PaperStateError("paper account is frozen or closed")
        decision_dt = _datetime(decision_at)
        idem = idempotency_key or _hash({"account_id": account_id, "decision_at": decision_dt.isoformat(), "strategy_version": strategy_version})
        run_id = "paper_run_" + _hash({"account_id": account_id, "decision_at": decision_dt.isoformat(), "strategy_version": strategy_version, "idempotency_key": idem})[:28]
        return self.repository.save_run({
            "run_id": run_id,
            "account_id": int(account_id),
            "decision_at": decision_dt,
            "strategy_version": str(strategy_version),
            "config_version": int(state["config"]["config_version"]),
            "status": "created",
            "idempotency_key": str(idem),
            "backend": (trace or {}).get("backend"),
            "model": (trace or {}).get("model"),
            "prompt_version": (trace or {}).get("prompt_version"),
            "skill_version": (trace or {}).get("skill_version"),
            "tool_trace": list((trace or {}).get("tool_trace") or []),
            "diagnostics": dict((trace or {}).get("diagnostics") or {}),
        })

    def get_observation(self, observation_id: str) -> Optional[Dict[str, Any]]:
        return self.repository.get_observation(observation_id)

    def list_runs(self, account_id: int, *, limit: int = 100) -> Sequence[Dict[str, Any]]:
        return self.repository.list_runs(account_id, limit=limit)

    def get_run(self, account_id: int, run_id: str) -> Optional[Dict[str, Any]]:
        run = self.repository.get_run(run_id)
        if run is None or int(run["account_id"]) != int(account_id):
            return None
        proposals = self.repository.list_run_proposals(run_id, limit=500)
        return {
            "run": run,
            "observation": self.repository.get_observation_for_run(run_id),
            "proposals": [
                {
                    "proposal": proposal,
                    "risk_decision": self.repository.get_risk_decision_for_proposal(proposal["proposal_id"]),
                    "order": self.repository.get_order_for_proposal(proposal["proposal_id"]),
                }
                for proposal in proposals
            ],
        }

    @staticmethod
    def _source_rows(value: Any) -> list[Dict[str, Any]]:
        """Normalize a provider payload without importing a provider SDK."""
        if value is None:
            return []
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                value = to_dict(orient="records")
            except TypeError:
                value = to_dict()
        if isinstance(value, Mapping):
            value = value.get("data", value.get("items", []))
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
            return []
        return [dict(item) for item in value if isinstance(item, Mapping)]

    @staticmethod
    def _source_bar_time(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value
        if isinstance(value, date):
            return datetime.combine(value, time.min)
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                return datetime.combine(date.fromisoformat(text[:10]), time.min)
            except ValueError:
                return None
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed

    def build_observation_inputs_from_sources(
        self,
        account_id: int,
        *,
        cutoff: date | datetime,
        symbols: Optional[Sequence[str]] = None,
        market_data_manager: Any = None,
        evidence_service: Any = None,
        shadow_repository: Any = None,
        shadow_profile_id: Optional[str] = None,
        source_policy: Any = None,
    ) -> Dict[str, Any]:
        """Build Paper inputs from owned runtimes, not caller-supplied facts.

        The returned mapping is an internal hand-off for ``run_cycle``.  A
        caller may select a symbol universe, but cannot provide account
        balances, OHLCV, Evidence or Shadow Signal values; those are read from
        the Ledger/Market Data/Evidence/Shadow seams and bounded by ``cutoff``.
        """
        state = self.inspect(account_id)
        if state is None:
            raise PaperStateError("paper account not found")
        cutoff_dt = _datetime(cutoff)
        cutoff_date = cutoff_dt.date()

        snapshot = self.portfolio.get_portfolio_snapshot(
            account_id=int(account_id),
            as_of=cutoff_date,
            include_realtime=False,
        )
        accounts = list(snapshot.get("accounts") or [])
        account_payload = dict(accounts[0]) if accounts else {}
        cash = float(snapshot.get("total_cash") or 0.0)
        # Paper initial cash seeds an account that has no Ledger cash history
        # yet.  Keying that off a zero balance would also fire for an account
        # that legitimately spent its cash down to zero, silently handing the
        # mandate fabricated buying power -- so ask the Ledger whether any cash
        # event exists instead of inferring it from the balance.
        if not self._account_has_cash_history(account_id, as_of=cutoff_date):
            configured_cash = float(state["config"].get("initial_cash") or 0.0)
            if configured_cash > 0:
                cash = configured_cash
        market_value = float(snapshot.get("total_market_value") or 0.0)
        equity = float(snapshot.get("total_equity") or 0.0)
        if abs(equity) < 1e-12:
            equity = cash + market_value
        account_payload.update({
            "cash": cash,
            "available_cash": cash,
            "total_equity": equity,
            "market_value": market_value,
            "as_of": snapshot.get("as_of") or cutoff_date.isoformat(),
            "data_quality": snapshot.get("data_quality", "ok"),
            "limitations": list(snapshot.get("limitations") or []),
        })

        configured_symbols = list(symbols or ())
        if not configured_symbols:
            configured_symbols = list(state["config"].get("mandate", {}).get("allowed_symbols") or ())
        if not configured_symbols:
            configured_symbols = [str(item.get("symbol")) for item in account_payload.get("positions", []) if item.get("symbol")]
        configured_symbols = sorted({str(item).strip().upper() for item in configured_symbols if str(item).strip()})
        if not configured_symbols:
            raise PaperStateError("source-backed observation requires at least one symbol")

        if market_data_manager is None:
            from data_provider.runtime import get_market_data_manager

            market_data_manager = get_market_data_manager()
        from data_provider.market_data_types import DataQuery, DataStatus, SourcePolicy

        policy = source_policy or SourcePolicy()
        market_payload: Dict[str, Any] = {}
        source_refs: list[Dict[str, Any]] = []
        for symbol in configured_symbols:
            try:
                query = DataQuery("daily_data", symbol, as_of=cutoff_date)
                try:
                    envelope = market_data_manager.fetch(query, policy)
                except TypeError:
                    # Small legacy test doubles commonly expose ``fetch(query)``.
                    envelope = market_data_manager.fetch(query)
            except Exception as exc:
                raise PaperStateError(f"market data source failed for {symbol}: {type(exc).__name__}") from exc
            if getattr(envelope, "status", None) not in {DataStatus.OK, DataStatus.VALID_EMPTY}:
                raise PaperStateError(f"market data unavailable for {symbol}: {getattr(envelope, 'status', 'unknown')}")
            rows = []
            for row in self._source_rows(getattr(envelope, "data", None)):
                timestamp = self._source_bar_time(row.get("timestamp") or row.get("datetime") or row.get("date"))
                if timestamp is not None and timestamp <= cutoff_dt:
                    row["timestamp"] = timestamp.isoformat()
                    rows.append((timestamp, row))
            if not rows:
                raise PaperStateError(f"market data has no cutoff-visible bar for {symbol}")
            latest_time, latest = sorted(rows, key=lambda item: item[0])[-1]
            provenance = getattr(envelope, "provenance", None)
            source = str(getattr(envelope, "source", "market_data"))
            source_tier = str(getattr(envelope, "source_tier", "primary"))
            latest.update({
                "as_of": latest.get("as_of") or latest_time.isoformat(),
                "source": source,
                "source_tier": source_tier,
                "is_stale": bool(getattr(envelope, "is_stale", False)),
                "quality_flags": list(getattr(envelope, "quality_flags", ()) or ()),
            })
            market_payload[symbol] = latest
            source_refs.append({
                "symbol": symbol,
                "source": source,
                "source_tier": source_tier,
                "as_of": latest_time.isoformat(),
                "provenance": provenance.to_dict() if hasattr(provenance, "to_dict") else dict(provenance or {}),
            })

        evidence: list[Dict[str, Any]] = []
        if evidence_service is None:
            from src.services.research_evidence_service import ResearchEvidenceService
            from src.repositories.research_evidence_repo import ResearchEvidenceRepository

            evidence_service = ResearchEvidenceService(ResearchEvidenceRepository(self.repository.db))
        try:
            for symbol in configured_symbols:
                evidence.extend(evidence_service.list(
                    code=symbol,
                    cutoff=cutoff_dt,
                    include_stale=False,
                    limit=100,
                ))
        except Exception as exc:
            raise PaperStateError(f"evidence source failed: {type(exc).__name__}") from exc
        dedup_evidence = {str(item.get("content_hash") or item.get("evidence_id") or _hash(item)): item for item in evidence if isinstance(item, Mapping)}

        shadow_signals: list[Dict[str, Any]] = []
        if shadow_profile_id:
            if shadow_repository is None:
                from src.shadow_research.repository import ShadowResearchRepository

                shadow_repository = ShadowResearchRepository(self.repository.db)
            try:
                for symbol in configured_symbols:
                    shadow_signals.extend(shadow_repository.list_signals(shadow_profile_id, code=symbol, limit=100))
            except Exception as exc:
                raise PaperStateError(f"shadow signal source failed: {type(exc).__name__}") from exc

        return {
            "account_snapshot": account_payload,
            "market_data": market_payload,
            "market_source_refs": source_refs,
            "evidence": list(dedup_evidence.values()),
            "shadow_signals": shadow_signals,
        }

    def run_cycle_from_sources(
        self,
        account_id: int,
        *,
        decision_at: date | datetime,
        strategy_version: str,
        symbols: Optional[Sequence[str]] = None,
        market_data_manager: Any = None,
        evidence_service: Any = None,
        shadow_repository: Any = None,
        shadow_profile_id: Optional[str] = None,
        source_policy: Any = None,
        controller: Any = None,
        quota_available: bool = True,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run one cycle using internally-owned Ledger and provider inputs."""
        inputs = self.build_observation_inputs_from_sources(
            account_id,
            cutoff=decision_at,
            symbols=symbols,
            market_data_manager=market_data_manager,
            evidence_service=evidence_service,
            shadow_repository=shadow_repository,
            shadow_profile_id=shadow_profile_id,
            source_policy=source_policy,
        )
        return self.run_cycle(
            account_id,
            decision_at=decision_at,
            strategy_version=strategy_version,
            account_snapshot=inputs["account_snapshot"],
            market_data=inputs["market_data"],
            evidence=inputs["evidence"],
            shadow_signals=inputs["shadow_signals"],
            market_source_refs=inputs["market_source_refs"],
            cutoff=decision_at,
            controller=controller,
            quota_available=quota_available,
            idempotency_key=idempotency_key,
        )

    def list_proposals(self, account_id: int, *, limit: int = 100) -> Sequence[Dict[str, Any]]:
        return self.repository.list_proposals(account_id, limit=limit)

    def get_proposal(self, account_id: int, proposal_id: str) -> Optional[Dict[str, Any]]:
        proposal = self.repository.get_proposal(proposal_id)
        if proposal is None or int(proposal["account_id"]) != int(account_id):
            return None
        return proposal

    # Virtual Order/Fill facade.  Imports stay lazy to avoid a service-module
    # cycle while keeping one public PaperAccountService entry point.
    def stage_order(self, account_id: int, proposal_id: str, **kwargs: Any) -> Dict[str, Any]:
        from .order_service import PaperOrderService

        return PaperOrderService(self).stage_order(account_id, proposal_id, **kwargs)

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        from .order_service import PaperOrderService

        return PaperOrderService(self).get_order(order_id)

    def list_orders(self, account_id: int, *, limit: int = 100) -> Sequence[Dict[str, Any]]:
        from .order_service import PaperOrderService

        return PaperOrderService(self).list_orders(account_id, limit=limit)

    def transition_order(self, order_id: str, target: str, *, expected_version: Optional[int] = None) -> Dict[str, Any]:
        from .order_service import PaperOrderService

        return PaperOrderService(self).transition_order(order_id, target, expected_version=expected_version)

    def cancel_orders_on_freeze(self, account_id: int) -> Sequence[Dict[str, Any]]:
        from .order_service import PaperOrderService

        return PaperOrderService(self).cancel_orders_on_freeze(account_id)

    def match_order(self, order_id: str, bars: Sequence[Mapping[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        from .order_service import PaperOrderService

        return PaperOrderService(self).match_order(order_id, bars, **kwargs)

    def advance_order(
        self,
        order_id: str,
        *,
        as_of: date | datetime,
        market_chart_service: Any = None,
        project_ledger: bool = True,
    ) -> Dict[str, Any]:
        """Advance a virtual order with server-fetched, normalized candles."""
        from src.services.market_chart_service import MarketChartService

        order = self.get_order(order_id)
        if order is None:
            raise PaperStateError("paper order not found")
        # A staged order is already backed by an approved Proposal (stage_order
        # enforces that), so the cycle promotes it here.  Exposing an arbitrary
        # status transition to callers instead would let them drive the state
        # machine around the approval and matching rules.
        if order["status"] == "staged":
            order = self.transition_order(order_id, "approved")
        cutoff = _datetime(order["observation_cutoff"])
        as_of_dt = _datetime(as_of)
        if as_of_dt <= cutoff:
            raise PaperStateError("matching as_of must be after the observation cutoff")
        market = market_chart_service or MarketChartService()
        result = market.get_candles(
            order["symbol"],
            period="daily",
            start=cutoff.date(),
            end=as_of_dt.date(),
            limit=500,
        )
        if result.get("stale") or result.get("data_quality") not in {"ok", None}:
            raise PaperStateError("trusted market data is stale or unavailable")
        bars = [
            item for item in list(result.get("candles") or [])
            if _datetime(item["timestamp"]) <= as_of_dt
        ]
        matched = self.match_order(order_id, bars, project_ledger=project_ledger)
        return {
            **matched,
            "market_data": {
                "source": result.get("source"),
                "as_of": result.get("as_of"),
                "stale": result.get("stale"),
            },
        }

    def get_fill(self, fill_id: str) -> Optional[Dict[str, Any]]:
        return self.repository.get_fill(fill_id)

    def list_fills(self, account_id: int, *, order_id: Optional[str] = None, limit: int = 100) -> Sequence[Dict[str, Any]]:
        return self.repository.list_fills(account_id, order_id=order_id, limit=limit)

    def apply_fill(self, fill_id: str) -> Dict[str, Any]:
        from .order_service import PaperOrderService

        return PaperOrderService(self).apply_fill(fill_id)

    def compare_performance(self, account_ids: Sequence[int], *, start_date: date, end_date: date, benchmark: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        from .performance import PaperPerformanceService

        return PaperPerformanceService(self.portfolio).compare(
            account_ids,
            start_date=start_date,
            end_date=end_date,
            benchmark=dict(benchmark or {}) or None,
        )

    def freeze_observation(
        self,
        account_id: int,
        *,
        run_id: str,
        account_snapshot: Mapping[str, Any],
        market_data: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]] = (),
        shadow_signals: Sequence[Mapping[str, Any]] = (),
        market_source_refs: Sequence[Mapping[str, Any]] = (),
        cutoff: date | datetime,
    ) -> Dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run is None or int(run["account_id"]) != int(account_id):
            raise PaperStateError("decision run not found for paper account")
        observation = build_paper_observation(
            account_snapshot=account_snapshot,
            market_data=market_data,
            evidence=evidence,
            shadow_signals=shadow_signals,
            market_source_refs=market_source_refs,
            cutoff=cutoff,
        )
        # The content hash remains reusable, while the persisted observation
        # identity is scoped to one decision run (the table stores one run FK).
        # This permits two runs with identical market inputs without silently
        # attaching the second run to the first run's observation.
        observation_id = "paper_obs_" + _hash({
            "run_id": run_id,
            "observation_hash": observation["observation_hash"],
        })[:28]
        return self.repository.save_observation({
            "observation_id": observation_id,
            "run_id": run_id,
            "account_id": int(account_id),
            "cutoff_at": _datetime(cutoff),
            "payload_hash": observation["observation_hash"],
            "payload": {**observation, "observation_id": observation_id},
            "status": "frozen",
        })

    def run_cycle(
        self,
        account_id: int,
        *,
        decision_at: date | datetime,
        strategy_version: str,
        account_snapshot: Mapping[str, Any],
        market_data: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]] = (),
        shadow_signals: Sequence[Mapping[str, Any]] = (),
        market_source_refs: Sequence[Mapping[str, Any]] = (),
        cutoff: Optional[date | datetime] = None,
        controller: Any = None,
        context: Optional[Mapping[str, Any]] = None,
        quota_available: bool = True,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run one deterministic Observation -> Proposal -> Risk cycle.

        The method intentionally stops at a Proposal/Risk Decision.  Virtual
        orders and fills are a later stage and cannot be created by this API.
        """

        state = self.inspect(account_id)
        if state is None:
            raise PaperStateError("paper account not found")
        if state["config"]["state"] in {"frozen", "closed"}:
            raise PaperStateError("paper account is frozen or closed")

        if controller is None:
            try:
                controller = self._resolve_controller(state["config"]["controller_kind"])
            except Exception as exc:
                # The service must not silently downgrade an LLM account to a
                # Shadow proposal.  Preserve the stable state error contract
                # for API and scheduler callers while retaining the exception
                # class in logs/traceback for diagnostics.
                raise PaperStateError(
                    f"paper controller unavailable for {state['config']['controller_kind']}: "
                    f"{type(exc).__name__}"
                ) from exc

        trace = controller.trace() if hasattr(controller, "trace") else {}
        run = self.create_run(
            account_id,
            decision_at=decision_at,
            strategy_version=strategy_version,
            idempotency_key=idempotency_key,
            trace=trace,
        )

        # A retry of an already terminal run returns its immutable result and
        # never calls the model twice.
        if run["status"] in {"completed", "rejected", "skipped"}:
            observation = self.repository.get_observation_for_run(run["run_id"])
            proposals = self.repository.list_run_proposals(run["run_id"])
            replay_proposal = proposals[0] if proposals else None
            return {
                "run": run,
                "observation": observation,
                "proposal": replay_proposal,
                "risk_decision": (
                    self.repository.get_risk_decision_for_proposal(replay_proposal["proposal_id"])
                    if replay_proposal else None
                ),
                "staged_order": (
                    self.repository.get_order_for_proposal(replay_proposal["proposal_id"])
                    if replay_proposal else None
                ),
                "idempotent_replay": True,
            }

        if not quota_available:
            run = self.repository.update_run_status(
                run["run_id"], status="skipped", diagnostics={"reason": "quota_exhausted"}
            )
            return {
                "run": run,
                "observation": None,
                "proposal": None,
                "risk_decision": None,
                "idempotent_replay": False,
            }

        observation = self.freeze_observation(
            account_id,
            run_id=run["run_id"],
            account_snapshot=account_snapshot,
            market_data=market_data,
            evidence=evidence,
            shadow_signals=shadow_signals,
            market_source_refs=market_source_refs,
            cutoff=cutoff or decision_at,
        )
        try:
            candidate = controller.generate(observation["payload"])
        except Exception as exc:
            self.repository.update_run_status(
                run["run_id"],
                status="failed",
                diagnostics={"error": type(exc).__name__},
            )
            raise

        if candidate is None:
            run = self.repository.update_run_status(
                run["run_id"], status="skipped", diagnostics={"reason": "controller_no_proposal"}
            )
            return {
                "run": run,
                "observation": observation,
                "proposal": None,
                "risk_decision": None,
                "idempotent_replay": False,
            }

        result = self.submit_proposal(
            account_id,
            run_id=run["run_id"],
            observation_id=observation["observation_id"],
            proposal=candidate,
            context=context,
        )
        final_status = "completed" if result["risk_decision"]["decision"] == "accepted" else "rejected"
        run = self.repository.update_run_status(run["run_id"], status=final_status)
        staged_order = None
        if result["proposal"]["status"] == "approved":
            staged_order = self.stage_order(
                account_id,
                result["proposal"]["proposal_id"],
                observation_id=observation["observation_id"],
            )
        return {
            "run": run,
            "observation": observation,
            "proposal": result["proposal"],
            "risk_decision": result["risk_decision"],
            "staged_order": staged_order,
            "idempotent_replay": False,
        }

    def replay_cycle(
        self,
        account_id: int,
        run_id: str,
        *,
        context: Optional[Mapping[str, Any]] = None,
        bars: Optional[Sequence[Mapping[str, Any]]] = None,
        match: bool = False,
        matching_kwargs: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Replay persisted Observation/Proposal facts without calling a model."""

        state = self.inspect(account_id)
        run = self.repository.get_run(run_id)
        observation = self.repository.get_observation_for_run(run_id)
        proposals = self.repository.list_run_proposals(run_id)
        if state is None or run is None or int(run["account_id"]) != int(account_id):
            raise PaperStateError("decision run not found for paper account")
        if observation is None:
            raise PaperStateError("decision run has no frozen observation")
        mandate = PaperMandate.from_mapping(
            state["config"]["mandate"], version=state["config"]["mandate_version"]
        )
        replayed = []
        for proposal in proposals:
            normalized = validate_proposal({
                key: proposal[key]
                for key in (
                    "symbol", "market", "side", "order_type", "quantity",
                    "target_weight", "limit_price", "stop_price", "rationale", "evidence_refs",
                )
            })
            stored_risk = self.repository.get_risk_decision_for_proposal(proposal["proposal_id"])
            frozen_risk_context = (
                stored_risk.get("details", {}).get("risk_context")
                if stored_risk else None
            )
            replay_context = dict(context or {})
            if isinstance(frozen_risk_context, Mapping):
                replay_context.update(frozen_risk_context)
            decision = evaluate_proposal(mandate, normalized, replay_context)
            replayed.append({
                "proposal": proposal,
                "risk_decision": stored_risk,
                "recomputed": {
                    "decision": "accepted" if decision.accepted else "rejected",
                    "rule_codes": list(decision.rule_codes),
                    "details": dict(decision.details),
                },
                "consistent": bool(
                    stored_risk
                    and stored_risk["decision"] == ("accepted" if decision.accepted else "rejected")
                    and list(stored_risk["rule_codes"]) == list(decision.rule_codes)
                ),
            })
        matched = []
        if match and bars is not None:
            from .order_service import PaperOrderService

            order_service = PaperOrderService(self)
            for proposal in proposals:
                order = self.repository.get_order_for_proposal(proposal["proposal_id"])
                if order is not None:
                    matched.append(order_service.match_order(
                        order["order_id"], bars, **dict(matching_kwargs or {})
                    ))
        return {
            "run": run,
            "observation": observation,
            "items": replayed,
            "matched": matched,
            "model_called": False,
        }

    def submit_proposal(
        self,
        account_id: int,
        *,
        run_id: str,
        observation_id: str,
        proposal: Mapping[str, Any],
        context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        state = self.inspect(account_id)
        if state is None:
            raise PaperStateError("paper account not found")
        if state["config"]["state"] in {"frozen", "closed"}:
            raise PaperStateError("paper account is frozen or closed")
        run = self.repository.get_run(run_id)
        observation = self.repository.get_observation(observation_id)
        if run is None or observation is None or run["account_id"] != account_id or observation["run_id"] != run_id:
            raise PaperStateError("proposal must reference the same frozen decision run and observation")
        normalized = validate_proposal(proposal)
        mandate = PaperMandate.from_mapping(state["config"]["mandate"], version=state["config"]["mandate_version"])
        decision_at = _datetime(run["decision_at"])
        open_orders = sum(
            1 for order in self.repository.list_orders(account_id, limit=500)
            if order["status"] in {"staged", "approved", "open", "partially_filled"}
        )
        prior_runs = [
            item for item in self.repository.list_runs(account_id, limit=2)
            if item["run_id"] != run_id and item.get("decision_at") is not None
        ]
        # No prior run means the frequency gate is satisfied by definition.
        minutes_since_last = 1_000_000_000.0
        if prior_runs:
            minutes_since_last = max(
                0.0,
                (decision_at - _datetime(prior_runs[0]["decision_at"])).total_seconds() / 60.0,
            )
        derived_context = build_risk_context(
            observation=observation,
            proposal=normalized,
            decision_at=decision_at,
            open_orders=open_orders,
            minutes_since_last_decision=minutes_since_last,
        )
        # ``context`` is a compatibility seam for trusted in-process callers.
        # Observation-derived values always win; model tools cannot supply it.
        risk_context = dict(context or {})
        risk_context.update({key: value for key, value in derived_context.items() if value is not None})
        decision: MandateDecision = evaluate_proposal(mandate, normalized, risk_context)
        proposal_hash = _hash({"run_id": run_id, "observation_id": observation_id, "proposal": normalized})
        proposal_id = "paper_prop_" + proposal_hash[:28]
        if not decision.accepted:
            status = "rejected"
        elif state["config"]["approval_mode"] == "recommend_only":
            status = "recommended"
        elif state["config"]["approval_mode"] == "human_confirm":
            status = "pending_confirmation"
        elif auto_paper_mode_enabled(self._config):
            status = "approved"
        else:
            # An account row persisted while auto mode was enabled must not keep
            # auto-approving after the flag is turned off.  Degrade to the
            # default human gate rather than silently auto-creating orders.
            status = "pending_confirmation"
        saved_proposal = self.repository.save_proposal({
            "proposal_id": proposal_id,
            "run_id": run_id,
            "account_id": int(account_id),
            "proposal_version": 1,
            "symbol": normalized["symbol"],
            "market": normalized["market"],
            "side": normalized["side"],
            "order_type": normalized["order_type"],
            "quantity": normalized["quantity"],
            "target_weight": normalized["target_weight"],
            "limit_price": normalized["limit_price"],
            "stop_price": normalized["stop_price"],
            "rationale": normalized["rationale"],
            "evidence_refs": normalized["evidence_refs"],
            "proposal_hash": proposal_hash,
            "status": status,
        })
        risk_id = "paper_risk_" + _hash({"proposal_id": proposal_id, "decision": decision.accepted, "codes": decision.rule_codes})[:28]
        risk_details = dict(decision.details)
        risk_details["risk_context"] = risk_context
        risk = self.repository.save_risk_decision({
            "risk_decision_id": risk_id,
            "proposal_id": proposal_id,
            "run_id": run_id,
            "decision": "accepted" if decision.accepted else "rejected",
            "rule_codes": list(decision.rule_codes),
            "details": risk_details,
            "mandate_version": mandate.version,
        })
        return {"proposal": saved_proposal, "risk_decision": risk}

    def approve_proposal(
        self,
        account_id: int,
        proposal_id: str,
        *,
        approved_by: str = "local_user",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        proposal = self.repository.get_proposal(proposal_id)
        state = self.inspect(account_id)
        if proposal is None or state is None or proposal["account_id"] != int(account_id):
            raise PaperStateError("paper proposal not found")
        if state["config"]["state"] in {"frozen", "closed"}:
            raise PaperStateError("paper account is frozen or closed")
        if not str(approved_by or "").strip():
            raise PaperStateError("approver identity is required")
        if expected_version is not None and int(state["config"]["config_version"]) != int(expected_version):
            raise PaperStateError("paper account version conflict")
        if proposal["status"] != "pending_confirmation":
            raise PaperStateError("only pending_confirmation proposals can be approved")
        # Proposal rows are immutable facts; approval is an explicit audit
        # transition implemented as a narrow update.  A quantity-backed
        # approval immediately creates its deterministic Virtual Order so
        # callers cannot drive a second public "stage" state-machine step.
        approved = self._set_proposal_status(proposal_id, "approved", decided_by=approved_by)
        if proposal.get("quantity") is not None and proposal.get("side") in {"buy", "sell"}:
            self.stage_order(account_id, proposal_id)
        return approved

    def reject_proposal(
        self,
        account_id: int,
        proposal_id: str,
        *,
        rejected_by: str = "local_user",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        proposal = self.repository.get_proposal(proposal_id)
        state = self.inspect(account_id)
        if proposal is None or state is None or proposal["account_id"] != int(account_id):
            raise PaperStateError("paper proposal not found")
        if state["config"]["state"] in {"frozen", "closed"}:
            raise PaperStateError("paper account is frozen or closed")
        if not str(rejected_by or "").strip():
            raise PaperStateError("rejector identity is required")
        if expected_version is not None and int(state["config"]["config_version"]) != int(expected_version):
            raise PaperStateError("paper account version conflict")
        if proposal["status"] not in {"recommended", "pending_confirmation"}:
            raise PaperStateError("proposal is not awaiting a decision")
        return self._set_proposal_status(proposal_id, "rejected", decided_by=rejected_by)

    def cancel_proposal(
        self,
        account_id: int,
        proposal_id: str,
        *,
        cancelled_by: str = "local_user",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        proposal = self.repository.get_proposal(proposal_id)
        state = self.inspect(account_id)
        if proposal is None or state is None or int(proposal["account_id"]) != int(account_id):
            raise PaperStateError("paper proposal not found")
        if state["config"]["state"] in {"frozen", "closed"}:
            raise PaperStateError("paper account is frozen or closed")
        if expected_version is not None and int(state["config"]["config_version"]) != int(expected_version):
            raise PaperStateError("paper account version conflict")
        if proposal["status"] not in {"recommended", "pending_confirmation"}:
            raise PaperStateError("proposal is not cancellable")
        if not str(cancelled_by or "").strip():
            raise PaperStateError("canceller identity is required")
        return self._set_proposal_status(proposal_id, "cancelled", decided_by=cancelled_by)

    def _set_proposal_status(
        self,
        proposal_id: str,
        status: str,
        *,
        decided_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        def _write(session):
            from src.storage import PaperProposal

            row = session.get(PaperProposal, proposal_id)
            if row is None:
                raise PaperStateError("paper proposal not found")
            row.status = status
            row.decision_by = str(decided_by).strip() if decided_by else row.decision_by
            row.decision_at = datetime.now()
            row.updated_at = datetime.now()
            session.flush()
            return self.repository.proposal_dict(row)

        return self.repository.db._run_write_transaction(f"paper.proposal_status[{proposal_id}:{status}]", _write)

    # ------------------------------------------------------------------
    # CONTEXT.md vocabulary aliases
    #
    # design 6 names the public Paper cycle `review_proposal` / `advance_orders`
    # / `control` / `inspect` / `run_cycle`.  CONTEXT.md asks code, API, docs and
    # prompts to share one set of names, so those names are bound here to the
    # existing implementations rather than introducing a second vocabulary.
    # ------------------------------------------------------------------

    def review_proposal(
        self,
        account_id: int,
        proposal_id: str,
        *,
        decision: str,
        actor: str = "local_user",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Approve or reject one Proposal (design 6 `review_proposal`)."""
        normalized = str(decision).strip().lower()
        if normalized == "approve":
            return self.approve_proposal(
                account_id, proposal_id, approved_by=actor, expected_version=expected_version
            )
        if normalized == "reject":
            return self.reject_proposal(
                account_id, proposal_id, rejected_by=actor, expected_version=expected_version
            )
        raise PaperStateError(f"unsupported proposal decision: {decision}")

    def advance_orders(
        self,
        account_id: int,
        *,
        as_of: date | datetime,
        market_chart_service: Any = None,
        project_ledger: bool = True,
    ) -> Sequence[Dict[str, Any]]:
        """Advance every matchable order for one account (design 6 `advance_orders`)."""
        results = []
        for order in self.list_orders(account_id, limit=500):
            if order["status"] not in {"staged", "approved", "open", "partially_filled"}:
                continue
            results.append(
                self.advance_order(
                    order["order_id"],
                    as_of=as_of,
                    market_chart_service=market_chart_service,
                    project_ledger=project_ledger,
                )
            )
        return results

    def control(
        self,
        account_id: int,
        command: str,
        *,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Run one lifecycle command (design 6 `control`)."""
        normalized = str(command).strip().lower()
        if normalized not in TRANSITIONS and normalized not in STATES:
            raise PaperStateError(f"unsupported paper account command: {command}")
        return self.transition(account_id, normalized, expected_version=expected_version)


__all__ = ["PaperAccountService", "PaperStateError", "STATES", "auto_paper_mode_enabled"]
