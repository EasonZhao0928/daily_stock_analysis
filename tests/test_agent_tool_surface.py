# -*- coding: utf-8 -*-
"""Tests for the internal DSA Tool Surface."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from src.agent.stock_scope import StockScope
from src.agent.tool_surface import ToolSurface
from src.agent.tools.execution import ToolAccessContext, check_tool_execution
from src.agent.tools.registry import (
    ExecutionProfile,
    ToolDefinition,
    ToolInvocation,
    ToolParameter,
    ToolPolicy,
    ToolRegistry,
    ToolResult,
)


def _single_tool_registry(tool: ToolDefinition) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


def _registry_with_echo(executed=None) -> ToolRegistry:
    calls = executed if executed is not None else []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo a message.",
            parameters=[
                ToolParameter(name="message", type="string", description="Message"),
                ToolParameter(
                    name="mode",
                    type="string",
                    description="Mode",
                    required=False,
                    default="plain",
                    enum=["plain", "loud"],
                ),
            ],
            handler=lambda message, mode="plain": calls.append((message, mode)) or {"message": message, "mode": mode},
            category="data",
            policy=ToolPolicy.declared(
                read_only=True,
                side_effects=[],
                permissions=["test:read"],
            ),
        )
    )
    return registry


def _tool_surface_snapshot(surface: ToolSurface) -> dict:
    codex_visible_names = {
        item["name"]
        for item in surface.list_tools("public", cancellation_safe_only=True)
    }
    return {
        item["name"]: {
            **{key: value for key, value in item.items() if key != "name"},
            "codex_visible": item["name"] in codex_visible_names,
        }
        for item in surface.list_tools("public")
    }


def _result_snapshot(result: dict) -> dict:
    snapshot = dict(result)
    snapshot["audit"] = {**result["audit"], "duration": 0.0}
    return snapshot


_EXPECTED_PRODUCTION_TOOL_SNAPSHOT = {
    "get_realtime_quote": {
        "description": (
            "Get real-time stock quote including price, change%, volume ratio, "
            "turnover rate, PE, PB, market cap. Returns live market data."
        ),
        "category": "data",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "Stock code, e.g., '600519' (A-share), 'AAPL' (US), 'hk00700' (HK)",
                }
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "get_daily_history": {
        "description": (
            "Get daily OHLCV (open, high, low, close, volume) historical data with "
            "MA5/MA10/MA20 indicators. Returns the last N trading days."
        ),
        "category": "data",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of trading days to fetch (default: 60)",
                },
                "stock_code": {
                    "type": "string",
                    "description": "Stock code, e.g., '600519' (A-share), 'AAPL' (US)",
                },
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read", "db_read", "db_write_cache"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "get_chip_distribution": {
        "description": (
            "Get chip distribution analysis for a stock. Returns profit ratio, "
            "average cost, chip concentration at 90% and 70% levels. Useful for "
            "judging support/resistance and holding structure."
        ),
        "category": "data",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "A-share stock code, e.g., '600519'",
                }
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "get_analysis_context": {
        "description": (
            "Get historical analysis context from the database for a stock. Returns "
            "today's and yesterday's OHLCV data, MA alignment status, volume and "
            "price changes. Provides the technical data foundation."
        ),
        "category": "data",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "Stock code, e.g., '600519'",
                }
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["db_read"],
            "permissions": ["analysis_context:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "get_stock_info": {
        "description": (
            "Get stock fundamental information: valuation, growth, earnings, institution "
            "flow, stock sector membership (belong_boards; boards is compatibility alias) "
            "and sector rankings. Returns a compact fundamental_context to reduce token usage."
        ),
        "category": "data",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "Stock code: A-share '600519', US 'AAPL', HK '00700'",
                }
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "get_portfolio_snapshot": {
        "description": (
            "Get portfolio snapshot summary and optional risk blocks. Default returns "
            "compact summary for lower token usage; set include_positions=true to include "
            "full position details."
        ),
        "category": "data",
        "parameters": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "integer",
                    "description": "Optional account id; omit to use all active accounts.",
                },
                "as_of": {
                    "type": "string",
                    "description": "Optional snapshot date in YYYY-MM-DD format (default: today).",
                },
                "cost_method": {
                    "type": "string",
                    "description": "Cost method: fifo or avg (default: fifo).",
                    "enum": ["fifo", "avg"],
                },
                "include_positions": {
                    "type": "boolean",
                    "description": "Whether to include full positions in snapshot output (default: false).",
                },
                "include_risk": {
                    "type": "boolean",
                    "description": "Whether to include risk summary block (default: true).",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["db_read"],
            "permissions": ["portfolio:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": [], "requires_stock_scope": False},
        "codex_visible": True,
    },
    "get_capital_flow": {
        "description": (
            "Get main-force (主力) capital flow data for an A-share stock. Returns today's "
            "net inflow, 5-day and 10-day cumulative inflows, and top sector-level capital "
            "flow rankings. Only supported for A-share individual stocks (not ETFs, indices, "
            "HK, or US stocks)."
        ),
        "category": "data",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "A-share stock code, e.g., '600519'",
                }
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "analyze_trend": {
        "description": (
            "Run comprehensive technical trend analysis on a stock. Fetches historical data "
            "from database or data source. Returns MA alignment, bias rates, MACD status, RSI "
            "levels, volume analysis, support/resistance levels, and a buy/sell signal with a "
            "score (0-100)."
        ),
        "category": "analysis",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "Stock code to analyze, e.g., '600519'",
                }
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read", "db_read"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "calculate_ma": {
        "description": (
            "Calculate moving averages (MA5/10/20/30/60/120/250 or custom periods) for a "
            "stock. Returns each MA value, price bias %, and whether price is above each MA. "
            "Also returns overall MA alignment (多头/空头/混合)."
        ),
        "category": "analysis",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of trading days to fetch history for (default: 120)",
                },
                "periods": {
                    "type": "string",
                    "description": (
                        "Comma-separated MA periods to calculate (default: '5,10,20,30,60,120,250'). "
                        "E.g., '5,10,20,60'"
                    ),
                },
                "stock_code": {
                    "type": "string",
                    "description": "Stock code, e.g., '600519'",
                },
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read", "db_read"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "get_volume_analysis": {
        "description": (
            "Analyse volume-price relationship for a stock. Returns volume ratios, average "
            "volume on up vs down days, volume trend (expanding/shrinking), and pattern "
            "interpretation (量价配合/背离). Useful for confirming trend strength and detecting "
            "distribution or accumulation phases."
        ),
        "category": "analysis",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of recent trading days to analyse (default: 30)",
                },
                "stock_code": {
                    "type": "string",
                    "description": "Stock code, e.g., '600519'",
                },
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read", "db_read"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "analyze_pattern": {
        "description": (
            "Detect candlestick and chart patterns in recent price history. Identifies: Doji, "
            "Hammer, Shooting Star, Morning/Evening Star, Engulfing, Double Bottom, upward "
            "breakout, box oscillation, and more. Returns pattern list with type "
            "(bullish/bearish/reversal) and strength."
        ),
        "category": "analysis",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of recent trading days to scan (default: 60)",
                },
                "stock_code": {
                    "type": "string",
                    "description": "Stock code, e.g., '600519'",
                },
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read", "db_read"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "search_stock_news": {
        "description": (
            "Search for the latest news articles about a specific stock. Requires both "
            "stock_code and stock_name for accurate search. Returns news titles, snippets, "
            "sources, and URLs."
        ),
        "category": "search",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "Stock code, e.g., '600519'",
                },
                "stock_name": {
                    "type": "string",
                    "description": "Stock name in Chinese, e.g., '贵州茅台'",
                },
            },
            "required": ["stock_code", "stock_name"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read", "db_write_cache"],
            "permissions": ["news:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "search_comprehensive_intel": {
        "description": (
            "Multi-dimensional intelligence search: latest news, market analysis, risk "
            "checking, earnings outlook, and industry trends for a stock. Returns a formatted "
            "report and structured results."
        ),
        "category": "search",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "Stock code, e.g., '600519'",
                },
                "stock_name": {
                    "type": "string",
                    "description": "Stock name in Chinese, e.g., '贵州茅台'",
                },
            },
            "required": ["stock_code", "stock_name"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read", "db_write_cache"],
            "permissions": ["intel:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
    "get_market_indices": {
        "description": (
            "Get major market indices (e.g., Shanghai Composite, Shenzhen Component, CSI "
            "300 for China; S&P 500, Nasdaq, Dow for US). Provides market overview."
        ),
        "category": "market",
        "parameters": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": (
                        "Market region: 'cn' for China A-shares, 'hk' for Hong Kong, 'us' for "
                        "US stocks (default: 'cn')"
                    ),
                    "enum": ["cn", "hk", "us"],
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": [], "requires_stock_scope": False},
        "codex_visible": True,
    },
    "get_sector_rankings": {
        "description": (
            "Get sector/industry performance rankings. Returns top N and bottom N sectors by "
            "daily change percentage. Useful for sector rotation analysis."
        ),
        "category": "market",
        "parameters": {
            "type": "object",
            "properties": {
                "top_n": {
                    "type": "integer",
                    "description": "Number of top/bottom sectors to return (default: 10)",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["network_read"],
            "permissions": ["market_data:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": [], "requires_stock_scope": False},
        "codex_visible": True,
    },
    "get_skill_backtest_summary": {
        "description": (
            "Inspect backtest data for a specific skill when skill-scoped stats exist. Provide "
            "skill_id for a targeted lookup; use get_strategy_backtest_summary for overall "
            "metrics. When skill-scoped rollups are unavailable, returns an informational "
            "response instead of fabricating metrics."
        ),
        "category": "data",
        "parameters": {
            "type": "object",
            "properties": {
                "eval_window_days": {
                    "type": "integer",
                    "description": (
                        "Evaluation window in days (default: 30). How many trading days after "
                        "signal to evaluate."
                    ),
                },
                "skill_id": {
                    "type": "string",
                    "description": "Skill identifier, e.g. 'bull_trend'.",
                },
            },
            "required": ["skill_id"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["db_read"],
            "permissions": ["backtest:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": [], "requires_stock_scope": False},
        "codex_visible": True,
    },
    "get_strategy_backtest_summary": {
        "description": "Legacy alias returning the overall backtest performance summary without triggering new backtests.",
        "category": "data",
        "parameters": {
            "type": "object",
            "properties": {
                "eval_window_days": {
                    "type": "integer",
                    "description": (
                        "Evaluation window in days (default: 30). How many trading days after "
                        "signal to evaluate."
                    ),
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["db_read"],
            "permissions": ["backtest:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": [], "requires_stock_scope": False},
        "codex_visible": True,
    },
    "get_stock_backtest_summary": {
        "description": (
            "Get backtest performance data for a specific stock: per-stock summary (win rate, "
            "accuracy, avg return) plus recent evaluation records. Read-only, does not trigger "
            "new backtests."
        ),
        "category": "data",
        "parameters": {
            "type": "object",
            "properties": {
                "eval_window_days": {
                    "type": "integer",
                    "description": "Evaluation window in days (default: 30)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of recent evaluation records to return (default: 10)",
                },
                "stock_code": {
                    "type": "string",
                    "description": "Stock code, e.g., '600519' (A-share), 'AAPL' (US), 'hk00700' (HK)",
                },
            },
            "required": ["stock_code"],
            "additionalProperties": False,
        },
        "policy": {
            "read_only": True,
            "side_effects": ["db_read"],
            "permissions": ["backtest:read"],
            "policy_status": "declared",
            "cancellation_safe": True,
        },
        "scope": {"scope_dimensions": ["stock"], "requires_stock_scope": True},
        "codex_visible": True,
    },
}


def test_public_descriptor_does_not_expose_handler_and_includes_policy_scope() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="quote",
            description="Quote",
            parameters=[ToolParameter(name="stock_code", type="string", description="Stock")],
            handler=lambda stock_code: {"code": stock_code},
            category="data",
            policy=ToolPolicy.declared(
                read_only=True,
                side_effects=["network_read"],
                permissions=["market_data:read"],
                scope_dimensions=["stock"],
            ),
        )
    )

    descriptor = ToolSurface(registry).list_tools("public")[0]
    encoded = json.dumps(descriptor, ensure_ascii=False)

    assert descriptor["policy"]["policy_status"] == "declared"
    assert descriptor["policy"]["cancellation_safe"] is False
    assert descriptor["scope"]["scope_dimensions"] == ["stock"]
    assert descriptor["scope"]["requires_stock_scope"] is True
    assert "handler" not in encoded
    assert "callable" not in encoded
    assert "<function" not in encoded


# The baseline the task 1.1 characterization tests protect, plus the task
# 5.2-5.4 capability tools added later.  The count is written out so growing the
# surface stays a deliberate, reviewed act.
_EXPECTED_PRODUCTION_TOOL_COUNT = 28


def test_default_registry_tool_surface_is_a_current_snapshot() -> None:
    """Existing tool contracts must not drift; additions must be deliberate."""
    from src.agent.factory import get_tool_registry

    registry = get_tool_registry()
    surface = ToolSurface(registry)

    assert len(registry) == _EXPECTED_PRODUCTION_TOOL_COUNT
    # Every baseline tool keeps its exact descriptor.
    snapshot = _tool_surface_snapshot(surface)
    for name, expected in _EXPECTED_PRODUCTION_TOOL_SNAPSHOT.items():
        assert name in snapshot, f"baseline tool disappeared: {name}"
        assert snapshot[name] == expected, f"baseline tool contract drifted: {name}"
    # Baseline order is preserved at the front of the registry.
    assert registry.list_names()[: len(_EXPECTED_PRODUCTION_TOOL_SNAPSHOT)] == list(
        _EXPECTED_PRODUCTION_TOOL_SNAPSHOT
    )
    assert registry.validate_tool_policies(strict=True) == []


def test_default_registry_openai_surface_keeps_current_tool_order_and_schema() -> None:
    from src.agent.factory import get_tool_registry

    registry = get_tool_registry()
    openai_tools = ToolSurface(registry).list_tools("openai")

    assert openai_tools == registry.to_openai_tools()
    names = [item["function"]["name"] for item in openai_tools]
    assert names[: len(_EXPECTED_PRODUCTION_TOOL_SNAPSHOT)] == list(
        _EXPECTED_PRODUCTION_TOOL_SNAPSHOT
    )
    assert len(names) == _EXPECTED_PRODUCTION_TOOL_COUNT


def test_tool_success_envelope_is_a_current_snapshot() -> None:
    result = ToolSurface(_registry_with_echo()).execute_tool(
        "echo",
        {"message": "hello"},
        ToolAccessContext(backend="test", session_id="s1"),
    )

    assert _result_snapshot(result) == {
        "ok": True,
        "tool_name": "echo",
        "result": {"message": "hello", "mode": "plain"},
        "result_text": '{"message": "hello", "mode": "plain"}',
        "error": None,
        "audit": {
            "tool_name": "echo",
            "arguments_summary": '{"message": "hello"}',
            "duration": 0.0,
            "result_summary": '{"message": "hello", "mode": "plain"}',
            "error_code": None,
            "backend": "test",
            "session_id": "s1",
        },
        "diagnostics": {
            "redacted": True,
            "result_length": 37,
            "result_truncated": False,
            "preview": '{"message": "hello", "mode": "plain"}',
        },
    }


def test_tool_error_envelope_is_a_current_snapshot() -> None:
    from src.agent.factory import get_tool_registry

    result = ToolSurface(get_tool_registry()).execute_tool(
        "get_realtime_quote",
        {},
        ToolAccessContext(backend="codex", session_id="session-1"),
    )

    assert _result_snapshot(result) == {
        "ok": False,
        "tool_name": "get_realtime_quote",
        "result": None,
        "result_text": (
            '{"error": "missing required argument: stock_code", '
            '"code": "invalid_arguments", "retriable": false}'
        ),
        "error": {
            "code": "invalid_arguments",
            "message": "missing required argument: stock_code",
            "retriable": False,
            "details": {},
        },
        "audit": {
            "tool_name": "get_realtime_quote",
            "arguments_summary": "{}",
            "duration": 0.0,
            "result_summary": (
                '{"error": "missing required argument: stock_code", '
                '"code": "invalid_arguments", "retriable": false}'
            ),
            "error_code": "invalid_arguments",
            "backend": "codex",
            "session_id": "session-1",
        },
        "diagnostics": {
            "redacted": True,
            "result_length": 99,
            "result_truncated": False,
            "preview": (
                '{"error": "missing required argument: stock_code", '
                '"code": "invalid_arguments", "retriable": false}'
            ),
        },
    }


def test_tool_result_truncation_is_a_current_snapshot() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="large",
            description="Large",
            parameters=[],
            handler=lambda: {"text": "abcdefghij"},
        )
    )

    result = ToolSurface(registry).execute_tool(
        "large",
        {},
        ToolAccessContext(max_result_bytes=20),
    )

    assert _result_snapshot(result) == {
        "ok": True,
        "tool_name": "large",
        "result": None,
        "result_text": '{"text": <truncated>',
        "error": None,
        "audit": {
            "tool_name": "large",
            "arguments_summary": "{}",
            "duration": 0.0,
            "result_summary": '{"text": <truncated>',
            "error_code": None,
            "backend": None,
            "session_id": None,
        },
        "diagnostics": {
            "redacted": True,
            "result_length": 20,
            "result_truncated": True,
            "preview": '{"text": <truncated>',
        },
    }


def test_cancellation_safe_filter_only_lists_explicitly_safe_tools() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="safe",
            description="Safe",
            parameters=[],
            handler=lambda: None,
            policy=ToolPolicy.declared(read_only=True, cancellation_safe=True),
        )
    )
    registry.register(
        ToolDefinition(
            name="unsafe",
            description="Unsafe",
            parameters=[],
            handler=lambda: None,
            policy=ToolPolicy.declared(read_only=True),
        )
    )

    surface = ToolSurface(registry)

    assert [item["name"] for item in surface.list_tools("public")] == ["safe", "unsafe"]
    assert [
        item["name"]
        for item in surface.list_tools("public", cancellation_safe_only=True)
    ] == ["safe"]


def test_controlled_execution_rejects_tool_without_cancellation_contract() -> None:
    called = False

    def handler():
        nonlocal called
        called = True

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="unsafe",
            description="Unsafe",
            parameters=[],
            handler=handler,
            policy=ToolPolicy.declared(read_only=True),
        )
    )

    result = ToolSurface(registry).execute_tool(
        "unsafe",
        {},
        ToolAccessContext(cancel_event=threading.Event()),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "cancellation_unsupported"
    assert called is False


def test_cancellation_safe_handler_exits_before_controlled_call_returns() -> None:
    entered = threading.Event()
    cancel_event = threading.Event()
    results = []

    def handler():
        entered.set()
        while True:
            check_tool_execution()
            cancel_event.wait(0.01)

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="cooperative",
            description="Cooperative",
            parameters=[],
            handler=handler,
            policy=ToolPolicy.declared(read_only=True, cancellation_safe=True),
        )
    )
    thread = threading.Thread(
        target=lambda: results.append(
            ToolSurface(registry).execute_tool(
                "cooperative",
                {},
                ToolAccessContext(cancel_event=cancel_event),
            )
        )
    )

    thread.start()
    assert entered.wait(timeout=1)
    cancel_event.set()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert results[0]["error"]["code"] == "cancelled"


def test_controlled_deadline_is_checked_before_safe_handler() -> None:
    called = False

    def handler():
        nonlocal called
        called = True

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="safe",
            description="Safe",
            parameters=[],
            handler=handler,
            policy=ToolPolicy.declared(read_only=True, cancellation_safe=True),
        )
    )

    result = ToolSurface(registry).execute_tool(
        "safe",
        {},
        ToolAccessContext(deadline=time.monotonic() - 1),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "timeout"
    assert called is False


def test_openai_schema_is_structurally_equal_to_registry_output() -> None:
    registry = _registry_with_echo()

    assert ToolSurface(registry).list_tools("openai") == registry.to_openai_tools()
    encoded = json.dumps(ToolSurface(registry).list_tools("openai"))
    assert "policy" not in encoded
    assert "permissions" not in encoded
    assert "side_effects" not in encoded
    assert "scope" not in encoded


def test_mcp_descriptor_is_descriptor_only() -> None:
    descriptor = ToolSurface(_registry_with_echo()).list_tools("mcp_descriptor")[0]
    expected_schema = _registry_with_echo().get("echo")._params_json_schema()
    expected_schema.setdefault("required", [])
    expected_schema["additionalProperties"] = False

    assert descriptor == {
        "name": "echo",
        "description": "Echo a message.",
        "inputSchema": expected_schema,
    }
    assert "transport" not in descriptor
    assert "server" not in descriptor


def test_execute_exact_tool_name_success() -> None:
    calls = []
    result = ToolSurface(_registry_with_echo(calls)).execute_tool(
        "echo",
        {"message": "hello"},
        ToolAccessContext(backend="test", session_id="s1"),
    )

    assert result["ok"] is True
    assert result["result"] == {"message": "hello", "mode": "plain"}
    assert json.loads(result["result_text"]) == {"message": "hello", "mode": "plain"}
    assert result["audit"]["backend"] == "test"
    assert result["audit"]["session_id"] == "s1"
    assert calls == [("hello", "plain")]


def test_rejects_unregistered_namespaced_and_unknown_tools() -> None:
    surface = ToolSurface(_registry_with_echo())

    assert surface.execute_tool("default_api:echo", {}, None)["error"]["code"] == "invalid_tool_name"
    assert surface.execute_tool("provider.tool", {}, None)["error"]["code"] == "invalid_tool_name"
    assert surface.execute_tool("provider:tool", {}, None)["error"]["code"] == "invalid_tool_name"
    assert surface.execute_tool("missing", {}, None)["error"]["code"] == "tool_not_found"


def test_registered_dotted_name_uses_exact_match_only() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="provider.tool",
            description="Exact dotted tool",
            parameters=[],
            handler=lambda: {"ok": True},
        )
    )
    surface = ToolSurface(registry)

    assert surface.execute_tool("provider.tool", {}, None)["ok"] is True
    assert surface.execute_tool("other.tool", {}, None)["error"]["code"] == "invalid_tool_name"


def test_argument_validation_errors_before_handler() -> None:
    calls = []
    surface = ToolSurface(_registry_with_echo(calls))

    cases = [
        (None, "arguments must be an object"),
        ({}, "missing required argument"),
        ({"message": "x", "extra": 1}, "unexpected argument"),
        ({"message": "x", "mode": "quiet"}, "must be one of"),
        ({"message": "x", "mode": None}, "must not be null"),
        ({"message": 123}, "must be string"),
    ]
    for arguments, expected in cases:
        result = surface.execute_tool("echo", arguments, None)
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_arguments"
        assert expected in result["error"]["message"]

    assert calls == []


def test_optional_null_arguments_are_rejected_but_omitted_defaults_still_work() -> None:
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="optional_params",
            description="Optional params",
            parameters=[
                ToolParameter(name="message", type="string", description="Message"),
                ToolParameter(name="count", type="integer", description="Count", required=False, default=1),
                ToolParameter(name="enabled", type="boolean", description="Enabled", required=False, default=True),
                ToolParameter(name="metadata", type="object", description="Metadata", required=False),
            ],
            handler=lambda message, count=1, enabled=True, metadata=None: calls.append(
                (message, count, enabled, metadata)
            )
            or {
                "message": message,
                "count": count,
                "enabled": enabled,
                "metadata": metadata,
            },
        )
    )
    surface = ToolSurface(registry)

    for key in ["count", "enabled", "metadata"]:
        result = surface.execute_tool("optional_params", {"message": "x", key: None}, None)
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_arguments"
        assert "must not be null" in result["error"]["message"]

    result = surface.execute_tool("optional_params", {"message": "x"}, None)
    assert result["ok"] is True
    assert result["result"] == {
        "message": "x",
        "count": 1,
        "enabled": True,
        "metadata": None,
    }
    assert calls == [("x", 1, True, None)]


def test_extra_arguments_allowed_when_handler_accepts_kwargs() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="kwargs_tool",
            description="Allows kwargs",
            parameters=[],
            handler=lambda **kwargs: kwargs,
        )
    )

    result = ToolSurface(registry).execute_tool("kwargs_tool", {"extra": 1}, None)
    descriptor = ToolSurface(registry).list_tools("public")[0]

    assert result["ok"] is True
    assert result["result"] == {"extra": 1}
    assert descriptor["parameters"]["additionalProperties"] is True


def test_stock_scope_violation_blocks_handler() -> None:
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="quote",
            description="Quote",
            parameters=[ToolParameter(name="stock_code", type="string", description="Stock")],
            handler=lambda stock_code: calls.append(stock_code) or {"code": stock_code},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["market_data:read"],
                scope_dimensions=["stock"],
            ),
        )
    )

    result = ToolSurface(registry).execute_tool(
        "quote",
        {"stock_code": "AAPL"},
        ToolAccessContext(stock_scope=StockScope(expected_stock_code="600519", allowed_stock_codes={"600519"})),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "stock_scope_violation"
    assert calls == []


def test_declared_stock_scope_requires_explicit_stock_context_before_handler() -> None:
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="quote",
            description="Quote",
            parameters=[ToolParameter(name="stock_code", type="string", description="Stock")],
            handler=lambda stock_code: calls.append(stock_code) or {"code": stock_code},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["market_data:read"],
                scope_dimensions=["stock"],
            ),
        )
    )

    result = ToolSurface(registry).execute_tool(
        "quote",
        {"stock_code": "AAPL"},
        None,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "stock_scope_violation"
    assert result["error"]["details"]["reason"] == "stock_scope_required"
    assert calls == []


def test_handler_error_is_structured_without_traceback() -> None:
    def _fail():
        raise RuntimeError("secret stack")

    registry = ToolRegistry()
    registry.register(ToolDefinition(name="fail", description="Fail", parameters=[], handler=_fail))

    result = ToolSurface(registry).execute_tool("fail", {}, None)

    assert result["ok"] is False
    assert result["error"]["code"] == "handler_error"
    assert "Traceback" not in result["result_text"]
    assert "secret stack" not in result["result_text"]


def test_serialization_fallback_for_non_json_native_object() -> None:
    class Payload:
        def __init__(self) -> None:
            self.value = "ok"

    registry = ToolRegistry()
    registry.register(ToolDefinition(name="payload", description="Payload", parameters=[], handler=lambda: Payload()))

    result = ToolSurface(registry).execute_tool("payload", {}, None)

    assert result["ok"] is True
    assert result["result"] == {"value": "ok"}
    assert json.loads(result["result_text"]) == {"value": "ok"}
    json.dumps(result)


def test_audit_and_diagnostics_are_redacted() -> None:
    plain_secret = "plainsecret1234567890"
    cookie_secret = "sessionid=abcdef1234567890"
    basic_auth_secret = "dXNlcjpwYXNzMTIzNDU2"
    proxy_auth_secret = "cHJveHk6c2VjcmV0MTIz"
    api_auth_secret = "plainauthsecret123456"
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="secret",
            description="Secret",
            parameters=[
                ToolParameter(name="message", type="string", description="Message"),
                ToolParameter(name="api_key", type="string", description="API key", required=False),
                ToolParameter(name="headers", type="object", description="Headers", required=False),
            ],
            handler=lambda message, api_key=None, headers=None: {
                "Authorization": "Bearer sk-secret-token-1234567890",
                "api_key": plain_secret,
                "token": plain_secret,
                "secret": plain_secret,
                "headers": {
                    "cookie": cookie_secret,
                    "set-cookie": cookie_secret,
                    "authorization": plain_secret,
                },
                "path": "/Users/massif/private/file.txt",
                "message": message * 50,
            },
        )
    )

    result = ToolSurface(registry).execute_tool(
        "secret",
        {
            "message": (
                "Authorization: Bearer sk-argument-token-1234567890 "
                f"Authorization: Basic {basic_auth_secret} "
                f"Proxy-Authorization: Basic {proxy_auth_secret} "
                f"authorization=ApiKey {api_auth_secret} "
                "/Users/massif/.env "
            ),
            "api_key": plain_secret,
            "headers": {
                "cookie": cookie_secret,
                "set-cookie": cookie_secret,
                "authorization": plain_secret,
            },
        },
        ToolAccessContext(audit_context={"secret": plain_secret}),
    )
    visible = json.dumps({"audit": result["audit"], "diagnostics": result["diagnostics"]}, ensure_ascii=False)

    assert "sk-secret-token-1234567890" not in visible
    assert "sk-argument-token-1234567890" not in visible
    assert basic_auth_secret not in visible
    assert proxy_auth_secret not in visible
    assert api_auth_secret not in visible
    assert plain_secret not in visible
    assert cookie_secret not in visible
    assert "/Users/massif/private" not in visible
    assert "/Users/massif/.env" not in visible
    assert "[REDACTED" in visible or "<truncated" in visible


def test_external_result_redaction_happens_before_payload_limit() -> None:
    plain_secret = "plain-secret-value-1234567890"
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="external_secret",
            description="External result",
            parameters=[],
            handler=lambda: {
                "api_key": plain_secret,
                "nested": {"token": plain_secret},
                "text": "x" * 200,
            },
        )
    )

    result = ToolSurface(registry).execute_tool(
        "external_secret",
        {},
        ToolAccessContext(
            backend="codex_app_server",
            redact_result=True,
            max_result_bytes=120,
        ),
    )

    assert result["ok"] is True
    assert plain_secret not in result["result_text"]
    assert plain_secret not in json.dumps(result["result"], ensure_ascii=False)
    assert result["diagnostics"]["result_truncated"] is True
    assert len(result["result_text"].encode("utf-8")) <= 120


def test_policy_unknown_does_not_break_registry_but_strict_validation_reports_issue() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="plain", description="Plain", parameters=[], handler=lambda: None))

    issues = registry.validate_tool_policies(strict=True)

    assert registry.validate_tool_policies(strict=False) == []
    assert issues
    assert issues[0]["code"] == "policy_unknown"


def test_strict_validation_reports_stock_scope_policy_mismatch() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="undeclared_stock",
            description="Stock param without policy scope.",
            parameters=[ToolParameter(name="stock_code", type="string", description="Stock")],
            handler=lambda stock_code: {"code": stock_code},
            policy=ToolPolicy.declared(read_only=True, permissions=["market_data:read"]),
        )
    )
    registry.register(
        ToolDefinition(
            name="missing_stock_param",
            description="Policy scope without stock_code param.",
            parameters=[ToolParameter(name="ticker", type="string", description="Ticker")],
            handler=lambda ticker: {"code": ticker},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["market_data:read"],
                scope_dimensions=["stock"],
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="unsupported_market_scope",
            description="Unsupported market scope.",
            parameters=[ToolParameter(name="region", type="string", description="Region")],
            handler=lambda region: {"region": region},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["market_data:read"],
                scope_dimensions=["market"],
            ),
        )
    )

    issue_codes = {issue["code"] for issue in registry.validate_tool_policies(strict=True)}
    non_strict_issue_codes = {issue["code"] for issue in registry.validate_tool_policies(strict=False)}

    assert "stock_scope_missing" in issue_codes
    assert "stock_scope_parameter_missing" in issue_codes
    assert "unsupported_scope_dimension" in issue_codes
    assert "stock_scope_missing" not in non_strict_issue_codes
    assert "stock_scope_parameter_missing" not in non_strict_issue_codes
    assert "unsupported_scope_dimension" not in non_strict_issue_codes


def test_tool_surface_stock_param_without_declared_scope_fails_closed() -> None:
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="undeclared_stock",
            description="Stock param without policy scope.",
            parameters=[ToolParameter(name="stock_code", type="string", description="Stock")],
            handler=lambda stock_code: calls.append(stock_code) or {"code": stock_code},
            policy=ToolPolicy.declared(read_only=True, permissions=["market_data:read"]),
        )
    )

    result = ToolSurface(registry).execute_tool(
        "undeclared_stock",
        {"stock_code": "AAPL"},
        ToolAccessContext(stock_scope=StockScope(expected_stock_code="600519", allowed_stock_codes={"600519"})),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "scope_contract_violation"
    assert result["error"]["details"]["missing_scope_dimension"] == "stock"
    assert calls == []


def test_tool_surface_declared_stock_scope_without_stock_code_fails_closed() -> None:
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="ticker_tool",
            description="Declares stock scope with ticker parameter.",
            parameters=[ToolParameter(name="ticker", type="string", description="Ticker")],
            handler=lambda ticker: calls.append(ticker) or {"code": ticker},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["market_data:read"],
                scope_dimensions=["stock"],
            ),
        )
    )

    result = ToolSurface(registry).execute_tool(
        "ticker_tool",
        {"ticker": "AAPL"},
        ToolAccessContext(stock_scope=StockScope(expected_stock_code="600519", allowed_stock_codes={"600519"})),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "scope_contract_violation"
    assert result["error"]["details"]["missing_parameter"] == "stock_code"
    assert calls == []


def test_tool_surface_unsupported_scope_dimension_fails_closed() -> None:
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="market_tool",
            description="Declares unsupported market scope.",
            parameters=[ToolParameter(name="region", type="string", description="Region")],
            handler=lambda region: calls.append(region) or {"region": region},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["market_data:read"],
                scope_dimensions=["market"],
            ),
        )
    )

    result = ToolSurface(registry).execute_tool(
        "market_tool",
        {"region": "us"},
        ToolAccessContext(market="cn"),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "scope_contract_violation"
    assert result["error"]["details"]["unsupported_scope_dimensions"] == ["market"]
    assert calls == []


def test_default_production_registry_has_supported_declared_policies() -> None:
    from src.agent.factory import get_tool_registry

    registry = get_tool_registry()

    assert registry.validate_tool_policies(strict=True) == []


def test_default_production_registry_only_exposes_bounded_tools_to_codex() -> None:
    from src.agent.factory import get_tool_registry

    safe_names = {
        item["name"]
        for item in ToolSurface(get_tool_registry()).list_tools(
            "public",
            cancellation_safe_only=True,
        )
    }

    assert safe_names == {
        "get_realtime_quote",
        "get_daily_history",
        "get_chip_distribution",
        "get_analysis_context",
        "get_stock_info",
        "get_capital_flow",
        "analyze_trend",
        "calculate_ma",
        "get_volume_analysis",
        "analyze_pattern",
        "get_market_indices",
        "get_portfolio_snapshot",
        "get_skill_backtest_summary",
        "get_strategy_backtest_summary",
        "get_stock_backtest_summary",
        "search_stock_news",
        "search_comprehensive_intel",
        "get_sector_rankings",
        # Task 5.2-5.4 capability tools: bounded, read-only and cancellation-safe.
        "get_financial_statement",
        "get_consensus_estimate",
        "get_announcements",
        "get_research_reports",
        "get_dragon_tiger",
        "get_margin_trading",
        "get_block_trades",
        "get_shareholder_counts",
        "get_share_unlocks",
        "get_dividends",
    }


def test_analysis_context_honors_cancellation_after_database_read(monkeypatch) -> None:
    from src.agent.tools import data_tools

    cancel_event = threading.Event()

    class _Database:
        def get_analysis_context(self, _stock_code):
            cancel_event.set()
            return {"code": "600519"}

    monkeypatch.setattr(data_tools, "_get_db", lambda: _Database())

    result = ToolSurface(_single_tool_registry(data_tools.get_analysis_context_tool)).execute_tool(
        "get_analysis_context",
        {"stock_code": "600519"},
        ToolAccessContext(
            stock_scope=StockScope(
                expected_stock_code="600519",
                allowed_stock_codes={"600519"},
            ),
            cancel_event=cancel_event,
        ),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "cancelled"


def test_backtest_summary_honors_cancellation_after_database_read(monkeypatch) -> None:
    from src.agent.tools import backtest_tools

    cancel_event = threading.Event()

    class _BacktestService:
        def get_summary(self, **_kwargs):
            cancel_event.set()
            return {"scope": "overall"}

    monkeypatch.setattr(backtest_tools, "_get_backtest_service", lambda: _BacktestService())

    result = ToolSurface(
        _single_tool_registry(backtest_tools.get_strategy_backtest_summary_tool)
    ).execute_tool(
        "get_strategy_backtest_summary",
        {},
        ToolAccessContext(cancel_event=cancel_event),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "cancelled"


def test_future_scope_context_fields_do_not_block_undeclared_tools() -> None:
    result = ToolSurface(_registry_with_echo()).execute_tool(
        "echo",
        {"message": "ok"},
        ToolAccessContext(
            market="us",
            time_range={"from": "2026-01-01", "to": "2026-01-31"},
            data_sources=["fixture"],
        ),
    )

    assert result["ok"] is True


def test_timeout_does_not_return_while_handler_is_still_running() -> None:
    finished = threading.Event()

    def slow_handler():
        time.sleep(0.4)
        finished.set()
        return {"done": True}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="slow",
            description="Slow",
            parameters=[],
            handler=slow_handler,
            policy=ToolPolicy.declared(read_only=True, cancellation_safe=True),
        )
    )

    started = time.time()
    result = ToolSurface(registry).execute_tool(
        "slow",
        {},
        ToolAccessContext(timeout_seconds=0.01),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "timeout"
    assert finished.is_set()
    assert time.time() - started >= 0.35


def test_max_result_bytes_truncates_public_payload_and_marks_diagnostics() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="large", description="Large", parameters=[], handler=lambda: {"text": "x" * 200}))

    result = ToolSurface(registry).execute_tool(
        "large",
        {},
        ToolAccessContext(max_result_bytes=20),
    )

    assert result["ok"] is True
    assert result["result"] is None
    assert result["diagnostics"]["result_truncated"] is True
    assert result["result_text"].endswith("<truncated>")
    assert len(result["result_text"].encode("utf-8")) <= 20


def test_max_result_bytes_does_not_return_raw_object_when_text_fits() -> None:
    class Payload:
        def __init__(self) -> None:
            self.value = "ok"
            self._private = "x" * 10000

    registry = ToolRegistry()
    registry.register(ToolDefinition(name="payload", description="Payload", parameters=[], handler=lambda: Payload()))

    result = ToolSurface(registry).execute_tool(
        "payload",
        {},
        ToolAccessContext(max_result_bytes=100),
    )

    assert result["ok"] is True
    assert result["result_text"] == '{"value": "ok"}'
    assert result["result"] == {"value": "ok"}
    assert result["diagnostics"]["result_truncated"] is False


def test_descriptors_include_explicit_empty_required_without_changing_openai_shape() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="empty", description="Empty", parameters=[], handler=lambda: None))

    surface = ToolSurface(registry)

    assert surface.list_tools("public")[0]["parameters"]["required"] == []
    assert surface.list_tools("public")[0]["parameters"]["additionalProperties"] is False
    assert surface.list_tools("mcp_descriptor")[0]["inputSchema"]["required"] == []
    assert surface.list_tools("mcp_descriptor")[0]["inputSchema"]["additionalProperties"] is False
    assert "required" not in registry.to_openai_tools()[0]["function"]["parameters"]
    assert "additionalProperties" not in registry.to_openai_tools()[0]["function"]["parameters"]


def test_max_result_bytes_caps_error_result_text() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="empty", description="Empty", parameters=[], handler=lambda: None))

    result = ToolSurface(registry).execute_tool(
        "empty",
        {"unexpected": "x" * 200},
        ToolAccessContext(max_result_bytes=16),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert result["diagnostics"]["result_truncated"] is True
    assert len(result["result_text"].encode("utf-8")) <= 16


def test_stock_scope_no_longer_imports_runner_for_normalization() -> None:
    source = Path("src/agent/stock_scope.py").read_text(encoding="utf-8")

    assert "from src.agent.runner import _normalize_tool_stock_code" not in source


def _profile_contract_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="research_read",
            description="Research read",
            parameters=[],
            handler=lambda: {"kind": "research"},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["market_data:read"],
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="portfolio_read",
            description="Portfolio read",
            parameters=[
                ToolParameter(
                    name="account_id",
                    type="string",
                    description="Account",
                )
            ],
            handler=lambda account_id: {"account_id": account_id},
            policy=ToolPolicy.declared(
                read_only=True,
                side_effects=["db_read"],
                permissions=["portfolio:read"],
                scope_dimensions=["account"],
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="paper_state_read",
            description="Paper state read",
            parameters=[],
            handler=lambda: {"kind": "paper"},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["paper:read"],
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="paper_propose",
            description="Paper proposal",
            parameters=[],
            handler=lambda: {"kind": "proposal"},
            policy=ToolPolicy.declared(
                read_only=False,
                side_effects=["paper_proposal"],
                permissions=["paper:proposal"],
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="paper_approve",
            description="Paper approval",
            parameters=[],
            handler=lambda: {"kind": "approval"},
            policy=ToolPolicy.declared(
                read_only=False,
                side_effects=["paper_approval"],
                permissions=["paper:approval"],
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="shell_exec",
            description="Shell",
            parameters=[],
            handler=lambda: {"kind": "shell"},
            policy=ToolPolicy.declared(
                read_only=False,
                side_effects=["shell"],
                permissions=["shell:execute"],
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="paper_direct_write",
            description="Direct paper-side write with forbidden capabilities",
            parameters=[],
            handler=lambda: {"kind": "unsafe"},
            policy=ToolPolicy.declared(
                read_only=False,
                side_effects=["db_write", "broker_order", "file_write"],
                permissions=["paper:proposal"],
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="symbol_read",
            description="Symbol read",
            parameters=[
                ToolParameter(
                    name="symbol",
                    type="string",
                    description="Symbol",
                )
            ],
            handler=lambda symbol: {"symbol": symbol},
            policy=ToolPolicy.declared(
                read_only=True,
                permissions=["market_data:read"],
                scope_dimensions=["symbol"],
            ),
        )
    )
    return registry


def test_execution_profiles_filter_tools_without_changing_descriptors() -> None:
    surface = ToolSurface(_profile_contract_registry())

    assert [profile.value for profile in ExecutionProfile] == [
        "research_readonly",
        "portfolio_readonly",
        "paper_proposal",
        "paper_approval",
    ]
    assert [item["name"] for item in surface.describe("research_readonly")] == [
        "research_read",
        "symbol_read",
    ]
    assert [item["name"] for item in surface.describe("portfolio_readonly")] == [
        "research_read",
        "portfolio_read",
        "symbol_read",
    ]
    # ``paper_proposal`` deliberately excludes market_data:read tools
    # (``research_read`` / ``symbol_read``): a Paper cycle decides against a
    # frozen Observation, so any live read would reach past its own cutoff.
    assert [item["name"] for item in surface.describe("paper_proposal")] == [
        "paper_state_read",
        "paper_propose",
    ]
    assert [item["name"] for item in surface.describe("paper_approval")] == [
        "paper_approve",
    ]

    all_descriptors = surface.list_tools("public")
    assert surface.list_tools("public") == all_descriptors
    assert surface.list_tools("openai", profile="research_readonly") == [
        item
        for item in surface.list_tools("openai")
        if item["function"]["name"] in {"research_read", "symbol_read"}
    ]
    assert surface.list_tools("mcp_descriptor", profile="paper_approval") == [
        item
        for item in surface.list_tools("mcp_descriptor")
        if item["name"] == "paper_approve"
    ]

    denied = {
        item["tool"]: item["code"]
        for item in surface.profile_diagnostics("research_readonly")
        if not item["visible"]
    }
    assert denied["shell_exec"] == "tool_not_allowed"

    paper_names = [item["name"] for item in surface.describe("paper_proposal")]
    assert "paper_direct_write" not in paper_names
    paper_diagnostic = next(
        item
        for item in surface.profile_diagnostics("paper_proposal")
        if item["tool"] == "paper_direct_write"
    )
    assert paper_diagnostic["details"]["reason"] == "side_effect_not_allowed"


def test_tool_invocation_and_tool_result_have_stable_serialization() -> None:
    invocation = ToolInvocation(
        tool_name="symbol_read",
        arguments={"symbol": "AAPL"},
        profile="research_readonly",
        scope={"symbols": ["AAPL"], "account_ids": ["paper-1"]},
    )

    invocation_payload = invocation.to_dict()
    assert invocation_payload == {
        "tool_name": "symbol_read",
        "arguments": {"symbol": "AAPL"},
        "profile": "research_readonly",
        "scope": {"account_ids": ["paper-1"], "symbols": ["AAPL"]},
    }
    assert json.loads(invocation.to_json()) == invocation_payload
    assert ToolInvocation.from_json(invocation.to_json()).to_dict() == invocation_payload

    result = ToolSurface(_profile_contract_registry()).execute(invocation)

    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.tool_name == "symbol_read"
    assert result.result == {"symbol": "AAPL"}
    assert json.loads(result.to_json()) == result.to_dict()
    assert result.to_dict()["error"] is None


def test_profile_authorization_and_scope_errors_are_stable() -> None:
    surface = ToolSurface(_profile_contract_registry())

    not_allowed = surface.execute(
        ToolInvocation(
            name="paper_approve",
            arguments={},
            profile="research_readonly",
        )
    )
    assert not_allowed.error["code"] == "tool_not_allowed"
    assert not_allowed.error["retriable"] is False
    assert not_allowed.to_dict() == ToolResult.from_json(not_allowed.to_json()).to_dict()

    missing_account = surface.execute(
        ToolInvocation(
            name="portfolio_read",
            arguments={"account_id": "account-1"},
            profile="portfolio_readonly",
        )
    )
    assert missing_account.error["code"] == "account_scope_violation"
    assert missing_account.error["details"]["reason"] == "account_scope_required"

    outside_account = surface.execute(
        ToolInvocation(
            name="portfolio_read",
            arguments={"account_id": "account-2"},
            profile="portfolio_readonly",
            scope={"account_ids": ["account-1"]},
        )
    )
    assert outside_account.error["code"] == "account_scope_violation"
    assert outside_account.error["details"]["reason"] == "account_scope_mismatch"

    missing_symbol = surface.execute(
        ToolInvocation(
            name="symbol_read",
            arguments={"symbol": "AAPL"},
            profile="research_readonly",
        )
    )
    assert missing_symbol.error["code"] == "symbol_scope_violation"
    assert missing_symbol.error["details"]["reason"] == "symbol_scope_required"

    outside_symbol = surface.execute(
        ToolInvocation(
            name="symbol_read",
            arguments={"symbol": "MSFT"},
            profile="research_readonly",
            scope={"symbols": ["AAPL"]},
        )
    )
    assert outside_symbol.error["code"] == "symbol_scope_violation"
    assert outside_symbol.error["details"]["reason"] == "symbol_scope_mismatch"
