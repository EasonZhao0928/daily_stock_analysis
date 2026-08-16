# -*- coding: utf-8 -*-
"""Process-scoped Market Data runtime and injectable test seam.

All normal application callers obtain one ``DataFetcherManager`` from this
module.  A test or an isolated worker can construct ``MarketDataRuntime`` with
its own manager factory and supplier registry without mutating process-global
state.
"""

from __future__ import annotations

import threading
import sys
from typing import Any, Callable, Optional

from .supplier_runtime import SupplierRuntimeRegistry, get_supplier_runtime_registry


class MarketDataRuntime:
    """Own one manager and one supplier registry for a caller scope."""

    def __init__(
        self,
        *,
        manager_factory: Optional[Callable[..., Any]] = None,
        supplier_runtime: Optional[SupplierRuntimeRegistry] = None,
    ) -> None:
        self._manager_factory = manager_factory
        self._supplier_runtime = supplier_runtime or SupplierRuntimeRegistry()
        self._manager: Any = None
        self._lock = threading.RLock()

    @property
    def supplier_runtime(self) -> SupplierRuntimeRegistry:
        return self._supplier_runtime

    def manager(self) -> Any:
        with self._lock:
            factory, cacheable = self._resolve_factory()
            if not cacheable:
                # Preserve established patch seams such as
                # ``patch("data_provider.DataFetcherManager", ...)`` and
                # ``patch("data_provider.base.DataFetcherManager", ...)``.
                # Tests receive their injected manager while production still
                # uses one process-scoped instance.
                return self._instantiate(factory)
            if self._manager is None:
                self._manager = self._instantiate(factory)
            return self._manager

    def _instantiate(self, factory: Callable[..., Any]) -> Any:
        try:
            return factory(supplier_runtime=self._supplier_runtime)
        except TypeError as exc:
            # Small test doubles and legacy factories may not accept the new
            # keyword; keep the injectable seam backwards compatible without
            # weakening the production factory.
            try:
                return factory()
            except TypeError:
                raise exc

    def _resolve_factory(self) -> tuple[Callable[..., Any], bool]:
        if self._manager_factory is not None:
            return self._manager_factory, True
        from .base import DataFetcherManager as base_factory

        package = sys.modules.get("data_provider")
        package_factory = getattr(package, "DataFetcherManager", base_factory)
        original = _ORIGINAL_MANAGER_FACTORY or base_factory
        if base_factory is not original:
            return base_factory, False
        if package_factory is not original:
            return package_factory, False
        return base_factory, True

    def reset(self, *, close: bool = True, close_sessions: bool = False) -> None:
        """Drop the cached manager so a config reload takes effect.

        Supplier HTTP sessions are process-wide infrastructure that other
        components hold long-lived references to (screening, hotspot), so they
        are *not* closed here: doing so left those holders with a dead session
        after any settings save.  Circuit and rate state is still reset, which
        is what a config reload actually needs.  Pass ``close_sessions=True``
        only at process shutdown or for test isolation.
        """
        with self._lock:
            manager = self._manager
            self._manager = None
            if close and manager is not None:
                close_fn = getattr(manager, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
            self._supplier_runtime.reset()
            if close_sessions:
                self._supplier_runtime.close()


_GLOBAL_MARKET_DATA_RUNTIME = MarketDataRuntime(
    supplier_runtime=get_supplier_runtime_registry(),
)

try:
    from .base import DataFetcherManager as _ORIGINAL_MANAGER_FACTORY
except Exception:  # pragma: no cover - defensive import during interpreter teardown
    _ORIGINAL_MANAGER_FACTORY = None


def get_market_data_runtime() -> MarketDataRuntime:
    return _GLOBAL_MARKET_DATA_RUNTIME


def get_market_data_manager() -> Any:
    return _GLOBAL_MARKET_DATA_RUNTIME.manager()


def reset_market_data_runtime(*, close: bool = True, close_sessions: bool = False) -> None:
    _GLOBAL_MARKET_DATA_RUNTIME.reset(close=close, close_sessions=close_sessions)


__all__ = [
    "MarketDataRuntime",
    "get_market_data_manager",
    "get_market_data_runtime",
    "reset_market_data_runtime",
]
