# -*- coding: utf-8 -*-
"""Shadow Research: deterministic rules, frozen observations and signals."""

from src.shadow_research.backtest import ShadowBacktestRunner
from src.shadow_research.dsl import CompiledShadowRule, compile_shadow_rule
from src.shadow_research.ledger import LedgerSnapshotError, freeze_account_ledger
from src.shadow_research.service import ShadowResearchService
from src.shadow_research.snapshot import build_feature_snapshot, build_market_observations

__all__ = [
    "CompiledShadowRule",
    "ShadowBacktestRunner",
    "ShadowResearchService",
    "build_feature_snapshot",
    "build_market_observations",
    "compile_shadow_rule",
    "LedgerSnapshotError",
    "freeze_account_ledger",
]
