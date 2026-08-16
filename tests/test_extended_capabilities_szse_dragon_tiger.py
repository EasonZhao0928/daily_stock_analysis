# -*- coding: utf-8 -*-
"""Tests for SzseDragonTigerSource and its fallback wiring into
ExtendedCapabilityAdapter's default dragon_tiger source chain
(akshare primary, szse backup).
"""

import unittest
from datetime import date
from unittest.mock import patch

from data_provider.extended_capabilities import (
    AkshareExtendedSource,
    CninfoAnnouncementSource,
    ExtendedCapabilityAdapter,
    SinaCapitalFlowSource,
    SzseDragonTigerSource,
)
from data_provider.market_data_types import DataQuery, DataStatus, SourcePolicy


class TestSzseDragonTigerSource(unittest.TestCase):
    def test_non_dragon_tiger_capability_raises_without_network_call(self) -> None:
        source = SzseDragonTigerSource()
        with patch("data_provider.screening_sources.fetch_szse_dragon_tiger") as mocked:
            with self.assertRaises(ValueError):
                source.fetch(DataQuery("capital_flow", "600519.SH"))
        mocked.assert_not_called()

    def test_success_path_returns_raw_rows(self) -> None:
        source = SzseDragonTigerSource()
        rows = [{"date": "2026-08-14", "code": "000582", "name": "北部湾港"}]
        with patch("data_provider.screening_sources.fetch_szse_dragon_tiger", return_value=rows) as mocked:
            result = source.fetch(DataQuery("dragon_tiger", "000582.SZ", as_of=date(2026, 8, 14)))

        self.assertEqual(result, rows)
        mocked.assert_called_once_with(date(2026, 8, 14), timeout=source._TIMEOUT_SECONDS)

    def test_ignores_security_id_uses_as_of_for_trade_date(self) -> None:
        """龙虎榜是"当日全市场"查询，跟现有 AkshareExtendedSource 一样不按标的过滤，
        任何合法 security_id 都应该拿到同一天的市场数据，不是该标的专属数据。"""
        source = SzseDragonTigerSource()
        with patch("data_provider.screening_sources.fetch_szse_dragon_tiger", return_value=[]) as mocked:
            source.fetch(DataQuery("dragon_tiger", "600519.SH", as_of=date(2026, 8, 14)))
        mocked.assert_called_once_with(date(2026, 8, 14), timeout=source._TIMEOUT_SECONDS)

    def test_no_as_of_defaults_to_today(self) -> None:
        source = SzseDragonTigerSource()
        with patch("data_provider.screening_sources.fetch_szse_dragon_tiger", return_value=[]) as mocked:
            source.fetch(DataQuery("dragon_tiger", "600519.SH"))
        mocked.assert_called_once_with(date.today(), timeout=source._TIMEOUT_SECONDS)


class TestExtendedCapabilityAdapterDragonTigerFallback(unittest.TestCase):
    """反例覆盖：主源（akshare）失败必须正确 fallback 到备胎（szse），
    两者都失败必须是 UPSTREAM_BLOCKED 而不是静默空值。
    """

    def test_default_sources_order_has_akshare_before_szse(self) -> None:
        adapter = ExtendedCapabilityAdapter()
        order = list(adapter._sources)
        self.assertLess(order.index("akshare"), order.index("szse"))
        self.assertIsInstance(adapter._sources["szse"], SzseDragonTigerSource)

    def test_akshare_success_short_circuits_szse(self) -> None:
        """dragon_tiger 的默认主源仍是 akshare（东财），不是 szse。"""
        adapter = ExtendedCapabilityAdapter()
        akshare_rows = [{"代码": "000582", "名称": "北部湾港", "上榜日期": "2026-08-14"}]

        with patch.object(AkshareExtendedSource, "fetch", return_value=akshare_rows), patch.object(
            SzseDragonTigerSource, "fetch"
        ) as szse_fetch:
            result = adapter.fetch(DataQuery("dragon_tiger", "000582.SZ"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.OK)
        self.assertEqual(result.source, "akshare")
        szse_fetch.assert_not_called()

    def test_akshare_failure_falls_back_to_szse_success(self) -> None:
        adapter = ExtendedCapabilityAdapter()
        szse_rows = [{"date": "2026-08-14", "code": "000582", "name": "北部湾港"}]

        with patch.object(
            AkshareExtendedSource, "fetch", side_effect=RuntimeError("eastmoney blocked")
        ), patch.object(SzseDragonTigerSource, "fetch", return_value=szse_rows):
            result = adapter.fetch(DataQuery("dragon_tiger", "000582.SZ"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.OK)
        self.assertEqual(result.source, "szse")
        self.assertEqual(result.source_tier, "backup")
        self.assertEqual(result.fallback_chain, ("cninfo", "akshare", "sina"))

    def test_both_akshare_and_szse_failing_reports_upstream_blocked(self) -> None:
        adapter = ExtendedCapabilityAdapter()

        with patch.object(
            AkshareExtendedSource, "fetch", side_effect=RuntimeError("eastmoney blocked")
        ), patch.object(SzseDragonTigerSource, "fetch", side_effect=RuntimeError("szse blocked too")):
            result = adapter.fetch(DataQuery("dragon_tiger", "000582.SZ"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.UPSTREAM_BLOCKED)
        self.assertEqual(result.fallback_chain, ("cninfo", "akshare", "sina", "szse"))

    def test_other_capabilities_unaffected_by_szse_registration(self) -> None:
        adapter = ExtendedCapabilityAdapter()
        cninfo_rows = [{"announcementTitle": "公告", "announcementTime": 1786723200000}]

        with patch.object(CninfoAnnouncementSource, "fetch", return_value=cninfo_rows), patch.object(
            SzseDragonTigerSource, "fetch"
        ) as szse_fetch:
            result = adapter.fetch(DataQuery("announcement", "600519.SH"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.OK)
        self.assertEqual(result.source, "cninfo")
        szse_fetch.assert_not_called()

    def test_capital_flow_unaffected_by_szse_registration(self) -> None:
        adapter = ExtendedCapabilityAdapter()
        sina_rows = [{"date": "2026-08-14", "net_amount": "-647006812.4600"}]

        with patch.object(AkshareExtendedSource, "fetch", side_effect=RuntimeError("blocked")), patch.object(
            SinaCapitalFlowSource, "fetch", return_value=sina_rows
        ), patch.object(SzseDragonTigerSource, "fetch") as szse_fetch:
            result = adapter.fetch(DataQuery("capital_flow", "600519.SH"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.OK)
        self.assertEqual(result.source, "sina")
        szse_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
