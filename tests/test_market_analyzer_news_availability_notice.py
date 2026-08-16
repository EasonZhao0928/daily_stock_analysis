# -*- coding: utf-8 -*-
"""Tests for MarketAnalyzer's deterministic "news unavailable" notice.

When a market review run gets zero total news results (SearXNG + CLS telegraph
both empty/unconfigured), the reader should not be left guessing whether the
report's commentary is backed by real news — the report itself must say so,
without relying on the LLM to volunteer that information.
"""

import unittest
from unittest.mock import MagicMock, patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.market_analyzer import MarketAnalyzer, MarketOverview, SearchResult  # noqa: E402

NOTICE_ZH = "本次未获取到有效新闻资讯"
NOTICE_EN = "No news results were retrieved for this run"


def _news_item() -> SearchResult:
    return SearchResult(title="标题", snippet="内容", url="", source="财联社", published_date="2026-08-16")


class TestApplyNewsAvailabilityNotice(unittest.TestCase):
    def test_empty_news_prepends_zh_notice(self) -> None:
        analyzer = MarketAnalyzer(search_service=None, region="cn")
        result = analyzer._apply_news_availability_notice("## 报告正文", [])
        self.assertTrue(result.startswith("> ⚠️"))
        self.assertIn(NOTICE_ZH, result)
        self.assertIn("## 报告正文", result)

    def test_non_empty_news_leaves_review_untouched(self) -> None:
        analyzer = MarketAnalyzer(search_service=None, region="cn")
        result = analyzer._apply_news_availability_notice("## 报告正文", [_news_item()])
        self.assertEqual(result, "## 报告正文")
        self.assertNotIn(NOTICE_ZH, result)

    def test_none_news_is_treated_as_empty(self) -> None:
        analyzer = MarketAnalyzer(search_service=None, region="cn")
        result = analyzer._apply_news_availability_notice("## 报告正文", None)
        self.assertIn(NOTICE_ZH, result)

    def test_english_region_uses_english_notice(self) -> None:
        with patch("src.market_analyzer.get_config", return_value=MagicMock(report_language="en")):
            analyzer = MarketAnalyzer(search_service=None, region="us")
        result = analyzer._apply_news_availability_notice("## Report body", [])
        self.assertIn(NOTICE_EN, result)
        self.assertNotIn(NOTICE_ZH, result)


class TestGenerateMarketReviewNoticeWiring(unittest.TestCase):
    """End-to-end (template fallback path, no LLM call) verification that the
    notice actually reaches the final report text callers/readers see."""

    def test_template_fallback_with_empty_news_includes_notice(self) -> None:
        analyzer = MarketAnalyzer(search_service=None, analyzer=None, region="cn")
        overview = MarketOverview(date="2026-08-16")

        report = analyzer.generate_market_review(overview, [])

        self.assertIn(NOTICE_ZH, report)
        self.assertTrue(report.startswith("> ⚠️"))

    def test_template_fallback_with_news_omits_notice(self) -> None:
        analyzer = MarketAnalyzer(search_service=None, analyzer=None, region="cn")
        overview = MarketOverview(date="2026-08-16")

        report = analyzer.generate_market_review(overview, [_news_item()])

        self.assertNotIn(NOTICE_ZH, report)

    def test_llm_path_with_empty_news_includes_notice(self) -> None:
        mock_analyzer = MagicMock()
        mock_analyzer.is_available.return_value = True
        mock_analyzer.generate_text.return_value = "## 2026-08-16 大盘复盘\n\n### 一、盘面总览\n内容"
        mock_analyzer.get_generation_call_metadata.return_value = {}

        analyzer = MarketAnalyzer(search_service=None, analyzer=mock_analyzer, region="cn")
        overview = MarketOverview(date="2026-08-16")

        with patch.object(analyzer, "_get_analyzer_generation_backend_config_error", return_value=None):
            report = analyzer.generate_market_review(overview, [])

        self.assertIn(NOTICE_ZH, report)
        self.assertTrue(report.startswith("> ⚠️"))


if __name__ == "__main__":
    unittest.main()
