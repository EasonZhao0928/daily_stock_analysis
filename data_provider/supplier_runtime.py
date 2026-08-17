# -*- coding: utf-8 -*-
"""Process-wide supplier-family session, rate-gate and circuit runtime.

Provider adapters are instantiated in more than one service (and some are
created lazily), so an adapter-local lock cannot protect the upstream domain.
This module keeps the coordination state outside individual fetchers while
remaining injectable for offline tests.
"""

from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Mapping, Optional


# Exceptions that mean "this adapter could not understand the payload", not
# "this supplier is down".  Counting them toward the circuit let a single bad
# symbol trip the whole supplier family for every caller in the process.
_NON_TRANSPORT_EXCEPTIONS = (
    ValueError,
    KeyError,
    IndexError,
    TypeError,
    AttributeError,
    NotImplementedError,
)


def is_transport_failure(exc: BaseException) -> bool:
    """Whether ``exc`` indicates a supplier/transport problem worth counting.

    A parse or lookup error says nothing about supplier health, so only
    transport-shaped failures (or anything carrying an HTTP status) advance the
    circuit breaker.
    """
    if getattr(exc, "status_code", None) is not None:
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) is not None:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, _NON_TRANSPORT_EXCEPTIONS):
        return False
    # requests/urllib3 raise their own hierarchies; match by module so this
    # module keeps no hard dependency on either package.
    module = type(exc).__module__ or ""
    return module.startswith(("requests", "urllib3", "http", "socket", "ssl", "aiohttp"))


class SupplierRuntimeError(RuntimeError):
    """Base class for supplier gate failures."""


class SupplierCircuitOpen(SupplierRuntimeError):
    """Raised when a supplier family is in its cooldown window."""

    def __init__(self, family: str, retry_after_seconds: float) -> None:
        super().__init__(f"supplier family {family!r} circuit is open")
        self.family = family
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))


@dataclass(frozen=True)
class SupplierPolicy:
    """Coordination policy shared by all callers of one supplier family."""

    max_concurrency: int = 4
    min_interval_seconds: float = 0.0
    jitter_seconds: float = 0.0
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if self.failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        if self.min_interval_seconds < 0 or self.jitter_seconds < 0 or self.cooldown_seconds < 0:
            raise ValueError("supplier timing values must not be negative")


@dataclass(frozen=True)
class SupplierHealth:
    family: str
    state: str
    in_flight: int
    consecutive_failures: int
    last_started_at: Optional[float]
    opened_at: Optional[float]


@dataclass
class SupplierLease:
    family: str
    acquired_at: float
    half_open_probe: bool = False
    _success: bool = True
    _status_code: Optional[int] = None

    def complete(self, *, success: bool = True, status_code: Optional[int] = None) -> None:
        """Override the default successful context result for a response."""
        self._success = bool(success)
        self._status_code = status_code

    def observe_response(self, response: Any) -> None:
        """Mark a response unsuccessful for HTTP 403/429/5xx statuses."""
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and (status_code == 403 or status_code == 429 or status_code >= 500):
            self.complete(success=False, status_code=status_code)


@dataclass
class _SupplierState:
    policy: SupplierPolicy
    condition: threading.Condition
    state: str = "closed"
    in_flight: int = 0
    consecutive_failures: int = 0
    last_started_at: Optional[float] = None
    next_allowed_at: float = 0.0
    opened_at: Optional[float] = None
    half_open_probe_in_flight: bool = False
    session: Any = None


DEFAULT_SUPPLIER_POLICIES: Mapping[str, SupplierPolicy] = {
    # EastMoney-backed adapters are the most burst-sensitive family.
    "eastmoney": SupplierPolicy(max_concurrency=2, min_interval_seconds=0.15, jitter_seconds=0.05),
    "tencent": SupplierPolicy(max_concurrency=3, min_interval_seconds=0.05, jitter_seconds=0.02),
    "tushare": SupplierPolicy(max_concurrency=2, min_interval_seconds=0.1, jitter_seconds=0.02),
    # 网关实测存在瞬时 5xx 抖动，冷却期放短一些以便尽快恢复主数据源。
    "promax": SupplierPolicy(
        max_concurrency=4, min_interval_seconds=0.05, jitter_seconds=0.02,
        failure_threshold=5, cooldown_seconds=30.0,
    ),
    "yahoo": SupplierPolicy(max_concurrency=3, min_interval_seconds=0.05, jitter_seconds=0.02),
    "tickflow": SupplierPolicy(max_concurrency=3, min_interval_seconds=0.05, jitter_seconds=0.02),
    "cninfo": SupplierPolicy(max_concurrency=2, min_interval_seconds=0.2, jitter_seconds=0.05),
    "szse": SupplierPolicy(max_concurrency=2, min_interval_seconds=0.2, jitter_seconds=0.05),
}


