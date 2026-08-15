# ADR-0002：深化 ToolSurface 与 Market Data，不建立平行框架

- 状态：Accepted
- 日期：2026-08-14

## Context

LiteLLM 直接使用 ToolRegistry，Codex 经 ToolSurface、transport 和子进程，而子进程会从全局 factory 重建 registry。另一方面，`DataFetcherManager` 已承担多来源路由，但选股、搜索和候选上下文仍存在直连供应商与局部频控。原设计提出新的 ToolCatalog、BoundedToolRunner 和 AShareDataGateway，存在第二套框架和 shallow module 风险。

## Decision

深化现有 ToolSurface，使 registry、Execution Profile、policy、隔离、deadline、取消、结果 envelope 和审计成为其内部 implementation。LiteLLM 与 Codex 只保留 transport adapter 差异。

深化现有 Market Data module，使 SecurityId、MarketClock、Data Envelope、Source Policy、域族频控、缓存、熔断和来源诊断成为同一深入口。保留旧调用的兼容 facade，逐步消除供应商旁路；不新增平行 AShareDataGateway。

## Consequences

- Codex parity 的测试 surface 变成“同一 Execution Profile 在不同 transport 下结果一致”。
- 子进程必须使用调用方注入的 registry/profile，不能回退全局 registry。
- 现有调用方可渐进迁移，避免一次性重写 3,751 行 DataFetcherManager。
- 新来源只有在至少两个实际 implementation 存在时才形成 adapter seam。
