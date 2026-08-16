# 数据源审计与优化配置方案（零代码）

> 适用范围：本方案只调整启动环境 / `.env` 环境变量 / 调度（yaml）配置 / 运维 SOP，**不修改任何 `.py` 代码**。
> 依据：`data_provider/` 全量静态核查（12 个 Fetcher + 供应商域族治理层）+ 今日实测日志
> `logs/api_server_20260816.log`（288KB，覆盖 01:10–12:32）与
> `logs/api_server_debug_20260816.log`（覆盖至 13:22）+ `.env` / `src/config.py` 实际取值核对。
> 上一版方案中关于 Skill 通道的结论未能在本仓库当前环境复核，已在第 2 节改写为待验证事项，请勿直接照搬执行。

---

## 0. 结论摘要

1. **数据源清单**：`data_provider/` 下共有 **12 个 Fetcher 级数据源**（6 个常驻 + 5 个按 token/key 条件启用 + 1 个懒加载专项源），另有若干挂在 `screening_sources.py` 下、复用同一供应商域族限流的筛选专用端点（非独立 Fetcher）。**今日实际运行的进程只加载了 6 个**——5 个可选源在 `.env` 中 token/key 全部为空，未实例化。
2. **适配完整性**：12 个 Fetcher 代码层面均已完成基础适配（配置读取、调用封装、字段标准化），不存在"完全没写"的数据源；但发现 3 类真实缺口：① `BaostockFetcher` 在 `bs.login()` 返回 `None` 时未判空即访问 `.error_code`，把网络失败包装成语义不清的 `AttributeError`；② 12 个源里只有 3 个（Pytdx/Longbridge/Tushare）暴露了 `is_available`/`is_available_for_request` 预检探针，其余 9 个只能"打了才知道"；③ 概念排行、筹码分布这类扩展能力，默认 6 源里只有 1–2 个真正实现，覆盖面明显窄于日线/实时行情。
3. **迁移背景澄清**：核查 `docs/adr/0004-adopt-a-stock-data-as-reference.md` 与 `THIRD_PARTY_NOTICES.md` 后确认——本项目行情基础设施（本节第 1 条列出的 12 个 Fetcher）**并非从 GitHub 仓库 `a-stock-data` 迁移代码而来，而是原生独立实现**。`a-stock-data` 仅在财务 Evidence/扩展能力这一层（`evidence_adapters.py`、`financial_types.py`、`extended_capabilities.py`、`market_evidence.py`、`provider_fixtures.py` 五个文件）被引用为**开发期参考资料**（端点知识、字段别名、失效案例、脱敏 fixture），ADR 明确写明"不引入运行时依赖""独立重写，不拷贝可执行代码"，并有 `scripts/check_a_stock_data_notice.py` 门禁防止这条边界漂移。如果任务前提是"数据主要来自 a-stock-data 迁移"，需要按此结论修正。
4. **"无法分析"根因**：不是数据源缺失代码适配，而是**环境代理阻断 + 扩展能力覆盖面过窄 + 可选高质量源未配置**三者叠加，外加 Baostock 一处可观测性缺陷。详见第 1、4 节。

---

## 1. 数据源清单与适配核查

### 1.1 常驻 6 源（无需配置即实例化，`data_provider/base.py:_init_default_fetchers`）

| # | Fetcher | 默认优先级 | 覆盖范围 | 今日调用 成功/失败 | 适配状态 |
|---|---------|-----------|---------|---------|---------|
| 1 | `EfinanceFetcher` | P0 | A股日线/实时（东方财富） | 0 / 11 | 代码适配完整；今日 **100% 因 `ProxyError` 无法连通** `push2his.eastmoney.com`，非适配问题 |
| 2 | `AkshareFetcher` | P1 | A股日线/实时，内部聚合 东财→新浪→腾讯 三级 fallback | 5 / 6 | 适配完整；成功全部来自内部切到新浪的分支，东财分支本身与 Efinance 同样被代理拦截 |
| 3 | `PytdxFetcher` | P2 | A股通达信协议直连 | 0 / 6 | 有 `is_available_for_request` 探针；今日仍 6/6 失败，需人工核实 `PYTDX_HOST`/默认候选服务器可达性 |
| 4 | `BaostockFetcher` | P3 | A股日线（独立 TCP 协议，不走 HTTP 代理） | 9 / 4 | **发现代码缺陷**：`bs.login()` 返回 `None` 时未判空直接访问 `.error_code`（`data_provider/baostock_fetcher.py:112-114`），网络抖动时抛出未分类 `AttributeError` |
| 5 | `YfinanceFetcher` | P4 | 美股/港股为主，A股为占位 | 0 / 4 | A股查不到是覆盖范围限制非 bug；日志另见 `ValueError("time data '20240101' does not match format '%Y-%m-%d'")`，说明紧凑日期格式在某分支未被正确归一化为"不支持"状态 |
| 6 | `TencentFetcher` | P5 | A股日线最终兜底 + 概念排行/筹码分布 | 日线 0/0（未被触达，说明前 5 源已兜住）；概念排行/筹码分布路径被触达且返回空结果 | 适配完整；今日在扩展能力上是"唯一还在尝试但交不出数据"的最后一环 |

