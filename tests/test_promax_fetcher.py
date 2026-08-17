# -*- coding: utf-8 -*-
"""
PromaxFetcher offline unit tests.

覆盖网关传输层契约（GET / 路径段接口名 / X-API-Key 头）、失效模式
（非 JSON 响应、5xx 重试、限流）、优先级解析与市场路由边界。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from data_provider.base import DataFetchError, RateLimitError  # noqa: E402
from data_provider.promax_fetcher import (  # noqa: E402
    DEFAULT_PROMAX_BASE_URL,
    PromaxFetcher,
    PromaxHTTPError,
    _PromaxHttpClient,
)


def _json_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = "{}"
    return resp


def _html_response(status_code=200):
    """网关过载时返回 HTML 错误页而非 JSON。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.side_effect = ValueError("Expecting value")
    resp.text = "<!DOCTYPE html><html><title>系统发生错误</title></html>"
    return resp


_OK_PAYLOAD = {
    "code": 0,
    "msg": "",
    "data": {
        "fields": ["ts_code", "trade_date", "close"],
        "items": [["000001.SZ", "20260817", 11.1]],
    },
}


class TestPromaxHttpClientContract(unittest.TestCase):
    """网关调用约定：GET + 路径段接口名 + X-API-Key 头 + query 参数。"""

    def setUp(self):
        self.client = _PromaxHttpClient(api_key="test-key", max_retries=0)

    def test_uses_get_with_api_name_as_path_segment(self):
        with patch("data_provider.promax_fetcher.requests.get") as mock_get:
            mock_get.return_value = _json_response(_OK_PAYLOAD)
            df = self.client.query("daily", ts_code="000001.SZ", start_date="20260801")

        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], f"{DEFAULT_PROMAX_BASE_URL}/daily")
        self.assertEqual(kwargs["headers"], {"X-API-Key": "test-key"})
        self.assertEqual(kwargs["params"]["ts_code"], "000001.SZ")
        self.assertEqual(kwargs["params"]["start_date"], "20260801")
        self.assertEqual(list(df.columns), ["ts_code", "trade_date", "close"])
        self.assertEqual(len(df), 1)

    def test_drops_empty_params_so_gateway_does_not_receive_none(self):
        # TushareFetcher.get_stock_list 传 exchange=''，query string 会把
        # None/'' 编码成字面量，必须在客户端剔除。
        with patch("data_provider.promax_fetcher.requests.get") as mock_get:
            mock_get.return_value = _json_response(_OK_PAYLOAD)
            self.client.query("stock_basic", exchange="", list_status="L", extra=None)

        params = mock_get.call_args.kwargs["params"]
        self.assertNotIn("exchange", params)
        self.assertNotIn("extra", params)
        self.assertEqual(params["list_status"], "L")

    def test_attribute_access_maps_to_api_name(self):
        with patch("data_provider.promax_fetcher.requests.get") as mock_get:
            mock_get.return_value = _json_response(_OK_PAYLOAD)
            self.client.hk_daily(ts_code="00700.HK")

        self.assertEqual(mock_get.call_args.args[0], f"{DEFAULT_PROMAX_BASE_URL}/hk_daily")

    def test_verify_ssl_flag_is_forwarded(self):
        client = _PromaxHttpClient(api_key="k", verify_ssl=True, max_retries=0)
        with patch("data_provider.promax_fetcher.requests.get") as mock_get:
            mock_get.return_value = _json_response(_OK_PAYLOAD)
            client.query("daily")
        self.assertIs(mock_get.call_args.kwargs["verify"], True)


