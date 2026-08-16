# -*- coding: utf-8 -*-
"""Tests for the CLS (财联社) telegraph adapter."""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from data_provider.cls_telegraph import fetch_cls_telegraph


class TestFetchClsTelegraph(unittest.TestCase):
    def test_success_normalizes_rows(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "标题": "示例标题",
                    "内容": "示例内容正文",
                    "发布日期": "2026-08-16",
                    "发布时间": "14:01:30",
                },
                {
                    "标题": "第二条",
                    "内容": "第二条内容",
                    "发布日期": "2026-08-16",
                    "发布时间": "14:05:00",
                },
            ]
        )
        with patch("akshare.stock_info_global_cls", return_value=df):
            items = fetch_cls_telegraph(limit=10)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "示例标题")
        self.assertEqual(items[0]["snippet"], "示例内容正文")
        self.assertEqual(items[0]["source"], "财联社")
        self.assertEqual(items[0]["published_date"], "2026-08-16 14:01:30")
        self.assertEqual(items[0]["url"], "")

    def test_respects_limit(self) -> None:
        df = pd.DataFrame(
            [{"标题": f"标题{i}", "内容": "内容", "发布日期": "2026-08-16", "发布时间": "10:00:00"} for i in range(20)]
        )
        with patch("akshare.stock_info_global_cls", return_value=df):
            items = fetch_cls_telegraph(limit=3)
        self.assertEqual(len(items), 3)

    def test_zero_or_negative_limit_returns_empty_without_calling_akshare(self) -> None:
        with patch("akshare.stock_info_global_cls") as mocked:
            self.assertEqual(fetch_cls_telegraph(limit=0), [])
            self.assertEqual(fetch_cls_telegraph(limit=-1), [])
        mocked.assert_not_called()

    def test_empty_dataframe_returns_empty_list(self) -> None:
        with patch("akshare.stock_info_global_cls", return_value=pd.DataFrame()):
            items = fetch_cls_telegraph()
        self.assertEqual(items, [])

    def test_none_result_returns_empty_list(self) -> None:
        with patch("akshare.stock_info_global_cls", return_value=None):
            items = fetch_cls_telegraph()
        self.assertEqual(items, [])

    def test_upstream_exception_is_fail_open(self) -> None:
        with patch("akshare.stock_info_global_cls", side_effect=RuntimeError("boom")):
            items = fetch_cls_telegraph()
        self.assertEqual(items, [])

    def test_rows_missing_title_and_content_are_skipped(self) -> None:
        df = pd.DataFrame(
            [
                {"标题": "", "内容": "", "发布日期": "2026-08-16", "发布时间": "10:00:00"},
                {"标题": "有效标题", "内容": "有效内容", "发布日期": "2026-08-16", "发布时间": "10:01:00"},
            ]
        )
        with patch("akshare.stock_info_global_cls", return_value=df):
            items = fetch_cls_telegraph()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "有效标题")


if __name__ == "__main__":
    unittest.main()