### 1.2 可选 5 源（需 token/key 才实例化，今日 **全部未配置，0 次调用**）

| Fetcher | 触发条件 | `.env` 当前状态 | 说明 |
|---------|---------|----------------|------|
| `TushareFetcher` | `TUSHARE_TOKEN` | 未设置 | 代码含 `get_chip_distribution` 实现，若启用可作为筹码分布的第二数据源 |
| `TickFlowFetcher` | `TICKFLOW_API_KEY` | 未设置 | 支持批量预取与复权口径统一配置 |
| `LongbridgeFetcher` | OAuth 或 legacy access token | 未设置 | 港股/美股专用兜底，有 `is_available_for_request` 探针 |
| `FinnhubFetcher` | `FINNHUB_API_KEY` | 未设置 | 美股补充源 |
| `AlphaVantageFetcher` | `ALPHAVANTAGE_API_KEY` | 未设置 | 美股/全球补充源 |

这 5 个源**不是代码适配问题**——条件实例化逻辑、专属错误处理都已就位，纯粹是运维配置缺口：当前系统事实上只运行在"6 个免费/公开抓取源"的最低配置下，没有任何付费/授权级数据源兜底。

### 1.3 专项源（1 个，懒加载单例）

- `TwInstitutionalFetcher`（`data_provider/base.py:3303` 附近懒加载）：台股三大法人资金流，无需 token，自带独立熔断器。今日日志中**未见任何调用痕迹**，无法判断实际连通性，只能标注"未在当日请求路径中验证"。

### 1.4 供应商域族层（非独立 Fetcher，但影响冗余判断）

`data_provider/supplier_runtime.py:supplier_family_for_name` 把多个 Fetcher 映射到同一限流"家族"：

- **`eastmoney` 家族**：`EfinanceFetcher` + `AkshareFetcher` 的东财(`akshareem`)分支，共享 `max_concurrency=2, min_interval=0.15s` 的进程级限流闸门。
- **`yahoo` 家族**：`YfinanceFetcher` + `AlphaVantageFetcher` + `FinnhubFetcher`。
- 其余各自独立：`tencent`、`tushare`、`tickflow`、`longbridge`、`pytdx`、`baostock`。

`docs/adr/0004` 明确写着："EastMoney 域族共享进程级 rate gate；同域不同 URL 不算独立备胎"——**这意味着 Efinance 和 Akshare 的东财分支在故障时会同时倒下，不是两条独立冗余链路**。今天日线数据能兜住，靠的其实是 Akshare 内部切到新浪（真正不同域族）和 Baostock（独立协议），而不是"6 个源里坏 1 个还有 5 个"这种朴素认知。

### 1.5 可用性探针覆盖缺口

12 个 Fetcher 中，只有 3 个对外暴露了统一的可用性预检方法：`PytdxFetcher.is_available_for_request`、`LongbridgeFetcher.is_available_for_request`、`TushareFetcher.is_available`。其余 9 个（Efinance、Akshare、Baostock、Yfinance、Tencent、TickFlow、Finnhub、AlphaVantage、TwInstitutional）没有管理器可调用的探针——即使 Efinance/Akshare 内部有 `circuit_breaker.is_available` 检查，也只用于内部分支选择，不对外暴露。这意味着熔断器必须先真实失败 3 次（`_daily_source_health = CircuitBreaker(failure_threshold=3, cooldown_seconds=300.0)`，见 `base.py:650`）才会跳过该源，无法在请求发出前就预判。

### 1.6 扩展能力覆盖面