class TestPromaxHttpClientFailureModes(unittest.TestCase):
    """失效模式必须可归因：传输故障计入熔断，业务/解析错误不计入。"""

    def test_non_json_body_becomes_transport_error_with_status(self):
        client = _PromaxHttpClient(api_key="k", max_retries=0)
        with patch("data_provider.promax_fetcher.requests.get") as mock_get:
            mock_get.return_value = _html_response(status_code=200)
            with self.assertRaises(PromaxHTTPError) as ctx:
                client.query("daily")

        # 带 status_code 才会被 supplier_runtime.is_transport_failure 计入熔断
        self.assertEqual(ctx.exception.status_code, 200)
        self.assertIn("非 JSON", str(ctx.exception))

    def test_non_json_error_is_counted_as_transport_failure(self):
        from data_provider.supplier_runtime import is_transport_failure

        self.assertTrue(is_transport_failure(PromaxHTTPError("boom", status_code=503)))

    def test_rate_limit_status_raises_rate_limit_error(self):
        from data_provider.supplier_runtime import is_transport_failure

        client = _PromaxHttpClient(api_key="k", max_retries=0)
        for status in (403, 429):
            with patch("data_provider.promax_fetcher.requests.get") as mock_get:
                mock_get.return_value = _json_response({}, status_code=status)
                with self.assertRaises(RateLimitError) as ctx:
                    client.query("daily")
            # 必须带 status_code，否则 promax 熔断器永远不会推进，
            # 密钥被吊销后每次请求都要白跑一趟网关
            self.assertEqual(ctx.exception.status_code, status)
            self.assertTrue(is_transport_failure(ctx.exception))

    def test_business_error_code_raises_data_fetch_error(self):
        client = _PromaxHttpClient(api_key="k", max_retries=0)
        with patch("data_provider.promax_fetcher.requests.get") as mock_get:
            mock_get.return_value = _json_response({"code": 40001, "msg": "无权限"})
            with self.assertRaises(DataFetchError) as ctx:
                client.query("daily")
        self.assertIn("无权限", str(ctx.exception))


class TestPromaxHttpClientRetry(unittest.TestCase):
    """网关实测存在瞬时读超时与 5xx，有限重试是 P0 数据源的前提。"""

    def test_retries_transient_5xx_then_succeeds(self):
        client = _PromaxHttpClient(api_key="k", max_retries=2)
        responses = [
            _json_response({}, status_code=503),
            _json_response({}, status_code=502),
            _json_response(_OK_PAYLOAD),
        ]
        with patch("data_provider.promax_fetcher.requests.get", side_effect=responses) as mock_get:
            with patch("data_provider.promax_fetcher.time.sleep"):
                df = client.query("daily")

        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(len(df), 1)

    def test_retries_read_timeout_then_succeeds(self):
        import requests

        client = _PromaxHttpClient(api_key="k", max_retries=2)
        side_effect = [requests.ReadTimeout("timed out"), _json_response(_OK_PAYLOAD)]
        with patch("data_provider.promax_fetcher.requests.get", side_effect=side_effect) as mock_get:
            with patch("data_provider.promax_fetcher.time.sleep"):
                df = client.query("daily")

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(len(df), 1)

    def test_gives_up_after_max_retries(self):
        import requests

        client = _PromaxHttpClient(api_key="k", max_retries=1)
        with patch(
            "data_provider.promax_fetcher.requests.get",
            side_effect=requests.ReadTimeout("timed out"),
        ) as mock_get:
            with patch("data_provider.promax_fetcher.time.sleep"):
                with self.assertRaises(PromaxHTTPError):
                    client.query("daily")

        self.assertEqual(mock_get.call_count, 2)

    def test_does_not_retry_rate_limit(self):
        client = _PromaxHttpClient(api_key="k", max_retries=2)
        with patch("data_provider.promax_fetcher.requests.get") as mock_get:
            mock_get.return_value = _json_response({}, status_code=429)
            with self.assertRaises(RateLimitError):
                client.query("daily")

        self.assertEqual(mock_get.call_count, 1)


class _FakeConfig:
    def __init__(self, **overrides):
        self.promax_api_key = "test-key"
        self.promax_base_url = DEFAULT_PROMAX_BASE_URL
        self.promax_priority = -2
        self.promax_verify_ssl = False
        self.promax_rate_limit_per_minute = 200
        self.promax_timeout = 30
        self.promax_max_retries = 2
        self.enable_chip_distribution = True
        self.__dict__.update(overrides)


