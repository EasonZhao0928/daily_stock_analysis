# Promax 聚合网关能力调研

> 目的：逐项核对本项目消费的数据能力能否由 Promax 聚合网关
> （`https://pcd.mobcvb.cn/tushare/pro`）提供，作为数据源收敛的决策依据。
>
> 调研方法：对每个候选接口发真实请求，记录返回行数、耗时、字段，并与现有
> 数据源的产出做数值比对。测量日期 2026-08-17 ~ 2026-08-18，标的以
> `300189`、`600519`、`000001` 为样本，交易日取 `20260817`。
>
> **网关耗时波动极大**（同一接口实测 1.2s ~ 93s，偶发读超时），下表耗时为
> 成功调用的观测值，不代表稳定上界。所有接入都必须保留下游兜底。

---

## 1. 结论速览

| 分类 | 能力数 | 说明 |
| --- | --- | --- |
| 已接入 Promax | 8 | 日线、实时、筹码、板块排行、涨跌统计、概念排行、所属板块、股票列表 |
| 可接入但不划算 | 2 | 主指数、新闻语料 |
| 网关不支持 | 2 | 美股行情、券商研报 |
| 保持现状 | 其余 | 基本面、股东、资金流等已验证可用，但现有链路稳定，暂不迁移 |

---

## 2. 核心行情能力

| 能力 | 现状数据源 | Promax 接口 | 实测 | 结论 |
| --- | --- | --- | --- | --- |
| A 股日线 | Promax | `daily` | 6~20s | ✅ 已接入 |
| 港股日线 | Promax | `hk_daily` | 1.3s / 10 行 | ✅ 已接入（文档清单未列，实际可用） |
| ETF 日线 | Promax | `fund_daily` | 正常 | ✅ 已接入 |
| 美股日线 | YFinance | `us_daily` | 传日期区间稳定 5xx；不传日期可返回 33 行 | ❌ 不接入 |
| A 股实时 | Promax | `rt_k` | 1.4s | ✅ 已接入 |
| 美股/港股实时 | YFinance / Longbridge | `rt_k` | 美股返回 0 行 | ❌ 不接入 |
| 实时补充指标 | ~~Tencent~~ → Promax | `daily_basic` | 1.5s，量比/换手与 Tencent 完全一致 | ✅ 已接入 |
| 筹码分布 | Promax | `cyq_perf` | 3.5s | ✅ 已接入 |
| 股票列表 | Promax | `stock_basic` | 2.2~4.4s / 5530 行，偶发 60~84s 卡死 | ✅ 已接入 |

### 2.1 实时行情的坑（重要）

父类 `TushareFetcher.get_realtime_quote` 先调 `quotation`（**网关无此接口，实测
ReadTimeout**），失败后会静默降级到 `import tushare; ts.get_realtime_quotes()`
——那是 Tushare 官方 SDK 直连新浪，完全绕开网关。

若直接继承该实现，网关不可用时仍会返回数据并被标成 Promax，既掩盖故障又让
来源统计失真。`PromaxFetcher` 因此**重写**了该方法，只认 `rt_k`。

---

## 3. 大盘复盘能力

| 能力 | 现状 | Promax 接口 | 实测 | 结论 |
| --- | --- | --- | --- | --- |
| 涨跌统计 | ~~AkShare 40.8s~~ → Promax | `daily(trade_date)` + `limit_list_d` | 9.6s（波动至 93s） | ✅ 已接入 |
| 板块排行 | Promax | `moneyflow_ind_ths` | 2s | ✅ 已接入 |
| 概念排行 | ~~全链路失败~~ → Promax | `dc_index` | 4.1s / 1031 行 | ✅ 已接入（净增益） |
| 个股所属板块 | ~~Efinance~~ → Promax | `ths_member` + `ths_index` | 0.6~1.4s（映射表预热后） | ✅ 已接入 |
| 主指数 | TickFlow **2s** | `index_daily` 17.2s；`rt_idx_k` 返回 0 行 | 慢 8 倍 | ❌ 维持 TickFlow |

### 3.1 涨跌统计的正确写法

父类用 `ts_code='3*.SZ,6*.SH,0*.SZ,92*.BJ'` 通配符筛全市场，**网关不支持该
语法，静默返回 0 行**，随后还要调 `stock_basic` 取股票名判断 ST——那正是偶发
卡死的调用。曾导致单次 `get_market_stats` 空耗 112.57s。

正确写法是 `daily(trade_date=...)` 取全市场，`limit_list_d` 直接取涨跌停名单
（`limit` 字段：U=涨停 / D=跌停 / Z=炸板），**因此完全不需要股票名，也就不
需要 `stock_basic`**。

与 AkShare 链路的比对（2026-08-17 收盘）：

| 指标 | Promax | AkShare | |
| --- | --- | --- | --- |
| 涨 | 4335 | 4335 | ✅ |
| 跌 | 1064 | 1063 | ✅ |
| 平 | 140 | 140 | ✅ |
| 涨停 | 106 | 106 | ✅ |
| 跌停 | 1 | 1 | ✅ |
| 成交额(亿) | 24025 | 24021 | ✅ |
| 炸板 | 12 | — | Promax 独有 |

