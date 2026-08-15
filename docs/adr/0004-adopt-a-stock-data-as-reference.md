# ADR-0004：将 a-stock-data 作为 adapter 参考而非运行时依赖

- 状态：Accepted
- 日期：2026-08-14

## Context

a-stock-data 以约 3,328 行单文件 SKILL.md 汇总多种 A 股公开端点、字段校准和备胎策略，但没有可导入包结构、统一返回模型、自动化测试或随仓库分发的历史数据集。整份注入会扩大上下文，并绕过 DSA 的 ToolSurface、Market Data、限流和审计。

## Decision

按能力提取端点知识、字段映射、失效案例和 fixture，优先重写为 DSA 原生 adapter。必要的最小代码复制遵守 Apache-2.0，添加来源头、修改声明和 `THIRD_PARTY_NOTICES.md`。每个来源必须通过 Data Envelope 契约、字段漂移、合法空值、陈旧和独立备胎测试后才能启用。

## Consequences

- “47 个端点”是研究线索，不是生产可用性承诺。
- DSA 继续维护单一 Market Data 路由和缓存目录。
- EastMoney 域族共享进程级 rate gate；同域不同 URL 不算独立备胎。
