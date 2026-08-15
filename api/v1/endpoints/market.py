# -*- coding: utf-8 -*-
"""Market Candle, annotations and shared SSE snapshot endpoints."""

from __future__ import annotations

import json
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from api.deps import get_database_manager
from api.v1.schemas.market import AnnotationResponse, CandleResponse, MarketSnapshotResponse
from src.services.market_annotations_service import MarketAnnotationsService
from src.services.market_chart_service import MarketChartError, MarketChartService
from src.services.market_stream_service import DEFAULT_MAX_STREAM_EVENTS, MarketSnapshotHub
from src.storage import DatabaseManager

router = APIRouter()
snapshot_hub = MarketSnapshotHub()


def get_market_chart_service() -> MarketChartService:
    return MarketChartService()


@router.get("/{symbol}/candles", response_model=CandleResponse)
def get_market_candles(
    symbol: str,
    period: str = Query("daily"),
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    limit: int = Query(240, ge=1, le=2000),
    service: MarketChartService = Depends(get_market_chart_service),
):
    try:
        return service.get_candles(symbol, period=period, start=start, end=end, limit=limit)
    except MarketChartError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_candle_query", "message": str(exc)})


@router.get("/{symbol}/snapshot", response_model=MarketSnapshotResponse)
def get_market_snapshot(
    symbol: str,
    service: MarketChartService = Depends(get_market_chart_service),
):
    try:
        return service.get_snapshot(symbol)
    except MarketChartError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_snapshot_query", "message": str(exc)})


@router.get("/{symbol}/annotations", response_model=AnnotationResponse)
def get_market_annotations(
    symbol: str,
    account_id: Optional[int] = Query(None, ge=1),
    profile_id: Optional[str] = Query(None),
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db_manager: DatabaseManager = Depends(get_database_manager),
):
    try:
        return MarketAnnotationsService(db_manager).collect(
            symbol,
            account_id=account_id,
            profile_id=profile_id,
            start=start,
            end=end,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_annotation_query", "message": str(exc)})


@router.get("/{symbol}/stream")
def stream_market_snapshots(
    symbol: str,
    period: str = Query("daily"),
    limit: int = Query(240, ge=1, le=2000),
    interval_seconds: float = Query(10.0, ge=1.0, le=60.0),
    max_events: Optional[int] = Query(None, ge=1, le=DEFAULT_MAX_STREAM_EVENTS),
):
    # Every SSE response pins one sync-generator threadpool worker until it
    # ends, so the stream is both length-bounded and count-bounded (R10.6).
    bounded_events = DEFAULT_MAX_STREAM_EVENTS if max_events is None else int(max_events)
    if not snapshot_hub.acquire_stream_slot():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "market_stream_capacity",
                "message": "Too many open market streams; retry shortly.",
            },
        )

    def iterator():
        try:
            yield from snapshot_hub.stream(
                symbol,
                period=period,
                limit=limit,
                interval_seconds=interval_seconds,
                max_events=bounded_events,
            )
        except Exception as exc:
            payload = json.dumps(
                {"error": "market_snapshot_failed", "message": str(exc)},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        finally:
            # Runs on client disconnect too, so slots are never leaked.
            snapshot_hub.release_stream_slot()

    return StreamingResponse(
        iterator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