class TestPromaxFetcherLifecycle(unittest.TestCase):
    def test_available_and_priority_when_key_configured(self):
        with patch("data_provider.promax_fetcher.get_config", return_value=_FakeConfig()):
            fetcher = PromaxFetcher()

        self.assertTrue(fetcher.is_available())
        self.assertEqual(fetcher.priority, -2)
        self.assertEqual(fetcher.name, "PromaxFetcher")

    def test_priority_outranks_tushare_with_token(self):
        # TushareFetcher 配置 Token 后为 -1，Promax 必须严格更小才稳定居首
        from data_provider.promax_fetcher import DEFAULT_PROMAX_PRIORITY

        self.assertLess(DEFAULT_PROMAX_PRIORITY, -1)

    def test_unavailable_without_key_sinks_priority(self):
        with patch(
            "data_provider.promax_fetcher.get_config",
            return_value=_FakeConfig(promax_api_key=""),
        ):
            fetcher = PromaxFetcher()

        self.assertFalse(fetcher.is_available())
        self.assertEqual(fetcher.priority, 99)

    def test_custom_base_url_is_used(self):
        cfg = _FakeConfig(promax_base_url="https://gateway.example.com/pro")
        with patch("data_provider.promax_fetcher.get_config", return_value=cfg):
            fetcher = PromaxFetcher()
            with patch("data_provider.promax_fetcher.requests.get") as mock_get:
                mock_get.return_value = _json_response(_OK_PAYLOAD)
                fetcher._api.query("daily")

        self.assertEqual(mock_get.call_args.args[0], "https://gateway.example.com/pro/daily")

    def test_us_stock_is_rejected_so_chain_falls_back(self):
        with patch("data_provider.promax_fetcher.get_config", return_value=_FakeConfig()):
            fetcher = PromaxFetcher()
            with self.assertRaises(DataFetchError):
                fetcher._fetch_raw_data("AAPL", "20260801", "20260817")

    def _rt_k_frame(self):
        import pandas as pd

        return pd.DataFrame(
            [["600519.SH", "20260817", "贵州茅台", 1300.0, 1295.0, 1310.0, 1290.0, 1293.09, 784295600, 1.01e10]],
            columns=[
                "ts_code", "trade_time", "name", "pre_close", "open",
                "high", "low", "close", "vol", "amount",
            ],
        )

    def test_realtime_quote_reads_rt_k_and_labels_promax(self):
        from data_provider.realtime_types import RealtimeSource

        with patch("data_provider.promax_fetcher.get_config", return_value=_FakeConfig()):
            fetcher = PromaxFetcher()
            with patch.object(fetcher, "_call_api_with_rate_limit", return_value=self._rt_k_frame()) as call:
                with patch.object(fetcher, "_supplement_from_daily_basic"):
                    quote = fetcher.get_realtime_quote("600519")

        self.assertEqual(call.call_args.args[0], "rt_k")
        self.assertEqual(quote.source, RealtimeSource.PROMAX)
        self.assertEqual(quote.name, "贵州茅台")
        self.assertAlmostEqual(quote.price, 1293.09)
        # UnifiedRealtimeQuote.volume 口径是股；rt_k 的 vol 已是股，不得再换算，
        # 否则该值被覆盖到日线 df 后会让量比凭空缩水 100 倍
        self.assertEqual(quote.volume, 784295600)

    def test_realtime_quote_never_falls_back_to_tushare_sdk(self):
        """网关不可用时必须返回 None。

        父类实现会在 Pro 接口失败后 ``import tushare`` 直连新浪，那会绕开网关
        返回数据并被标成 Promax，既掩盖网关故障又让来源统计失真。
        """
        # 用假模块顶掉 sys.modules['tushare']，这样断言不依赖真实 SDK 是否安装，
        # 也不受其他用例对该模块做的 stub 影响。
        fake_tushare = MagicMock()
        with patch("data_provider.promax_fetcher.get_config", return_value=_FakeConfig()):
            fetcher = PromaxFetcher()
            with patch.object(
                fetcher, "_call_api_with_rate_limit",
                side_effect=PromaxHTTPError("gateway down", status_code=503),
            ):
                with patch.dict(sys.modules, {"tushare": fake_tushare}):
                    self.assertIsNone(fetcher.get_realtime_quote("600519"))

        fake_tushare.get_realtime_quotes.assert_not_called()

    def test_realtime_quote_skips_hk(self):
        with patch("data_provider.promax_fetcher.get_config", return_value=_FakeConfig()):
            fetcher = PromaxFetcher()
            with patch.object(fetcher, "_call_api_with_rate_limit") as call:
                self.assertIsNone(fetcher.get_realtime_quote("hk00700"))
        call.assert_not_called()

    def test_market_cap_is_converted_from_wan_yuan_to_yuan(self):
        """daily_basic 市值单位是万元，项目统一用元；差 10000 倍且无下游可纠正。"""
        import pandas as pd

        basic = pd.DataFrame(
            [[3.05, 33.2543, 49.5957, 7.7214, 531456.0, 459963.9576]],
            columns=["volume_ratio", "turnover_rate", "pe", "pb", "total_mv", "circ_mv"],
        )
        with patch("data_provider.promax_fetcher.get_config", return_value=_FakeConfig()):
            fetcher = PromaxFetcher()
            with patch.object(fetcher, "_resolve_settled_trade_date", return_value="20260817"):
                with patch.object(
                    fetcher, "_call_api_with_rate_limit",
                    side_effect=[self._rt_k_frame(), basic],
                ):
                    quote = fetcher.get_realtime_quote("600519")

        self.assertAlmostEqual(quote.total_mv, 531456.0 * 10000)
        self.assertAlmostEqual(quote.circ_mv, 459963.9576 * 10000)
        # 比率类字段不做换算
        self.assertAlmostEqual(quote.volume_ratio, 3.05)
        self.assertAlmostEqual(quote.turnover_rate, 33.2543)

    def test_priority_attribute_is_not_parsed_at_import(self):
        """import 期解析非法环境变量会让整个 data_provider 包崩掉。"""
        with patch.dict(os.environ, {"PROMAX_PRIORITY": "not-an-int"}, clear=False):
            import importlib

            import data_provider.promax_fetcher as module

            importlib.reload(module)
            self.assertEqual(module.PromaxFetcher.priority, module.DEFAULT_PROMAX_PRIORITY)

    def test_amplitude_is_computed_even_when_daily_basic_is_empty(self):
        import pandas as pd

        with patch("data_provider.promax_fetcher.get_config", return_value=_FakeConfig()):
            fetcher = PromaxFetcher()
            with patch.object(fetcher, "_resolve_settled_trade_date", return_value="20260817"):
                with patch.object(
                    fetcher, "_call_api_with_rate_limit",
                    side_effect=[self._rt_k_frame(), pd.DataFrame()],
                ):
                    quote = fetcher.get_realtime_quote("600519")

        # (1310 - 1290) / 1300 * 100
        self.assertAlmostEqual(quote.amplitude, 1.5385, places=3)
        self.assertIsNone(quote.volume_ratio)