`get_concept_rankings`（概念排行）仅 `AkshareFetcher`（东财端点，无新浪等价 fallback）与 `TencentFetcher` 实现；`get_chip_distribution`（筹码分布）仅 `AkshareFetcher`、`TushareFetcher`（未启用）、`TencentFetcher` 实现。相比日线/实时行情有 6 个源可轮转，这两个扩展能力实质只有 1–2 个"真正独立"的备胎，一旦 EastMoney 家族被拦截且 Tencent 当天返回空，就会直接见底（详见第 4 节实测）。

---

## 2. Skill 通道核查（对上一版方案的重要修正）

上一版文档第 2 节列出了 `market-query`、`a-share-daily-review`、`westockdata`、`a-stock-data`、`stock-diagnosis`、`mx-finance-data`、`mx-stocks-screener`、`etf-filter`、`fund-*`、`news-search`、`daily-financial-news`、`market-trend-assessment` 等一批"零代码金融查询 Skill"，并将其作为方案的核心兜底手段。

本次核查了本仓库 `.claude/skills/` 目录与当前 Claude Code 会话可调用的全部 Skill 清单，**结果如下**：

- `.claude/skills/` 下实际只有三个仓库维护类 Skill：`analyze-issue`、`analyze-pr`、`fix-issue`。
- 当前会话可用 Skill 列表中也没有任何金融/股票查询类 Skill（均为通用工程类：`code-review`、`security-review`、`dataviz`、`artifact-*` 等）。
- 未在 `~/.claude` 全局目录或本机可见的 marketplace/plugin 缓存中找到上述金融 Skill 的任何文件痕迹。

**结论**：上一版"Skill 是零代码备份通道"的判断，在本仓库、本会话环境下**无法被验证为真**，很可能来自另一个产品界面（例如 claude.ai 网页版连接的 MCP connector）的上下文，与本仓库/本 CLI 会话不是同一套能力。继续把这些 Skill 名称写进正式运维 SOP 会导致真正执行时全部落空。

**处理方式**：本方案第 5 节已移除"改走 XX Skill"的具体断言，替换为"先用 `claude mcp list` 或产品内 connector 设置核实当前环境是否接入了任何金融数据 MCP/Skill，若有则可作人工核查兜底；若没有，则单票核查应退回到直接调用本地 `python main.py --stocks <code> --dry-run` 或人工访问券商/行情网站"。

---

## 3. 迁移背景说明

- 依据：`docs/adr/0004-adopt-a-stock-data-as-reference.md`（Accepted，2026-08-14）、`THIRD_PARTY_NOTICES.md`、`docs/dsa-vibe-integration-changes.md`、`scripts/check_a_stock_data_notice.py`。
- `a-stock-data`（`https://github.com/simonlin1212/a-stock-data`，引用 revision `3a3149d`）是一个约 3328 行单文件 `SKILL.md` 的社区项目，汇总多种 A 股公开端点、字段校准与备胎策略，但没有可导入包结构、统一返回模型、自动化测试或随仓库分发的历史数据集。
- ADR-0004 的决策是**明确不将其作为运行时依赖**：只提取"端点知识、字段映射、失效案例和 fixture"，DSA 的 adapter 是独立重写，不拷贝可执行代码或长 payload；必要的最小代码复制遵循 Apache-2.0，需带来源头/修改声明并记录到 `THIRD_PARTY_NOTICES.md`。
- 携带 `a-stock-data` 引用头的文件仅 5 个，且都在**财务 Evidence / 扩展能力层**：`data_provider/evidence_adapters.py`、`financial_types.py`、`extended_capabilities.py`、`market_evidence.py`、`provider_fixtures.py`——**不包括**本文档第 1 节列出的 12 个基础行情 Fetcher。
- ADR 原文强调："'47 个端点' 是研究线索，不是生产可用性承诺"——这句话本身直接否定了"数据主要来自 a-stock-data 迁移"这个前提。

**修正结论**：本项目的核心行情数据链路是原生实现，`a-stock-data` 只是开发期的参考资料来源，且通过版权头 + `check_a_stock_data_notice.py` 静态门禁做了合规隔离。若后续沟通/文档中仍在使用"迁移自 a-stock-data"这一表述，建议一并修正，避免误导对数据源可靠性和维护责任的判断。

---

## 4. "无法分析"问题诊断（基于 2026-08-16 实测日志）

### 4.1 环境代理仍在阻断 EastMoney 家族