### 3.2 所属板块的接口选择

- `ths_member(con_code=...)` 返回 52 个板块，但**只有板块代码**（`885877.TI`），
  `con_name` 是股票名不是板块名；名称需由 `ths_index` 全量映射表补齐。
- `ths_index` 必须**不带参数**调用，传 `exchange` 会返回空。
- `dc_member(con_code=...)` 同一标的只返回 2 个板块且耗时约 30s，覆盖度远不如 THS。

`ths_index` 的 `type` 字段用于过滤噪声：仅保留 `N`(概念)/`I`(行业)/`TH`(主题)，
剔除宽基(`BB`)、风格(`ST`)、特色(`S`)等——否则"同花顺全A(加权)"这类会把 52 个
板块灌进 prompt，稀释真正的题材信号。过滤后 300189 得 23 个，含转基因、乡村振兴、
海南自贸区、粮食概念等有效题材。

---

## 4. 基本面与参考数据（已验证可用，暂未迁移）

现有链路稳定，迁移收益有限，此处仅记录可用性供后续决策。

| 能力 | Promax 接口 | 实测 |
| --- | --- | --- |
| 财务指标 | `fina_indicator` | ✅ 56 行 |
| 利润表 / 资产负债表 / 现金流 | `income` / `balancesheet` / `cashflow` | ✅ 82 / 92 / 87 行 |
| 业绩预告 / 快报 | `forecast` / `express` | ✅ 41 / 10 行 |
| 分红送股 | `dividend` | ✅ 47 行 |
| 前十大（流通）股东 | `top10_holders` / `top10_floatholders` | ✅ 各 10 行 |
| 股东人数 | `stk_holdernumber` | ✅ 2 行 |
| 个股资金流 | `moneyflow` / `moneyflow_dc` | ✅ 11 行 |
| 龙虎榜 | `top_list` / `top_inst` | ✅ 59 / 600 行 |
| 大宗交易 | `block_trade` | ✅ 165 行 |
| 限售解禁 | `share_float` | ✅ 39 行 |
| 热榜 | `ths_hot` / `dc_hot` | ✅ 544 / 776 行 |
| 券商金股 | `broker_recommend` | ✅ 100 行 |
| 融资融券 | `margin` / `margin_detail` | ⚠️ 返回 0 行（当日数据未发布） |

---

## 5. 不可用能力

| 能力 | 接口 | 实测 | 说明 |
| --- | --- | --- | --- |
| 券商研报 | `research_report` | 6/6 次 HTTP 503 | 未开通，需联系服务方 |
| 新闻快讯 | `news` | 返回 0 行 | 未开通 |
| 长篇新闻 / 公告 | `major_news` / `anns_d` | HTTP 400 | 参数格式未知或未开通 |
| 新闻联播 | `cctv_news` | ✅ 11 行但 34.2s | 可用但过慢 |

新闻链路目前依赖 SearXNG + 各搜索引擎，日志显示 SearXNG 实例频繁超时，但这
属于本地实例问题，与数据源收敛是独立议题。

---

## 6. 数据源一（datahubco）评估结论：不接入

`http://datahubco.com/app-api/openapi/v1/tushare/stock-basic`

| 观察项 | 实测结果 |
| --- | --- |
| 接口覆盖 | 仅 `stock-basic`，`daily`/`trade_cal`/`daily_basic`/`rt-k` 均 HTTP 500 |
| 单发可用性 | ✅ 0.8~1.5s |
| 突发限流 | 连续请求后触发封禁，**恢复窗口 4~6 分钟** |
| 超限响应 | 返回 **HTML 错误页**而非 JSON，直接 `.json()` 会抛 `JSONDecodeError` |
| 分页能力 | 无 offset 参数；`limit=1000` 返回 1000 行，`limit=5000` **静默返回 0 行**，`limit=5600` HTTP 500 |
| 缓存行为 | 相同查询返回相同 `request_id`，疑似整体缓存 |

结论：能力面被 Promax 完全覆盖，却额外引入"超限静默返回空"和"5 分钟全 key
封禁"两类高危失效模式。**不接入。**

---

## 7. 接入后的失效行为

已验证：仅打断 Promax 客户端、其余数据源保持真实网络时，各能力均正确回落。

| 能力 | Promax 失效后 |
| --- | --- |
| 日线 | → Efinance → AkShare → Pytdx → **TickFlow 成功** |
| 实时行情 | 返回 None → Tencent / Sina / Efinance |
| 所属板块 | → **Efinance 成功**（count=12） |
| 涨跌统计 | → **AkShare 成功**（41.8s，up=4335） |
| 概念排行 | → AkShare（本次亦失败，属既有问题，非本次回归） |

---

*文档版本：[Unreleased] — Promax 能力调研（初版）*