class TestPromaxMarketStats(unittest.TestCase):
    """全市场统计：父类的通配符 ts_code 在网关上返回空，必须改走 trade_date。"""

    def _fetcher(self):
        with patch("data_provider.promax_fetcher.get_config", return_value=_FakeConfig()):
            return PromaxFetcher()

    def _daily_frame(self):
        import pandas as pd

        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"],
                "close": [11.0, 9.0, 5.0, 7.0],
                "pre_close": [10.0, 10.0, 5.0, 7.0],
                # amount 单位为千元
                "amount": [1000.0, 2000.0, 3000.0, 0.0],
            }
        )

    def _limit_frame(self):
        import pandas as pd

        return pd.DataFrame({"limit": ["U", "U", "Z", "D"]})

    def test_market_stats_uses_trade_date_not_wildcard(self):
        fetcher = self._fetcher()
        with patch.object(fetcher, "_get_trade_dates", return_value=["20260817"]):
            with patch.object(fetcher, "_resolve_settled_trade_date", return_value="20260817"):
                with patch.object(fetcher, "_get_china_now") as now:
                    now.return_value.strftime.side_effect = lambda fmt: (
                        "20260818" if fmt == "%Y%m%d" else "22:00"
                    )
                    with patch.object(fetcher._api, "query", return_value=self._daily_frame()) as query:
                        with patch.object(fetcher, "_fetch_limit_counts", return_value={}):
                            stats = fetcher.get_market_stats()

        params = query.call_args.kwargs
        self.assertEqual(params.get("trade_date"), "20260817")
        self.assertNotIn("ts_code", params)
        self.assertEqual(stats["up_count"], 1)
        self.assertEqual(stats["down_count"], 1)
        # 成交额为 0 的停牌标的必须排除，否则会被算进平盘
        self.assertEqual(stats["flat_count"], 1)
        self.assertAlmostEqual(stats["total_amount"], 6000.0 * 1000 / 1e8)

    def test_market_stats_returns_none_during_trading_session(self):
        """盘中当日 daily 尚未出数，应让位给实时快照源而不是给空结果。"""
        fetcher = self._fetcher()
        with patch.object(fetcher, "_get_trade_dates", return_value=["20260818"]):
            with patch.object(fetcher, "_get_china_now") as now:
                now.return_value.strftime.side_effect = lambda fmt: (
                    "20260818" if fmt == "%Y%m%d" else "10:30"
                )
                with patch.object(fetcher._api, "query") as query:
                    self.assertIsNone(fetcher.get_market_stats())
        query.assert_not_called()

    def test_limit_counts_from_limit_list_d(self):
        fetcher = self._fetcher()
        with patch.object(fetcher._api, "query", return_value=self._limit_frame()):
            counts = fetcher._fetch_limit_counts("20260817")
        self.assertEqual(counts["limit_up_count"], 2)
        self.assertEqual(counts["limit_down_count"], 1)
        self.assertEqual(counts["broken_board_count"], 1)

    def test_limit_counts_failure_does_not_break_stats(self):
        fetcher = self._fetcher()
        with patch.object(fetcher._api, "query", side_effect=PromaxHTTPError("down", status_code=503)):
            self.assertEqual(fetcher._fetch_limit_counts("20260817"), {})

    def test_full_market_call_does_not_retry(self):
        """重型全市场调用必须快速失败，否则重试会放大成分钟级阻塞。"""
        from data_provider.promax_fetcher import _FULL_MARKET_RETRIES, _FULL_MARKET_TIMEOUT

        self.assertEqual(_FULL_MARKET_RETRIES, 0)
        self.assertLessEqual(_FULL_MARKET_TIMEOUT, 30)

    def test_market_stats_is_cached_per_trade_date(self):
        fetcher = self._fetcher()
        with patch.object(fetcher, "_get_trade_dates", return_value=["20260817"]):
            with patch.object(fetcher, "_resolve_settled_trade_date", return_value="20260817"):
                with patch.object(fetcher, "_get_china_now") as now:
                    now.return_value.strftime.side_effect = lambda fmt: (
                        "20260818" if fmt == "%Y%m%d" else "22:00"
                    )
                    with patch.object(fetcher._api, "query", return_value=self._daily_frame()) as query:
                        with patch.object(fetcher, "_fetch_limit_counts", return_value={}):
                            first = fetcher.get_market_stats()
                            second = fetcher.get_market_stats()

        self.assertEqual(first, second)
        self.assertEqual(query.call_count, 1)


