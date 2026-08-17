# -*- coding: utf-8 -*-
"""
===================================
PromaxFetcher - Promax 聚合网关（最高优先级）
===================================

数据来源：Promax Tushare 聚合网关（默认 ``https://pcd.mobcvb.cn/tushare/pro``）

与 Tushare Pro 官方接口的差异**只在传输层**：

| 维度   | Tushare 官方                       | Promax 网关                     |
| ------ | ---------------------------------- | ------------------------------- |
| 方法   | ``POST``                           | ``GET``                         |
| 接口名 | body 中的 ``api_name`` 字段        | URL 路径段 ``/pro/<接口名>``    |
| 鉴权   | body 中的 ``token`` 字段           | 请求头 ``X-API-Key``            |
| 参数   | body 中的 ``params`` 对象          | query string                    |
| 响应体 | ``{code,msg,data:{fields,items}}`` | **完全相同**                    |

因为响应体结构一致，本类直接继承 :class:`TushareFetcher`，复用其全部代码
转换、字段标准化、单位缩放、筹码计算与大盘统计逻辑，只替换 HTTP client、
凭证来源、优先级与限流参数。

已知能力边界（接入前实测）：

- A 股 / ETF / 港股日线、指数、每日指标、资金流、财务指标均可用；
- ``rt_k`` / ``rt_min`` 实时行情**仅覆盖 A 股**，美股返回空；
- ``us_daily`` 传 ``start_date`` / ``end_date`` 会稳定触发网关 5xx。

因此美股不走本数据源——父类 ``_fetch_raw_data`` / ``_convert_stock_code``
已对美股代码抛 ``DataFetchError``，本类继承该行为，美股会自动回落到
YfinanceFetcher / FinnhubFetcher / LongbridgeFetcher。
"""

import logging
import time
from typing import Optional, Tuple

import pandas as pd
import requests

from .base import DataFetchError, RateLimitError, _is_hk_market, normalize_stock_code
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int
from .tushare_fetcher import TushareFetcher
from src.config import get_config

logger = logging.getLogger(__name__)


DEFAULT_PROMAX_BASE_URL = "https://pcd.mobcvb.cn/tushare/pro"
# 比 TushareFetcher 配置 Token 时的 -1 更小，确保稳定排在所有数据源之前。
DEFAULT_PROMAX_PRIORITY = -2
# 网关未公布配额，此值为保守默认，可用 PROMAX_RATE_LIMIT_PER_MINUTE 调整。
DEFAULT_PROMAX_RATE_LIMIT = 200
DEFAULT_PROMAX_TIMEOUT = 30
# 网关实测存在瞬时读超时与 5xx，轻量接口偶尔需要第 4 次尝试才成功
# （实测 ths_member 第 4 次、daily(trade_date) 第 2 次），因此默认给 3 次重试。
DEFAULT_PROMAX_MAX_RETRIES = 3
# 全市场重型接口必须少重试：一次失败叠加重试会被放大成几分钟阻塞，
# 而它们本来就有下游数据源兜底，早失败早让位反而更快。
_HEAVY_TIMEOUT = 60
# 全市场 daily 约 460KB，网关耗时实测在 6s~93s 之间剧烈波动，偶尔直接超时。
# 下游 AkShare 兜底约 40s，所以等待超过 30s 就不划算：这里不重试、超时即让位，
# 把最坏情况压到 30s，而常见的 6~17s 快路径仍然能吃到。
_FULL_MARKET_RETRIES = 0
_FULL_MARKET_TIMEOUT = 30
# ths_index 的 type 取值：N-概念 I-行业 TH-主题 R-地域 S-特色 ST-风格 BB-宽基。
# 只有概念/行业/主题对个股分析有意义；宽基与风格指数（"同花顺全A(加权)"这类）
# 会把 52 个板块灌进 prompt，稀释真正的题材信号。
_USEFUL_THS_BOARD_TYPES = {"N": "概念", "I": "行业", "TH": "主题"}
# 板块代码→名称映射基本静态，缓存一天；失败只缓存几分钟，避免瞬时抖动导致
# 该能力整天不可用，同时又不让批量分析反复重试这张 60s 超时的大表。
_THS_INDEX_TTL = 24 * 3600.0
_THS_INDEX_FAILURE_TTL = 300.0
_RETRYABLE_STATUS = (500, 502, 503, 504)
# 数据源不可用时沉到队尾，避免占用最高优先级却每次都失败。
_UNAVAILABLE_PRIORITY = 99


