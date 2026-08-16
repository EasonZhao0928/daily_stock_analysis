# -*- coding: utf-8 -*-
"""Paper Account and Proposal API schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PaperAccountCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    market: str = Field("cn", min_length=2, max_length=8)
    base_currency: str = Field("CNY", min_length=3, max_length=8)
    controller_kind: str = Field("llm", min_length=1, max_length=32)
    approval_mode: str = Field("human_confirm", min_length=1, max_length=24)
    mandate: Dict[str, Any] = Field(default_factory=dict)
    mandate_version: str = Field("1", min_length=1, max_length=32)
    initial_cash: float = Field(0.0, ge=0)


class PaperStateRequest(BaseModel):
    expected_version: Optional[int] = Field(None, ge=1)


class PaperMandateUpdateRequest(BaseModel):
    mandate: Dict[str, Any] = Field(default_factory=dict)
    mandate_version: str = Field("1", min_length=1, max_length=32)
    expected_version: Optional[int] = Field(None, ge=1)


class PaperProposalDecisionRequest(BaseModel):
    expected_version: Optional[int] = Field(None, ge=1)


class PaperPageResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    page: int = 1
    page_size: int = 100
    total: int = 0
    has_more: bool = False


# Backwards-compatible name used by older clients/tests.
PaperProposalListResponse = PaperPageResponse


class PaperAccountListResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)


class PaperAccountResponse(BaseModel):
    account: Dict[str, Any]
    config: Dict[str, Any]
    proposals: List[Dict[str, Any]] = Field(default_factory=list)


class PaperProposalResponse(BaseModel):
    proposal: Dict[str, Any]


class PaperRiskResponse(BaseModel):
    risk_decision: Dict[str, Any]


class PaperOrderResponse(BaseModel):
    order: Dict[str, Any]


class PaperFillResponse(BaseModel):
    fill: Dict[str, Any]


class PaperRunDetailResponse(BaseModel):
    run: Dict[str, Any]
    observation: Optional[Dict[str, Any]] = None
    proposals: List[Dict[str, Any]] = Field(default_factory=list)


class PaperOrderCancelRequest(BaseModel):
    """Cancel is the only user-driven order transition (design 6 / ADR-0003)."""

    expected_version: Optional[int] = Field(None, ge=1)


class PaperOrderAdvanceRequest(BaseModel):
    as_of: date | datetime


class PaperPerformanceRequest(BaseModel):
    account_ids: List[int] = Field(..., min_length=1, max_length=100)
    start_date: date
    end_date: date
    benchmark: Dict[str, Any] = Field(default_factory=dict)


class PaperRunCycleRequest(BaseModel):
    """Trigger one decision cycle.

    The caller may pick a symbol universe but cannot supply prices, balances,
    Evidence or Shadow Signals: those are read from the owning seams and bounded
    by the decision cutoff (design 6).
    """

    decision_at: date | datetime
    strategy_version: str = Field(..., min_length=1, max_length=64)
    symbols: Optional[List[str]] = Field(None, max_length=200)
    shadow_profile_id: Optional[str] = Field(None, max_length=64)
    idempotency_key: Optional[str] = Field(None, max_length=128)