- 今日日志中 `ProxyError`/`RemoteDisconnected` 共出现 **230 次**，全部指向 `push2his.eastmoney.com` / `push2.eastmoney.com` / `17.push2.eastmoney.com` 等东财域名。
- 精确定位到本地代理地址为 `127.0.0.1:1082`（`host='127.0.0.1', port=1082`，日志中出现 21 次），与上一版文档记录的 `63459` 端口不同——**说明本地代理端口会变化，不应在方案里写死具体端口号**，只锁定"东财域名走本地代理失败"这个事实。
- `.env` 文件本身**没有设置任何** `HTTP_PROXY`/`HTTPS_PROXY`/`PROXY_HOST`/`PROXY_PORT`，说明代理是从启动数据进程的**外部 shell/IDE 环境注入**的，不是本项目配置出来的，第 5 节的代理豁免方案依旧成立且仍是最高优先级修复项。

### 4.2 日线数据：最终能拿到，但链路又长又慢

以 5 个被追踪股票代码（含 600519、300750、159202、159622 等）为样本，**日线数据今日最终全部成功获取**，没有出现"完全查不到日线"的情况，但典型链路是：

```
EfinanceFetcher(代理失败，~2-3s) → AkshareFetcher 东财分支(代理失败，~5s) → AkshareFetcher 内部切新浪(成功，~3-8s)
```

单只股票日线拉取实测耗时 **11–12 秒**，全部消耗在前两个必然失败的东财分支上。批量分析多只股票时，这段浪费会线性放大，如果上层调用方（Bot、API 客户端）设了较短的超时阈值，就会表现为"卡住/无法分析"，即便底层数据其实是能拿到的。

### 4.3 扩展能力：概念排行 / 筹码分布 今日 100% 失败

`所有数据源均失败` 在今日日志中出现 **8 次**，**全部集中在概念排行与筹码分布这两个能力**（8/8，`base.py:4012/4024` 概念排行、`base.py:2546/2558` 筹码分布）。核查具体链路（见下）确认根因同样是代理：

```
[概念排行] AkshareFetcher 调用 ak.stock_board_concept_name_em() → ProxyError(push2.eastmoney.com)
         → TencentFetcher 返回空结果 → 所有数据源均失败
```

`get_concept_rankings` 在 `AkshareFetcher` 内部**只实现了东财端点，没有像日线那样有新浪等价 fallback**，所以一旦东财被代理拦截就直接掉到 Tencent；而 Tencent 当天对这两个能力返回空结果，链路见底。**这不是一个独立的新故障，而是同一个代理问题在覆盖面更窄的能力上被放大**——修复 4.1 的代理问题大概率会同时恢复这两个能力。

### 4.4 Baostock 的一处可观测性缺陷

`BaostockFetcher` 今日成功率尚可（9/13），但失败案例里反复出现：

```
[BaostockFetcher] 600519 获取失败: error_type=AttributeError, reason=Baostock 获取数据失败: 'NoneType' object has no attribute 'error_code'
```

对应代码 `data_provider/baostock_fetcher.py:112-114`：`bs.login()` 在网络异常时可能返回 `None`，但代码直接访问 `login_result.error_code` 未做判空。这会把一次网络失败包装成语义不清的 `AttributeError`，混在熔断器和告警统计里，不利于后续排查"到底是网络问题还是真 bug"。**本方案不改代码，仅记录该发现供后续 fix 类任务参考**。

### 4.5 综合诊断结论

用户反馈的"频繁无法分析"由三个因素叠加造成，**不是数据源缺失代码适配**：

1. **环境代理阻断 EastMoney 域名**（`.env` 之外的外部代理注入）——影响面最大，同时拖慢日线数据获取、并直接打空概念排行/筹码分布这两个覆盖面窄的扩展能力；
2. **可选高质量数据源全部未配置**（Tushare/TickFlow/Longbridge/Finnhub/AlphaVantage 5 个 token/key 皆空）——系统实际只运行在 6 个免费/爬虫源的最低配置，没有真正独立于 EastMoney/Tencent 之外的付费级兜底；
3. **扩展能力覆盖面设计过窄**（概念排行/筹码分布实质只有 1–2 个可用源）——一旦 EastMoney 家族失效，几乎没有第二层可退，直接呈现为"分析失败"而非优雅降级。

