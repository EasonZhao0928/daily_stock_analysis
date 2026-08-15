# -*- coding: utf-8 -*-
"""Supplier adapters used by screening snapshots and hotspot research."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd
from .supplier_runtime import SupplierRuntimeError, get_supplier_runtime_registry


THS_CONCEPT_URL_TEMPLATE = "https://q.10jqka.com.cn/gn/detail/code/{code}/"
THS_REFERER = "https://q.10jqka.com.cn/gn/"

# Screening daily-history endpoints.  Supplier URLs live in data_provider so
# the static bypass gate can see every governed endpoint in one place; service
# modules import these instead of embedding their own literals.
TENCENT_DAILY_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_DAILY_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"
)
SINA_MARKET_CENTER_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
SINA_MARKET_CENTER_REFERER = "https://vip.stock.finance.sina.com.cn/mkt/"


def akshare_screening_call(function_name: str, *, timeout: float, **kwargs: Any) -> Any:
    """Call one allowlisted AkShare screening function behind a family gate."""
    import akshare as ak
    from . import akshare_fetcher

    function = getattr(ak, function_name, None)
    if not callable(function) or not function_name.startswith("stock_"):
        raise ValueError(f"unsupported AkShare screening function: {function_name}")
    family = "eastmoney" if function_name.endswith("_em") else "tonghuashun"
    try:
        with get_supplier_runtime_registry().request(family, timeout=timeout):
            return akshare_fetcher._akshare_call_with_timeout(
                function,
                timeout=timeout,
                call_name=f"screening.{function_name}",
                **kwargs,
            )
    except SupplierRuntimeError as exc:
        if "timed out" in str(exc):
            raise TimeoutError(str(exc)) from exc
        raise


def fetch_akshare_full_market_snapshot(*, timeout: float = 30.0) -> Any:
    return akshare_screening_call("stock_zh_a_spot_em", timeout=timeout)


def fetch_ths_constituents(code: str, *, timeout: Any) -> pd.DataFrame:
    runtime = get_supplier_runtime_registry()
    session = runtime.get_session("tonghuashun")
    # The session is owned by SupplierRuntime and shared with every THS
    # adapter instance.  Rate/concurrency/circuit accounting must surround the
    # actual request rather than only the parser.
    with runtime.request("tonghuashun", timeout=timeout) as lease:
        response = session.get(
            THS_CONCEPT_URL_TEMPLATE.format(code=code),
            headers={"User-Agent": "Mozilla/5.0", "Referer": THS_REFERER},
            timeout=timeout,
        )
        lease.observe_response(response)
    response.raise_for_status()
    html = response.content.decode("gbk", "ignore")
    rows = []
    seen = set()
    for match in re.finditer(r">(\d{6})<.*?>([^<>\n]{2,12})<", html, re.S):
        security_code = match.group(1)
        name = re.sub(r"\s+", "", match.group(2))
        if security_code in seen or not name or re.search(r"\d", name):
            continue
        seen.add(security_code)
        rows.append({"code": security_code, "name": name})
        if len(rows) >= 80:
            break
    return pd.DataFrame(rows)


__all__ = ["akshare_screening_call", "fetch_akshare_full_market_snapshot", "fetch_ths_constituents"]
