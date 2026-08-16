# Paper Account 与 Shadow Research

本文说明 DSA 的纸面账户（Paper Account）、影子研究（Shadow Research）与相关 API、配置和安全边界。

术语以 [`CONTEXT.md`](../CONTEXT.md) 为准，架构决策见 [`docs/adr/`](./adr/)。

> **首期范围**：不提供真实下单。`portfolio_accounts.external_execution_enabled` 恒为 `false`，仓库中不存在券商执行 implementation。

---

## 1. 三个概念的区别

这三者常被混称为「影子账户」，在 DSA 中是三个不同的 module，不可互换：

| 概念 | 拥有什么 | 不拥有什么 |
|---|---|---|
| **Shadow Research** | 历史画像、受限 DSL 规则、回测、归因、Shadow Signal | 订单、成交、账本状态 |
| **Paper Account** | Observation、Proposal、Mandate、Risk Decision、Virtual Order、撮合 | 真实成交事实、真实券商能力 |
| **Account Ledger** | 现金、成交、公司行动、成本法、重放、snapshot | 决策逻辑、风控判断 |

Virtual Fill 通过 `trade_uid=paper:<fill_id>` 的幂等 seam 投影进 Account Ledger，这是 Paper 唯一能影响账本的路径。

---

## 2. 决策 cycle

```
run_cycle_from_sources(account_id, decision_at, strategy_version)
  ├─ build_observation_inputs_from_sources   从 Ledger / Market Data / Evidence / Shadow 读取
  ├─ build_paper_observation                 冻结并按 cutoff 校验，产出 observation_hash
  ├─ controller.generate                     模型/规则只产出结构化 Proposal
  ├─ evaluate_proposal                       确定性 Paper Mandate 与 Risk Decision
  └─ approval                                按 approval_mode 决定 Proposal 状态
```

### 2.1 入口选择

| 入口 | 用途 |
|---|---|
| `POST /api/v1/paper/accounts/{id}/run` | **对外唯一入口**。请求体只能指定 symbol universe 与策略版本 |
| 调度器（`--schedule` 模式） | 按 `PAPER_SCHEDULER_INTERVAL_MINUTES` 定期扫描活跃账户 |
| `run_cycle_from_sources(...)` | 进程内推荐入口。行情、账户、Evidence、Shadow Signal 全部由 module 自己从各 seam 读取 |
| `run_cycle(..., market_data=..., account_snapshot=...)` | 兼容 seam，接受调用方直传事实。仅供测试与受信任的进程内调用方使用 |

`run_cycle` 是渐进迁移用的兼容入口。删除条件：所有测试迁移到 source-owned 入口后移除。

**HTTP 接口不接受任何行情、余额、Evidence 或 Shadow Signal 值** —— `PaperRunCycleRequest` 里没有这些字段，由测试断言保证。

### 2.2 调度器

```bash
PAPER_SCHEDULER_ENABLED=false          # 默认关闭
PAPER_SCHEDULER_INTERVAL_MINUTES=60
```

`PaperDecisionWorker` 沿用 `AlertWorker` 的 `background_tasks` 模式注册进 `--schedule` 模式，不是第二套调度框架。它只决定**何时**运行：

- 只扫描 `account_kind == "paper"` 且状态为 `active` 的账户；paused/frozen/closed 一律跳过
- 单账户异常按 skip 计数，不中断整轮，也不会让后台线程退出
- `run_immediately=False` —— 进程重启不会立刻重放决策
- 幂等 replay 不计入 `ran`

该 flag 与 `PAPER_AUTO_MODE_ENABLED` **相互独立**：前者决定何时跑 cycle，后者决定风控通过的 Proposal 能否跳过人工审批。两者都关时，调度器不运行；只开前者时，cycle 会跑但 Proposal 停在 `pending_confirmation`。

### 2.3 point-in-time 保证

`build_paper_observation` 对**四类输入**统一执行 cutoff 校验：