class TestPromaxBoardsAndConcepts(unittest.TestCase):
    def _fetcher(self):
        with patch("data_provider.promax_fetcher.get_config", return_value=_FakeConfig()):
            return PromaxFetcher()

    def test_belong_board_joins_ths_member_with_index_names(self):
        import pandas as pd

        fetcher = self._fetcher()
        member = pd.DataFrame({"ts_code": ["885877.TI", "700001.TI", "883300.TI"]})
        index = pd.DataFrame(
            {
                "ts_code": ["885877.TI", "700001.TI", "883300.TI"],
                "name": ["转基因", "农业指数", "沪深300样本股"],
                "type": ["N", "I", "BB"],
            }
        )
        with patch.object(fetcher._api, "query", side_effect=[member, index]):
            boards = fetcher.get_belong_board("300189")

        names = boards["name"].tolist()
        self.assertIn("转基因", names)
        self.assertIn("农业指数", names)
        # 宽基指数（BB）是噪声，不能进 prompt
        self.assertNotIn("沪深300样本股", names)
        self.assertEqual(boards[boards["name"] == "转基因"]["type"].iloc[0], "概念")

    def test_ths_index_map_is_cached_per_day(self):
        import pandas as pd

        fetcher = self._fetcher()
        index = pd.DataFrame({"ts_code": ["885877.TI"], "name": ["转基因"], "type": ["N"]})
        with patch.object(fetcher._api, "query", return_value=index) as query:
            fetcher._get_ths_index_map()
            fetcher._get_ths_index_map()
        self.assertEqual(query.call_count, 1)

    def test_ths_index_failure_is_cached_so_batches_do_not_retry(self):
        """失败不缓存的话，批量分析里每只股票都会重试这张 60s 超时的大表。"""
        fetcher = self._fetcher()
        with patch.object(
            fetcher._api, "query", side_effect=PromaxHTTPError("down", status_code=503)
        ) as query:
            for _ in range(5):
                self.assertEqual(fetcher._get_ths_index_map(), {})
        self.assertEqual(query.call_count, 1)

    def test_ths_index_failure_cache_expires_sooner_than_success(self):
        """失败只缓存几分钟，避免一次抖动让所属板块整天不可用。"""
        from data_provider.promax_fetcher import _THS_INDEX_FAILURE_TTL, _THS_INDEX_TTL

        self.assertLess(_THS_INDEX_FAILURE_TTL, _THS_INDEX_TTL)
        self.assertLessEqual(_THS_INDEX_FAILURE_TTL, 600)

    def test_concept_rankings_declines_intraday(self):
        """与 get_market_stats 同口径，避免复盘混用当日宽度与昨日概念龙头。"""
        fetcher = self._fetcher()
        with patch.object(fetcher, "_is_intraday", return_value=True):
            with patch.object(fetcher._api, "query") as query:
                self.assertIsNone(fetcher.get_concept_rankings(5))
        query.assert_not_called()

    def test_ths_index_is_called_without_params(self):
        """实测传 exchange 参数会让 ths_index 返回空，必须裸调。"""
        import pandas as pd

        fetcher = self._fetcher()
        index = pd.DataFrame({"ts_code": ["885877.TI"], "name": ["转基因"], "type": ["N"]})
        with patch.object(fetcher._api, "query", return_value=index) as query:
            fetcher._get_ths_index_map()
        self.assertEqual(query.call_args.args[0], "ths_index")
        self.assertNotIn("exchange", query.call_args.kwargs)

    def test_concept_rankings_filter_concept_boards(self):
        import pandas as pd

        fetcher = self._fetcher()
        frame = pd.DataFrame(
            {
                "name": ["高带宽内存", "培育钻石", "银行", "历史新高"],
                "pct_change": [5.73, 5.06, 0.5, -2.19],
                "idx_type": ["概念板块", "概念板块", "行业板块", "概念板块"],
            }
        )
        with patch.object(fetcher, "_resolve_settled_trade_date", return_value="20260817"):
            with patch.object(fetcher._api, "query", return_value=frame):
                top, bottom = fetcher.get_concept_rankings(2)

        self.assertEqual([row["name"] for row in top], ["高带宽内存", "培育钻石"])
        self.assertEqual(bottom[0]["name"], "历史新高")
        # 行业板块归 get_sector_rankings 管，不应混进概念榜
        self.assertNotIn("银行", [row["name"] for row in top + bottom])


