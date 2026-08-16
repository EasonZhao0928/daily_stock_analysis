# Market Data 能力路由与供应商治理

本文说明 DSA 统一的行情/财务/公告数据访问入口、供应商域族治理，以及 K 线与 SSE 接口的边界。

术语以 [`CONTEXT.md`](../CONTEXT.md) 为准；架构决策见 [`ADR-0002`](./adr/0002-deepen-tool-surface-and-market-data.md) 与 [`ADR-0004`](./adr/0004-adopt-a-stock-data-as-reference.md)。

---

## 1. 唯一访问入口

业务代码通过 `DataFetcherManager` 访问数据，不直连供应商：

```python
from data_provider.runtime import get_market_data_manager
from data_provider.market_data_types import DataQuery, SourcePolicy

envelope = get_market_data_manager().fetch(
    DataQuery("daily_data", "600519.SH", start=..., as_of=..., limit=240),
    SourcePolicy(primary_sources=("tencent",), fallback_sources=("akshare",)),
)
```

旧的 `get_daily_data` / `get_realtime_quote` 等方法保留为兼容 facade，内部转入同一 capability route。**新代码只使用 `fetch`。**

### 1.1 DataQuery 的时间窗

| 字段 | 含义 |
|---|---|
| `as_of` | 窗口上界，同时用于 stale 判定 |
| `start` | 窗口下界 |
| `limit` | 需要的最大条数 |

能力路由会把这些转发给**目标方法真正声明的参数**（`start_date` / `end_date` / `days`）。签名较窄的 adapter 不受影响，不会收到它不认识的关键字。

> 这一转发是必需的：早期版本只传 `stock_code`，导致 `get_daily_data` 永远使用 `days=30` 默认值，`?limit=240` 的 K 线请求被静默截断到约 30 天。

### 1.2 Data Envelope

返回值统一携带 capability、SecurityId、source、source tier、`as_of`、`retrieved_at`、单位、币种、stale 标记、quality flags 与 fallback chain。

空值分类互不混淆：

```
valid_empty      合法空值（如当日无龙虎榜）
market_closed    休市
symbol_invalid   无效标的
stale_symbol     陈旧标的
upstream_blocked 上游阻断 / 能力未启用
schema_changed   字段漂移
```

金额、比例、股/手、币种、复权和报告期**不做缺省零修复**。无法可靠归一化时拒绝聚合。

---

## 2. 供应商域族治理

### 2.1 共享 session / 频控 / 熔断

同一供应商域族在**进程级**共享连接、最小间隔、抖动、退避与熔断状态：

```python
from data_provider.supplier_runtime import get_supplier_runtime_registry

runtime = get_supplier_runtime_registry()
with runtime.request("sina"):
    resp = runtime.get_session("sina").get(url, ...)
```

受治理的域族：`eastmoney`、`sina`、`tencent`、`tonghuashun`、`cninfo`、交易所。同一供应商的不同 URL **不算独立备胎**。

### 2.2 熔断只统计传输层失败

`SupplierRuntimeRegistry.request()` 区分两类异常：

| 类别 | 是否计入熔断 | 例子 |
|---|---|---|
| 传输失败 | 是 | 超时、连接重置、带 HTTP status 的错误、`requests`/`urllib3` 异常 |
| 解析/业务错误 | **否** | `ValueError`、`KeyError`、`TypeError` 等 |

理由：一个标的的字段解析失败，说明不了供应商健康状况。早期版本把所有异常都计入，连续 3 次就为整个 `eastmoney` 域族打开 60 秒进程级熔断，一个坏标的会连坐全部调用方。

### 2.3 静态旁路门禁

`scripts/check_supplier_bypasses.py` 禁止在白名单 adapter 之外出现受治理域名的直连：

```bash
python scripts/check_supplier_bypasses.py
```

覆盖 6 个域族：`eastmoney.com`、`sinajs.cn`、`finance.sina.com.cn`、`gtimg.cn`、`10jqka.com.cn`、`cninfo.com.cn`。

> 门禁的价值取决于覆盖面。此前只治理 `eastmoney.com`，扩域后立刻查出三处此前完全不可见的裸连接：新浪全市场分页（约 55 次请求/次）、腾讯与新浪的逐股 K 线。

**`TRANSITION_ALLOWLIST` 是只减不增的迁移账本。** `MAX_TRANSITION_FILES` 写死为常量并由 `tests/test_supplier_bypass_gate.py` 断言；新增旁路会使门禁失败，正确做法是把调用并入 Market Data，而不是加白名单。