| 输入 | 时间字段（按序取首个非空） | 缺失时 |
|---|---|---|
| `market_data[symbol]` | `as_of` / `timestamp` / `datetime` / `bar_timestamp` / `date` | **拒绝** |
| `evidence[]` | `as_of` / `published_at` / `date` | **拒绝** |
| `shadow_signals[]` | `data_cutoff` / `signal_date` / `date` | **拒绝** |
| `account_snapshot` | `as_of` / `date` | 允许（账本快照本身即以 cutoff 查询） |

时间戳不可解析同样**拒绝**，不会跳过校验。这是有意的 fail-closed：无法定位时点的数据在纸面交易里等同于未来函数。

`market_data` 只放 symbol → 行情行；来源信息走并列的 `market_source_refs`，避免遍历 symbol 的消费者把元数据当成标的。

### 2.4 幂等

同一 `account_id + decision_time + strategy_version` 只产生一次 run。重试返回原运行结果（`idempotent_replay: true`），不重复调用模型，也不重复产生 Proposal / Order / Fill。

---

## 3. 审批模式与 feature flag

| approval_mode | 行为 | 是否需要 flag |
|---|---|---|
| `recommend_only` | 只保存 Proposal 与 Risk Decision | 否 |
| `human_confirm` | **默认**。等待 Web/微信确认后才建单 | 否 |
| `auto_paper` | 风控通过即建 Virtual Order（仍无真实券商） | **是** |

### 3.1 `PAPER_AUTO_MODE_ENABLED`

`auto_paper` 是唯一无人参与就能创建 Virtual Order 的模式，因此设为 opt-in，并有两层守卫：

1. **建账号时**：未开启 flag 时 `create_account(approval_mode="auto_paper")` 直接抛 `PaperStateError`
2. **审批时**：即使数据库中已有 `auto_paper` 账户，flag 关闭后也会退回 `pending_confirmation`，不会继续自动建单

配置读取失败一律视为「关闭」——这个门禁 fail-closed。

```bash
PAPER_AUTO_MODE_ENABLED=false   # 默认；置 true 才允许 auto_paper
```

### 3.2 `EXTENDED_MARKET_DATA_ENABLED`

扩展 A 股能力（财报三表、一致预期、公告、研报、龙虎榜、两融、大宗、股东户数、解禁、分红）尚未完成外部验收，默认关闭。关闭时 `MarketData.fetch()` 对这些 capability 返回 `UPSTREAM_BLOCKED`，不会静默返回空数据。

```bash
EXTENDED_MARKET_DATA_ENABLED=false
```

> 曾存在的 `SHADOW_BACKGROUND_SCAN_ENABLED` 已移除：仓库中没有后台扫描器，该 flag 无对应行为路径。Shadow 扫描目前只能由 API 显式触发。

---

## 4. 模型权限边界

Paper 决策使用独立的 `paper_proposal` Execution Profile。可见工具仅 5 个：

```
read_paper_context      读取本轮冻结的 Observation
list_paper_proposals    列出本轮 Proposal
get_paper_proposal      读取单个 Proposal
submit_paper_proposal   提交结构化 Proposal
cancel_paper_proposal   撤回 Proposal
```

该 profile **不包含**：

- 真实券商、成交、现金、持仓修改工具
- 数据库写、Shell、任意文件工具
- **任何实时数据源工具**（`get_realtime_quote`、`get_capital_flow`、`get_market_indices` 等）

最后一条容易被忽略：Paper 的要求是「只读**冻结**上下文」，不是一般意义的只读。放行实时行情等于给模型开了一条绕过 Observation cutoff 的通道。因此 `_PROFILE_PERMISSIONS[PAPER_PROPOSAL]` 刻意不继承 `_RESEARCH_PERMISSIONS`，且 profile 的 side effects 不含 `network_read`。

审批走独立的 `paper_approval` profile（4 个工具），不含任何研究权限。

---

## 5. API

基路径 `/api/v1/paper`。

### 5.1 账户与控制

