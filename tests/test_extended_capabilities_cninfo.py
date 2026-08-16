# -*- coding: utf-8 -*-
"""Tests for CninfoAnnouncementSource and its fallback wiring into
ExtendedCapabilityAdapter's default announcement source chain.
"""

import unittest
from unittest.mock import patch

from data_provider.extended_capabilities import (
    AkshareExtendedSource,
    CninfoAnnouncementSource,
    ExtendedCapabilityAdapter,
)
from data_provider.market_data_types import DataQuery, DataStatus, SourcePolicy


class TestCninfoAnnouncementSource(unittest.TestCase):
    def test_non_announcement_capability_raises_without_network_call(self) -> None:
        source = CninfoAnnouncementSource()
        with patch("data_provider.screening_sources.cninfo_resolve_org_id") as mocked_resolve:
            with self.assertRaises(ValueError):
                source.fetch(DataQuery("capital_flow", "600519.SH"))
        mocked_resolve.assert_not_called()

    def test_success_path_returns_raw_announcement_rows(self) -> None:
        source = CninfoAnnouncementSource()
        rows = [{"announcementTitle": "示例公告", "announcementTime": 1786723200000}]
        with patch(
            "data_provider.screening_sources.cninfo_resolve_org_id", return_value="gssh0600519"
        ) as mocked_resolve, patch(
            "data_provider.screening_sources.fetch_cninfo_announcements", return_value=rows
        ) as mocked_fetch:
            result = source.fetch(DataQuery("announcement", "600519.SH"))

        self.assertEqual(result, rows)
        mocked_resolve.assert_called_once_with("600519", timeout=source._TIMEOUT_SECONDS)
        mocked_fetch.assert_called_once_with("600519", "gssh0600519", "SH", timeout=source._TIMEOUT_SECONDS)

    def test_org_id_not_found_raises(self) -> None:
        source = CninfoAnnouncementSource()
        with patch("data_provider.screening_sources.cninfo_resolve_org_id", return_value=None):
            with self.assertRaises(ValueError):
                source.fetch(DataQuery("announcement", "600519.SH"))


class TestExtendedCapabilityAdapterAnnouncementFallback(unittest.TestCase):
    """反例覆盖：主源（cninfo）失败必须正确 fallback 到备胎（akshare），
    两者都失败必须是 UPSTREAM_BLOCKED 而不是静默空值——不能只测 happy path。
    """

    def test_default_sources_try_cninfo_before_akshare(self) -> None:
        adapter = ExtendedCapabilityAdapter()
        order = list(adapter._sources)
        self.assertLess(order.index("cninfo"), order.index("akshare"))
        self.assertIsInstance(adapter._sources["cninfo"], CninfoAnnouncementSource)
        self.assertIsInstance(adapter._sources["akshare"], AkshareExtendedSource)

    def test_cninfo_failure_falls_back_to_akshare_success(self) -> None:
        adapter = ExtendedCapabilityAdapter()
        akshare_rows = [{"公告标题": "东财公告", "公告日期": "2026-08-14"}]

        with patch.object(
            CninfoAnnouncementSource, "fetch", side_effect=RuntimeError("cninfo unavailable")
        ), patch.object(AkshareExtendedSource, "fetch", return_value=akshare_rows):
            result = adapter.fetch(DataQuery("announcement", "600519.SH"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.OK)
        self.assertEqual(result.source, "akshare")
        self.assertEqual(result.fallback_chain, ("cninfo",))

    def test_cninfo_success_short_circuits_akshare(self) -> None:
        adapter = ExtendedCapabilityAdapter()
        cninfo_rows = [{"announcementTitle": "巨潮公告", "announcementTime": 1786723200000}]

        with patch.object(CninfoAnnouncementSource, "fetch", return_value=cninfo_rows), patch.object(
            AkshareExtendedSource, "fetch"
        ) as akshare_fetch:
            result = adapter.fetch(DataQuery("announcement", "600519.SH"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.OK)
        self.assertEqual(result.source, "cninfo")
        self.assertEqual(result.source_tier, "official")
        self.assertEqual(result.fallback_chain, ())
        akshare_fetch.assert_not_called()

    def test_both_sources_failing_reports_upstream_blocked_not_silent_empty(self) -> None:
        adapter = ExtendedCapabilityAdapter()

        with patch.object(
            CninfoAnnouncementSource, "fetch", side_effect=RuntimeError("cninfo down")
        ), patch.object(AkshareExtendedSource, "fetch", side_effect=RuntimeError("akshare down too")):
            result = adapter.fetch(DataQuery("announcement", "600519.SH"), SourcePolicy())

        self.assertEqual(result.status, DataStatus.UPSTREAM_BLOCKED)
        # sina/szse 也在默认 sources 里，但它们分别只承接 capital_flow/dragon_tiger，
        # 对 announcement 会立即 raise 并出现在 fallback_chain 末尾——这也是一种
        # "零网络成本跳过"的验证。
        self.assertEqual(result.fallback_chain, ("cninfo", "akshare", "sina", "szse"))

    def test_other_capabilities_unaffected_by_cninfo_registration(self) -> None:
        """financial_statement 等其余 11 项能力不应该因为新增 cninfo 而改变最终结果，
        只是 fallback_chain 里会多一条 cninfo 的即时失败记录。
        """
        adapter = ExtendedCapabilityAdapter()
        payload = [{"报告期": "2025-12-31", "营业收入": "10亿元"}]

        with patch.object(AkshareExtendedSource, "fetch", return_value=payload):
            result = adapter.fetch(
                DataQuery("financial_statement", "600519.SH", fields=("income_statement",)), SourcePolicy()
            )

        self.assertEqual(result.status, DataStatus.OK)
        self.assertEqual(result.source, "akshare")
        self.assertEqual(result.fallback_chain, ("cninfo",))


if __name__ == "__main__":
    unittest.main()
