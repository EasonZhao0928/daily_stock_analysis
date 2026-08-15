# DSA 领域上下文

本文档定义 DSA 集成研究工作台的统一语言。代码、数据库、API、提示词、技能和文档应优先使用这些名称，避免同一概念出现多套叫法。

## 核心领域

### Research Session

一次由用户发起的研究会话。它可以使用 LiteLLM 或 Codex transport，但看到相同的工具结果契约。Research Session 不拥有账户状态，也不能直接创建成交事实。

### ToolSurface

Agent 调用 DSA 能力的唯一执行 module。它拥有工具发现、Execution Profile、参数校验、作用域、deadline、取消、隔离、结果大小、脱敏和审计。LiteLLM 与 Codex 是 ToolSurface 外侧的 transport adapter。

### Execution Profile

某类 Agent 在一次运行中可见和可执行的工具集合及其约束。至少包括 `research_readonly`、`portfolio_readonly` 和 `paper_proposal`。Profile 决定能力，不由提示词决定权限。

### Market Data

DSA 对行情、财务、公告、研报、资金和市场结构数据的唯一访问 module。它拥有标的识别、市场时钟、来源路由、域族频控、缓存、熔断、陈旧判断和来源诊断。

### SecurityId

无歧义的证券身份，由市场、品种和规范代码组成。`000001` 等裸代码不得脱离市场和品种解释。

### MarketClock

按市场解释交易日、时区、开收盘、数据截止点和陈旧阈值的规则。所有决策时间和撮合时间均使用带时区值。

### Data Envelope

Market Data 返回的规范化结果。除业务数据外，必须携带 capability、SecurityId、来源层级、`as_of`、`retrieved_at`、单位、币种、陈旧标记、质量标记和 fallback 链。

### Source Policy

某项数据能力的声明式来源策略，定义主源、独立备胎、timeout、重试、缓存 TTL、交易时段与陈旧阈值。同一供应商域族不是独立备胎。

### Evidence

可被研究、影子规则或 Paper Account 引用的版本化事实。Evidence 有来源、时间、内容哈希和质量状态；自由文本推断不是 Evidence。

### Research Capability

一组可发现、可校验、可渐进加载的研究意图。它把 skill 元数据、`required_tools`、可用性和 Evidence 规则绑定在一起，但不复制工具 implementation。

## 交易研究与账户

### Account Ledger

账户事实的唯一账本 module。它拥有现金、成交、公司行动、幂等、超卖约束、成本法、重放和 snapshot invariant。手工导入、Web 和 Paper Account 只能通过账本 seam 提交事实。

### Shadow Profile

从指定 Account Ledger 的历史闭环交易中提取的行为画像。画像本身只用于研究和生成规则候选，未批准画像不得产生 Shadow Signal。

### Shadow Rule

由受限 DSL 表达、可确定性执行的交易规则。LLM 可以提出候选，但编译器决定规则是否合法。

### Shadow Signal

已批准 Shadow Rule 在特定 observation cutoff 上产生的研究信号。它不是 Order，也不改变任何账户事实。

### Paper Account

只能产生虚拟订单和虚拟成交的实验账户。它与手工账户在数据库、工具、UI 和审计中显式区分，永远没有真实券商执行能力。

### Observation

Paper Account 在某个 decision time 冻结的账户、行情、Evidence 和 Shadow Signal 快照。Observation 具有 cutoff、版本和哈希；决策只能使用 cutoff 之前的信息。

### Proposal

模型或 Shadow Rule 基于 Observation 提交的结构化虚拟交易意图。Proposal 不是 Order，必须经过 Paper Mandate、Risk Decision 和审批模式。

### Paper Mandate

用户为 Paper Account 设置的确定性授权范围，包括标的、市场、仓位、现金、换手、亏损、回撤、时段、订单类型和决策频率。模型不能修改或解释覆盖 Paper Mandate。

### Risk Decision

确定性规则对 Proposal 给出的接受或拒绝结果，使用稳定 rule code。Risk Decision 是审计事实，不保存模型隐藏思维链。

### Virtual Order

通过 Risk Decision 且满足审批模式的纸面订单。只有 Paper Account module 可以创建、取消和推进其状态。

### Virtual Fill

Virtual Order 按固定版本撮合规则和历史可见行情得到的虚拟成交。Virtual Fill 通过幂等 seam 提交给 Account Ledger，生成账户成交事实。

## 监控与交互

### Alert Cycle

从规则加载、评估、trigger 持久化、去重、cooldown 到通知结果的一次完整运行。Alert module 拥有整个 cycle；scheduler 只负责触发。

### Conversation Channel

长运行双向消息 module，拥有登录状态、游标、去重、allowlist、退避、会话串行化、健康检查和优雅停止。它把规范消息交给现有 Dispatcher。

### Channel Adapter

微信、飞书、钉钉或 webhook 的协议 implementation。只有存在两个以上实际 implementation 时才保留通用 seam；协议凭据停留在 adapter 内。

## 不变量

1. 模型只能产生 Proposal，不能产生 Virtual Fill 或修改 Account Ledger。
2. 真实券商写工具不进入任何 Paper Account Execution Profile。
3. 同一能力在 LiteLLM 和 Codex transport 下返回同一工具结果契约。
4. 业务调用方不得绕过 Market Data 直接访问已纳入治理的供应商域族。
5. Observation cutoff 之后的数据不得影响本轮 Proposal、Risk Decision 或 Virtual Fill。
6. 同一 `account_id + decision_time + strategy_version` 只产生一次 Paper Account run。
7. Channel Adapter 失效不得阻塞 Web、Account Ledger、Alert 或其他通知渠道。
8. ChatGPT 订阅降低的是受支持 Codex 路径的调用成本，不等同于无限 API 配额。
