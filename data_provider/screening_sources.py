# -*- coding: utf-8 -*-
"""Supplier adapters used by screening snapshots and hotspot research."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

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

# CNINFO (巨潮资讯网) 公告直连端点。用于 ExtendedCapabilityAdapter 的 announcement
# 能力独立备胎（见 data_provider/extended_capabilities.py 的 CninfoAnnouncementSource），
# 与东财 stock_notice_report 走完全不同的域名/风控面。
# 端点选用 cninfo 作为公告能力主源与 a-stock-data SKILL.md 的端点索引一致（独立实现，
# 未拷贝其代码）；topSearch/hisAnnouncement 的具体请求参数经真实调用核实可用。
CNINFO_SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_ANNOUNCEMENT_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_BASE = "http://static.cninfo.com.cn/"
CNINFO_REFERER = "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"

# cninfo 用不同的 column/plate 参数区分交易所；只支持沪深主板/创业板/科创板，北交所
# 未验证，交给调用方 fallback 到其他 source。
_CNINFO_EXCHANGE_PARAMS: Dict[str, tuple] = {"SH": ("sse", "sh"), "SZ": ("szse", "sz")}

# 新浪个股资金流端点，用于 ExtendedCapabilityAdapter 的 capital_flow 能力独立备胎
# （见 extended_capabilities.py 的 SinaCapitalFlowSource），东财主源被限流时降级使用。
# 与已有的 SINA_MARKET_CENTER_URL/SINA_DAILY_KLINE_URL 同域名，共用 "sina" 供应商家族。
SINA_MONEY_FLOW_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_qsfx_zjlrqs"
)
SINA_MONEY_FLOW_REFERER = "https://finance.sina.com.cn/"

# 代码前缀由交易所决定；北交所必须用 "bj" 前缀，用 "sh"/"sz" 会静默返回空数组
# （实测确认，不是文档臆测）。
_SINA_EXCHANGE_PREFIXES: Dict[str, str] = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

# 深交所"竞价交易公开信息"（龙虎榜）官方端点，用于 ExtendedCapabilityAdapter 的
# dragon_tiger 能力独立备胎（见 SzseDragonTigerSource）。只覆盖深市，上交所的对应
# 端点（query.sse.com.cn/infodisplay/showTradePublicFile.do）返回的是整份文本公告
# （固定宽度表格），不是结构化 JSON，解析成本和脆弱度明显更高，本轮不做，交易所字段
# 不是 SZ 时直接 raise，交给 akshare 兜底（akshare 本身覆盖沪深两市）。
SZSE_DRAGON_TIGER_URL = "https://www.szse.cn/api/report/ShowReport/data"
SZSE_DRAGON_TIGER_REFERER = "https://www.szse.cn/disclosure/supervision/dealinfo/index.html"


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


def cninfo_resolve_org_id(code: str, *, timeout: float) -> Optional[str]:
    """把股票代码解析为 cninfo 内部 orgId（公告查询接口需要）。"""
    runtime = get_supplier_runtime_registry()
    session = runtime.get_session("cninfo")
    with runtime.request("cninfo", timeout=timeout) as lease:
        response = session.post(
            CNINFO_SEARCH_URL,
            data={"keyWord": code, "maxNum": "5"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        )
        lease.observe_response(response)
    response.raise_for_status()
    for item in response.json() or []:
        if str(item.get("code")) == code:
            return item.get("orgId")
    return None


def fetch_cninfo_announcements(
    code: str, org_id: str, exchange: str, *, page_size: int = 20, timeout: float
) -> List[Dict[str, Any]]:
    """巨潮公告直连查询，返回原始 announcements 记录列表。

    字段名（``announcementTitle``/``announcementTime``/``adjunctUrl`` 等）与
    ``data_provider/evidence_adapters.py`` 的字段识别列表天然兼容，调用方无需
    额外映射；``adjunctUrl`` 会在这里补全为可直接访问的绝对 URL。
    """
    params = _CNINFO_EXCHANGE_PARAMS.get(exchange.upper())
    if params is None:
        raise ValueError(f"unsupported exchange for cninfo announcements: {exchange!r}")
    column, plate = params

    runtime = get_supplier_runtime_registry()
    session = runtime.get_session("cninfo")
    with runtime.request("cninfo", timeout=timeout) as lease:
        response = session.post(
            CNINFO_ANNOUNCEMENT_QUERY_URL,
            data={
                "stock": f"{code},{org_id}",
                "tabName": "fulltext",
                "pageSize": str(page_size),
                "pageNum": "1",
                "column": column,
                "category": "",
                "plate": plate,
                "seDate": "",
                "searchkey": "",
                "secid": "",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": CNINFO_REFERER},
            timeout=timeout,
        )
        lease.observe_response(response)
    response.raise_for_status()
    payload = response.json() or {}
    records: List[Dict[str, Any]] = []
    for item in payload.get("announcements") or []:
        record = dict(item)
        adjunct = record.get("adjunctUrl")
        if adjunct:
            record["adjunctUrl"] = CNINFO_STATIC_BASE + str(adjunct).lstrip("/")
        records.append(record)
    return records


def fetch_sina_capital_flow(
    code: str, exchange: str, *, days: int = 60, timeout: float
) -> List[Dict[str, Any]]:
    """新浪个股资金流查询，日度四档单净额。

    返回记录用 ``date``/``net_amount`` 等清晰字段名——
    ``data_provider/market_evidence.py`` 的归一化器不要求特定的非日期字段名，
    只按 ``_DATE_KEYS`` 识别日期列，其余按数值/原样处理，不需要额外映射表。
    """
    prefix = _SINA_EXCHANGE_PREFIXES.get(exchange.upper())
    if prefix is None:
        raise ValueError(f"unsupported exchange for sina capital flow: {exchange!r}")

    runtime = get_supplier_runtime_registry()
    session = runtime.get_session("sina")
    with runtime.request("sina", timeout=timeout) as lease:
        response = session.get(
            SINA_MONEY_FLOW_URL,
            params={
                "page": "1",
                "num": str(days),
                "sort": "opendate",
                "asc": "0",
                "daima": f"{prefix}{code}",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": SINA_MONEY_FLOW_REFERER},
            timeout=timeout,
        )
        lease.observe_response(response)
    response.raise_for_status()
    rows = response.json() or []
    records: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        records.append(
            {
                "date": row.get("opendate"),
                "close": row.get("trade"),
                "net_amount": row.get("netamount"),
                "turnover": row.get("turnover"),
                "change_ratio": row.get("changeratio"),
            }
        )
    return records


def fetch_szse_dragon_tiger(trade_date: date, *, timeout: float) -> List[Dict[str, Any]]:
    """深交所"竞价交易公开信息"（龙虎榜）官方查询，返回当日深市上榜股票列表。

    只覆盖深市；只取第一页（该报表每页固定 10 条，接口未暴露可用的翻页/放大页面
    大小参数，实测 ``pageNo``/``PAGESIZE`` 均不生效）——作为限流降级期间的补充证据，
    覆盖当日最靠前的上榜个股即可，不追求与主源逐条对齐。
    """
    day_text = trade_date.strftime("%Y-%m-%d")
    runtime = get_supplier_runtime_registry()
    session = runtime.get_session("szse")
    with runtime.request("szse", timeout=timeout) as lease:
        response = session.get(
            SZSE_DRAGON_TIGER_URL,
            params={
                "SHOWTYPE": "JSON",
                "CATALOGID": "1842_xxpl",
                "TABKEY": "tab1",
                "txtStart": day_text,
                "txtEnd": day_text,
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": SZSE_DRAGON_TIGER_REFERER},
            timeout=timeout,
        )
        lease.observe_response(response)
    response.raise_for_status()
    payload = response.json() or []
    rows = payload[0].get("data") if payload else None
    records: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        records.append(
            {
                "date": row.get("dqrq"),
                "code": row.get("zqdm"),
                "name": row.get("zqjc"),
                "amount_10k_cny": row.get("cjje"),
                "volume": row.get("cjsl"),
                "reason": row.get("plyy"),
            }
        )
    return records


__all__ = [
    "akshare_screening_call",
    "fetch_akshare_full_market_snapshot",
    "fetch_ths_constituents",
    "cninfo_resolve_org_id",
    "fetch_cninfo_announcements",
    "fetch_sina_capital_flow",
    "fetch_szse_dragon_tiger",
]
