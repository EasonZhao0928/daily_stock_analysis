# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""Optional Top-K candidate news, announcement and fund-flow context."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import pandas as pd

from data_provider.market_data_types import DataEnvelope, DataQuery, DataStatus, SourcePolicy

_NEGATIVE_EVENT_KEYWORDS = {
    "减持": ("减持", "拟减持", "被动减持"),
    "监管": ("处罚", "立案", "监管函", "问询函", "警示函", "调查"),
    "业绩压力": ("预亏", "亏损", "业绩下滑", "业绩减少", "净利润下降"),
    "财务风险": ("债务", "逾期", "违约", "商誉减值", "资产减值"),
    "退市风险": ("退市", "ST", "*ST", "终止上市"),
    "诉讼风险": ("诉讼", "仲裁", "冻结", "质押"),
}
_POSITIVE_EVENT_KEYWORDS = {
    "回购增持": ("回购", "增持"),
    "订单经营": ("中标", "合同", "订单", "定点", "合作"),
    "业绩改善": ("预增", "扭亏", "增长", "净利润增长"),
    "股东回报": ("分红", "派息"),
    "激励": ("股权激励", "员工持股"),
}
_ANNOUNCEMENT_CATEGORY_KEYWORDS = {
    "业绩": ("业绩", "利润", "营收", "预增", "预亏", "扭亏", "年报", "季报"),
    "回购增持": ("回购", "增持"),
    "减持": ("减持", "被动减持"),
    "监管问询": ("监管函", "问询函", "警示函", "立案", "调查", "处罚"),
    "重大合同": ("中标", "合同", "订单", "定点", "合作协议"),
    "分红融资": ("分红", "派息", "配股", "定增", "可转债", "融资"),
    "诉讼担保": ("诉讼", "仲裁", "担保", "冻结", "质押"),
    "股权激励": ("股权激励", "员工持股"),
}
_SOURCE_WEIGHTS = {
    "announcement": 1.0,
    "quote": 0.85,
    "news": 0.65,
    "fund_flow": 0.75,
}
_DEFAULT_MAX_WORKERS = 4


