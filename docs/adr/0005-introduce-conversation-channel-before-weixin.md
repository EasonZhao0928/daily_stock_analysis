# ADR-0005：先建立 Conversation Channel，再增加个人微信 adapter

- 状态：Accepted
- 日期：2026-08-14

## Context

现有 BotPlatform interface 偏向 webhook；飞书和钉钉 stream 各自实现启动、停止、重连、排队和回复。个人微信 iLink 又是扫码与 HTTP 长轮询。直接新增 PersonalWeixinGateway 会形成第三套生命周期 implementation，并可能继续扩大 NotificationService。

## Decision

建立 deep Conversation Channel module，统一登录状态、游标、去重、allowlist、退避、会话串行化、健康检查和停止语义。飞书、钉钉和个人微信作为 Channel Adapter，通过现有 Dispatcher seam 进入命令系统。

Notification module 继续负责主动投递与 fallback，不吸收长轮询生命周期。个人微信 adapter 位于特性开关后，MVP 仅支持本人一对一文本。

## Consequences

- 微信协议变化只影响 adapter implementation。
- token/context token 留在 adapter 凭据存储，使用 Keychain 或 `0600` 原子文件。
- 微信不可用不影响 Web、Account Ledger、Alert 或其他通知渠道。
