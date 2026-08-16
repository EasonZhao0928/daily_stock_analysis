# -*- coding: utf-8 -*-
"""Time-split, no-future-function Shadow Research backtest runner."""

from __future__ import annotations

from datetime import date
from collections import Counter
from statistics import mean, pstdev
from typing import Any, Mapping, Optional, Sequence

from src.shadow_research.dsl import CompiledShadowRule
from src.shadow_research.snapshot import SnapshotError, build_feature_snapshot, build_market_observations


class ShadowBacktestError(ValueError):
    """Invalid historical observation or backtest configuration."""


def _date(value: Any) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        raise ShadowBacktestError(f"invalid observation date: {value!r}")


class ShadowBacktestRunner:
    """Evaluate a compiled rule against frozen, point-in-time observations."""

    def run_from_market_data(
        self,
        *,
        code: str,
        rule: CompiledShadowRule,
        start_date: date,
        end_date: date,
        split_date: date,
        market_data_manager: Any,
        fee_bps: float = 5.0,
        slippage_bps: float = 5.0,
        source_refs: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        observations, source_snapshot = build_market_observations(
            code=code,
            start_date=start_date,
            end_date=end_date,
            market_data_manager=market_data_manager,
        )
        result = self.run(
            code=code,
            rule=rule,
            observations=observations,
            split_date=split_date,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            source_refs=[*source_refs, source_snapshot],
        )
        result["market_data_snapshot"] = source_snapshot
        return result

    def run(
        self,
        *,
        code: str,
        rule: CompiledShadowRule,
        observations: Sequence[Mapping[str, Any]],
        split_date: date,
        fee_bps: float = 5.0,
        slippage_bps: float = 5.0,
        source_refs: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        if not observations:
            raise ShadowBacktestError("observations must not be empty")
        if fee_bps < 0 or slippage_bps < 0:
            raise ShadowBacktestError("fee_bps and slippage_bps must be non-negative")
        normalized = []
        seen_dates: set[date] = set()
        for observation in observations:
            if not isinstance(observation, Mapping):
                raise ShadowBacktestError("observations must contain objects")
            current_date = _date(observation.get("date"))
            if current_date in seen_dates:
                raise ShadowBacktestError(f"duplicate observation date: {current_date}")
            seen_dates.add(current_date)
            features = observation.get("features")
            if not isinstance(features, Mapping):
                raise ShadowBacktestError("observation.features must be an object")
            if any(str(key).startswith("future_") for key in features):
                raise ShadowBacktestError("future-derived feature is not allowed")
            feature_as_of = observation.get("features_as_of")
            if feature_as_of and _date(feature_as_of) > current_date:
                raise ShadowBacktestError("feature snapshot is newer than observation date")
            next_return = observation.get("next_return_pct")
            if next_return is not None:
                try:
                    next_return = float(next_return)
                except (TypeError, ValueError):
                    raise ShadowBacktestError("next_return_pct must be numeric")
            normalized.append({
                "date": current_date,
                "features": dict(features),
                "next_return_pct": next_return,
                "source_refs": observation.get("source_refs") or [],
                "halted": bool(observation.get("halted") or observation.get("suspended")),
                "price_limited": bool(
                    observation.get("limit_up")
                    or observation.get("limit_down")
                    or observation.get("price_limited")
                ),
            })
        normalized.sort(key=lambda item: item["date"])

        cutoff = max(item["date"] for item in normalized)
        observation_refs = [ref for item in normalized for ref in item["source_refs"]]
        snapshot = build_feature_snapshot(
            code=code,
            cutoff=cutoff,
            features={
                "observations": [
                    {
                        "date": item["date"].isoformat(),
                        "features": item["features"],
                        "halted": item["halted"],
                        "price_limited": item["price_limited"],
                    }
                    for item in normalized
                ],
            },
            source_refs=[*source_refs, *observation_refs],
        )
        fee_pct = (float(fee_bps) + float(slippage_bps)) / 100.0
        rows = []
        failure_modes: Counter[str] = Counter()
        for item in normalized:
            matched = rule.evaluate(item["features"])
            realized = None
            execution_status = "no_match"
            if matched:
                if item["halted"]:
                    execution_status = "halted"
                    failure_modes[execution_status] += 1
                elif item["price_limited"]:
                    execution_status = "price_limited"
                    failure_modes[execution_status] += 1
                elif item["next_return_pct"] is not None:
                    realized = float(item["next_return_pct"]) - fee_pct
                    execution_status = "executed"
                else:
                    execution_status = "missing_return"
                    failure_modes[execution_status] += 1
            rows.append({
                "date": item["date"].isoformat(),
                "matched": matched,
                "execution_status": execution_status,
                "return_pct": realized,
            })
        in_sample = [row for row in rows if _date(row["date"]) < split_date]
        out_sample = [row for row in rows if _date(row["date"]) >= split_date]
        returns = [float(row["return_pct"]) for row in out_sample if row["return_pct"] is not None]
        # Benchmark is the buy-and-hold next-bar return over the same visible
        # out-of-sample window; it is intentionally independent of rule matches.
        gross_returns = [
            float(item["next_return_pct"])
            for item in normalized
            if item["date"] >= split_date and item["next_return_pct"] is not None
        ]
        out_sample_matched = sum(1 for row in out_sample if row["matched"])
        out_sample_executed = sum(1 for row in out_sample if row["execution_status"] == "executed")
        wins = [value for value in returns if value > 0]
        cumulative = 1.0
        for value in returns:
            cumulative *= 1 + value / 100.0
        benchmark_cumulative = 1.0
        for value in gross_returns:
            benchmark_cumulative *= 1 + value / 100.0
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for value in returns:
            equity *= 1 + value / 100.0
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, (equity / peak - 1.0) * 100.0)
        annualized_volatility = pstdev(returns) * (252.0 ** 0.5) if len(returns) > 1 else 0.0 if returns else None
        status = "completed" if out_sample and returns else "degraded"
        return {
            "status": status,
            "eligibility": "tradeable" if status == "completed" else "degraded",
            "code": str(code).strip().upper(),
            "rule_hash": rule.rule_hash,
            "rule_version": rule.version,
            "split_date": split_date.isoformat(),
            "fee_bps": float(fee_bps),
            "slippage_bps": float(slippage_bps),
            "source_snapshot_hash": snapshot["snapshot_hash"],
            "observation_count": len(rows),
            "in_sample_count": len(in_sample),
            "out_sample_count": len(out_sample),
            "in_sample_signal_count": sum(1 for row in in_sample if row["matched"]),
            "out_sample_signal_count": out_sample_matched,
            "out_sample_executed_signal_count": out_sample_executed,
            "out_sample_coverage_pct": (out_sample_executed / out_sample_matched * 100.0) if out_sample_matched else None,
            "out_sample_return_count": len(returns),
            "out_sample_avg_return_pct": mean(returns) if returns else None,
            "out_sample_cumulative_return_pct": (cumulative - 1.0) * 100.0 if returns else None,
            "out_sample_win_rate_pct": (len(wins) / len(returns) * 100.0) if returns else None,
            "out_sample_volatility_pct": annualized_volatility,
            "out_sample_max_drawdown_pct": max_drawdown if returns else None,
            "benchmark_return_pct": (benchmark_cumulative - 1.0) * 100.0 if gross_returns else None,
            "relative_return_pct": ((cumulative - benchmark_cumulative) * 100.0) if returns and gross_returns else None,
            "attribution": {
                "signal_selection_pct": ((cumulative - benchmark_cumulative) * 100.0) if returns and gross_returns else None,
                "cost_drag_pct": (sum(gross_returns) - sum(returns)) if returns else None,
            },
            "failure_modes": dict(failure_modes),
            "observations": rows,
        }

    def run_many(self, *, jobs: Sequence[Mapping[str, Any]], split_date: date, **kwargs: Any) -> dict[str, Any]:
        """Run a deterministic cross-symbol evaluation using the same runner."""
        results = []
        for job in jobs:
            payload = dict(kwargs)
            payload.update(job)
            payload["split_date"] = split_date
            results.append(self.run(**payload))
        completed = [item for item in results if item.get("status") == "completed"]
        return {
            "status": "completed" if completed else "degraded",
            "results": results,
            "completed_count": len(completed),
            "average_out_sample_return_pct": mean(
                [item["out_sample_cumulative_return_pct"] for item in completed if item.get("out_sample_cumulative_return_pct") is not None]
            ) if completed else None,
        }


__all__ = ["ShadowBacktestError", "ShadowBacktestRunner"]
