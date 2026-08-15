# -*- coding: utf-8 -*-
"""Market-aware security identity and code normalization.

The module is deliberately independent from provider implementations.  It
provides the small identity value object needed by the Market Data contract;
existing provider-facing helpers remain compatible and are not migrated here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .us_index_mapping import US_INDEX_MAPPING


CN_MARKET = "cn"
HK_MARKET = "hk"
US_MARKET = "us"
STOCK = "stock"
INDEX = "index"
ETF = "etf"

# 81/82/88 are retained because the existing provider helper recognizes the
# historical BSE special-instrument ranges.  R2.3 specifically needs 43/83/87
# and the new 92xxxx range in addition to those legacy forms.
BSE_CODE_PREFIXES = ("43", "81", "82", "83", "87", "88", "92")
ETF_SH_PREFIXES = ("51", "52", "56", "58")
ETF_SZ_PREFIXES = ("15", "16", "18")
ETF_PREFIXES = ETF_SH_PREFIXES + ETF_SZ_PREFIXES

# Explicit exchange input is required to distinguish these known index codes
# from a bare stock-compatible lookup (for example 000001.SH vs 000001.SZ).
CN_INDEX_EXCHANGES = {
    "000001": "SH",
    "000016": "SH",
    "000300": "SH",
    "000688": "SH",
    "399001": "SZ",
    "399006": "SZ",
}

_CN_EXCHANGES = {"SH", "SZ", "BJ"}
_CN_PREFIXES = ("SH", "SS", "SZ", "BJ")
_CN_SUFFIXES = ("SH", "SS", "SZ", "BJ")
_US_TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")
_SUFFIX_MARKETS = {
    "T": ("jp", (4, 5)),
    "KS": ("kr", (6,)),
    "KQ": ("kr", (6,)),
    "TW": ("tw", (4, 5, 6)),
    "TWO": ("tw", (4, 5, 6)),
}


class SecurityIdError(ValueError):
    """Raised when a code cannot be converted to a reliable identity."""


def _is_bse_numeric(code: str) -> bool:
    return (
        len(code) == 6
        and code.isdigit()
        and not code.startswith("900")
        and code.startswith(BSE_CODE_PREFIXES)
    )


def _is_etf_numeric(code: str) -> bool:
    return len(code) == 6 and code.isdigit() and code.startswith(ETF_PREFIXES)


def _etf_exchange(code: str) -> str:
    if code.startswith(ETF_SH_PREFIXES):
        return "SH"
    if code.startswith(ETF_SZ_PREFIXES):
        return "SZ"
    return ""


def _infer_cn_exchange(code: str) -> Optional[str]:
    if not (len(code) == 6 and code.isdigit()):
        return None
    if _is_bse_numeric(code):
        return "BJ"
    if _is_etf_numeric(code):
        return _etf_exchange(code)
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("0", "2", "3")):
        return "SZ"
    return None


def _split_explicit_exchange(text: str) -> Optional[tuple[str, str]]:
    """Return ``(exchange, base)`` for supported prefix/suffix forms."""

    for suffix in _CN_SUFFIXES:
        marker = f".{suffix}"
        if text.endswith(marker):
            exchange = "SH" if suffix == "SS" else suffix
            return exchange, text[: -len(marker)]
    for prefix in _CN_PREFIXES:
        dotted = f"{prefix}."
        if text.startswith(dotted):
            exchange = "SH" if prefix == "SS" else prefix
            return exchange, text[len(dotted):]
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            exchange = "SH" if prefix == "SS" else prefix
            return exchange, text[len(prefix):]
    if text.endswith(".HK"):
        return "HK", text[:-3]
    if text.startswith("HK."):
        return "HK", text[3:]
    if text.startswith("HK") and text[2:].isdigit():
        return "HK", text[2:]
    return None


def is_bse_code(value: str) -> bool:
    """Return whether *value* is a recognized Beijing exchange code."""

    text = str(value or "").strip().upper()
    explicit = _split_explicit_exchange(text)
    if explicit is not None:
        exchange, text = explicit
        return exchange == "BJ" and _is_bse_numeric(text)
    return _is_bse_numeric(text)


def is_a_share_etf_code(value: str) -> bool:
    """Return whether *value* is an A-share ETF code with a valid exchange."""

    text = str(value or "").strip().upper()
    explicit = _split_explicit_exchange(text)
    if explicit is None:
        return _is_etf_numeric(text)
    exchange, text = explicit
    return exchange in _CN_EXCHANGES and _is_etf_numeric(text) and _etf_exchange(text) == exchange


def infer_a_share_exchange(value: str) -> Optional[str]:
    """Infer ``SH``, ``SZ`` or ``BJ`` from a six-digit code."""

    text = str(value or "").strip().upper()
    explicit = _split_explicit_exchange(text)
    if explicit is not None:
        exchange, text = explicit
        if exchange not in _CN_EXCHANGES:
            return None
    return _infer_cn_exchange(text)


def _normalize_asset_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized == "fund":
        normalized = ETF
    if normalized not in {STOCK, INDEX, ETF}:
        raise SecurityIdError(f"unsupported asset_type: {value!r}")
    return normalized


def _normalize_market_hint(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if value is None:
        return None, None
    normalized = str(value).strip().upper()
    if normalized in {"A", "A股", "A-SHARE", "A_SHARE", "CN"}:
        return CN_MARKET, None
    if normalized in _CN_EXCHANGES or normalized == "SS":
        return CN_MARKET, "SH" if normalized == "SS" else normalized
    market = normalized.lower()
    if market in {HK_MARKET, US_MARKET, "jp", "kr", "tw"}:
        return market, None
    raise SecurityIdError(f"unsupported market hint: {value!r}")


def _validate_market_hint(
    market_hint: Optional[str], market: str, exchange: Optional[str]
) -> None:
    hinted_market, hinted_exchange = _normalize_market_hint(market_hint)
    if hinted_market is not None and hinted_market != market:
        raise SecurityIdError(f"market hint {market_hint!r} conflicts with {market!r}")
    if hinted_exchange is not None and hinted_exchange != exchange:
        raise SecurityIdError(f"exchange hint {market_hint!r} conflicts with {exchange!r}")


def _is_suffix_symbol(text: str) -> bool:
    if "." not in text:
        return False
    suffix = text.rsplit(".", 1)[1]
    return suffix in _SUFFIX_MARKETS or suffix == "US"


@dataclass(frozen=True)
class SecurityId:
    """Canonical market-aware security identity.

    Bare mainland index-shaped codes retain the existing stock-compatible
    default, but expose ``possible_asset_types`` and ``is_reliable=False`` so
    later Market Data aggregation can reject an unresolved identity per R2.10.
    """

    market: str
    code: str
    exchange: Optional[str] = None
    asset_type: str = STOCK
    possible_asset_types: tuple[str, ...] = (STOCK,)
    raw: str = field(default="", compare=False, repr=False)

    @classmethod
    def from_input(
        cls,
        value: str,
        *,
        market_hint: Optional[str] = None,
        exchange_hint: Optional[str] = None,
        asset_type: Optional[str] = None,
        strict: bool = False,
    ) -> "SecurityId":
        """Parse legacy, exchange-prefixed, suffix and bare code forms."""

        raw = str(value or "").strip()
        text = raw.upper()
        if not text:
            raise SecurityIdError("security code is empty")

        requested_type = _normalize_asset_type(asset_type)
        hinted_market, hinted_exchange = _normalize_market_hint(market_hint)
        if exchange_hint is not None:
            exchange = str(exchange_hint).strip().upper()
            exchange = "SH" if exchange == "SS" else exchange
            if exchange not in _CN_EXCHANGES:
                raise SecurityIdError(f"unsupported exchange hint: {exchange_hint!r}")
            if hinted_exchange is not None and hinted_exchange != exchange:
                raise SecurityIdError("market_hint and exchange_hint conflict")
            hinted_market, hinted_exchange = CN_MARKET, exchange

        explicit = _split_explicit_exchange(text)
        if explicit is not None:
            exchange, base = explicit
            result = (
                cls._from_hk(base, raw=raw, requested_type=requested_type)
                if exchange == "HK"
                else cls._from_cn(
                    base,
                    raw=raw,
                    explicit_exchange=exchange,
                    requested_type=requested_type,
                )
            )
        elif _is_suffix_symbol(text):
            result = cls._from_suffix(text, raw=raw, requested_type=requested_type)
        elif text.isdigit():
            result = cls._from_numeric(
                text,
                raw=raw,
                requested_type=requested_type,
                market_hint=hinted_market,
                exchange_hint=hinted_exchange,
            )
        else:
            result = cls._from_us(text, raw=raw, requested_type=requested_type, market_hint=hinted_market)

        _validate_market_hint(market_hint, result.market, result.exchange)
        if exchange_hint is not None and result.exchange != hinted_exchange:
            raise SecurityIdError(f"exchange hint {exchange_hint!r} conflicts with {result.exchange!r}")
        if strict:
            result.require_reliable()
        return result

    @classmethod
    def parse(cls, value: str, **kwargs) -> "SecurityId":
        return cls.from_input(value, **kwargs)

    @classmethod
    def from_code(cls, value: str, **kwargs) -> "SecurityId":
        return cls.from_input(value, **kwargs)

    @classmethod
    def _from_numeric(
        cls,
        text: str,
        *,
        raw: str,
        requested_type: Optional[str],
        market_hint: Optional[str],
        exchange_hint: Optional[str],
    ) -> "SecurityId":
        if market_hint == "jp" and len(text) in {4, 5}:
            return cls._from_suffix(f"{text}.T", raw=raw, requested_type=requested_type)
        if market_hint == "kr" and len(text) == 6:
            return cls._from_suffix(f"{text}.KS", raw=raw, requested_type=requested_type)
        if market_hint in {"jp", "kr", "tw"}:
            raise SecurityIdError(f"numeric code {raw!r} conflicts with market {market_hint!r}")
        if len(text) == 6 and market_hint != HK_MARKET:
            return cls._from_cn(
                text,
                raw=raw,
                explicit_exchange=exchange_hint,
                requested_type=requested_type,
            )
        if market_hint == CN_MARKET:
            raise SecurityIdError(f"A-share code must contain six digits: {raw!r}")
        if len(text) not in {4, 5}:
            raise SecurityIdError(f"invalid Hong Kong code: {raw!r}")
        return cls._from_hk(text, raw=raw, requested_type=requested_type)

    @classmethod
    def _from_cn(
        cls,
        base: str,
        *,
        raw: str,
        explicit_exchange: Optional[str],
        requested_type: Optional[str],
    ) -> "SecurityId":
        if not (base.isdigit() and len(base) == 6):
            raise SecurityIdError(f"invalid A-share code: {raw!r}")
        inferred = _infer_cn_exchange(base)
        if inferred is None:
            raise SecurityIdError(f"cannot infer A-share exchange: {raw!r}")

        known_index = CN_INDEX_EXCHANGES.get(base)
        exchange = explicit_exchange or (
            known_index if requested_type == INDEX and known_index is not None else inferred
        )
        if explicit_exchange is not None and exchange != inferred and known_index != exchange:
            raise SecurityIdError(f"code {base!r} does not belong to exchange {exchange!r}")
        if exchange == "BJ" and not _is_bse_numeric(base):
            raise SecurityIdError(f"code {base!r} is not a BSE code")

        etf_exchange = _etf_exchange(base) if _is_etf_numeric(base) else None
        if etf_exchange is not None and exchange != etf_exchange:
            raise SecurityIdError(f"ETF code {base!r} does not belong to exchange {exchange!r}")
        if requested_type == ETF and etf_exchange is None:
            raise SecurityIdError(f"code {base!r} is not an A-share ETF")
        if requested_type == STOCK and etf_exchange is not None:
            raise SecurityIdError(f"code {base!r} is an A-share ETF")
        if requested_type == INDEX and known_index is None and explicit_exchange is None:
            raise SecurityIdError(f"bare code {base!r} has no known index exchange")

        if requested_type is not None:
            resolved_type, possible = requested_type, (requested_type,)
        elif etf_exchange is not None:
            resolved_type, possible = ETF, (ETF,)
        elif explicit_exchange is not None and known_index == exchange:
            resolved_type, possible = INDEX, (INDEX,)
        elif explicit_exchange is not None:
            resolved_type, possible = STOCK, (STOCK,)
        elif known_index is not None:
            resolved_type, possible = STOCK, (STOCK, INDEX)
        else:
            resolved_type, possible = STOCK, (STOCK,)
        return cls(CN_MARKET, base, exchange, resolved_type, possible, raw)

    @classmethod
    def _from_hk(cls, base: str, *, raw: str, requested_type: Optional[str]) -> "SecurityId":
        if not (base.isdigit() and 1 <= len(base) <= 5):
            raise SecurityIdError(f"invalid Hong Kong code: {raw!r}")
        if requested_type not in {None, STOCK}:
            raise SecurityIdError(f"Hong Kong code cannot be typed as {requested_type!r}")
        return cls(HK_MARKET, base.zfill(5), "HK", STOCK, (STOCK,), raw)

    @classmethod
    def _from_suffix(cls, text: str, *, raw: str, requested_type: Optional[str]) -> "SecurityId":
        base, suffix = text.rsplit(".", 1)
        if suffix == "US" and _US_TICKER_RE.fullmatch(base):
            return cls._from_us(base, raw=raw, requested_type=requested_type, market_hint=US_MARKET)
        spec = _SUFFIX_MARKETS.get(suffix)
        if spec is None or not (base.isdigit() and len(base) in spec[1]):
            raise SecurityIdError(f"invalid market symbol: {raw!r}")
        if requested_type not in {None, STOCK}:
            raise SecurityIdError(f"{spec[0]} symbol cannot be typed as {requested_type!r}")
        return cls(spec[0], f"{base}.{suffix}", None, STOCK, (STOCK,), raw)

    @classmethod
    def _from_us(
        cls,
        text: str,
        *,
        raw: str,
        requested_type: Optional[str],
        market_hint: Optional[str],
    ) -> "SecurityId":
        if market_hint not in {None, US_MARKET} or not (text.startswith("^") or _US_TICKER_RE.fullmatch(text)):
            raise SecurityIdError(f"unsupported security code: {raw!r}")
        is_index = text in US_INDEX_MAPPING
        if requested_type == ETF or (requested_type == INDEX and not is_index):
            raise SecurityIdError(f"invalid US asset type for {raw!r}")
        resolved = INDEX if is_index else (requested_type or STOCK)
        return cls(US_MARKET, text, None, resolved, (resolved,), raw)

    @property
    def is_stock(self) -> bool:
        return self.asset_type == STOCK

    @property
    def is_index(self) -> bool:
        return self.asset_type == INDEX

    @property
    def is_etf(self) -> bool:
        return self.asset_type == ETF

    @property
    def is_ambiguous(self) -> bool:
        return len(self.possible_asset_types) > 1

    @property
    def is_reliable(self) -> bool:
        return not self.is_ambiguous and self.asset_type in self.possible_asset_types

    @property
    def canonical_code(self) -> str:
        return self.code

    @property
    def canonical_id(self) -> str:
        if self.market == CN_MARKET and self.exchange:
            return f"{self.exchange.lower()}{self.code}"
        if self.market == HK_MARKET:
            return f"HK{self.code}"
        return self.code

    @property
    def qualified_code(self) -> str:
        if self.market == CN_MARKET and self.exchange:
            return f"{self.code}.{self.exchange}"
        if self.market == HK_MARKET:
            return f"{self.code}.HK"
        return self.code

    def require_reliable(self) -> "SecurityId":
        if not self.is_reliable:
            raise SecurityIdError(f"security identity is ambiguous: {self.raw or self.canonical_id!r}")
        return self

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "exchange": self.exchange,
            "code": self.code,
            "asset_type": self.asset_type,
            "canonical_id": self.canonical_id,
            "is_ambiguous": self.is_ambiguous,
        }

    def __str__(self) -> str:
        return self.canonical_id


def parse_security_id(value: str, **kwargs) -> SecurityId:
    return SecurityId.from_input(value, **kwargs)


def normalize_security_id(value: str, **kwargs) -> SecurityId:
    return parse_security_id(value, **kwargs)


__all__ = [
    "BSE_CODE_PREFIXES",
    "CN_INDEX_EXCHANGES",
    "ETF_PREFIXES",
    "SecurityId",
    "SecurityIdError",
    "infer_a_share_exchange",
    "is_a_share_etf_code",
    "is_bse_code",
    "normalize_security_id",
    "parse_security_id",
]
