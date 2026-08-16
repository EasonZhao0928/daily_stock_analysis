# -*- coding: utf-8 -*-
"""Tests for SinaCapitalFlowSource and its fallback wiring into
ExtendedCapabilityAdapter's default capital_flow source chain
(akshare primary, sina backup).
"""

import unittest
from unittest.mock import patch

from data_provider.extended_capabilities import (
    AkshareExtendedSource,
    CninfoAnnouncementSource,
    ExtendedCapabilityAdapter,
    SinaCapitalFlowSource,
)
from data_provider.market_data_types import DataQuery, DataStatus, SourcePolicy


class TestSinaCapitalFlowSource(unittest.TestCase):
    def test_non_capital_flow_capability_raises_without_network_call(self) -> None:
        source = SinaCapitalFlowSource()
        with patch("data_provider.screening_sources.fetch_sina_capital_flow") as mocked:
            with self.assertRaises(ValueError):
                source.fetch(DataQuery("announcement", "600519.SH"))
        mocked.assert_not_called()

    def test_success_path_returns_raw_rows(self) -> None:
        source = SinaCapitalFlowSource()
        rows = [{"date": "2026-08-14", "net_amount": "-647006812.4600"}]
        with patch("data_provider.screening_sources.fetch_sina_capital_flow", return_value=rows) as mocked:
            result = source.fetch(DataQuery("capital_flow", "600519.SH"))

        self.assertEqual(result, rows)
        mocked.assert_called_once_with("600519", "SH", timeout=source._TIMEOUT_SECONDS)


class TestExtendedCapabilityAdapterCapitalFlowFallback(unittest.TestCase):
    """反例覆盖：主源（akshare）失败必须正确 fallback 到备胎（sina），
    两者都失败必须是 UPSTREAM_BLOCKED 而不是静默空值。
    """

    def test_default_sources_order_has_akshare_before_sina(self) -> None:
        adapter = ExtendedCapabilityAdapter()
        order = list(adapter._sources)
        self.assertLess(order.index("akshare"), order.index("sina"))
        self.assertIsInstance(adapter._sources["sina"], SinaCapitalFlowSource)

    def test_akshare_success_short_circuits_sina(self) -> None:
        """capital_flow 的默认主源仍是 akshare（东财），不是 sina。"""
        adapter = ExtendedCapabilityAdapter()
        akshare_rows = [{"日期": "2026-08-14", "主力净流入": "1.2亿元"}]

        with patch.object(AkshareExtendedSource, "fetch", return_value=akshare_rows), patch.object(
            SinaCapitalFlowSource, "fetch"
        ) as sina_fetch:
            result = adapter.fetch(DataQuery("capital_flow", "600519.SH"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.OK)
        self.assertEqual(result.source, "akshare")
        sina_fetch.assert_not_called()

    def test_akshare_failure_falls_back_to_sina_success(self) -> None:
        adapter = ExtendedCapabilityAdapter()
        sina_rows = [{"date": "2026-08-14", "net_amount": "-647006812.4600"}]

        with patch.object(
            AkshareExtendedSource, "fetch", side_effect=RuntimeError("eastmoney blocked")
        ), patch.object(SinaCapitalFlowSource, "fetch", return_value=sina_rows):
            result = adapter.fetch(DataQuery("capital_flow", "600519.SH"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.OK)
        self.assertEqual(result.source, "sina")
        self.assertEqual(result.source_tier, "backup")
        self.assertEqual(result.fallback_chain, ("cninfo", "akshare"))

    def test_both_akshare_and_sina_failing_reports_upstream_blocked(self) -> None:
        adapter = ExtendedCapabilityAdapter()

        with patch.object(
            AkshareExtendedSource, "fetch", side_effect=RuntimeError("eastmoney blocked")
        ), patch.object(SinaCapitalFlowSource, "fetch", side_effect=RuntimeError("sina blocked too")):
            result = adapter.fetch(DataQuery("capital_flow", "600519.SH"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.UPSTREAM_BLOCKED)
        # szse 也在默认 sources 里，但只承接 dragon_tiger，对 capital_flow 立即
        # raise 并出现在 fallback_chain 末尾。
        self.assertEqual(result.fallback_chain, ("cninfo", "akshare", "sina", "szse"))

    def test_announcement_capability_unaffected_by_sina_registration(self) -> None:
        """新增 sina 不应该改变 announcement 的行为，cninfo 依然是主源。"""
        adapter = ExtendedCapabilityAdapter()
        cninfo_rows = [{"announcementTitle": "公告", "announcementTime": 1786723200000}]

        with patch.object(CninfoAnnouncementSource, "fetch", return_value=cninfo_rows), patch.object(
            SinaCapitalFlowSource, "fetch"
        ) as sina_fetch:
            result = adapter.fetch(DataQuery("announcement", "600519.SH"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.OK)
        self.assertEqual(result.source, "cninfo")
        sina_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
