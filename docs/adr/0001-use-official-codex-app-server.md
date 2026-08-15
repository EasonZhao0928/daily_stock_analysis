# ADR-0001：使用官方 Codex App Server 管理 ChatGPT 登录

- 状态：Accepted
- 日期：2026-08-14

## Context

DSA 已有 `AGENT_BACKEND=codex_app_server`。Vibe-Trading 通过自管 OAuth token 直接调用 ChatGPT 内部 Codex Responses 端点，工具覆盖更广，但需要自行处理刷新令牌、SSE 和协议变化。官方文档确认 Codex 支持 ChatGPT 订阅登录，App Server 提供浏览器/设备码登录、账号状态、ChatGPT rate limits 和动态工具调用。

## Decision

DSA 继续以官方 `codex app-server` 作为 ChatGPT 登录和动态工具 transport，不复制 Vibe 的内部端点实现，不读取或共享其他应用的刷新令牌文件。

首期只覆盖 `AGENT_BACKEND` 控制的 Research Session。日报、定时分析和市场复盘必须通过后续统一 LLM 路由逐项迁移，不因环境变量静默改道。

## Consequences

- 登录、令牌刷新和协议兼容由官方 App Server 管理。
- DSA 需要深化 ToolSurface 才能获得与 LiteLLM 接近的工具能力。
- ChatGPT 订阅仍受滚动窗口、周限额或 credits 约束，不能承诺“无限免费 API”。
- LiteLLM 保留为用户显式配置的 transport adapter。

## References

- https://learn.chatgpt.com/docs/auth
- https://learn.chatgpt.com/docs/app-server
- https://learn.chatgpt.com/docs/pricing
