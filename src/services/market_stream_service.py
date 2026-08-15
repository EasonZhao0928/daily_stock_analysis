# -*- coding: utf-8 -*-
"""Small in-process shared snapshot poller for chart/SSE consumers."""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Generator, Optional, Tuple

from .market_chart_service import MarketChartService

# Concurrently open SSE responses. Each one holds a threadpool worker, so this
# stays well under a typical worker pool for a single-user deployment.
DEFAULT_MAX_CONCURRENT_STREAMS = 16
# Events after which a stream closes and the browser's EventSource reconnects.
# An endless response can never release its worker (R10.6).
DEFAULT_MAX_STREAM_EVENTS = 360


@dataclass
class _CacheItem:
    created_at: float
    snapshot: Dict[str, Any]


@dataclass
class _Channel:
    subscribers: Dict[int, "queue.Queue[Tuple[str, Dict[str, Any]]]" ]
    stop: threading.Event
    thread: Optional[threading.Thread] = None


class MarketSnapshotHub:
    """Share one normalized snapshot fetch across browser subscribers."""

    def __init__(
        self,
        chart_service: Optional[MarketChartService] = None,
        *,
        clock=time.monotonic,
        max_concurrent_streams: int = DEFAULT_MAX_CONCURRENT_STREAMS,
    ) -> None:
        self.chart_service = chart_service or MarketChartService()
        self.clock = clock
        self._cache: Dict[Tuple[Any, ...], _CacheItem] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._inflight: set[Tuple[Any, ...]] = set()
        self._channels: Dict[Tuple[Any, ...], _Channel] = {}
        self._next_subscriber_id = 0
        self._dropped_events = 0
        # Each SSE response occupies one sync-generator threadpool worker for
        # its whole lifetime, so an unbounded number of open charts would
        # starve every other sync route (R10.6).
        self._max_concurrent_streams = max(1, int(max_concurrent_streams))
        self._open_streams = 0

    def acquire_stream_slot(self) -> bool:
        """Reserve one concurrent SSE slot; ``False`` when the cap is reached."""
        with self._lock:
            if self._open_streams >= self._max_concurrent_streams:
                return False
            self._open_streams += 1
            return True

    def release_stream_slot(self) -> None:
        with self._lock:
            self._open_streams = max(0, self._open_streams - 1)

    @property
    def open_streams(self) -> int:
        with self._lock:
            return self._open_streams

    def snapshot(self, symbol: str, *, period: str = "daily", start=None, end=None, limit: int = 240, ttl_seconds: float = 10.0) -> Dict[str, Any]:
        key = (str(symbol), str(period), str(start), str(end), int(limit))
        now = float(self.clock())
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached.created_at < max(0.0, float(ttl_seconds)):
                return {**cached.snapshot, "cache": "hit"}
            while key in self._inflight:
                self._condition.wait()
                cached = self._cache.get(key)
                if cached is not None and float(self.clock()) - cached.created_at < max(0.0, float(ttl_seconds)):
                    return {**cached.snapshot, "cache": "hit"}
            self._inflight.add(key)
        try:
            value = self.chart_service.get_candles(symbol, period=period, start=start, end=end, limit=limit)
            with self._lock:
                self._cache[key] = _CacheItem(created_at=float(self.clock()), snapshot=value)
        finally:
            with self._lock:
                self._inflight.discard(key)
                self._condition.notify_all()
        return {**value, "cache": "miss"}

    def stream(
        self,
        symbol: str,
        *,
        period: str = "daily",
        start=None,
        end=None,
        limit: int = 240,
        interval_seconds: float = 10.0,
        max_events: Optional[int] = None,
        sleep_fn=time.sleep,
    ) -> Generator[str, None, None]:
        if sleep_fn is time.sleep:
            yield from self._shared_stream(
                symbol,
                period=period,
                start=start,
                end=end,
                limit=limit,
                interval_seconds=interval_seconds,
                max_events=max_events,
            )
            return
        # Deterministic direct mode for fake-clock tests. Production always
        # uses the shared channel above.
        interval = max(0.1, min(float(interval_seconds), 60.0))
        emitted = 0
        last_fingerprint = None
        while max_events is None or emitted < max_events:
            snapshot = self.snapshot(symbol, period=period, start=start, end=end, limit=limit, ttl_seconds=interval)
            fingerprint = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprint != last_fingerprint or emitted == 0:
                event = "stale" if snapshot.get("stale") else "snapshot"
                yield f"event: {event}\ndata: {json.dumps(snapshot, ensure_ascii=False, default=str)}\n\n"
                last_fingerprint = fingerprint
                emitted += 1
            elif max_events is not None:
                # Bounded streams must terminate even when the provider keeps
                # returning an identical cached snapshot (useful for smoke
                # checks and deterministic clients).  Unbounded browser
                # streams continue to wait for the next changed snapshot.
                yield f"event: heartbeat\ndata: {json.dumps({'cache': 'hit', 'symbol': symbol}, ensure_ascii=False)}\n\n"
                emitted += 1
            if max_events is not None and emitted >= max_events:
                break
            sleep_fn(interval)

    def _shared_stream(
        self,
        symbol: str,
        *,
        period: str,
        start: Any,
        end: Any,
        limit: int,
        interval_seconds: float,
        max_events: Optional[int],
    ) -> Generator[str, None, None]:
        interval = max(5.0, min(float(interval_seconds), 15.0))
        key = (str(symbol), str(period), str(start), str(end), int(limit), interval)
        subscriber: "queue.Queue[Tuple[str, Dict[str, Any]]]" = queue.Queue(maxsize=2)
        with self._lock:
            self._next_subscriber_id += 1
            subscriber_id = self._next_subscriber_id
            channel = self._channels.get(key)
            if channel is None:
                channel = _Channel(subscribers={}, stop=threading.Event())
                self._channels[key] = channel
            channel.subscribers[subscriber_id] = subscriber
            cached = self._cache.get((str(symbol), str(period), str(start), str(end), int(limit)))
            if cached is not None:
                event = "stale" if cached.snapshot.get("stale") else "snapshot"
                subscriber.put_nowait((event, {**cached.snapshot, "cache": "hit"}))
            if channel.thread is None or not channel.thread.is_alive():
                channel.thread = threading.Thread(
                    target=self._poll_channel,
                    args=(key, channel, symbol, period, start, end, limit, interval),
                    name=f"market-sse-{symbol}",
                    daemon=True,
                )
                channel.thread.start()
        emitted = 0
        try:
            while max_events is None or emitted < max_events:
                try:
                    event, snapshot = subscriber.get(timeout=interval * 2.0)
                except queue.Empty:
                    event, snapshot = "heartbeat", {"symbol": symbol, "health": self.health()}
                yield f"event: {event}\ndata: {json.dumps(snapshot, ensure_ascii=False, default=str)}\n\n"
                emitted += 1
        finally:
            with self._lock:
                active = self._channels.get(key)
                if active is not None:
                    active.subscribers.pop(subscriber_id, None)
                    if not active.subscribers:
                        active.stop.set()
                        self._channels.pop(key, None)

    def _poll_channel(
        self,
        key: Tuple[Any, ...],
        channel: _Channel,
        symbol: str,
        period: str,
        start: Any,
        end: Any,
        limit: int,
        interval: float,
    ) -> None:
        last_fingerprint = None
        while not channel.stop.is_set():
            try:
                snapshot = self.snapshot(
                    symbol, period=period, start=start, end=end, limit=limit, ttl_seconds=interval
                )
                fingerprint = json.dumps(
                    {key: value for key, value in snapshot.items() if key != "cache"},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                event = "stale" if snapshot.get("stale") else "snapshot"
                if fingerprint == last_fingerprint:
                    event = "heartbeat"
                    snapshot = {"symbol": symbol, "cache": snapshot.get("cache", "hit")}
                last_fingerprint = fingerprint
            except Exception as exc:
                event, snapshot = "error", {"error": "market_snapshot_failed", "message": str(exc)}
            with self._lock:
                subscribers = list(channel.subscribers.values())
            for subscriber in subscribers:
                try:
                    subscriber.put_nowait((event, dict(snapshot)))
                except queue.Full:
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        pass
                    with self._lock:
                        self._dropped_events += 1
                    subscriber.put_nowait((event, {**snapshot, "backpressure_dropped": True}))
            channel.stop.wait(interval)

    def health(self) -> Dict[str, int]:
        with self._lock:
            return {
                "channels": len(self._channels),
                "subscribers": sum(len(channel.subscribers) for channel in self._channels.values()),
                "inflight": len(self._inflight),
                "dropped_events": self._dropped_events,
            }


__all__ = ["MarketSnapshotHub"]
