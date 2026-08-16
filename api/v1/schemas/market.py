# -*- coding: utf-8 -*-
"""Market chart and annotation response schemas."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CandleItem(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    amount: Optional[float] = None
    change_percent: Optional[float] = None
    indicators: Dict[str, Optional[float]] = Field(default_factory=dict)


class CandleResponse(BaseModel):
    security_id: Dict[str, Any]
    period: str
    candles: List[CandleItem] = Field(default_factory=list)
    source: str
    source_status: str
    fallback_chain: List[str] = Field(default_factory=list)
    as_of: Optional[str] = None
    delay_seconds: Optional[float] = None
    stale: bool = False
    data_quality: str
    limitations: List[str] = Field(default_factory=list)


class MarketSnapshotResponse(BaseModel):
    security_id: Dict[str, Any]
    quote: Optional[CandleItem] = None
    source: str
    source_status: str
    as_of: Optional[str] = None
    delay_seconds: Optional[float] = None
    stale: bool = False
    data_quality: str
    limitations: List[str] = Field(default_factory=list)


class AnnotationItem(BaseModel):
    type: str
    source: str
    timestamp: Optional[str] = None
    account_id: Optional[int] = None
    symbol: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class AnnotationResponse(BaseModel):
    symbol: str
    start: Optional[str] = None
    end: Optional[str] = None
    items: List[AnnotationItem] = Field(default_factory=list)
    page: int = 1
    page_size: int = 100
    total: int = 0
    has_more: bool = False
    partial: bool = False
    limitations: List[str] = Field(default_factory=list)