def supplier_family_for_name(name: str) -> str:
    """Map adapter names to an upstream supplier family."""
    normalized = str(name or "").strip().lower()
    if normalized in {"efinancefetcher", "aksharefetcher", "akshareem", "aksharesina", "akshareqq"}:
        return "eastmoney"
    if "tencent" in normalized or normalized in {"akshare_qq", "akshareqqfetcher"}:
        return "tencent"
    # Promax 是独立的第三方网关，与 Tushare 官方共享熔断状态会互相误伤。
    if "promax" in normalized:
        return "promax"
    if "tushare" in normalized:
        return "tushare"
    if "yfinance" in normalized or "alphavantage" in normalized or "finnhub" in normalized:
        return "yahoo"
    if "tickflow" in normalized:
        return "tickflow"
    if "longbridge" in normalized:
        return "longbridge"
    if "pytdx" in normalized:
        return "pytdx"
    if "baostock" in normalized:
        return "baostock"
    return normalized or "unknown"


class SupplierRuntimeRegistry:
    """Coordinate all calls and sessions belonging to supplier families."""

    def __init__(
        self,
        policies: Optional[Mapping[str, SupplierPolicy]] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self._clock = clock
        self._sleep = sleeper
        self._random = random_source
        self._policies = dict(DEFAULT_SUPPLIER_POLICIES)
        self._policies.update(dict(policies or {}))
        self._lock = threading.RLock()
        self._states: Dict[str, _SupplierState] = {}

    def configure(self, family: str, policy: SupplierPolicy) -> None:
        normalized = self._normalize_family(family)
        with self._lock:
            current = self._states.get(normalized)
            if current is not None and current.in_flight:
                raise SupplierRuntimeError(f"cannot reconfigure active supplier family {normalized!r}")
            self._policies[normalized] = policy
            self._states.pop(normalized, None)

    def policy(self, family: str) -> SupplierPolicy:
        normalized = self._normalize_family(family)
        with self._lock:
            return self._policies.get(normalized, SupplierPolicy())

    def acquire(self, family: str, *, timeout: Optional[float] = None) -> SupplierLease:
        normalized = self._normalize_family(family)
        started_wait = self._clock()
        with self._lock:
            state = self._state_locked(normalized)
            while True:
                now = self._clock()
                self._refresh_circuit_locked(state, now)
                if state.state == "open":
                    retry_after = (state.opened_at or now) + state.policy.cooldown_seconds - now
                    raise SupplierCircuitOpen(normalized, retry_after)
                if state.state == "half_open" and state.half_open_probe_in_flight:
                    wait_for = self._remaining_wait(started_wait, timeout, 0.05)
                    if wait_for <= 0:
                        raise SupplierRuntimeError(f"supplier family {normalized!r} half-open probe is busy")
                    state.condition.wait(wait_for)
                    continue
                if state.in_flight >= state.policy.max_concurrency:
                    wait_for = self._remaining_wait(started_wait, timeout, 0.05)
                    if wait_for <= 0:
                        raise SupplierRuntimeError(f"supplier family {normalized!r} concurrency gate timed out")
                    state.condition.wait(wait_for)
                    continue
                interval_wait = max(0.0, state.next_allowed_at - now)
                if interval_wait > 0:
                    wait_for = self._remaining_wait(started_wait, timeout, interval_wait)
                    if wait_for <= 0:
                        raise SupplierRuntimeError(f"supplier family {normalized!r} rate gate timed out")
                    state.condition.wait(wait_for)
                    continue
                state.in_flight += 1
                state.last_started_at = now
                jitter = state.policy.jitter_seconds * max(0.0, min(1.0, self._random()))
                state.next_allowed_at = now + state.policy.min_interval_seconds + jitter
                half_open_probe = state.state == "half_open"
                if half_open_probe:
                    state.half_open_probe_in_flight = True
                return SupplierLease(normalized, acquired_at=now, half_open_probe=half_open_probe)

    def release(
        self,
        lease: SupplierLease,
        *,
        success: Optional[bool] = None,
        status_code: Optional[int] = None,
    ) -> None:
        with self._lock:
            state = self._states.get(lease.family)
            if state is None:
                return
            state.in_flight = max(0, state.in_flight - 1)
            if lease.half_open_probe:
                state.half_open_probe_in_flight = False
            outcome_success = lease._success if success is None else bool(success)
            effective_status = status_code if status_code is not None else lease._status_code
            retryable = effective_status is None or effective_status == 403 or effective_status == 429 or effective_status >= 500
            if outcome_success or not retryable:
                state.state = "closed"
                state.consecutive_failures = 0
                state.opened_at = None
            else:
                state.consecutive_failures += 1
                if state.consecutive_failures >= state.policy.failure_threshold:
                    state.state = "open"
                    state.opened_at = self._clock()
            state.condition.notify_all()

    @contextmanager
    def request(self, family: str, *, timeout: Optional[float] = None) -> Iterator[SupplierLease]:
        lease = self.acquire(family, timeout=timeout)
        try:
            yield lease
        except Exception as exc:
            if not is_transport_failure(exc):
                # Release the slot without advancing the circuit: the supplier
                # answered, this adapter just could not use the payload.
                self.release(lease, success=True)
                raise
            status_code = getattr(exc, "status_code", None)
            response = getattr(exc, "response", None)
            if status_code is None and response is not None:
                status_code = getattr(response, "status_code", None)
            self.release(lease, success=False, status_code=status_code)
            raise
        else:
            self.release(lease)

    def get_session(self, family: str, *, factory: Optional[Callable[[], Any]] = None) -> Any:
        normalized = self._normalize_family(family)
        with self._lock:
            state = self._state_locked(normalized)
            if state.session is None:
                session_factory = factory or self._default_session_factory
                state.session = session_factory()
            return state.session

    def close(self) -> None:
        with self._lock:
            sessions = [state.session for state in self._states.values() if state.session is not None]
            for state in self._states.values():
                state.session = None
            for session in sessions:
                close = getattr(session, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    def health(self, family: str) -> SupplierHealth:
        normalized = self._normalize_family(family)
        with self._lock:
            state = self._state_locked(normalized)
            self._refresh_circuit_locked(state, self._clock())
            return SupplierHealth(
                family=normalized,
                state=state.state,
                in_flight=state.in_flight,
                consecutive_failures=state.consecutive_failures,
                last_started_at=state.last_started_at,
                opened_at=state.opened_at,
            )

    def reset(self) -> None:
        with self._lock:
            for state in self._states.values():
                state.state = "closed"
                state.in_flight = 0
                state.consecutive_failures = 0
                state.opened_at = None
                state.next_allowed_at = 0.0
                state.half_open_probe_in_flight = False
                state.condition.notify_all()

    def _state_locked(self, family: str) -> _SupplierState:
        state = self._states.get(family)
        if state is None:
            state = _SupplierState(
                policy=self._policies.get(family, SupplierPolicy()),
                condition=threading.Condition(self._lock),
            )
            self._states[family] = state
        return state

    def _refresh_circuit_locked(self, state: _SupplierState, now: float) -> None:
        if state.state == "open" and state.opened_at is not None:
            if now - state.opened_at >= state.policy.cooldown_seconds:
                state.state = "half_open"
                state.half_open_probe_in_flight = False

    @staticmethod
    def _normalize_family(family: str) -> str:
        normalized = str(family or "").strip().lower()
        if not normalized:
            raise ValueError("supplier family must not be empty")
        return normalized

    def _remaining_wait(self, started: float, timeout: Optional[float], desired: float) -> float:
        if timeout is None:
            return max(0.001, desired)
        remaining = timeout - (self._clock() - started)
        return max(0.0, min(desired, remaining))

    @staticmethod
    def _default_session_factory() -> Any:
        import requests

        return requests.Session()


_GLOBAL_SUPPLIER_RUNTIME = SupplierRuntimeRegistry()


def get_supplier_runtime_registry() -> SupplierRuntimeRegistry:
    return _GLOBAL_SUPPLIER_RUNTIME


def reset_supplier_runtime_registry() -> None:
    _GLOBAL_SUPPLIER_RUNTIME.reset()


__all__ = [
    "DEFAULT_SUPPLIER_POLICIES",
    "SupplierCircuitOpen",
    "SupplierHealth",
    "SupplierLease",
    "SupplierPolicy",
    "SupplierRuntimeError",
    "SupplierRuntimeRegistry",
    "get_supplier_runtime_registry",
    "reset_supplier_runtime_registry",
    "supplier_family_for_name",
]