class PromaxHTTPError(DataFetchError):
    """网关返回非 200，或返回了无法解析为 JSON 的响应体。

    携带 ``status_code``，使 ``supplier_runtime.is_transport_failure`` 能把它
    识别为传输层故障并推进熔断器；解析类错误则不应计入熔断。
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _PromaxHttpClient:
    """Promax 聚合网关的 GET 客户端，暴露与 ``_TushareHttpClient`` 相同的接口。

    ``query()`` 与 ``__getattr__`` 的签名和返回类型都与 Tushare 客户端一致，
    因此 :class:`TushareFetcher` 中所有 ``self._api.<接口名>(**kwargs)`` 调用
    无需改动即可切换到本网关。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_PROMAX_BASE_URL,
        timeout: int = DEFAULT_PROMAX_TIMEOUT,
        verify_ssl: bool = False,
        max_retries: int = DEFAULT_PROMAX_MAX_RETRIES,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or DEFAULT_PROMAX_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        self._max_retries = max(0, int(max_retries))
        if not verify_ssl:
            # 关闭校验后 urllib3 会对每次请求告警，批量拉取时会淹没日志。
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:  # pragma: no cover - urllib3 缺失时不影响主流程
                pass

    def query(
        self,
        api_name: str,
        fields: str = "",
        *,
        _retries: Optional[int] = None,
        _timeout: Optional[int] = None,
        **kwargs,
    ) -> pd.DataFrame:
        """调用一个网关接口。

        ``_retries`` / ``_timeout`` 用于按调用分级：轻量单标的接口可以多重试
        几次抹平瞬时 503，全市场重型接口必须少重试，否则一次失败会被放大成
        几分钟的阻塞。下划线前缀避免与 Tushare 业务参数名冲突。
        """
        # 网关按 query string 传参，None / 空串会被原样编码成 "None"，需剔除。
        params = {key: value for key, value in kwargs.items() if value is not None and value != ""}
        if fields:
            params["fields"] = fields

        url = f"{self._base_url}/{api_name}"
        res = self._get_with_retry(url, params, api_name, retries=_retries, timeout=_timeout)

        if res.status_code in (403, 429):
            # 必须带 status_code：supplier_runtime.is_transport_failure 靠它把这类
            # 失败计入 promax 熔断，否则密钥被吊销后每次请求都要白跑一趟网关
            error = RateLimitError(f"Promax {api_name} 触发限流/鉴权失败 (HTTP {res.status_code})")
            error.status_code = res.status_code
            raise error
        if res.status_code != 200:
            raise PromaxHTTPError(
                f"Promax {api_name} 返回 HTTP {res.status_code}", status_code=res.status_code
            )

        # 网关过载时会返回 HTML 错误页而非 JSON，直接 .json() 会抛 ValueError，
        # 那属于解析错误、不会推进熔断，因此这里显式转成传输层错误。
        try:
            result = res.json()
        except ValueError as exc:
            snippet = (res.text or "")[:120].replace("\n", " ")
            raise PromaxHTTPError(
                f"Promax {api_name} 返回非 JSON 响应: {snippet!r}", status_code=res.status_code
            ) from exc

        if result.get("code") != 0:
            raise DataFetchError(
                result.get("msg") or f"Promax {api_name} 业务返回码 {result.get('code')}"
            )

        data = result.get("data") or {}
        columns = data.get("fields") or []
        items = data.get("items") or []
        return pd.DataFrame(items, columns=columns)

    def _get_with_retry(
        self,
        url: str,
        params: dict,
        api_name: str,
        *,
        retries: Optional[int] = None,
        timeout: Optional[int] = None,
    ):
        """发起 GET，并对瞬时读超时 / 5xx 做有限重试。

        只重试传输层抖动：``RequestException`` 与 5xx。403/429（限流鉴权）和
        业务返回码交由调用方处理，重试它们只会加重网关负担。
        """
        max_retries = self._max_retries if retries is None else max(0, int(retries))
        request_timeout = self._timeout if timeout is None else int(timeout)
        last_error: Optional[BaseException] = None
        for attempt in range(max_retries + 1):
            try:
                res = requests.get(
                    url,
                    params=params,
                    headers={"X-API-Key": self._api_key},
                    timeout=request_timeout,
                    verify=self._verify_ssl,
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                if res.status_code not in _RETRYABLE_STATUS or attempt == max_retries:
                    return res
                last_error = None
                logger.debug(
                    "Promax %s 返回 HTTP %s，第 %s 次重试", api_name, res.status_code, attempt + 1
                )

            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
                if last_error is not None:
                    logger.debug(
                        "Promax %s 传输失败(%s)，第 %s 次重试",
                        api_name, type(last_error).__name__, attempt + 1,
                    )

        raise PromaxHTTPError(f"Promax 请求 {api_name} 失败: {last_error}") from last_error

    def __getattr__(self, api_name: str):
        if api_name.startswith("_"):
            raise AttributeError(api_name)

        def caller(**kwargs) -> pd.DataFrame:
            return self.query(api_name, **kwargs)

        return caller


class PromaxFetcher(TushareFetcher):
    """Promax 聚合网关数据源，复用 TushareFetcher 的全部数据处理逻辑。"""

    name = "PromaxFetcher"
    # 类属性仅作占位，实例化时由 _determine_priority 依据 Config 解析后覆盖。
    # 不在此处读环境变量：import 期解析一个非法值会让 `import data_provider`
    # 直接崩掉，进而拖垮所有入口，而 Config 对同一变量是带告警的宽容解析。
    priority = DEFAULT_PROMAX_PRIORITY

    def __init__(self, rate_limit_per_minute: Optional[int] = None):
        if rate_limit_per_minute is None:
            config = get_config()
            rate_limit_per_minute = getattr(
                config, "promax_rate_limit_per_minute", DEFAULT_PROMAX_RATE_LIMIT
            )
        super().__init__(rate_limit_per_minute=rate_limit_per_minute)

    def _init_api(self) -> None:
        """用 Promax 凭证初始化网关客户端；未配置 Key 时数据源不可用。"""
        config = get_config()
        api_key = (getattr(config, "promax_api_key", None) or "").strip()

        if not api_key:
            logger.warning("Promax API Key 未配置，此数据源不可用")
            self._api = None
            return

        try:
            self._api = self._build_api_client(api_key)
            logger.info("Promax 聚合网关初始化成功: %s", self._resolve_base_url())
        except Exception as exc:
            logger.error("Promax 聚合网关初始化失败: %s", exc)
            self._api = None

    def _build_api_client(self, token: str) -> _PromaxHttpClient:
        """构造 Promax GET 客户端（``token`` 即 ``X-API-Key``）。"""
        config = get_config()
        verify_ssl = bool(getattr(config, "promax_verify_ssl", False))
        if not verify_ssl:
            logger.warning(
                "Promax 已关闭 TLS 证书校验（PROMAX_VERIFY_SSL=false），"
                "X-API-Key 与行情数据存在被中间人窃听的风险；实测网关证书可信，"
                "建议在可控网络下改为 true。"
            )
        return _PromaxHttpClient(
            api_key=token,
            base_url=self._resolve_base_url(),
            timeout=int(getattr(config, "promax_timeout", DEFAULT_PROMAX_TIMEOUT)),
            verify_ssl=verify_ssl,
            max_retries=int(getattr(config, "promax_max_retries", DEFAULT_PROMAX_MAX_RETRIES)),
        )

    @staticmethod
    def _resolve_base_url() -> str:
        config = get_config()
        base_url = (getattr(config, "promax_base_url", None) or "").strip()
        return base_url or DEFAULT_PROMAX_BASE_URL

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """实时行情，走网关 ``rt_k``。

        **不能复用父类实现**：父类先调 ``quotation``（Promax 无此接口，实测
        ReadTimeout），失败后会静默降级到 ``import tushare; ts.get_realtime_quotes()``
        ——那是 Tushare 官方 SDK 直连新浪，完全绕开本网关。继承该行为会导致
        网关不可用时仍返回数据并被标成 Promax，既掩盖故障又让来源统计失真。

        这里只认 ``rt_k``；取不到就返回 None，交给下游数据源兜底。
        """
        if self._api is None:
            return None
        if _is_hk_market(stock_code):
            logger.debug("[Promax] rt_k 不覆盖港股，跳过 %s", stock_code)
            return None

        try:
            ts_code = self._convert_stock_code(stock_code)
        except Exception as exc:
            logger.debug("[Promax] 实时行情代码转换失败 %s: %s", stock_code, exc)
            return None
        # rt_k 只覆盖 A 股
        if not ts_code.endswith((".SH", ".SZ", ".BJ")):
            return None

        try:
            df = self._call_api_with_rate_limit("rt_k", ts_code=ts_code)
        except Exception as exc:
            logger.debug("[Promax] rt_k 获取 %s 失败: %s", stock_code, exc)
            return None

        if df is None or df.empty:
            return None

        row = df.iloc[0]
        price = safe_float(row.get("close"))
        pre_close = safe_float(row.get("pre_close"))
        change_amount = None
        change_pct = None
        if price is not None and pre_close:
            change_amount = round(price - pre_close, 4)
            change_pct = round((price - pre_close) / pre_close * 100, 4)

        quote = UnifiedRealtimeQuote(
            code=normalize_stock_code(stock_code),
            name=str(row.get("name") or ""),
            source=RealtimeSource.PROMAX,
            price=price,
            pre_close=pre_close,
            open_price=safe_float(row.get("open")),
            high=safe_float(row.get("high")),
            low=safe_float(row.get("low")),
            change_amount=change_amount,
            change_pct=change_pct,
            # rt_k 的 vol 已是股，与 UnifiedRealtimeQuote.volume 口径一致，不要换算：
            # 该字段会被覆盖到日线 df 上参与量比计算，转成手会让成交量凭空缩水 100 倍
            volume=safe_int(row.get("vol")),
            amount=safe_float(row.get("amount")),
        )
        self._supplement_from_daily_basic(quote, stock_code)
        return quote

    def _supplement_from_daily_basic(self, quote: UnifiedRealtimeQuote, stock_code: str) -> None:
        """用 ``daily_basic`` 填补 rt_k 缺失的估值/换手指标（best-effort）。"""
        filled = []

        # rt_k 自带 high/low/pre_close，振幅本地即可算出。放在最前面，避免
        # daily_basic 取不到时连这个不依赖外部调用的字段也一起丢掉。
        if getattr(quote, "amplitude", None) is None:
            high, low, pre_close = quote.high, quote.low, quote.pre_close
            if all(v is not None for v in (high, low, pre_close)) and pre_close:
                quote.amplitude = round((float(high) - float(low)) / float(pre_close) * 100, 4)
                filled.append("amplitude")

        missing = [
            field
            for field in ("volume_ratio", "turnover_rate", "pe_ratio", "pb_ratio", "total_mv", "circ_mv")
            if getattr(quote, field, None) is None
        ]
        if not missing or self._api is None:
            return

        try:
            ts_code = self._convert_stock_code(stock_code)
        except Exception:
            return
        # daily_basic 只覆盖 A 股，港股/ETF 调用只会白白消耗一次请求
        if not ts_code.endswith((".SH", ".SZ", ".BJ")):
            return

        # 必须用"数据已出"的交易日：当日盘前/盘中 daily_basic 尚未发布，
        # 传当天只会拿到空结果，指标就永远补不上。
        trade_date = self._resolve_settled_trade_date()
        if not trade_date:
            return

        try:
            df = self._call_api_with_rate_limit("daily_basic", ts_code=ts_code, trade_date=trade_date)
        except Exception as exc:
            logger.debug("[Promax] daily_basic 补充 %s 失败: %s", stock_code, exc)
            return

        if df is None or df.empty:
            return

        row = df.iloc[0]
        # daily_basic 的字段名与 UnifiedRealtimeQuote 不完全同名，且市值单位是万元，
        # 而本项目的 total_mv / circ_mv 统一用元（见 AkshareFetcher 的 亿->元 换算），
        # 所以这里带上换算系数，否则市值会小 10000 倍且没有下游数据源能纠正。
        mapping = {
            "volume_ratio": ("volume_ratio", 1.0),
            "turnover_rate": ("turnover_rate", 1.0),
            "pe_ratio": ("pe", 1.0),
            "pb_ratio": ("pb", 1.0),
            "total_mv": ("total_mv", 10000.0),
            "circ_mv": ("circ_mv", 10000.0),
        }
        for field in missing:
            entry = mapping.get(field)
            if entry is None:
                continue
            source_col, scale = entry
            if source_col not in df.columns:
                continue
            value = row.get(source_col)
            if value is None or pd.isna(value):
                continue
            setattr(quote, field, float(value) * scale)
            filled.append(field)

        if filled:
            logger.debug("[Promax] daily_basic 补齐 %s 字段: %s", stock_code, filled)

    def get_market_stats(self) -> Optional[dict]:
        """全市场涨跌统计。

        父类实现用 ``ts_code='3*.SZ,6*.SH,...'`` 通配符筛全市场，Promax 网关
        不支持该语法（实测静默返回 0 行），随后还要调 ``stock_basic`` 取股票名
        判断 ST——那正是网关偶发卡死 60s+ 的调用。

        这里改用两个接口直接拿：``daily(trade_date=...)`` 给全市场收盘/昨收/
        成交额，``limit_list_d`` 直接给涨停(U)/跌停(D)/炸板(Z) 名单，因此完全
        不需要股票名，也就不需要 ``stock_basic``。实测 ~9.6s，且与 AkShare
        链路的涨跌家数、涨跌停数、成交额逐项吻合。

        盘中（09:30–16:30）当日 daily 尚未出数，直接返回 None 让位给能取实时
        快照的数据源，避免占着最高优先级却给出空结果。
        """
        if self._api is None:
            return None

        if self._is_intraday():
            logger.info("[Promax] 盘中不提供全市场统计（daily 尚未出数），让位给实时快照源")
            return None

        trade_date = self._resolve_settled_trade_date()
        if not trade_date:
            return None

        # 已收盘交易日的全市场统计是不变量，且这是本数据源最贵的一次调用
        # （实测 6s~99s），按交易日缓存可让当天后续调用零成本。
        cached = getattr(self, "_market_stats_cache", None)
        if cached and cached[0] == trade_date:
            logger.debug("[Promax] 命中 %s 全市场统计缓存", trade_date)
            return dict(cached[1])

        try:
            self._check_rate_limit()
            df = self._api.query(
                "daily",
                _retries=_FULL_MARKET_RETRIES,
                _timeout=_FULL_MARKET_TIMEOUT,
                trade_date=trade_date,
            )
        except Exception as exc:
            logger.warning("[Promax] 全市场 daily(%s) 获取失败: %s", trade_date, exc)
            return None

        if df is None or df.empty:
            logger.warning("[Promax] 全市场 daily(%s) 返回空", trade_date)
            return None

        df = df.copy()
        df.columns = [str(col).lower() for col in df.columns]
        for column in ("close", "pre_close", "amount"):
            if column not in df.columns:
                logger.warning("[Promax] 全市场 daily 缺少 %s 列，放弃统计", column)
                return None
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df = df.dropna(subset=["close", "pre_close"])
        # 停牌标的成交额为 0，计入会把"平盘"数量算大
        df = df[df["amount"] > 0]
        if df.empty:
            return None

        stats = {
            "up_count": int((df["close"] > df["pre_close"]).sum()),
            "down_count": int((df["close"] < df["pre_close"]).sum()),
            "flat_count": int((df["close"] == df["pre_close"]).sum()),
            "limit_up_count": 0,
            "limit_down_count": 0,
            # daily 的 amount 单位是千元，先转元再折算成亿元，与其他数据源对齐
            "total_amount": float(df["amount"].sum() * 1000 / 1e8),
        }
        stats.update(self._fetch_limit_counts(trade_date))
        self._market_stats_cache = (trade_date, dict(stats))
        return stats

    def _fetch_limit_counts(self, trade_date: str) -> dict:
        """用 ``limit_list_d`` 取涨跌停家数；失败时保持 0 而不是拖垮整体统计。"""
        try:
            self._check_rate_limit()
            df = self._api.query("limit_list_d", trade_date=trade_date)
        except Exception as exc:
            logger.warning("[Promax] limit_list_d(%s) 获取失败，涨跌停计 0: %s", trade_date, exc)
            return {}

        if df is None or df.empty or "limit" not in df.columns:
            return {}

        flags = df["limit"].astype(str)
        counts = {
            "limit_up_count": int((flags == "U").sum()),
            "limit_down_count": int((flags == "D").sum()),
        }
        broken = int((flags == "Z").sum())
        if broken:
            counts["broken_board_count"] = broken
        return counts

    def get_concept_rankings(self, n: int = 5) -> Optional[Tuple[list, list]]:
        """概念题材涨跌榜，走 ``dc_index``（东财板块行情）。

        此前该能力在 AkShare / Tencent 上全链路失败，日志里稳定输出
        "[概念排行] 所有数据源均失败"。``dc_index`` 一次返回全部板块的
        涨跌幅，实测 ~4s / 1000+ 行。
        """
        if self._api is None:
            return None

        # 与 get_market_stats 保持同一口径：盘中让位，否则 --market-review 会把
        # 当日大盘宽度和昨日概念龙头混在同一份复盘里。
        if self._is_intraday():
            logger.info("[Promax] 盘中不提供概念排行（dc_index 尚未出数），让位给实时源")
            return None

        trade_date = self._resolve_settled_trade_date()
        if not trade_date:
            return None

        try:
            self._check_rate_limit()
            df = self._api.query("dc_index", trade_date=trade_date)
        except Exception as exc:
            logger.warning("[Promax] dc_index(%s) 获取失败: %s", trade_date, exc)
            return None

        if df is None or df.empty or "name" not in df.columns or "pct_change" not in df.columns:
            return None

        df = df.copy()
        df["pct_change"] = pd.to_numeric(df["pct_change"], errors="coerce")
        df = df.dropna(subset=["pct_change"])
        if "idx_type" in df.columns:
            # 只取概念板块，行业板块由 get_sector_rankings 负责，避免两榜重复
            concept_only = df[df["idx_type"].astype(str) == "概念板块"]
            if not concept_only.empty:
                df = concept_only
        if df.empty:
            return None

        top = [
            {"name": row["name"], "change_pct": float(row["pct_change"])}
            for _, row in df.nlargest(n, "pct_change").iterrows()
        ]
        bottom = [
            {"name": row["name"], "change_pct": float(row["pct_change"])}
            for _, row in df.nsmallest(n, "pct_change").iterrows()
        ]
        return top, bottom

    def get_belong_board(self, stock_code: str) -> Optional[pd.DataFrame]:
        """个股所属板块，走同花顺 ``ths_member`` 反查 + ``ths_index`` 映射名称。

        ``ths_member(con_code=...)`` 只返回板块代码（如 ``885877.TI``），板块
        名称要靠 ``ths_index`` 的全量映射表补。该映射基本静态，按天缓存即可。

        走 THS 而非东财：实测 ``dc_member`` 反查同一标的只有 2 个板块且要
        ~30s，而 THS 路线返回 52 个板块、代码命中率 52/52、耗时约 1s。
        """
        if self._api is None:
            return None

        try:
            ts_code = self._convert_stock_code(stock_code)
        except Exception:
            return None
        if not ts_code.endswith((".SH", ".SZ", ".BJ")):
            return None

        try:
            self._check_rate_limit()
            member = self._api.query("ths_member", con_code=ts_code)
        except Exception as exc:
            logger.warning("[Promax] ths_member(%s) 获取失败: %s", ts_code, exc)
            return None

        if member is None or member.empty or "ts_code" not in member.columns:
            return None

        index_map = self._get_ths_index_map()
        if not index_map:
            return None

        rows = []
        seen = set()
        for board_code in member["ts_code"].astype(str):
            entry = index_map.get(board_code)
            if entry is None:
                continue
            name, board_type = entry
            if not name or name in seen:
                continue
            label = _USEFUL_THS_BOARD_TYPES.get(board_type)
            if label is None:
                continue
            seen.add(name)
            rows.append({"code": board_code, "name": name, "type": label})

        if not rows:
            return None
        return pd.DataFrame(rows)

    def _get_ths_index_map(self) -> dict:
        """同花顺板块代码→(名称, 类型) 映射，带成功/失败双 TTL 缓存。

        失败也必须缓存：这张表 2500+ 行、超时给到 60s，若不缓存失败，批量分析
        里每只股票都会重试一次，20 只股票就能把"回落到 efinance"拖到小时级。
        但失败只缓存几分钟，避免一次瞬时抖动让该能力整天不可用。

        注意：``ths_index`` 必须**不带参数**调用；实测传 ``exchange`` 会返回空。
        """
        now = time.monotonic()
        cached = getattr(self, "_ths_index_cache", None)
        if cached and cached[0] > now:
            return cached[1]

        def _remember(mapping: dict) -> dict:
            ttl = _THS_INDEX_TTL if mapping else _THS_INDEX_FAILURE_TTL
            self._ths_index_cache = (now + ttl, mapping)
            return mapping

        try:
            self._check_rate_limit()
            # 2500+ 行的全量映射表，30s 默认超时不够用
            df = self._api.query("ths_index", _timeout=_HEAVY_TIMEOUT)
        except Exception as exc:
            logger.warning("[Promax] ths_index 获取失败: %s", exc)
            return _remember({})

        if df is None or df.empty or "ts_code" not in df.columns or "name" not in df.columns:
            return _remember({})

        has_type = "type" in df.columns
        return _remember(
            {
                str(row["ts_code"]): (str(row["name"]), str(row["type"]) if has_type else "")
                for _, row in df.iterrows()
            }
        )

    def _is_intraday(self) -> bool:
        """是否处于交易日盘中时段（当日数据尚未落库）。"""
        china_now = self._get_china_now()
        current_date = china_now.strftime("%Y%m%d")
        current_clock = china_now.strftime("%H:%M")
        trade_dates = self._get_trade_dates(current_date)
        if not trade_dates:
            return False
        return current_date in trade_dates and "09:30" <= current_clock <= "16:30"

    def _resolve_settled_trade_date(self) -> Optional[str]:
        """返回"数据已出"的最近交易日。

        当日盘前/盘中 daily 与板块行情尚未发布，取当天只会拿到空结果，因此
        收盘前一律回退到上一个交易日。
        """
        china_now = self._get_china_now()
        current_date = china_now.strftime("%Y%m%d")
        current_clock = china_now.strftime("%H:%M")

        trade_dates = self._get_trade_dates(current_date)
        if not trade_dates:
            return None

        is_trading_day = current_date in trade_dates
        # 非交易日取最近交易日；交易日则要等收盘后当天数据才可用
        use_today = (not is_trading_day) or current_clock > "16:30"
        return self._pick_trade_date(trade_dates, use_today=use_today)

    def _determine_priority(self) -> int:
        """初始化成功即取配置优先级（默认 -2，高于所有现有数据源）。"""
        if self._api is None:
            return _UNAVAILABLE_PRIORITY

        config = get_config()
        priority = int(getattr(config, "promax_priority", DEFAULT_PROMAX_PRIORITY))
        logger.info("✅ Promax 聚合网关可用，数据源优先级设为 %s", priority)
        return priority
