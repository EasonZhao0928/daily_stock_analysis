# -*- coding: utf-8 -*-
"""Contract tests for the data-provider security identity model."""

from __future__ import annotations

import pytest

from data_provider.security_id import SecurityId, SecurityIdError, parse_security_id


@pytest.mark.parametrize(
    ("raw", "exchange", "code", "asset_type"),
    [
        ("600519", "SH", "600519", "stock"),
        ("SH600519", "SH", "600519", "stock"),
        ("SH.600519", "SH", "600519", "stock"),
        ("600519.SH", "SH", "600519", "stock"),
        ("SS600519", "SH", "600519", "stock"),
        ("600519.SS", "SH", "600519", "stock"),
        ("SZ.000001", "SZ", "000001", "stock"),
        ("000001.SZ", "SZ", "000001", "stock"),
        ("688981", "SH", "688981", "stock"),
        ("300750", "SZ", "300750", "stock"),
    ],
)
def test_security_id_normalizes_shanghai_and_shenzhen_inputs(
    raw: str, exchange: str, code: str, asset_type: str
) -> None:
    security_id = parse_security_id(raw)

    assert security_id.market == "cn"
    assert security_id.exchange == exchange
    assert security_id.code == code
    assert security_id.asset_type == asset_type
    assert security_id.is_reliable is True


@pytest.mark.parametrize(
    "raw",
    [
        "920748",
        "BJ920748",
        "BJ.920748",
        "920748.BJ",
        "430047",
        "830799",
        "838163",
        "870001",
    ],
)
def test_security_id_routes_new_and_legacy_bse_codes_to_bj(raw: str) -> None:
    security_id = parse_security_id(raw)

    assert security_id.market == "cn"
    assert security_id.exchange == "BJ"
    assert security_id.asset_type == "stock"
    assert security_id.code.isdigit()
    assert security_id.code.startswith(("43", "83", "87", "92"))
    assert security_id.is_reliable is True


def test_security_id_does_not_treat_shanghai_b_share_as_bse() -> None:
    security_id = parse_security_id("900901")

    assert security_id.exchange == "SH"
    assert security_id.exchange != "BJ"


@pytest.mark.parametrize(
    ("raw", "exchange"),
    [
        ("510300", "SH"),
        ("SH510300", "SH"),
        ("510300.SH", "SH"),
        ("588000", "SH"),
        ("501018", "SH"),
        ("159915", "SZ"),
        ("SZ159915", "SZ"),
        ("159915.SZ", "SZ"),
        ("184000", "SZ"),
    ],
)
def test_security_id_classifies_a_share_etfs_without_losing_exchange(
    raw: str, exchange: str
) -> None:
    security_id = parse_security_id(raw)

    assert security_id.market == "cn"
    assert security_id.exchange == exchange
    assert security_id.asset_type == "etf"
    assert security_id.is_etf is True
    assert security_id.is_reliable is True


@pytest.mark.parametrize(
    ("raw", "exchange", "code"),
    [
        ("SH000001", "SH", "000001"),
        ("SH.000001", "SH", "000001"),
        ("000001.SH", "SH", "000001"),
        ("SZ399001", "SZ", "399001"),
        ("399001.SZ", "SZ", "399001"),
        ("SH000300", "SH", "000300"),
    ],
)
def test_explicit_exchange_disambiguates_known_index_codes(
    raw: str, exchange: str, code: str
) -> None:
    security_id = parse_security_id(raw)

    assert security_id.market == "cn"
    assert security_id.exchange == exchange
    assert security_id.code == code
    assert security_id.asset_type == "index"
    assert security_id.is_index is True
    assert security_id.is_ambiguous is False


@pytest.mark.parametrize(
    ("raw", "default_exchange"),
    [("000001", "SZ"), ("000300", "SZ"), ("399001", "SZ")],
)
def test_bare_index_code_keeps_stock_compatibility_and_surfaces_ambiguity(
    raw: str, default_exchange: str
) -> None:
    security_id = parse_security_id(raw)

    assert security_id.exchange == default_exchange
    assert security_id.code == raw
    assert security_id.asset_type == "stock"
    assert security_id.is_ambiguous is True
    assert set(security_id.possible_asset_types) == {"stock", "index"}
    assert security_id.is_reliable is False
    with pytest.raises(SecurityIdError, match="ambiguous"):
        security_id.require_reliable()


def test_asset_type_hint_can_resolve_bare_index_code() -> None:
    security_id = SecurityId.from_input("000300", asset_type="index")

    assert security_id.exchange == "SH"
    assert security_id.code == "000300"
    assert security_id.asset_type == "index"
    assert security_id.is_reliable is True


@pytest.mark.parametrize(
    ("raw", "market", "exchange", "code"),
    [
        ("HK00700", "hk", "HK", "00700"),
        ("1810.HK", "hk", "HK", "01810"),
        ("AAPL", "us", None, "AAPL"),
        ("SPX", "us", None, "SPX"),
        ("7203.T", "jp", None, "7203.T"),
        ("005930.KS", "kr", None, "005930.KS"),
        ("2330.TW", "tw", None, "2330.TW"),
    ],
)
def test_security_id_keeps_existing_non_cn_input_forms(
    raw: str, market: str, exchange: str | None, code: str
) -> None:
    security_id = parse_security_id(raw)

    assert security_id.market == market
    assert security_id.exchange == exchange
    assert security_id.code == code


def test_known_us_index_is_not_misclassified_as_a_stock() -> None:
    security_id = parse_security_id("SPX")

    assert security_id.market == "us"
    assert security_id.asset_type == "index"
    assert security_id.is_index is True


@pytest.mark.parametrize("raw", ["", "SH.6005", "BJ.600519", "not-a-security"])
def test_security_id_rejects_unreliable_identity_inputs(raw: str) -> None:
    with pytest.raises(SecurityIdError):
        parse_security_id(raw)
