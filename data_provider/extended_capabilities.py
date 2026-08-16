# -*- coding: utf-8 -*-
# Reference: a-stock-data SKILL.md (Apache-2.0), independently rewritten for
# DSA; no upstream executable code is copied into this file.
"""Production adapters for the first extended A-share capabilities.

All optional supplier imports and calls live behind this Market Data boundary.
The service layer only sees :class:`DataEnvelope` and therefore cannot confuse
an upstream failure with a legal empty result.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from .evidence_adapters import normalize_evidence_records
from .financial_types import normalize_consensus_estimates, normalize_financial_statement
from .market_data_types import DataEnvelope, DataQuery, DataStatus, SourcePolicy
from .market_evidence import CAPABILITIES as MARKET_EVIDENCE_CAPABILITIES, normalize_market_evidence


EXTENDED_CAPABILITIES = frozenset(
    {
        "financial_statement",
        "consensus_estimate",
        "announcement",
        "news",
        "research_report",
        *MARKET_EVIDENCE_CAPABILITIES,
    }
)


def _records(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return value.to_dict(orient="records")
        except TypeError:
            return value.to_dict()
    return value


def _market_suffix(query: DataQuery) -> str:
    return {"cn": "", "hk": ".HK", "us": ""}.get(query.market or "", "")


class AkshareExtendedSource:
    """Best-effort AkShare source; the package remains an optional dependency."""

    name = "akshare"
    source_tier = "primary"

    @staticmethod
    def _module() -> Any:
        import akshare as ak  # optional dependency, loaded only when requested

        return ak

    @staticmethod
    def _call_first(candidates: Sequence[tuple[str, Mapping[str, Any]]]) -> Any:
        ak = AkshareExtendedSource._module()
        errors: list[Exception] = []
        for name, kwargs in candidates:
            function = getattr(ak, name, None)
            if not callable(function):
                continue
            try:
                return function(**dict(kwargs))
            except Exception as exc:  # provider versions expose different signatures
                errors.append(exc)
        if errors:
            raise errors[-1]
        raise RuntimeError("installed akshare exposes no compatible capability function")

    def fetch(self, query: DataQuery, *, statement_type: str = "", limit: int = 50) -> Any:
        code = query.security_id.canonical_code
        capability = query.capability
        if capability == "financial_statement":
            names = {
                "balance_sheet": ("stock_balance_sheet_by_report_em", "stock_balance_sheet_by_yearly_em"),
                "income_statement": ("stock_profit_sheet_by_report_em", "stock_profit_sheet_by_yearly_em"),
                "cash_flow_statement": ("stock_cash_flow_sheet_by_report_em", "stock_cash_flow_sheet_by_yearly_em"),
            }
            functions = names.get(statement_type, ())
            return self._call_first([(name, {"symbol": code}) for name in functions])
        if capability == "consensus_estimate":
            return self._call_first(
                [
                    ("stock_profit_forecast_ths", {"symbol": code}),
                    ("stock_profit_forecast_em", {"symbol": code}),
                ]
            )
        if capability == "announcement":
            cutoff = query.as_of.date() if isinstance(query.as_of, datetime) else query.as_of
            day = (cutoff or date.today()).strftime("%Y%m%d")
            rows = _records(self._call_first([("stock_notice_report", {"symbol": "全部", "date": day})]))
            if isinstance(rows, list):
                rows = [row for row in rows if str(row.get("代码") or row.get("股票代码") or "").zfill(6) == code]
            return rows
        if capability == "news":
            return self._call_first([("stock_news_em", {"symbol": code})])
        if capability == "research_report":
            return self._call_first([("stock_research_report_em", {"symbol": code})])
        if capability == "capital_flow":
            market = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(query.security_id.exchange or "", "sh")
            return self._call_first([("stock_individual_fund_flow", {"stock": code, "market": market})])
        if capability == "dragon_tiger":
            cutoff = query.as_of.date() if isinstance(query.as_of, datetime) else query.as_of
            day = (cutoff or date.today()).strftime("%Y%m%d")
            return self._call_first([("stock_lhb_detail_em", {"start_date": day, "end_date": day})])
        candidates = {
            "margin": [("stock_margin_detail_szse", {"date": (query.as_of or date.today()).strftime("%Y%m%d")})],
            "block_trade": [("stock_dzjy_mrmx", {"symbol": "A股", "start_date": "", "end_date": ""})],
            "holders": [("stock_zh_a_gdhs_detail_em", {"symbol": code})],
            "unlock": [("stock_restricted_release_detail_em", {"start_date": "", "end_date": ""})],
            "dividend": [("stock_history_dividend_detail", {"symbol": code, "indicator": "分红"})],
        }
        return self._call_first(candidates.get(capability, ()))


class CninfoAnnouncementSource:
    """巨潮资讯网公告直连——announcement 能力的独立备胎。

    与 ``AkshareExtendedSource`` 的东财路径（``stock_notice_report``）走完全不同的
    上游域名与限流面（巨潮 vs 东财），东财被限流时不会一起失效。
    只承接 ``announcement`` 能力；其余能力立即抛出，不发起网络请求，交给列表里的
    下一个 source（通常是 akshare）处理，代价可忽略。
    """

    name = "cninfo"
    source_tier = "official"

    _TIMEOUT_SECONDS = 10.0

    def fetch(self, query: DataQuery, *, statement_type: str = "", **_kwargs: Any) -> Any:
        if query.capability != "announcement":
            raise ValueError(f"CninfoAnnouncementSource does not support capability={query.capability!r}")

        from .screening_sources import cninfo_resolve_org_id, fetch_cninfo_announcements

        exchange = (query.security_id.exchange or "").upper()
        code = query.security_id.canonical_code
        org_id = cninfo_resolve_org_id(code, timeout=self._TIMEOUT_SECONDS)
        if not org_id:
            raise ValueError(f"cninfo could not resolve orgId for {code!r}")
        return fetch_cninfo_announcements(code, org_id, exchange, timeout=self._TIMEOUT_SECONDS)


class SinaCapitalFlowSource:
    """新浪资金流直连——capital_flow 能力的独立备胎（东财主源被限流时降级使用）。

    东财（``stock_individual_fund_flow``）仍是 capital_flow 的默认主源，新浪只在
    东财失败时才被尝试；两者走不同上游域名与限流面。只承接 ``capital_flow`` 能力，
    其余能力立即抛出，不发起网络请求。
    """

    name = "sina"
    source_tier = "backup"

    _TIMEOUT_SECONDS = 10.0

    def fetch(self, query: DataQuery, *, statement_type: str = "", **_kwargs: Any) -> Any:
        if query.capability != "capital_flow":
            raise ValueError(f"SinaCapitalFlowSource does not support capability={query.capability!r}")

        from .screening_sources import fetch_sina_capital_flow

        exchange = (query.security_id.exchange or "").upper()
        code = query.security_id.canonical_code
        return fetch_sina_capital_flow(code, exchange, timeout=self._TIMEOUT_SECONDS)


class SzseDragonTigerSource:
    """深交所官方直连——dragon_tiger 能力的独立备胎（东财主源被限流时降级使用）。

    东财（``stock_lhb_detail_em``）仍是默认主源，深交所只在东财失败时才被尝试。
    龙虎榜本质是"当日全市场上榜列表"而不是按单只标的查询——这一点沿用了现有
    ``AkshareExtendedSource`` 的既有语义（它同样忽略 ``query.security_id``，
    只按 ``query.as_of`` 取当日全部上榜记录）。只覆盖深市，上交所暂无结构化
    公开接口（详见 screening_sources.py 里的说明），只承接 ``dragon_tiger`` 能力，
    其余能力立即抛出，不发起网络请求。
    """

    name = "szse"
    source_tier = "backup"

    _TIMEOUT_SECONDS = 10.0

    def fetch(self, query: DataQuery, *, statement_type: str = "", **_kwargs: Any) -> Any:
        if query.capability != "dragon_tiger":
            raise ValueError(f"SzseDragonTigerSource does not support capability={query.capability!r}")

        from .screening_sources import fetch_szse_dragon_tiger

        cutoff = query.as_of.date() if isinstance(query.as_of, datetime) else query.as_of
        trade_date = cutoff or date.today()
        return fetch_szse_dragon_tiger(trade_date, timeout=self._TIMEOUT_SECONDS)


class ExtendedCapabilityAdapter:
    """Route extended capabilities through independently replaceable sources."""

    def __init__(self, sources: Optional[Mapping[str, Any]] = None) -> None:
        self._sources = dict(
            sources
            or {
                "cninfo": CninfoAnnouncementSource(),
                "akshare": AkshareExtendedSource(),
                "sina": SinaCapitalFlowSource(),
                "szse": SzseDragonTigerSource(),
            }
        )

    def fetch(self, query: DataQuery, policy: SourcePolicy) -> DataEnvelope:
        if query.capability not in EXTENDED_CAPABILITIES:
            raise ValueError(f"unsupported extended capability: {query.capability}")
        order = policy.source_chain or tuple(self._sources)
        attempted: list[str] = []
        last: Optional[DataEnvelope] = None
        for name in order:
            source = self._sources.get(name)
            if source is None:
                continue
            attempted.append(name)
            tier = str(getattr(source, "source_tier", "primary" if len(attempted) == 1 else "backup"))
            statement_type = query.fields[0] if query.fields else "balance_sheet"
            try:
                function: Callable[..., Any] = source.fetch if hasattr(source, "fetch") else source
                payload = function(query, statement_type=statement_type)
                envelope = self._normalize(query, _records(payload), name, tier, attempted, statement_type)
            except Exception as exc:
                envelope = self._blocked(query, name, tier, attempted, type(exc).__name__)
            last = envelope
            if envelope.status in {DataStatus.OK, DataStatus.VALID_EMPTY}:
                return envelope
        return last or self._blocked(query, "unavailable", "derived", attempted, "no_source")

    @staticmethod
    def _normalize(
        query: DataQuery,
        payload: Any,
        source: str,
        tier: str,
        attempted: Sequence[str],
        statement_type: str,
    ) -> DataEnvelope:
        kwargs = dict(
            security_id=query.security_id,
            source=source,
            source_tier=tier,
            fallback_chain=tuple(attempted[:-1]),
        )
        if query.capability == "financial_statement":
            return normalize_financial_statement(payload, statement_type=statement_type, as_of=query.as_of, **kwargs)
        if query.capability == "consensus_estimate":
            return normalize_consensus_estimates(payload, **kwargs)
        if query.capability in {"announcement", "research_report"}:
            cutoff = query.as_of.date() if isinstance(query.as_of, datetime) else query.as_of
            records = normalize_evidence_records(
                payload,
                kind=query.capability,
                code=query.security_id.canonical_code,
                source=source,
                source_tier=tier,
                cutoff=cutoff,
            )
            return DataEnvelope(
                capability=query.capability,
                security_id=query.security_id,
                data=[item.to_dict() for item in records],
                source=source,
                source_tier=tier,
                as_of=query.as_of,
                retrieved_at=datetime.now(timezone.utc),
                status=DataStatus.OK if records else DataStatus.VALID_EMPTY,
                fallback_chain=tuple(attempted[:-1]),
                provenance={"adapter": "evidence_index", "artifact_only": True},
            )
        if query.capability == "news":
            rows = payload if isinstance(payload, list) else []
            bounded = []
            for row in rows[:50]:
                if not isinstance(row, Mapping):
                    continue
                bounded.append(
                    {
                        "title": str(row.get("新闻标题") or row.get("title") or "")[:300],
                        "published_date": str(row.get("发布时间") or row.get("date") or "")[:40],
                        "source": str(row.get("文章来源") or row.get("source") or source)[:80],
                        "snippet": str(row.get("新闻内容") or row.get("content") or "")[:1000],
                        "url": str(row.get("新闻链接") or row.get("url") or "")[:1000],
                    }
                )
            return DataEnvelope(
                capability="news",
                security_id=query.security_id,
                data=bounded,
                source=source,
                source_tier=tier,
                as_of=query.as_of,
                retrieved_at=datetime.now(timezone.utc),
                status=DataStatus.OK if bounded else DataStatus.VALID_EMPTY,
                fallback_chain=tuple(attempted[:-1]),
                provenance={"adapter": "news_index", "bounded": True},
            )
        return normalize_market_evidence(payload, capability=query.capability, **kwargs)

    @staticmethod
    def _blocked(
        query: DataQuery, source: str, tier: str, attempted: Sequence[str], error: str
    ) -> DataEnvelope:
        return DataEnvelope(
            capability=query.capability,
            security_id=query.security_id,
            data=None,
            source=source,
            source_tier=tier,
            as_of=query.as_of,
            retrieved_at=datetime.now(timezone.utc),
            status=DataStatus.UPSTREAM_BLOCKED,
            quality_flags=("upstream_blocked",),
            fallback_chain=tuple(attempted),
            provenance={"adapter": "extended_capability", "error": error},
        )


def extended_market_data_enabled(config: Any = None) -> bool:
    """Return whether the extended A-share capabilities are explicitly enabled.

    These sources are still acceptance-pending, so R10.4 keeps them behind a
    default-off flag.  A configuration failure is treated as "disabled".
    """
    if config is None:
        try:
            from src.config import get_config

            config = get_config()
        except Exception:
            return False
    return bool(getattr(config, "extended_market_data_enabled", False))


__all__ = [
    "AkshareExtendedSource",
    "EXTENDED_CAPABILITIES",
    "ExtendedCapabilityAdapter",
    "extended_market_data_enabled",
]