外加 Baostock 一处不影响今日整体成功率、但影响可观测性的代码缺陷（4.4），建议记录为独立 `fix` 任务。

---

## 5. 配置层优化建议（可立即执行）

### 5.1 代理豁免（P0，最大收益，纯环境配置）

Efinance/Akshare-东财分支失败根因是继承了外部注入的 `HTTP_PROXY/HTTPS_PROXY`（今日实测代理地址为 `127.0.0.1:1082`，历史上出现过 `63459`——**端口会变，不要写死，按当次实际代理端口配置**）。**不改 `config.py`**，只在启动数据进程的环境中：

```bash
# 方案A：数据域名走直连（推荐）
export NO_PROXY="localhost,127.0.0.1,push2his.eastmoney.com,push2.eastmoney.com,17.push2.eastmoney.com,api.waditu.com,*.akshare.*,baostock.com,*.tencent.com"
# 方案B：数据进程完全不带代理启动
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
python main.py --stocks 600519 --dry-run
```

即在独立 shell / launch 配置里启动数据服务，避开 IDE/终端代理写回。由于这条链路同时影响日线、板块排行、概念排行、筹码分布、ETF 实时行情等多个能力（见 4.2/4.3），是本方案收益最大的单项修复。

### 5.2 重排优先级（环境变量，`int(os.getenv(...))` 已读取，无需改码）

```bash
# 把今日 6/6 全部失败的 Pytdx 移到链尾，避免无效等待
PYTDX_PRIORITY=6          # 原 2 → 链尾
YFINANCE_PRIORITY=7       # 原 4 → 最后（仅美股/港股有效，A股是占位）
# efinance=0 / akshare=1 保持；代理修复后二者即恢复为最快路径
```

> 注：`TUSHARE_PRIORITY`/`LONGBRIDGE_PRIORITY`/`TENCENT_PRIORITY` 等同理可调。但要注意 5.1 未修复前，把 Efinance 移到链尾并不能解决问题——它和 Akshare 东财分支同属 `eastmoney` 供应商家族（见 1.4），代理问题不解决，调整两者相对顺序收益有限。

### 5.3 增加/启用备用源（仅填令牌，无码）

在 `_init_default_fetchers`（`base.py:1226-1263`）中这些源是"有 key 才实例化"，纯配置即可上线，且能同时补上 4.3 提到的筹码分布覆盖面（`TushareFetcher` 已实现 `get_chip_distribution`）：

```bash
TUSHARE_TOKEN=xxx            # 免费注册可得 → 注入高质量 cn 主源 + 筹码分布第二数据源，优先级自动提升
TICKFLOW_API_KEY=xxx         # 启用批量预取
TICKFLOW_BATCH_DAILY_ENABLED=true
TICKFLOW_BATCH_SIZE=100      # 盘前预热日K进缓存，降低实时调用与延迟
TICKFLOW_KLINE_ADJUST=qfq    # 复权口径统一（none/forward/backward/forward_additive/backward_additive；.env.example 注释与代码 TICKFLOW_KLINE_ADJUST_VALUES 均以此为准）
# 港股/美股需求：长桥凭据 LONGBRIDGE_OAUTH_* 或 LONGBRIDGE_ACCESS_TOKEN
```

### 5.4 缓存与刷新频率（配置/yaml 层）

- **TickFlow 批量预取**（5.3）= 缓存预热，直接降低实时请求量，属配置生效。
- **概念排行等已有共享缓存**（`base.py` 中 `_concept_rankings_cache`，TTL 由 `_CONCEPT_RANKINGS_CACHE_TTL_SECONDS` / 空结果专用的 `_CONCEPT_RANKINGS_EMPTY_CACHE_TTL_SECONDS` 控制），无需改码即可受益；但要注意空结果也会按空 TTL 短暂缓存，代理修复后首次请求仍需等这个 TTL 过期才会重新尝试。
- **`STOCK_INDEX_REMOTE_UPDATE_ENABLED`**（默认 `True`，`src/config.py:894`）：代理修复前可临时 `=false` 减少噪声（代价是失去远程指数更新，权衡使用）。
- **调度错峰**：`.github/workflows` 与 `main.py --schedule` 的 cron 属配置层——建议盘前预热缓存、盘中降低并发/批量，避免多标的并发打满被限流；`eastmoney` 供应商家族限流阈值为 `max_concurrency=2, min_interval=0.15s`（`supplier_runtime.py`），批量任务并发数不宜超过该阈值太多。