```
POST   /accounts/{id}/run                 运行一次决策 cycle（唯一对外入口）
POST   /accounts                          创建（auto_paper 需 flag）
GET    /accounts                          列表
GET    /accounts/{id}                     详情
POST   /accounts/{id}/pause|resume|freeze|close
POST   /accounts/{id}/mandate             更新 mandate（需先 pause/freeze）
```

### 5.2 运行与提案

```
GET    /accounts/{id}/runs                分页
GET    /accounts/{id}/runs/{run_id}
GET    /accounts/{id}/proposals
GET    /accounts/{id}/proposals/{pid}
GET    /accounts/{id}/proposals/{pid}/risk
POST   /accounts/{id}/proposals/{pid}/approve
POST   /accounts/{id}/proposals/{pid}/reject
```

### 5.3 订单与成交

```
GET    /accounts/{id}/orders
POST   /accounts/{id}/orders/{oid}/advance       服务端取价推进撮合
POST   /accounts/{id}/orders/{oid}/cancel        撤单
GET    /accounts/{id}/fills
```

批准数量明确的 Proposal 时，服务端在同一审批动作内幂等创建 staged Virtual Order；因此不再提供由客户端手动调用的 `POST /accounts/{id}/orders` staging route（该路径返回 405）。target-weight Proposal 仍须先解析出数量，解析前不会建单。

**刻意不提供**的入口（design §6 / ADR-0003）：

| 曾存在 | 移除原因 |
|---|---|
| `POST /accounts/{id}/orders`（手动 staging） | 审批已经是唯一建单动作，保留第二入口会制造重复/绕过语义 |
| `POST /orders/{oid}/match` | 接受客户端传入 bar，等于让调用方决定成交价 |
| `POST /orders/{oid}/transition` | 任意状态跃迁可绕过审批与撮合规则 |
| `POST /fills/{fid}/apply` | 账本投影是 cycle 内部步骤，不是外部动作 |

Paper 的公开契约是**完整 cycle**，不是可任意组合的状态机步骤。`advance` 自行把 staged 订单提升为 approved（`stage_order` 已保证其背后是已批准 Proposal），因此不需要外部 transition。

### 5.4 绩效

```
POST   /performance/compare      对比 LLM / shadow / benchmark / manual 账户
```

---

## 6. 撮合语义

默认「收盘后决策、下一交易日开盘或限价满足时成交」。

- `advance_order` 只使用 observation cutoff **之后**、`as_of` **之前**的合格 bar
- 行情 stale 或 `data_quality` 非 `ok` 时拒绝撮合，不猜价格
- 撮合规则版本固定并随 fill 记录，支持重放
- 覆盖开盘价、限价、滑点、费用、停牌、涨跌停、部分成交与过期

---

## 7. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `approval_mode 'auto_paper' requires PAPER_AUTO_MODE_ENABLED=true` | flag 未开启 | 确认确实需要无人审批后再开启 |
| `market data for X has no timestamp` | 行情行缺 `as_of` 等时间字段 | 补时间字段；这是 point-in-time 守卫，不应绕过 |
| `market data for X is newer than the observation cutoff` | 传入了 cutoff 之后的行情 | 改用 `run_cycle_from_sources` 让 module 自己按 cutoff 取数 |
| `Proposal 一直停在 pending_confirmation` | `human_confirm`，或 `auto_paper` 但 flag 已关 | 走审批 API，或检查 flag |
| `trusted market data is stale or unavailable` | 撮合所需行情陈旧 | 等待行情恢复；不会用陈旧价格成交 |
| 扩展数据能力返回 `UPSTREAM_BLOCKED` | `EXTENDED_MARKET_DATA_ENABLED=false` | 按需开启 |

---

## 8. 相关文档

- [`CONTEXT.md`](../CONTEXT.md) — 统一词汇表与不变量
- [`docs/adr/0003-separate-shadow-paper-and-ledger.md`](./adr/0003-separate-shadow-paper-and-ledger.md)
- [`docs/market-data.md`](./market-data.md) — Market Data 能力路由与供应商治理
- [`docs/alerts.md`](./alerts.md) — Alert Cycle
