# -*- coding: utf-8 -*-
"""Research tools for the extended A-share Market Data capabilities.

Tasks 5.2-5.4 built financial-statement, consensus, disclosure and flow
capabilities inside ``data_provider``, but nothing exposed them to the Agent, so
skills such as ``financial_statement_quality`` and ``ownership_and_flow``
declared required tools they could never actually use.  design 3 forbids that:
a skill must map to real tools, and an unavailable capability must say so
instead of being faked by the prompt.

Every tool here goes through the Market Data capability route, so it inherits
SecurityId normalization, supplier-family rate gating, circuit breaking, stale
detection and the ``EXTENDED_MARKET_DATA_ENABLED`` gate.  None of them fetch a
supplier directly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolPolicy

# Read-only network research: same shape as the other market/data tools so a
# Paper cycle's frozen-context profile still excludes them.
_EXTENDED_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["network_read", "db_read"],
    permissions=["market_data:read"],
    # "stock" is the dimension name the registry pairs with a ``stock_code``
    # parameter; declaring "symbol" here would silently skip scope enforcement.
    scope_dimensions=["stock"],
    cancellation_safe=True,
)


def _fetch_capability(
    capability: str,
    stock_code: str,
    *,
    fields: tuple = (),
    as_of: Any = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Fetch one capability and return a bounded, provenance-carrying result."""
    from data_provider.market_data_types import DataQuery, DataStatus
    from data_provider.runtime import get_market_data_manager

    query = DataQuery(
        capability=capability,
        security_id=str(stock_code).strip(),
        fields=fields,
        as_of=as_of,
        limit=limit,
    )
    envelope = get_market_data_manager().fetch(query)
    status = getattr(envelope, "status", None)

    payload: Dict[str, Any] = {
        "capability": capability,
        "stock_code": str(stock_code).strip(),
        "status": getattr(status, "value", str(status)),
        "source": getattr(envelope, "source", None),
        "source_tier": getattr(envelope, "source_tier", None),
        "as_of": _text(getattr(envelope, "as_of", None)),
        "is_stale": bool(getattr(envelope, "is_stale", False)),
        "quality_flags": list(getattr(envelope, "quality_flags", ()) or ()),
        "fallback_chain": list(getattr(envelope, "fallback_chain", ()) or ()),
    }

    if status is DataStatus.OK:
        payload["data"] = _bounded(getattr(envelope, "data", None), limit)
        return payload

    payload["data"] = None
    if status is DataStatus.VALID_EMPTY:
        # A legal empty is a real answer, not a failure.
        payload["data"] = []
        payload["note"] = "No records for this capability and period."
        return payload
    if status is DataStatus.UPSTREAM_BLOCKED:
        payload["note"] = (
            "This capability is unavailable. Extended market data may be disabled "
            "(EXTENDED_MARKET_DATA_ENABLED) or the upstream source is blocked. "
            "Do not infer the underlying figures."
        )
        return payload
    payload["note"] = (
        "Capability unavailable; report it as missing rather than estimating the values."
    )
    return payload


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _bounded(value: Any, limit: Optional[int]) -> Any:
    """Keep tool results small; ToolSurface also enforces a hard size cap."""
    records = value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            records = value.to_dict(orient="records")
        except TypeError:
            records = value.to_dict()
    if isinstance(records, list):
        return records[: max(1, int(limit))] if limit else records[:100]
    return records


def _tool(
    name: str,
    capability: str,
    description: str,
    *,
    extra_parameters: Optional[List[ToolParameter]] = None,
    default_limit: int = 50,
    fields_from: Optional[str] = None,
) -> ToolDefinition:
    parameters = [
        ToolParameter("stock_code", "string", "Stock code, e.g. 600519 or 600519.SH.", required=True),
        ToolParameter(
            "limit", "integer", "Maximum records to return.", required=False, default=default_limit
        ),
    ]
    parameters.extend(extra_parameters or [])

    def handler(stock_code: str, limit: int = default_limit, **kwargs: Any) -> Dict[str, Any]:
        fields = ()
        if fields_from and kwargs.get(fields_from):
            fields = (str(kwargs[fields_from]),)
        return _fetch_capability(capability, stock_code, fields=fields, limit=int(limit))

    return ToolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        category="data",
        policy=_EXTENDED_READ_POLICY,
    )


get_financial_statement_tool = _tool(
    "get_financial_statement",
    "financial_statement",
    "Read normalized income statement, balance sheet or cash-flow periods with "
    "report period, unit, currency and source tier.",
    extra_parameters=[
        ToolParameter(
            "statement_type",
            "string",
            "income_statement, balance_sheet or cash_flow.",
            required=False,
            default="income_statement",
            enum=["income_statement", "balance_sheet", "cash_flow"],
        )
    ],
    fields_from="statement_type",
    default_limit=8,
)

get_consensus_estimate_tool = _tool(
    "get_consensus_estimate",
    "consensus_estimate",
    "Read analyst consensus estimates with forecast period, unit and source.",
    default_limit=8,
)

get_announcements_tool = _tool(
    "get_announcements",
    "announcement",
    "Read official disclosure announcements as metadata and evidence references; "
    "full PDFs are not inlined.",
)

get_research_reports_tool = _tool(
    "get_research_reports",
    "research_report",
    "Read broker research report index entries as metadata and evidence references.",
)

get_dragon_tiger_tool = _tool(
    "get_dragon_tiger",
    "dragon_tiger",
    "Read Dragon-Tiger (龙虎榜) records with amounts and seat detail.",
)

get_margin_trading_tool = _tool(
    "get_margin_trading",
    "margin",
    "Read margin financing and securities-lending balances.",
)

get_block_trades_tool = _tool(
    "get_block_trades",
    "block_trade",
    "Read block-trade (大宗交易) records with price, volume and discount.",
)

get_shareholder_counts_tool = _tool(
    "get_shareholder_counts",
    "holders",
    "Read shareholder-count history (股东户数) by reporting period.",
)

get_share_unlocks_tool = _tool(
    "get_share_unlocks",
    "unlock",
    "Read restricted-share unlock (解禁) schedule and sizes.",
)

get_dividends_tool = _tool(
    "get_dividends",
    "dividend",
    "Read dividend and bonus-issue history with ex-dates and per-share amounts.",
)


ALL_EXTENDED_DATA_TOOLS = [
    get_financial_statement_tool,
    get_consensus_estimate_tool,
    get_announcements_tool,
    get_research_reports_tool,
    get_dragon_tiger_tool,
    get_margin_trading_tool,
    get_block_trades_tool,
    get_shareholder_counts_tool,
    get_share_unlocks_tool,
    get_dividends_tool,
]

__all__ = ["ALL_EXTENDED_DATA_TOOLS", *[tool.name for tool in ALL_EXTENDED_DATA_TOOLS]]
