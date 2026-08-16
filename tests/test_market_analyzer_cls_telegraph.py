# -*- coding: utf-8 -*-
"""Tests for MarketAnalyzer.search_market_news's CLS telegraph supplement."""

import unittest
from unittest.mock import MagicMock, patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.market_analyzer import MarketAnalyzer, SearchResult  # noqa: E402


def _cls_item(title: str = "示例标题") -> dict:
    return {
        "title": title,
        "snippet": "示例内容",
        "source": "财联社",
        "published_date": "2026-08-16 14:00:00",
        "url": "",
    }


class TestSearchMarketNewsClsTelegraph(unittest.TestCase):
    def test_cls_telegraph_runs_even_without_search_service(self) -> None:
        """CLS 是零 key 的独立数据源，不应该被"没配置 search_service"挡住。"""
        analyzer = MarketAnalyzer(search_service=None, region="cn")

        with patch(
            "src.market_analyzer.fetch_cls_telegraph", return_value=[_cls_item(), _cls_item("第二条")]
        ) as mocked:
            news = analyzer.search_market_news()

        mocked.assert_called_once_with(limit=5)
        self.assertEqual(len(news), 2)
        self.assertIsInstance(news[0], SearchResult)
        self.assertEqual(news[0].source, "财联社")
        self.assertEqual(news[0].title, "示例标题")

    def test_cls_telegraph_supplements_search_service_results_not_replaces(self) -> None:
        mock_search_service = MagicMock()
        mock_search_service.search_stock_news.return_value = MagicMock(
            results=[SearchResult(title="搜索结果", snippet="s", url="u", source="SearXNG")]
        )
        analyzer = MarketAnalyzer(search_service=mock_search_service, region="cn")
        analyzer.profile.news_queries = ["query1"]

        with patch("src.market_analyzer.fetch_cls_telegraph", return_value=[_cls_item()]):
            news = analyzer.search_market_news()

        sources = {n.source for n in news}
        self.assertEqual(sources, {"SearXNG", "财联社"})

    def test_cls_telegraph_skipped_for_non_cn_region(self) -> None:
        with patch("src.market_analyzer.get_config", return_value=MagicMock(report_language="en")):
            analyzer = MarketAnalyzer(search_service=None, region="us")

        with patch("src.market_analyzer.fetch_cls_telegraph") as mocked:
            news = analyzer.search_market_news()

        mocked.assert_not_called()
        self.assertEqual(news, [])

    def test_cls_telegraph_empty_result_does_not_break_existing_news(self) -> None:
        mock_search_service = MagicMock()
        mock_search_service.search_stock_news.return_value = MagicMock(
            results=[SearchResult(title="搜索结果", snippet="s", url="u", source="SearXNG")]
        )
        analyzer = MarketAnalyzer(search_service=mock_search_service, region="cn")
        analyzer.profile.news_queries = ["query1"]

        with patch("src.market_analyzer.fetch_cls_telegraph", return_value=[]):
            news = analyzer.search_market_news()

        self.assertEqual(len(news), 1)
        self.assertEqual(news[0].source, "SearXNG")

    def test_cls_telegraph_exception_is_fail_open(self) -> None:
        """财联社异常时，已经拿到的 SearXNG 结果不能被吞掉——反例：不能整体抛出/清空。"""
        mock_search_service = MagicMock()
        mock_search_service.search_stock_news.return_value = MagicMock(
            results=[SearchResult(title="搜索结果", snippet="s", url="u", source="SearXNG")]
        )
        analyzer = MarketAnalyzer(search_service=mock_search_service, region="cn")
        analyzer.profile.news_queries = ["query1"]

        with patch("src.market_analyzer.fetch_cls_telegraph", side_effect=RuntimeError("boom")):
            news = analyzer.search_market_news()  # 不应该抛出

        self.assertEqual(len(news), 1)
        self.assertEqual(news[0].source, "SearXNG")


if __name__ == "__main__":
    unittest.main()
