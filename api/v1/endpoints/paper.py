# -*- coding: utf-8 -*-
"""Paper Account and structured Proposal endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.deps import get_database_manager
from api.v1.schemas.paper import (
    PaperAccountCreateRequest,
    PaperAccountListResponse,
    PaperAccountResponse,
    PaperFillResponse,
    PaperOrderResponse,
    PaperOrderAdvanceRequest,
    PaperOrderCancelRequest,
    PaperPerformanceRequest,
    PaperMandateUpdateRequest,
    PaperProposalDecisionRequest,
    PaperProposalListResponse,
    PaperProposalResponse,
    PaperRiskResponse,
    PaperRunCycleRequest,
    PaperRunDetailResponse,
    PaperStateRequest,
)
from src.paper_account.repository import PaperAccountRepository
from src.paper_account.service import PaperAccountService, PaperStateError
from src.storage import DatabaseManager
from src.auth import COOKIE_NAME, is_auth_enabled, verify_session

router = APIRouter()


def get_market_chart_service():
    from src.services.market_chart_service import MarketChartService

    return MarketChartService()


def _service(db_manager: DatabaseManager) -> PaperAccountService:
    return PaperAccountService(PaperAccountRepository(db_manager))


def _state_error(exc: PaperStateError) -> HTTPException:
    return HTTPException(status_code=409, detail={"error": "paper_state_error", "message": str(exc)})


def _request_actor(request: Request) -> str:
    """Return the server-verified single-user identity for audit records."""
    if not is_auth_enabled():
        return "local_admin"
    session = request.cookies.get(COOKIE_NAME)
    if not session or not verify_session(session):
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "Login required"},
        )
    return "web_admin"


def _page(items, page: int, page_size: int):
    values = list(items)
    offset = (page - 1) * page_size
    selected = values[offset:offset + page_size]
    return {
        "items": selected,
        "page": page,
        "page_size": page_size,
        "total": len(values),
        "has_more": offset + len(selected) < len(values),
    }


@router.post("/accounts", response_model=PaperAccountResponse)
def create_paper_account(
    request: PaperAccountCreateRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    try:
        return _service(db_manager).create_account(
            name=request.name,
            market=request.market,
            base_currency=request.base_currency,
            controller_kind=request.controller_kind,
            approval_mode=request.approval_mode,
            mandate=request.mandate,
            mandate_version=request.mandate_version,
            initial_cash=request.initial_cash,
        )
    except (PaperStateError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_paper_account", "message": str(exc)})


@router.get("/accounts", response_model=PaperAccountListResponse)
def list_paper_accounts(
    include_inactive: bool = Query(False),
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    return {"items": list(_service(db_manager).list_accounts(include_inactive=include_inactive))}


@router.get("/accounts/{account_id}", response_model=PaperAccountResponse)
def inspect_paper_account(account_id: int, db_manager: DatabaseManager = Depends(get_database_manager)):
    result = _service(db_manager).inspect(account_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"error": "paper_account_not_found", "message": "Paper account not found"})
    return result


@router.post("/accounts/{account_id}/pause")
def pause_paper_account(account_id: int, request: PaperStateRequest = PaperStateRequest(), db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return _service(db_manager).pause(account_id, expected_version=request.expected_version)
    except PaperStateError as exc:
        raise _state_error(exc)


@router.post("/accounts/{account_id}/resume")
def resume_paper_account(account_id: int, request: PaperStateRequest = PaperStateRequest(), db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return _service(db_manager).resume(account_id, expected_version=request.expected_version)
    except PaperStateError as exc:
        raise _state_error(exc)


@router.post("/accounts/{account_id}/freeze")
def freeze_paper_account(account_id: int, request: PaperStateRequest = PaperStateRequest(), db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return _service(db_manager).freeze(account_id, expected_version=request.expected_version)
    except PaperStateError as exc:
        raise _state_error(exc)


@router.post("/accounts/{account_id}/close")
def close_paper_account(account_id: int, request: PaperStateRequest = PaperStateRequest(), db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return _service(db_manager).close(account_id, expected_version=request.expected_version)
    except PaperStateError as exc:
        raise _state_error(exc)


@router.post("/accounts/{account_id}/mandate")
def update_paper_mandate(
    account_id: int,
    request: PaperMandateUpdateRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    service = _service(db_manager)
    current = service.inspect(account_id)
    if current is None:
        raise HTTPException(status_code=404, detail={"error": "paper_account_not_found", "message": "Paper account not found"})
    try:
        return service.update_mandate(
            account_id,
            mandate=request.mandate,
            mandate_version=request.mandate_version,
            expected_version=request.expected_version or int(current["config"]["config_version"]),
        )
    except PaperStateError as exc:
        raise _state_error(exc)


@router.post("/accounts/{account_id}/run")
def run_paper_decision_cycle(
    account_id: int,
    request: PaperRunCycleRequest,
    http_request: Request,
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    """Run one Observation -> Proposal -> Risk -> approval cycle.

    This is the only way to start a cycle from outside the module: inputs are
    read from Account Ledger, Market Data, Evidence and Shadow Signal at the
    decision cutoff, never supplied by the caller.
    """
    _request_actor(http_request)
    service = _service(db_manager)
    if service.inspect(account_id) is None:
        raise HTTPException(status_code=404, detail={"error": "paper_account_not_found", "message": "Paper account not found"})
    try:
        return service.run_cycle_from_sources(
            account_id,
            decision_at=request.decision_at,
            strategy_version=request.strategy_version,
            symbols=request.symbols,
            shadow_profile_id=request.shadow_profile_id,
            idempotency_key=request.idempotency_key,
        )
    except PaperStateError as exc:
        raise _state_error(exc)


@router.get("/accounts/{account_id}/runs", response_model=PaperProposalListResponse)
def list_paper_runs(
    account_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    service = _service(db_manager)
    if service.inspect(account_id) is None:
        raise HTTPException(status_code=404, detail={"error": "paper_account_not_found", "message": "Paper account not found"})
    return _page(service.list_runs(account_id, limit=500), page, page_size)


@router.get("/accounts/{account_id}/runs/{run_id}", response_model=PaperRunDetailResponse)
def get_paper_run(account_id: int, run_id: str, db_manager: DatabaseManager = Depends(get_database_manager)):
    result = _service(db_manager).get_run(account_id, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"error": "paper_run_not_found", "message": "Paper run not found"})
    return result


@router.get("/accounts/{account_id}/proposals", response_model=PaperProposalListResponse)
def list_paper_proposals(
    account_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    service = _service(db_manager)
    if service.inspect(account_id) is None:
        raise HTTPException(status_code=404, detail={"error": "paper_account_not_found", "message": "Paper account not found"})
    return _page(service.list_proposals(account_id, limit=500), page, page_size)


@router.get("/accounts/{account_id}/proposals/{proposal_id}", response_model=PaperProposalResponse)
def get_paper_proposal(account_id: int, proposal_id: str, db_manager: DatabaseManager = Depends(get_database_manager)):
    proposal = _service(db_manager).get_proposal(account_id, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail={"error": "paper_proposal_not_found", "message": "Paper proposal not found"})
    return {"proposal": proposal}


@router.get("/accounts/{account_id}/proposals/{proposal_id}/risk", response_model=PaperRiskResponse)
def get_paper_risk_decision(account_id: int, proposal_id: str, db_manager: DatabaseManager = Depends(get_database_manager)):
    risk = _service(db_manager).get_risk_decision(account_id, proposal_id)
    if risk is None:
        raise HTTPException(status_code=404, detail={"error": "paper_risk_not_found", "message": "Paper risk decision not found"})
    return {"risk_decision": risk}


@router.post("/accounts/{account_id}/proposals/{proposal_id}/approve", response_model=PaperProposalResponse)
def approve_paper_proposal(
    account_id: int,
    proposal_id: str,
    request_context: Request,
    request: PaperProposalDecisionRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    try:
        return {"proposal": _service(db_manager).approve_proposal(
            account_id,
            proposal_id,
            approved_by=_request_actor(request_context),
            expected_version=request.expected_version,
        )}
    except PaperStateError as exc:
        raise _state_error(exc)


@router.post("/accounts/{account_id}/proposals/{proposal_id}/reject", response_model=PaperProposalResponse)
def reject_paper_proposal(
    account_id: int,
    proposal_id: str,
    request_context: Request,
    request: PaperProposalDecisionRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    try:
        return {"proposal": _service(db_manager).reject_proposal(
            account_id,
            proposal_id,
            rejected_by=_request_actor(request_context),
            expected_version=request.expected_version,
        )}
    except PaperStateError as exc:
        raise _state_error(exc)


@router.get("/accounts/{account_id}/orders", response_model=PaperProposalListResponse)
def list_paper_orders(
    account_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    service = _service(db_manager)
    if service.inspect(account_id) is None:
        raise HTTPException(status_code=404, detail={"error": "paper_account_not_found", "message": "Paper account not found"})
    return _page(service.list_orders(account_id, limit=500), page, page_size)


# Cancelling is the only order transition a user drives directly.  Arbitrary
# status changes and fill projection are internal steps of the Paper cycle and
# are deliberately not reachable over HTTP (design 6 / ADR-0003).
@router.post("/accounts/{account_id}/orders/{order_id}/cancel", response_model=PaperOrderResponse)
def cancel_paper_order(
    account_id: int,
    order_id: str,
    request: PaperOrderCancelRequest = PaperOrderCancelRequest(),
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    service = _service(db_manager)
    order = service.get_order(order_id)
    if order is None or int(order["account_id"]) != int(account_id):
        raise HTTPException(status_code=404, detail={"error": "paper_order_not_found", "message": "Paper order not found"})
    try:
        return {"order": service.transition_order(order_id, "cancelled", expected_version=request.expected_version)}
    except PaperStateError as exc:
        raise _state_error(exc)


@router.post("/accounts/{account_id}/orders/{order_id}/advance", response_model=PaperOrderResponse)
def advance_paper_order(
    account_id: int,
    order_id: str,
    request: PaperOrderAdvanceRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
    market_chart_service=Depends(get_market_chart_service),
):
    service = _service(db_manager)
    order = service.get_order(order_id)
    if order is None or int(order["account_id"]) != int(account_id):
        raise HTTPException(status_code=404, detail={"error": "paper_order_not_found", "message": "Paper order not found"})
    try:
        return service.advance_order(
            order_id,
            as_of=request.as_of,
            market_chart_service=market_chart_service,
            project_ledger=True,
        )
    except PaperStateError as exc:
        raise _state_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_paper_advance", "message": str(exc)})


@router.get("/accounts/{account_id}/fills", response_model=PaperProposalListResponse)
def list_paper_fills(
    account_id: int,
    order_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    service = _service(db_manager)
    if service.inspect(account_id) is None:
        raise HTTPException(status_code=404, detail={"error": "paper_account_not_found", "message": "Paper account not found"})
    return _page(service.list_fills(account_id, order_id=order_id, limit=500), page, page_size)


@router.post("/performance/compare", response_model=dict)
def compare_paper_performance(
    request: PaperPerformanceRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    try:
        return _service(db_manager).compare_performance(
            request.account_ids,
            start_date=request.start_date,
            end_date=request.end_date,
            benchmark=request.benchmark,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_performance_window", "message": str(exc)})
