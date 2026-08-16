# ADR-0003：分离 Shadow Research、Paper Account 与 Account Ledger

- 状态：Accepted
- 日期：2026-08-14

## Context

Vibe 的影子账户主要是规则提取、回测和扫描，不是持续虚拟账本。DSA 已有 PortfolioService 的成交事实、现金、公司行动、重放和 snapshot。原设计把模型提案、风控、审批、虚拟订单、撮合与组合投影拆为多个并列 implementation，事务所有权和幂等 locality 不清晰。

## Decision

- Shadow Research 只产生已批准规则与 Shadow Signal，不创建订单。
- deep Paper Account module 独占 Observation、Proposal、Paper Mandate、Risk Decision、审批、Virtual Order、撮合和审计状态机。
- deep Account Ledger module 独占成交事实、现金、重放和 snapshot invariant。
- Virtual Fill 通过 `trade_uid=paper:<fill_id>` 的幂等 seam 原子提交给 Account Ledger。
- 模型仅是 Proposal adapter；模型永远不能直接创建 Virtual Order、Virtual Fill 或账本事实。

## Consequences

- 一次完整 Paper Account cycle 是主要测试 surface，不能通过分别 mock 风控和撮合掩盖集成错误。
- 默认审批模式为 `human_confirm`；`auto_paper` 需要用户显式启用且仍无真实执行能力。
- 任何真实下单能力必须另立项目和安全 ADR。
