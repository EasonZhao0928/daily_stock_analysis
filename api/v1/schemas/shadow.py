# -*- coding: utf-8 -*-
"""Shadow Research API schemas."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ShadowProfileCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    rule: Dict[str, Any]
    description: Optional[str] = Field(None, max_length=2000)
    version: str = Field("1", min_length=1, max_length=32)


class ShadowBacktestRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)
    observations: Optional[List[Dict[str, Any]]] = Field(None, min_length=1, max_length=5000)
    split_date: date
    market_data_start: Optional[date] = None
    market_data_end: Optional[date] = None
    fee_bps: float = Field(5.0, ge=0, le=1000)
    slippage_bps: float = Field(5.0, ge=0, le=1000)
    source_refs: List[Dict[str, Any]] = Field(default_factory=list, max_length=200)
    account_id: Optional[int] = Field(None, ge=1)
    ledger_start_date: Optional[date] = None
    ledger_end_date: Optional[date] = None


class ShadowApprovalRequest(BaseModel):
    approved_by: str = Field("local_user", min_length=1, max_length=120)


class ShadowScanRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)
    observations: List[Dict[str, Any]] = Field(..., min_length=1, max_length=5000)
    run_id: Optional[str] = None
    evidence_refs: List[Dict[str, Any]] = Field(default_factory=list, max_length=200)