class TestPromaxRouting(unittest.TestCase):
    def test_supplier_family_is_isolated_from_tushare(self):
        from data_provider.supplier_runtime import supplier_family_for_name

        self.assertEqual(supplier_family_for_name("PromaxFetcher"), "promax")
        self.assertEqual(supplier_family_for_name("TushareFetcher"), "tushare")

    def test_daily_market_support_excludes_us(self):
        from data_provider.base import DataFetcherManager

        supported = DataFetcherManager._DAILY_MARKET_FETCHER_SUPPORT["PromaxFetcher"]
        self.assertEqual(supported, {"cn", "hk"})

    def test_realtime_priority_injects_promax_first(self):
        from src.config import Config

        env = {"PROMAX_API_KEY": "k", "TUSHARE_TOKEN": "t"}
        with patch.dict(os.environ, env, clear=False):
            with patch.dict(os.environ, {"REALTIME_SOURCE_PRIORITY": ""}, clear=False):
                os.environ.pop("REALTIME_SOURCE_PRIORITY", None)
                resolved = Config._resolve_realtime_source_priority()

        self.assertTrue(resolved.startswith("promax,tushare,"))

    def test_explicit_realtime_priority_is_respected(self):
        from src.config import Config

        with patch.dict(
            os.environ,
            {"PROMAX_API_KEY": "k", "REALTIME_SOURCE_PRIORITY": "tencent,efinance"},
            clear=False,
        ):
            self.assertEqual(Config._resolve_realtime_source_priority(), "tencent,efinance")

    def test_proxy_bypass_covers_gateway_domain(self):
        """配了 HTTP_PROXY 时网关域名必须进 NO_PROXY，否则行情请求走代理超时。"""
        from src.config import Config

        with patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://127.0.0.1:10809", "NO_PROXY": ""},
            clear=False,
        ):
            Config._load_from_env()
            no_proxy = os.environ.get("NO_PROXY", "")

        self.assertIn("mobcvb.cn", no_proxy.split(","))


if __name__ == "__main__":
    unittest.main()
