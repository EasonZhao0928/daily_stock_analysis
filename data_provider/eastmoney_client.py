# -*- coding: utf-8 -*-
"""Governed EastMoney HTTP endpoints owned by the Market Data layer."""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .supplier_runtime import get_supplier_runtime_registry


DATACENTER_SELECTION_URL = "https://data.eastmoney.com/dataapi/xuangu/list"
DATACENTER_REFERER = "https://data.eastmoney.com/xuangu/"
BOARD_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"


def build_eastmoney_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class EastMoneyClient:
    """HTTP client sharing the process-wide supplier session/rate/circuit."""

    def __init__(self, *, runtime: Any = None, session: Any = None) -> None:
        self.runtime = runtime or get_supplier_runtime_registry()
        self.session = session or self.runtime.get_session("eastmoney", factory=build_eastmoney_session)

    def get(self, url: str, *, lease_timeout: float | None = None, raise_for_status: bool = False, **kwargs: Any) -> Any:
        with self.runtime.request("eastmoney", timeout=lease_timeout) as lease:
            response = self.session.get(url, **kwargs)
            lease.observe_response(response)
            if raise_for_status:
                response.raise_for_status()
            return response


__all__ = [
    "BOARD_LIST_URL",
    "DATACENTER_REFERER",
    "DATACENTER_SELECTION_URL",
    "EastMoneyClient",
    "build_eastmoney_session",
]