供应商 URL 字面量集中在 `data_provider/`（如 `screening_sources.py`），service 层从那里 import，不自带字面量。

### 2.4 运行时重置

```python
reset_market_data_runtime()                       # 配置重载：丢弃 manager，重置熔断/频控
reset_market_data_runtime(close_sessions=True)    # 进程退出 / 测试隔离：额外关闭 session
```

默认**不关闭**共享 HTTP session。筛选、热点等组件会长期持有 session 引用，配置保存时关闭它们会让这些持有者拿到已关闭的连接。

---

## 3. 扩展能力开关

财报三表、一致预期、公告、研报、龙虎榜、两融、大宗、股东户数、解禁、分红等能力尚未完成外部验收，默认关闭：

```bash
EXTENDED_MARKET_DATA_ENABLED=false
```

关闭时这些 capability 返回 `UPSTREAM_BLOCKED`，不会静默返回空数据。

### 3.1 对应的 Agent 工具

这些能力通过 `src/agent/tools/extended_data_tools.py` 暴露给 Agent，全部经能力路由，不直连供应商：

| 工具 | capability |
|---|---|
| `get_financial_statement` | `financial_statement`（三表，按 `statement_type` 选择） |
| `get_consensus_estimate` | `consensus_estimate` |
| `get_announcements` | `announcement` |
| `get_research_reports` | `research_report` |
| `get_dragon_tiger` | `dragon_tiger` |
| `get_margin_trading` | `margin` |
| `get_block_trades` | `block_trade` |
| `get_shareholder_counts` | `holders` |
| `get_share_unlocks` | `unlock` |
| `get_dividends` | `dividend` |

关闭 flag 时这些工具**仍然可见**，但返回 `status: upstream_blocked` 且 `data: null`，并附一句「不要推断底层数字」。这是有意的：design §3 要求缺少能力时显示 unavailable，而不是让 skill 用提示词伪造数据访问。返回空数组会被读成「这家公司没有财报」，因此空数组只用于 `valid_empty`。

工具带 `network_read` side effect，因此**不出现在** `paper_proposal` profile 中——纸面决策只读冻结上下文。

---

## 4. K 线与 SSE 接口

基路径 `/api/v1/market`。

```
GET  /{symbol}/candles       OHLCV + MA/MACD/RSI + source/as-of/stale
GET  /{symbol}/snapshot      最新快照
GET  /{symbol}/annotations   成交 / Shadow Signal / Virtual Order/Fill / Alert
GET  /{symbol}/stream        SSE 共享快照
```

endpoint 只从 Market Data 读取，**不自行抓供应商**；浏览器只订阅 DSA 事件，不直连上游。

### 4.1 SSE 资源上限

| 限制 | 默认 | 原因 |
|---|---|---|
| 并发连接 | 16 | 每个 SSE 响应占用一个同步生成器 threadpool worker |
| 单流事件数 | 360 | 无尽响应永远不会归还 worker |

超过并发上限返回 `503 market_stream_capacity`。达到事件上限后流正常结束，浏览器 `EventSource` 会自动重连。

「实时」在首期指**服务端共享轮询 + 最新快照叠加**，不是 tick 级实时。

---

## 5. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| K 线只有约 30 天 | 旧版本能力路由丢弃时间窗 | 已修复；确认 `DataQuery` 传了 `start`/`limit` |
| 某能力返回 `UPSTREAM_BLOCKED` | 扩展能力未启用 | 开启 `EXTENDED_MARKET_DATA_ENABLED` |
| 整个域族短时间不可用 | 熔断打开（连续传输失败） | 等待冷却；确认不是把业务异常误报为传输失败 |
| `503 market_stream_capacity` | SSE 并发已满 | 关闭多余图表标签页 |
| 门禁报未白名单域名 | 新增了直连 | 改为经 Market Data / supplier runtime，不要加白名单 |

---

## 6. 相关文档

- [`CONTEXT.md`](../CONTEXT.md) — 统一词汇表与不变量
- [`ADR-0002`](./adr/0002-deepen-tool-surface-and-market-data.md) / [`ADR-0004`](./adr/0004-adopt-a-stock-data-as-reference.md)
- [`docs/paper-account.md`](./paper-account.md) — Paper Account 与 Shadow Research
- [`docs/market-support.md`](./market-support.md) — 市场与代码支持范围
