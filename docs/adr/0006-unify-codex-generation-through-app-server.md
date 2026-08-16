# ADR-0006：通过官方 Codex App Server 统一普通生成

- 状态：Accepted
- 日期：2026-08-15

## Context

ADR-0001 已决定由官方 Codex App Server 管理 ChatGPT 登录和动态工具 transport，但当时把范围限制在 `AGENT_BACKEND` 控制的 Research Session。现有日报、定时分析、市场复盘和 screening 等普通生成由 `GENERATION_BACKEND` 控制；因此只设置 `AGENT_BACKEND=codex_app_server` 并不会让这些业务使用 Codex。

DSA 已有 `GenerationBackend` 和 `AgentBackend` 两个合理的能力边界，也已有可完成普通生成的 `codex_cli` adapter，以及实现账号、thread/turn 和动态工具的 App Server transport。需要补齐的是 App Server 的 Generation adapter 和跨业务路由完整性，而不是把问股 Agent 当作所有分析的替代入口。

## Decision

1. 在 `GenerationBackend` factory 中增加 `codex_app_server` 一等实现，使普通文本和严格 JSON 生成可以使用官方 Codex App Server 登录态。
2. 保留 `GenerationBackend` 与 `AgentBackend` 两个接口；二者共享 App Server Runtime、Account Client、进程生命周期、错误与观测，但不共享 thread、工具或业务上下文。
3. 普通 Generation 使用 ephemeral、无工具、无 MCP/Apps/Plugins、无网络的最小权限 session；Agent 继续通过 profile-bound ToolSurface 使用动态工具。
4. “统一 Codex”是设置页组合预设，等价于同时设置：

   ```dotenv
   GENERATION_BACKEND=codex_app_server
   AGENT_BACKEND=codex_app_server
   ```

   不新增会覆盖两个变量的总开关。
5. Codex App Server 失败时默认 fail closed。LiteLLM fallback 只有在用户显式配置时才允许，并在 UI 与审计中标明可能产生 API 费用。
6. 不复制 Vibe-Trading 的内部 OAuth/refresh-token/Responses endpoint 实现，不读取其他应用的 token 文件。
7. 首期迁移所有受支持的业务文本生成旁路，并用 route inventory gate 阻止新旁路。Vision provider 诊断和未适配的 Multi-Agent / Deep Research 明确排除；统一 Codex 模式下不支持的能力显式失败而非静默改用 LiteLLM。
8. 首期每个 operation 使用隔离的 App Server session 和有界并发，不引入常驻进程池。

## Consequences

### Positive

- 日报、复盘、调度等普通分析可以正式使用 ChatGPT 登录态和订阅额度。
- Agent/Paper 与普通生成共享官方账号和协议基础设施，减少实现漂移。
- 两类 backend 仍保持最小权限边界，普通生成不会获得动态工具。
- fallback 默认关闭，避免 Codex 不可用时意外产生 LiteLLM API 费用。
- route inventory 和 negative tests 能证明“受支持调用全部走 Codex”，而不是依赖配置表象。

### Negative / Trade-offs

- App Server 是本地子进程协议，DSA 需要持续维护协议 fixture 和 opt-in 兼容性 smoke。
- 订阅额度、模型可用性和 usage 字段由账号/服务端决定，不能承诺无限调用或精确货币成本。
- 首期隔离进程更安全但吞吐低于成熟的常驻池；性能优化需另立证据和设计。
- Vision 与部分多 Agent 能力仍需独立 provider 或后续适配，UI 必须清楚展示例外。

## Implementation and Evidence

本 ADR 已按 `.kiro/specs/codex-unified-generation/tasks.md` 的 Task 1-11 实施完成：

- `GenerationBackend` 已提供 `codex_app_server` adapter；普通文本/严格 JSON 使用共享的隔离 App Server Runtime，保持无工具、无 MCP/Apps/Plugins 和 fail-closed fallback 边界。
- Analyzer、MarketAnalyzer、Scheduler、Screening、轻量 Bot、Agent Chat 与 Paper proposal 已接入各自的 Generation/Agent contract；route inventory 仅保留设计 scope 中登记的 provider、Agent 兼容 transport、诊断、vision 和 unsupported 例外。
- 系统配置 API/Web 设置页提供 unified Codex draft preset、独立 backend 配置、脱敏账号/rate-limit 状态、无模型 quick check，以及必须显式确认额度风险的 text/JSON smoke。
- 目标后端回归套件 87 项通过；全量离线后端门禁 `6080 passed, 1 skipped, 4 deselected`，前端 lint/build/Vitest `1117 passed, 2 skipped`。真实 OAuth、模型 smoke 和在线供应商验证仍按设计保持 opt-in。

本地环境缺少 `flake8` executable，因此该单项 backend lint 需由 CI 使用 `requirements-ci.txt` 依赖执行；这不改变本 ADR 的路由、安全和默认 CI 不依赖真实 ChatGPT 凭据的决定。

## Compatibility

- 未修改配置的用户继续使用现有 Generation 默认后端。
- `codex_cli` 保留为独立普通生成选项。
- 只设置 `AGENT_BACKEND=codex_app_server` 的用户不会被自动迁移日报。
- 现有 LiteLLM adapter 和 provider-specific connection tests 保持可用。

## Supersession

本 ADR 一旦 Accepted 并完成实现，将取代 ADR-0001 中“首期只覆盖 Research Session、普通生成留待后续”的范围限制；ADR-0001 关于使用官方 App Server、禁止复制内部 OAuth/token 方案的决定继续有效。

## Alternatives Rejected

- 只使用 `codex_cli`：缺少与 App Server 相同的账号、rate-limit、Agent runtime 和统一观测控制面。
- 复制 Vibe 的内部 Codex OAuth：凭据与非公开协议风险不可接受。
- 合并 Generation 与 Agent 接口：会模糊工具授权并扩大普通生成权限。
- 新增单一总环境变量：与现有两个 backend 变量形成优先级歧义。
- 首期使用常驻 App Server 池：生命周期、隔离和崩溃恢复复杂度缺少当前证据支持。

## References

- `.kiro/specs/codex-unified-generation/requirements.md`
- `.kiro/specs/codex-unified-generation/design.md`
- `.kiro/specs/codex-unified-generation/tasks.md`
- `docs/adr/0001-use-official-codex-app-server.md`
- [Official Codex App Server](https://developers.openai.com/codex/app-server/)
- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [Codex CLI reference](https://developers.openai.com/codex/cli/reference)