def collect_candidate_context(
    candidate_df: pd.DataFrame,
    *,
    max_rows: int = 8,
    providers: list[str] | None = None,
    news_limit: int = 3,
    announcement_limit: int = 3,
    cache_dir: str | Path | None = None,
    cache_ttl_hours: int = 24,
    source_weights: dict[str, float] | None = None,
    market_data_manager: Any | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    """Collect candidate-level context rows keyed by stock code.

    The function is optional and best-effort. It should never decide
    eligibility; it only supplies LLM research material for already shortlisted
    candidates.
    """
    if candidate_df.empty or "code" not in candidate_df.columns or max_rows <= 0:
        return [], []

    providers = _normalize_providers(providers or [])
    tasks: list[dict[str, str]] = []
    for _, candidate in candidate_df.head(max_rows).iterrows():
        code = _normalize_code(candidate.get("code", ""))
        if not code or code == "000000":
            continue
        tasks.append(
            {
                "code": code,
                "name": str(candidate.get("name", "") or ""),
            }
        )

    if not tasks:
        return [], []

    results: list[tuple[dict[str, object] | None, list[str]] | None] = [None] * len(tasks)
    max_workers = min(_DEFAULT_MAX_WORKERS, len(tasks))
    if max_workers <= 1:
        for index, task in enumerate(tasks):
            results[index] = _collect_candidate_context_row(
                task,
                providers=providers,
                news_limit=news_limit,
                announcement_limit=announcement_limit,
                cache_dir=cache_dir,
                cache_ttl_hours=cache_ttl_hours,
                source_weights=source_weights,
                market_data_manager=market_data_manager,
            )
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index = {
                executor.submit(
                    _collect_candidate_context_row,
                    task,
                    providers=providers,
                    news_limit=news_limit,
                    announcement_limit=announcement_limit,
                    cache_dir=cache_dir,
                    cache_ttl_hours=cache_ttl_hours,
                    source_weights=source_weights,
                    market_data_manager=market_data_manager,
                ): index
                for index, task in enumerate(tasks)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = (None, [f"{tasks[index]['code']} context: {exc}"])

    rows: list[dict[str, object]] = []
    errors: list[str] = []
    for result in results:
        if result is None:
            continue
        row, row_errors = result
        errors.extend(row_errors)
        if row is not None:
            rows.append(row)
    return rows, errors


def _collect_candidate_context_row(
    candidate: dict[str, str],
    *,
    providers: list[str],
    news_limit: int,
    announcement_limit: int,
    cache_dir: str | Path | None,
    cache_ttl_hours: int,
    source_weights: dict[str, float] | None,
    market_data_manager: Any | None = None,
) -> tuple[dict[str, object] | None, list[str]]:
    code = candidate["code"]
    errors: list[str] = []
    try:
        cached = _read_cache(cache_dir, code, providers, cache_ttl_hours=cache_ttl_hours)
        if cached is not None:
            _ensure_context_row_enrichment(
                cached,
                requested_sources=providers,
                source_weights=source_weights,
            )
            return cached, []
        row: dict[str, object] = {
            "code": code,
            "name": candidate.get("name", ""),
        }
        successful_sources: list[str] = []
        diagnostics: dict[str, dict[str, object]] = {}

        requested_context = [provider for provider in providers if provider in {"news", "announcement", "fund_flow", "quote"}]
        for provider in requested_context:
            try:
                envelope = _fetch_candidate_context_envelope(
                    provider,
                    code,
                    stock_name=candidate.get("name", ""),
                    limit=(announcement_limit if provider == "announcement" else news_limit),
                    market_data_manager=market_data_manager,
                )
            except Exception as exc:
                envelope = _error_envelope(provider, code, exc)

            diagnostics[provider] = _diagnostic_from_envelope(envelope)
            if envelope.status is DataStatus.OK and _has_context_value(envelope.data):
                row[provider] = _context_value_to_text(provider, envelope.data)
                if row[provider]:
                    successful_sources.append(provider)
            elif envelope.status not in {DataStatus.VALID_EMPTY, DataStatus.OK}:
                detail = _envelope_error(envelope)
                errors.append(f"{code} {provider}: {detail}")

        if diagnostics:
            row["source_diagnostics"] = diagnostics
            row["source_status"] = {key: value.get("status") for key, value in diagnostics.items()}
        if diagnostics or any(value for key, value in row.items() if key not in {"code", "name"}):
            row["source_count"] = len(successful_sources)
            row["source_confidence"] = _source_confidence(successful_sources, providers)
            row["source_weight_score"] = _source_weight_score(
                successful_sources,
                providers,
                source_weights=source_weights,
            )
            _ensure_context_row_enrichment(
                row,
                requested_sources=providers,
                successful_sources=successful_sources,
                source_weights=source_weights,
            )
            try:
                _write_cache(cache_dir, code, providers, row)
            except Exception as exc:
                errors.append(f"{code} cache: {exc}")
            return row, errors
        return None, errors
    except Exception as exc:
        return None, [*errors, f"{code} context: {exc}"]


def fetch_stock_news_summary(code: str, *, limit: int = 3) -> str:
    """Compatibility facade returning the routed news envelope as text."""
    envelope = _fetch_candidate_context_envelope("news", code, limit=limit)
    return _context_value_to_text("news", envelope.data) if envelope.status is DataStatus.OK else ""


def fetch_stock_announcement_summary(code: str, *, limit: int = 3) -> str:
    """Compatibility facade returning the routed announcement envelope as text."""
    envelope = _fetch_candidate_context_envelope("announcement", code, limit=limit)
    return _context_value_to_text("announcement", envelope.data) if envelope.status is DataStatus.OK else ""


def fetch_stock_fund_flow_summary(code: str) -> str:
    """Compatibility facade returning the routed fund-flow envelope as text."""
    envelope = _fetch_candidate_context_envelope("fund_flow", code)
    return _context_value_to_text("fund_flow", envelope.data) if envelope.status is DataStatus.OK else ""


def fetch_stock_quote_summary(code: str) -> str:
    """Compatibility facade returning the routed quote envelope as text."""
    envelope = _fetch_candidate_context_envelope("quote", code)
    return _context_value_to_text("quote", envelope.data) if envelope.status is DataStatus.OK else ""


def _get_market_data_manager() -> Any:
    """Use the Screening/DataFetcherManager singleton as the context seam."""
    from src.services.screening_service import _get_dsa_fetcher_manager

    return _get_dsa_fetcher_manager()


def _get_search_service() -> Any:
    from src.search_service import get_search_service

    return get_search_service()


def _fetch_candidate_context_envelope(
    provider: str,
    code: str,
    *,
    stock_name: str = "",
    limit: int = 3,
    market_data_manager: Any | None = None,
) -> DataEnvelope:
    """Fetch one candidate capability and keep status/provenance separate.

    The old implementation returned ``""`` for all of: a legal empty result,
    a blocked provider, a malformed response and an invalid symbol.  This
    boundary preserves the legacy text facade while the pipeline receives a
    ``DataEnvelope``-compatible diagnostic for each requested source.
    """
    provider = _normalize_providers([provider])[0]
    normalized_code = _normalize_code(code)
    if not normalized_code or normalized_code == "000000":
        return _error_envelope(provider, normalized_code or code, ValueError("invalid security code"), status=DataStatus.SYMBOL_INVALID)

    manager = market_data_manager or _get_market_data_manager()
    if provider in {"news", "announcement"}:
        envelope = manager.fetch(
            DataQuery(provider, normalized_code),
            SourcePolicy(timeout_seconds=8.0, allow_stale=True),
        )
        if isinstance(envelope, DataEnvelope) and envelope.capability == provider:
            data = envelope.data
            if isinstance(data, list):
                envelope = DataEnvelope(**{**envelope.__dict__, "data": data[: max(1, int(limit or 1))]})
            return envelope
        # Compatibility for older injected managers while production uses the
        # Market Data capability route above.
        service = _get_search_service()
        if not getattr(service, "is_available", False):
            return _context_envelope(
                provider,
                normalized_code,
                [],
                source="market_data",
                status=DataStatus.UPSTREAM_BLOCKED,
                errors=["market data route unavailable"],
            )
        if provider == "news":
            response = service.search_stock_news(
                normalized_code, stock_name or normalized_code, max_results=max(1, int(limit or 1))
            )
            return _search_response_envelope(provider, normalized_code, response)
        response = service.search_stock_events(normalized_code, stock_name or normalized_code)
        return _search_response_envelope(provider, normalized_code, response, limit=limit)

    if provider == "fund_flow":
        envelope = manager.fetch(
            DataQuery("capital_flow", normalized_code),
            SourcePolicy(timeout_seconds=8.0, allow_stale=True),
        )
        if isinstance(envelope, DataEnvelope) and envelope.capability == "capital_flow":
            return DataEnvelope(**{**envelope.__dict__, "capability": "fund_flow"})
        block = manager.get_capital_flow_context(normalized_code, budget_seconds=8.0)
        return _fundamental_block_envelope(provider, normalized_code, block)

    if provider == "quote":
        fetch = getattr(manager, "fetch", None)
        if callable(fetch):
            envelope = fetch(
                DataQuery("realtime_quote", normalized_code),
                SourcePolicy(timeout_seconds=8.0, allow_stale=True),
            )
            if isinstance(envelope, DataEnvelope):
                return envelope
        quote = manager.get_realtime_quote(normalized_code, log_final_failure=False)
        if quote is None:
            return _context_envelope(
                provider,
                normalized_code,
                {},
                source="market_data",
                status=DataStatus.VALID_EMPTY,
            )
        return _context_envelope(provider, normalized_code, quote, source="market_data")

    return _error_envelope(provider, normalized_code, ValueError("unsupported context provider"))


def _search_response_envelope(
    provider: str,
    code: str,
    response: Any,
    *,
    limit: int | None = None,
) -> DataEnvelope:
    results = list(getattr(response, "results", []) or [])
    if limit is not None:
        results = results[: max(1, int(limit or 1))]
    source = _safe_text(getattr(response, "provider", "")) or "search"
    errors = []
    if not bool(getattr(response, "success", False)):
        errors.append(_safe_text(getattr(response, "error_message", "")) or "search provider failed")
    status = DataStatus.OK if results and bool(getattr(response, "success", False)) else (
        DataStatus.VALID_EMPTY if bool(getattr(response, "success", False)) else DataStatus.UPSTREAM_BLOCKED
    )
    return _context_envelope(
        provider,
        code,
        results,
        source=source,
        status=status,
        errors=errors,
        fallback_chain=[source],
    )


def _fundamental_block_envelope(provider: str, code: str, block: Any) -> DataEnvelope:
    payload = block.get("data", {}) if isinstance(block, Mapping) else {}
    errors = list(block.get("errors", []) or []) if isinstance(block, Mapping) else ["fundamental block malformed"]
    source_chain = _source_chain_names(block.get("source_chain", []) if isinstance(block, Mapping) else [])
    status_text = _safe_text(block.get("status", "")) if isinstance(block, Mapping) else ""
    has_payload = _has_context_value(payload)
    if status_text in {"ok", "partial"} and has_payload:
        status = DataStatus.OK
    elif status_text in {"ok", "partial", "not_supported"} and not has_payload and not errors:
        status = DataStatus.VALID_EMPTY
    else:
        status = DataStatus.UPSTREAM_BLOCKED
    return _context_envelope(
        provider,
        code,
        payload,
        source=source_chain[-1] if source_chain else "fundamental_pipeline",
        status=status,
        errors=errors,
        fallback_chain=source_chain,
        quality_flags=["partial"] if status_text == "partial" else [],
    )


def _context_envelope(
    capability: str,
    code: str,
    data: Any,
    *,
    source: str,
    status: DataStatus = DataStatus.OK,
    source_tier: str = "primary",
    errors: list[Any] | None = None,
    fallback_chain: list[str] | None = None,
    quality_flags: list[str] | None = None,
) -> DataEnvelope:
    details = {"errors": [_safe_text(item) for item in (errors or []) if _safe_text(item)]}
    return DataEnvelope(
        capability=capability,
        security_id=code,
        data=data,
        source=_safe_text(source) or "market_data",
        source_tier=source_tier,
        as_of=None,
        retrieved_at=datetime.now(timezone.utc),
        status=status,
        fallback_chain=[item for item in (fallback_chain or []) if _safe_text(item)],
        quality_flags=[item for item in (quality_flags or []) if _safe_text(item)],
        provenance=details,
    )


def _error_envelope(
    provider: str,
    code: str,
    error: BaseException,
    *,
    status: DataStatus = DataStatus.UPSTREAM_BLOCKED,
) -> DataEnvelope:
    return _context_envelope(
        provider,
        code,
        {},
        source="market_data",
        status=status,
        errors=[f"{type(error).__name__}: {error}"],
    )


def _source_chain_names(values: Any) -> list[str]:
    names: list[str] = []
    for item in values or []:
        value = item.get("provider") if isinstance(item, Mapping) else item
        text = _safe_text(value)
        if text and text not in names:
            names.append(text)
    return names


def _diagnostic_from_envelope(envelope: DataEnvelope) -> dict[str, object]:
    details = envelope.provenance.details if envelope.provenance is not None else {}
    return {
        "status": envelope.status.value,
        "source": envelope.source,
        "source_tier": envelope.source_tier,
        "retrieved_at": envelope.retrieved_at.isoformat(),
        "is_stale": envelope.is_stale,
        "quality_flags": list(envelope.quality_flags),
        "fallback_chain": list(envelope.fallback_chain),
        "errors": list(details.get("errors", [])) if isinstance(details, Mapping) else [],
    }


def _envelope_error(envelope: DataEnvelope) -> str:
    diagnostic = _diagnostic_from_envelope(envelope)
    errors = diagnostic.get("errors") or []
    return _safe_text(errors[0] if errors else diagnostic.get("status")) or "provider unavailable"


def _has_context_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_context_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_context_value(item) for item in value)
    if hasattr(value, "empty"):
        try:
            return not bool(value.empty)
        except (TypeError, ValueError):
            return True
    return True


def _context_value_to_text(provider: str, value: Any) -> str:
    if provider in {"news", "announcement"}:
        items = []
        for item in value if isinstance(value, list) else []:
            title = _safe_text(getattr(item, "title", "") if not isinstance(item, Mapping) else item.get("title"))
            published = _safe_text(
                getattr(item, "published_date", "") if not isinstance(item, Mapping) else item.get("published_date")
            )
            source = _safe_text(getattr(item, "source", "") if not isinstance(item, Mapping) else item.get("source"))
            snippet = _safe_text(getattr(item, "snippet", "") if not isinstance(item, Mapping) else item.get("snippet"), max_len=180)
            text = " ".join(item for item in [published, source, title or snippet] if item)
            if text:
                items.append(text)
        return _compress_text(" | ".join(_dedupe(items)), max_len=520)
    if provider == "fund_flow":
        payload = value.get("stock_flow", value) if isinstance(value, Mapping) else value
        if not isinstance(payload, Mapping):
            return _compress_text(payload, max_len=420)
        fields = []
        for key, item in payload.items():
            value_text = _safe_text(item)
            if value_text:
                fields.append(f"{key}={value_text}")
        return _compress_text("，".join(fields[:8]), max_len=420)
    if provider == "quote":
        payload = value.to_dict() if hasattr(value, "to_dict") and callable(value.to_dict) else value
        if not isinstance(payload, Mapping):
            return _compress_text(payload, max_len=360)
        labels = {
            "name": "名称", "price": "现价", "change_pct": "涨跌幅", "high": "最高",
            "low": "最低", "amount": "成交额", "turnover_rate": "换手率", "pe_ratio": "市盈率",
            "market_cap": "总市值", "float_market_cap": "流通市值",
        }
        fields = [
            f"{labels.get(key, key)}={_safe_text(item)}"
            for key, item in payload.items()
            if key in labels and _safe_text(item)
        ]
        return _compress_text("，".join(fields), max_len=360)
    return _compress_text(value, max_len=520)


def classify_context_events(row: dict[str, object]) -> list[str]:
    """Return coarse event tags from already collected candidate context."""
    text = _row_text(row)
    tags: list[str] = []
    for label, keywords in _POSITIVE_EVENT_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            tags.append(label)
    for label, keywords in _NEGATIVE_EVENT_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            tags.append(f"风险:{label}")
    return _dedupe(tags)


def classify_negative_events(row: dict[str, object]) -> list[str]:
    """Return negative event categories detected in candidate context."""
    text = _row_text(row)
    flags = [
        label
        for label, keywords in _NEGATIVE_EVENT_KEYWORDS.items()
        if any(keyword in text for keyword in keywords)
    ]
    return _dedupe(flags)


def classify_announcement_categories(row: dict[str, object]) -> list[str]:
    """Return coarse announcement categories from candidate context."""
    text = " ".join(
        str(row.get(key) or "")
        for key in ("announcement", "announcements")
        if row.get(key)
    )
    categories = [
        label
        for label, keywords in _ANNOUNCEMENT_CATEGORY_KEYWORDS.items()
        if any(keyword in text for keyword in keywords)
    ]
    return _dedupe(categories)


def _ensure_context_row_enrichment(
    row: dict[str, object],
    *,
    requested_sources: list[str] | None = None,
    successful_sources: list[str] | None = None,
    source_weights: dict[str, float] | None = None,
) -> None:
    successful_sources = successful_sources or _successful_sources_from_row(row)
    requested_sources = requested_sources or successful_sources
    if not isinstance(row.get("source_count"), int):
        row["source_count"] = len(successful_sources)
    if source_weights is not None or not isinstance(row.get("source_weight_score"), (int, float)):
        row["source_weight_score"] = _source_weight_score(
            successful_sources,
            requested_sources,
            source_weights=source_weights,
        )
    if not isinstance(row.get("announcement_categories"), list):
        row["announcement_categories"] = classify_announcement_categories(row)
    if not isinstance(row.get("event_tags"), list):
        row["event_tags"] = classify_context_events(row)
    if not isinstance(row.get("negative_event_flags"), list):
        row["negative_event_flags"] = classify_negative_events(row)
    summary = _safe_text(row.get("context_summary"), max_len=600)
    needs_summary = not summary
    if isinstance(row.get("event_tags"), list) and row["event_tags"] and "事件标签:" not in summary:
        needs_summary = True
    if (
        isinstance(row.get("announcement_categories"), list)
        and row["announcement_categories"]
        and "公告类别:" not in summary
    ):
        needs_summary = True
    if (
        isinstance(row.get("negative_event_flags"), list)
        and row["negative_event_flags"]
        and "负面风险:" not in summary
    ):
        needs_summary = True
    if needs_summary:
        row["context_summary"] = _summarize_row_context(row)


def _market_for_code(code: str) -> str:
    code = _normalize_code(code)
    if code.startswith("6"):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    return ""


def _tencent_symbol_for_code(code: str) -> str:
    code = _normalize_code(code)
    if code.startswith(("6", "5", "9")):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8", "920")):
        return f"bj{code}"
    return ""


def _part(parts: list[str], index: int) -> str:
    return _safe_text(parts[index] if index < len(parts) else "", max_len=80)


def _first_value(row: pd.Series, columns: list[str]) -> str:
    for column in columns:
        if column in row.index:
            value = _safe_text(row.get(column))
            if value:
                return value
    return ""


def _safe_text(value: object, *, max_len: int = 240) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return ""
    return text[:max_len]


def _normalize_code(value: object) -> str:
    text = _safe_text(value, max_len=80)
    if not text:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit():
        return text.zfill(6)[-6:]
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if match:
        return match.group(1)
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def _normalize_providers(providers: list[str]) -> list[str]:
    aliases = {"announcements": "announcement", "fundflow": "fund_flow"}
    result = []
    seen = set()
    for item in providers:
        key = aliases.get(str(item).strip().lower(), str(item).strip().lower())
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _source_confidence(successful_sources: list[str], requested_sources: list[str]) -> float:
    requested = set(requested_sources)
    if not requested:
        return 0.0
    coverage = len(set(successful_sources)) / len(requested)
    return round(min(1.0, max(0.0, coverage)), 4)


def _source_weight_score(
    successful_sources: list[str],
    requested_sources: list[str],
    *,
    source_weights: dict[str, float] | None = None,
) -> float:
    requested = _normalize_providers(requested_sources)
    successful = set(_normalize_providers(successful_sources))
    weights = _normalized_source_weights(source_weights)
    total = sum(weights.get(source, 0.5) for source in requested)
    if total <= 0:
        return 0.0
    score = sum(weights.get(source, 0.5) for source in successful if source in requested) / total
    return round(min(1.0, max(0.0, score)), 4)


def _normalized_source_weights(source_weights: dict[str, float] | None) -> dict[str, float]:
    result = dict(_SOURCE_WEIGHTS)
    for key, value in (source_weights or {}).items():
        normalized = _normalize_providers([str(key)])
        if not normalized:
            continue
        try:
            result[normalized[0]] = max(float(value), 0.0)
        except (TypeError, ValueError):
            continue
    return result


def _summarize_row_context(row: dict[str, object]) -> str:
    parts = []
    for key, label in (
        ("news", "新闻"),
        ("announcement", "公告"),
        ("fund_flow", "资金流"),
        ("quote", "行情估值"),
    ):
        value = _compress_text(row.get(key), max_len=180)
        if value:
            parts.append(f"{label}:{value}")
    event_tags = row.get("event_tags")
    if isinstance(event_tags, list) and event_tags:
        parts.append("事件标签:" + ",".join(str(item) for item in event_tags[:6]))
    announcement_categories = row.get("announcement_categories")
    if isinstance(announcement_categories, list) and announcement_categories:
        parts.append("公告类别:" + ",".join(str(item) for item in announcement_categories[:6]))
    negative_flags = row.get("negative_event_flags")
    if isinstance(negative_flags, list) and negative_flags:
        parts.append("负面风险:" + ",".join(str(item) for item in negative_flags[:6]))
    return _compress_text("；".join(parts), max_len=520)


def _row_text(row: dict[str, object]) -> str:
    fields = []
    for key in ("news", "announcement", "announcements", "fund_flow", "quote", "summary", "context", "text"):
        value = row.get(key)
        if value:
            fields.append(str(value))
    return " ".join(fields)


def _successful_sources_from_row(row: dict[str, object]) -> list[str]:
    diagnostics = row.get("source_diagnostics")
    if isinstance(diagnostics, Mapping):
        routed = [
            str(source)
            for source, item in diagnostics.items()
            if isinstance(item, Mapping) and item.get("status") == DataStatus.OK.value
        ]
        if routed:
            return routed
    sources = []
    if row.get("news"):
        sources.append("news")
    if row.get("announcement") or row.get("announcements"):
        sources.append("announcement")
    if row.get("fund_flow") or row.get("fundflow"):
        sources.append("fund_flow")
    if row.get("quote"):
        sources.append("quote")
    return sources


def _compress_text(value: object, *, max_len: int) -> str:
    text = _safe_text(value, max_len=max(max_len * 2, 240))
    if not text:
        return ""
    text = " ".join(text.replace("\n", " ").split())
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for delimiter in (" | ", "；", "。", "，", " "):
        idx = cut.rfind(delimiter)
        if idx >= max_len * 0.55:
            return cut[:idx].rstrip() + "..."
    return cut.rstrip() + "..."


def _cache_path(cache_dir: str | Path | None, code: str, providers: list[str]) -> Path | None:
    if cache_dir is None:
        return None
    key = "_".join(providers) or "none"
    return Path(cache_dir) / f"{str(code).zfill(6)}_{key}.json"


def _read_cache(
    cache_dir: str | Path | None,
    code: str,
    providers: list[str],
    *,
    cache_ttl_hours: int,
) -> dict[str, object] | None:
    path = _cache_path(cache_dir, code, providers)
    if path is None or not path.is_file() or cache_ttl_hours <= 0:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(str(data.get("cached_at", "")))
        if datetime.now() - cached_at > timedelta(hours=cache_ttl_hours):
            return None
        row = data.get("row")
        return row if isinstance(row, dict) else None
    except Exception:
        return None


def _write_cache(
    cache_dir: str | Path | None,
    code: str,
    providers: list[str],
    row: dict[str, object],
) -> None:
    path = _cache_path(cache_dir, code, providers)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cached_at": datetime.now().isoformat(), "row": row}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result