### 5.5 字段映射与过滤规则（配置可达项）

- `TICKFLOW_KLINE_ADJUST` 已可统一复权口径（配置）。
- `SCREENING_ENABLED`（默认 `False`，`src/config.py:897/2301`）、`EXTENDED_MARKET_DATA_ENABLED`（默认 `False`，`src/config.py:1296/2213`）：若要启用财务/公告/研报扩展能力，确保对应开关打开（配置/启动参数）。
- 严格字段映射（`STANDARD_COLUMNS`/`_normalize_data`）固化在代码，本方案不改动；建议在运维手册中固化"字段信任顺序：efinance/akshare 为准 → baostock/tencent 兜底 → pytdx 末位"，并补充一条：**efinance 与 akshare 的东财分支视为同一故障域，不能当作两次独立验证**。

### 5.6 人工核查兜底（替代原 Skill 章节）

- 本地"无法分析"时，先用 `claude mcp list`（或产品内 connector/Skill 设置页）核实当前环境是否接入了任何金融数据 MCP/Skill；若确有接入，可作为单票人工核查的补充通道。
- 若未接入（本仓库当前状态），单票核查应退回到：`python main.py --stocks <code> --dry-run` 直接看四层 fallback 的实时日志，或人工访问对应券商/行情网站交叉核对，而不是假设有现成的零代码 Skill 通道。

---

## 6. 预期效果

| 措施 | 预期效果 |
|------|---------|
| 5.1 代理豁免 | Efinance(P0) 恢复 + Akshare 东财分支直连恢复 → 日线数据链路从"必经 11-12s 无效重试"缩短为直接命中；同时预计一并恢复概念排行、筹码分布、ETF 实时行情（均为同一代理问题的下游表现） |
| 5.2 Pytdx/Yfinance 移链尾 | 批量分析单次耗时下降（省去每标的对已知失败源的无效等待） |
| 5.3 加 Tushare/TickFlow | 增加高质量主源 + 缓存预热；Tushare 同时补上筹码分布的第二数据源，降低对"仅 Tencent 兜底"这一单点的依赖 |
| 5.4/5.5 缓存与开关对齐 | 降低重复请求与限流风险，按需打开财务/公告扩展能力 |
| 5.6 人工核查兜底 | 用真实可用的核查手段（本地 dry-run/人工核对）替代未经证实的 Skill 假设，避免 SOP 执行时"无路可走" |

---

## 7. 实施步骤（均不触碰 `.py`，可逐步执行）

1. **验证假设**：在带代理 / 不带代理的两个 shell 分别跑 `python main.py --stocks 600519 --dry-run`，对比各源日志与成功率、耗时。
2. **代理豁免**：按 5.1 配置 `NO_PROXY` 或在独立 shell 启动数据进程；验证时同时观察概念排行/筹码分布是否随之恢复（4.3 的关联性判断）。
3. **优先级调整**：`.env` 写入 `PYTDX_PRIORITY=6`、`YFINANCE_PRIORITY=7`。
4. **启用备用源**：申请并填入 `TUSHARE_TOKEN`（同时验证 `get_chip_distribution` 是否补上筹码分布覆盖面）、`TICKFLOW_API_KEY`（港股/美股再加长桥凭据）。
5. **缓存预热**：启用 TickFlow 批量预取，观察缓存命中与延迟；注意概念排行空结果缓存 TTL 对验证时效的影响。
6. **开关对齐**：按需打开 `EXTENDED_MARKET_DATA_ENABLED`/`SCREENING_ENABLED`。
7. **调度错峰**：盘前预热、盘中降并发（改 workflow/cron 配置），并发数参考 `eastmoney` 家族限流阈值。
8. **核实真实兜底通道**：按 5.6 核实当前环境是否有可用的金融数据 MCP/Skill，写入运维 SOP 前先验证而非假设。
9. **监控基线**：以今日日志为基线（Efinance 0/11、Pytdx 0/6、Akshare 5/6、Baostock 9/13、概念排行&筹码分布 0/8），复测后对比成功率 + 单标的耗时 + 概念排行/筹码分布是否恢复。
10. **（超出本方案范围，建议另开 `fix` 任务跟踪）**：`BaostockFetcher.get_daily_data` 中 `bs.login()` 返回 `None` 时的判空处理（`data_provider/baostock_fetcher.py:112-114`）。
