# -*- coding: utf-8 -*-
"""Shadow Research API."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_database_manager
from api.v1.schemas.shadow import (
    ShadowApprovalRequest,
    ShadowBacktestRequest,
    ShadowProfileCreateRequest,
    ShadowScanRequest,
)
from src.shadow_research.repository import ShadowResearchRepository
from src.shadow_research.service import ShadowResearchService, ShadowStateError
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)
router = APIRouter()


def _service(db_manager: DatabaseManager) -> ShadowResearchService:
    return ShadowResearchService(ShadowResearchRepository(db_manager))


@router.post("/profiles")
def create_shadow_profile(request: ShadowProfileCreateRequest, db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return _service(db_manager).create_profile(name=request.name, rule=request.rule, description=request.description, version=request.version)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_shadow_profile", "message": str(exc)})


@router.get("/profiles")
def list_shadow_profiles(status: Optional[str] = Query(None), db_manager: DatabaseManager = Depends(get_database_manager)):
    return {"items": _service(db_manager).list_profiles(status=status)}


@router.get("/profiles/{profile_id}")
def get_shadow_profile(profile_id: str, db_manager: DatabaseManager = Depends(get_database_manager)):
    result = _service(db_manager).get_profile(profile_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"error": "shadow_profile_not_found", "message": "Shadow profile not found"})
    return result


@router.post("/profiles/{profile_id}/backtest")
def run_shadow_backtest(profile_id: str, request: ShadowBacktestRequest, db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return _service(db_manager).run_backtest(
            profile_id,
            code=request.code,
            observations=request.observations,
            split_date=request.split_date,
            fee_bps=request.fee_bps,
            slippage_bps=request.slippage_bps,
            source_refs=request.source_refs,
            account_id=request.account_id,
            ledger_start_date=request.ledger_start_date,
            ledger_end_date=request.ledger_end_date,
            market_data_start=request.market_data_start,
            market_data_end=request.market_data_end,
        )
    except ShadowStateError as exc:
        raise HTTPException(status_code=409, detail={"error": "shadow_state_error", "message": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_shadow_backtest", "message": str(exc)})


@router.post("/profiles/{profile_id}/approve")
def approve_shadow_profile(profile_id: str, request: ShadowApprovalRequest, db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return _service(db_manager).approve_profile(profile_id, approved_by=request.approved_by)
    except ShadowStateError as exc:
        raise HTTPException(status_code=409, detail={"error": "shadow_state_error", "message": str(exc)})


@router.post("/profiles/{profile_id}/scan")
def scan_shadow_signals(profile_id: str, request: ShadowScanRequest, db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return {"items": _service(db_manager).scan_signals(
            profile_id,
            code=request.code,
            observations=request.observations,
            market_data_start=request.market_data_start,
            market_data_end=request.market_data_end,
            run_id=request.run_id,
            evidence_refs=request.evidence_refs,
        )}
    except ShadowStateError as exc:
        raise HTTPException(status_code=409, detail={"error": "shadow_state_error", "message": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_shadow_scan", "message": str(exc)})


@router.get("/profiles/{profile_id}/signals")
def list_shadow_signals(profile_id: str, code: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=500), db_manager: DatabaseManager = Depends(get_database_manager)):
    if _service(db_manager).get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail={"error": "shadow_profile_not_found", "message": "Shadow profile not found"})
    repository = ShadowResearchRepository(db_manager)
    return {"items": repository.list_signals(profile_id, code=code, limit=limit)}
