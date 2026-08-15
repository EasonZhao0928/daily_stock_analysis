# DSA × Vibe-Trading 集成改造：前后端与运维变更总览

> 文档版本：2026-08-15（对应当前 checkout）  
> 适用范围：`daily_stock_analysis` 当前工作树  
> 目的：说明本轮新增/修改的文件、可见功能、配置开关、运行入口和验收边界。

## 先说结论：是不是“都开发好了”？

代码层面的集成骨架、API、Web 工作台、纸面账户/影子研究、市场数据展示、Codex App Server 登录闭环、个人微信 iLink 通道和部署启动脚本已经落在当前工作树中；对应的单元/接口/前端测试也已经补齐一批。

但这不等于所有能力都已经完成生产验收。下面这些仍然依赖真实环境，不能仅凭静态代码或离线测试宣布“已上线”：

- Codex 需要在**运行 DSA 后端的同一个操作系统用户**下安装、登录并完成真实问股 smoke；App Server 只支持本期声明的 single-agent 工具范围。
- LiteLLM、行情供应商、扩展数据源、通知渠道和个人微信 iLink 需要各自的真实凭据/网络；默认 feature flag 仍然关闭。
- 服务器的 systemd、SSH 权限、域名 DNS、Caddy 自动 HTTPS 和防火墙必须在目标服务器上单独验收。
- Paper/Shadow 始终是虚拟账户/研究账户，不连接券商下单；`PAPER_AUTO_MODE_ENABLED` 只控制是否允许跳过人工审批，不把它变成实盘。

因此，本文件是“当前代码改造和交付边界”说明；正式上线仍以两份操作手册的验收清单和真实环境 smoke 结果为准：

- [本地操作手册](operations-local.md)
- [服务器操作手册](operations-server.md)
- [实现审计记录](../.kiro/specs/dsa-vibe-integration/implementation-audit.md)
- [需求](../.kiro/specs/dsa-vibe-integration/requirements.md) / [设计](../.kiro/specs/dsa-vibe-integration/design.md) / [任务清单](../.kiro/specs/dsa-vibe-integration/tasks.md)

## 1. 当前运行拓扑

```text
浏览器 / Bot / 个人微信
          │
          ▼
FastAPI + 静态 Web（server:app，默认 127.0.0.1:8000）
          │
          ├─ Agent Chat
          │    ├─ LiteLLM（默认模型路径）
          │    └─ Codex App Server（opt-in，JSONL stdio，单 Agent）
          ├─ Market Data Runtime / Supplier Governance
          ├─ Analysis / Evidence / Skills
          ├─ Paper Account（虚拟账本、审批、订单、成交、绩效）
          ├─ Shadow Research（策略画像、回测、研究信号）
          ├─ Alert / Portfolio / Scheduler / Outbox
          └─ Conversation Adapter / WeChat iLink（opt-in）
```

本地通过 `scripts/dsa-service.sh` 启动；服务器通过 systemd 调用同一个脚本。公网部署时只允许 Caddy/Nginx 代理到 `127.0.0.1:8000`，不暴露 Uvicorn 端口，也不把 Codex App Server 的 stdio 子进程改成 TCP 服务。

## 2. 面向用户的新增能力

### 2.1 Agent 与 Codex

- `AGENT_BACKEND=auto|litellm|codex_app_server`：问股 Chat 与普通生成后端解耦。
- 新增 Codex App Server transport：受控环境、只读权限配置、MCP 禁用、JSONL stdio、ephemeral thread、超时/停止/回收闭环。
- 新增 Codex 账户状态、浏览器登录、device-code 登录、取消登录、退出登录 API；设置页显示可尝试状态和 rate limit 摘要。
- Codex 目前只开放脱敏的历史分析上下文、全局回测汇总、策略回测汇总查询；实时行情、新闻、持仓和交易工具不自动暴露给 Codex。
- Codex App Server 仅支持 `AGENT_ARCH=single`，并且 `AGENT_ORCHESTRATOR_TIMEOUT_S` 必须大于 0。
- `GENERATION_BACKEND=codex_app_server` 不是有效替代方案：Codex App Server 是 Agent Chat 路径；普通日报/调度仍走 LiteLLM 或 generation-only 的 `codex_cli`。

### 2.2 工具、技能与数据源

- Agent Tool Surface 统一注册权限、scope、结构化错误、审计脱敏和能力路由。
- 保留原有分析、搜索、市场、执行工具，并加入扩展数据工具（公告、研报、财务/股东/资金/解禁等能力按 flag 和供应商可用性开放）。
- 新增市场数据 runtime、supplier runtime、provider contract fixtures、数据 evidence envelope、market clock、security id 和 A-share data notice 门禁。
- 新增 `strategies/*.yaml` 研究技能：公司深度研究、三表质量、股东/资金、研究纪律、影子账户复盘、交易复盘和估值情景。
- 数据源路由保留 fallback、超时、熔断/频控、旁路静态检查和 point-in-time 证据约束；真实供应商是否可用仍取决于 `.env` 和网络。

### 2.3 Paper Account 与 Shadow Research

- Paper Account：账户生命周期、资金/持仓账本、mandate、risk context、AI proposal、人工审批/自动审批、订单 advance/cancel、fills、绩效对比和决策 cycle。
- Shadow Research：研究画像、策略 DSL、历史回测、扫描、研究信号、快照和独立 ledger；不与 Paper Account 的真实虚拟交易账本混用。
- 所有 Paper/Shadow 数据落到 SQLite 仓库，带数据源时间/观测截止时间等约束；不会连接券商或产生真实订单。
- 运行入口：Web 的 `/paper-workbench`、`/shadow`；API 的 `/api/v1/paper/*`、`/api/v1/shadow/*`；Bot 增加 paper 命令。

### 2.4 行情工作台与监控

- 新增 K 线图组件，支持 candle、annotation、stale/metadata 标识。
- 新增 market API：K 线、snapshot、annotations、SSE stream。
- Portfolio 和 Paper Workbench 复用同一行情图组件；前端保留数据陈旧提示，不把缓存数据伪装成实时行情。
- Alert worker、portfolio alert、outbox 和运行时 scheduler 与新增 Paper cycle 对接，但未开启任何默认后台扫描。

### 2.5 IM / 个人微信

- 新增 conversation 抽象、凭据引用和 WeChat iLink 平台适配器。
- `WECHAT_CHANNEL_ENABLED=false` 默认关闭；allowlist、poll timeout、credential-store reference 均可配置。
- token 本体不放入 `.env`，只放凭据存储；个人使用场景按单用户/小流量设计，不承诺群发或高并发。

## 3. 后端变更摘要

| 区域 | 主要实现 | 对外入口 |
| --- | --- | --- |
| Agent/Codex | `src/agent/codex_app_server_transport.py`、`src/services/codex_account_service.py`、Tool Surface、runner/executor 生命周期 | `/api/v1/agent/status`、`/api/v1/agent/account*`、`/api/v1/agent/chat*` |
| Market | `data_provider/runtime.py`、`supplier_runtime.py`、market types/evidence/clock、chart/annotation/stream services | `/api/v1/market/{symbol}/candles`、`snapshot`、`annotations`、`stream` |
| Paper | `src/paper_account/`、`paper_decision_worker.py`、`paper_tools.py`、paper schemas/endpoints | `/api/v1/paper/accounts/...`、`/performance/compare` |
| Shadow | `src/shadow_research/`、shadow schemas/endpoints | `/api/v1/shadow/profiles/...` |
| Evidence | `research_evidence_repo.py`、`research_evidence_service.py`、provider evidence adapters | Tool Surface、研究/回测上下文 |
| Portfolio/Alerts | portfolio ledger/risk/import/alerts/outbox 的扩展 | 原有 `/portfolio`、`/alerts`、Home/Portfolio |
| Conversation | `bot/conversation.py`、`bot/credentials.py`、`bot/platforms/wechat_ilink.py` | Bot/个人微信通道（opt-in） |
| Runtime/部署 | `scripts/dsa-service.sh`、`scripts/deploy_personal.sh`、systemd unit、CI gate | `web/full/service/doctor/health` |

### 3.1 关键 API 清单

以下路径都在 `/api/v1` 下；具体请求体/响应以 OpenAPI 和 endpoint schema 为准。

**Agent**

- `GET /agent/models`
- `GET /agent/status`
- `GET /agent/account`（兼容别名 `/agent/account/status`）
- `POST /agent/account/login`，`POST /agent/account/login/cancel`，`POST /agent/account/logout`
- `GET /agent/skills`
- `POST /agent/chat`、`POST /agent/chat/stream`、`POST /agent/chat/stream/{request_id}/cancel`
- `GET/DELETE /agent/chat/sessions...`
- `POST /agent/research`

**Market**

- `GET /market/{symbol}/candles`
- `GET /market/{symbol}/snapshot`
- `GET /market/{symbol}/annotations`
- `GET /market/{symbol}/stream`（SSE）

**Paper Account**

- `POST/GET /paper/accounts`、`GET /paper/accounts/{account_id}`
- `POST /paper/accounts/{account_id}/pause|resume|freeze|close|mandate|run`
- `GET /paper/accounts/{account_id}/runs|proposals|orders|fills`
- `GET /paper/accounts/{account_id}/runs/{run_id}`
- `GET /paper/accounts/{account_id}/proposals/{proposal_id}|risk`
- `POST /paper/accounts/{account_id}/proposals/{proposal_id}/approve|reject`
- `POST /paper/accounts/{account_id}/orders/{order_id}/cancel|advance`
- `POST /paper/performance/compare`

**Shadow Research**

- `POST/GET /shadow/profiles`
- `GET /shadow/profiles/{profile_id}`
- `POST /shadow/profiles/{profile_id}/backtest|approve|scan`
- `GET /shadow/profiles/{profile_id}/signals`

## 4. 前端变更摘要

### 新增页面/组件

- `/paper-workbench`：Paper Account 创建、运行 cycle、proposal 审批、risk、orders/fills、绩效和 K 线工作区。
- `/shadow`：影子研究画像、回测、扫描和信号。
- `components/market/CandlestickChart.tsx`：K 线、标注、陈旧状态和元数据展示。
- `api/market.ts`、`api/paper.ts`、`api/shadow.ts` 与 `types/{market,paper,shadow}.ts`：前端 API/类型契约。
- 设置页 `AgentBackendStatusPanel`：Agent backend、Codex 账户登录/退出、single-agent/timeout 约束和状态提示。

### 修改页面/布局

- `App.tsx`、Sidebar、ShellHeader、i18n：挂载新路由、导航项和文案。
- Home、Portfolio：接入 portfolio/market chart、告警和运行态信息。
- Agent/Alert API 与相应测试：对 Codex account、stream cancel、错误和 feature flag 做契约化处理。

## 5. 配置与默认开关

| 变量 | 默认/推荐 | 作用与边界 |
| --- | --- | --- |
| `GENERATION_BACKEND` | `litellm` | 日报、复盘、普通生成；不等于 Codex App Server。 |
| `AGENT_BACKEND` | `auto` | 问股 Agent；`codex_app_server` 是显式 opt-in。 |
| `AGENT_ARCH` | `single` | Codex 必须是 single；LiteLLM 可按设计使用 multi。 |
| `AGENT_ORCHESTRATOR_TIMEOUT_S` | LiteLLM 可按原语义；Codex 推荐 `600` | Codex 必须大于 0，保证中断/超时可回收。 |
| `PAPER_AUTO_MODE_ENABLED` | `false` | 允许 auto approval；仍是虚拟账户。 |
| `EXTENDED_MARKET_DATA_ENABLED` | `false` | 三表/公告/研报等扩展工具的总闸门。 |
| `PAPER_SCHEDULER_ENABLED` | `false` | `full`/schedule 下的无人值守 Paper cycle；不负责审批权限。 |
| `PAPER_SCHEDULER_INTERVAL_MINUTES` | `60` | Paper cycle 间隔。 |
| `SCHEDULE_ENABLED` | `false` | runtime scheduler 总闸门；`web` 模式会强制抑制。 |
| `ADMIN_AUTH_ENABLED` | 本地可 `false`；服务器必须 `true` | Web/API 登录保护。 |
| `TRUST_X_FORWARDED_FOR` | `false` | 仅一层可信反代时按需打开。 |
| `WECHAT_CHANNEL_ENABLED` | `false` | 个人微信 iLink 通道总闸门。 |
| `WECHAT_ILINK_TOKEN_REF` | `wechat-ilink-token` | 凭据引用，不是 token 本体。 |
| `DATABASE_PATH` | `./data/stock_analysis.db` | SQLite 数据库位置；同一实例必须保持稳定。 |
| `LOG_DIR` | `./logs` | 文件日志目录。 |

历史文档中出现过的 `SHADOW_BACKGROUND_SCAN_ENABLED` 已不属于当前配置契约；请删除，不要把它当作可用开关。Shadow 是否运行由 profile/scan API 与现有 runtime 调度语义决定。

## 6. 运行与部署文件

- `scripts/dsa-service.sh`：统一的 Web/API 启动器。`web` 强制不启动 scheduler；`full` 读取 `SCHEDULE_ENABLED`；`service` 供 systemd 使用；`build-web`、`doctor`、`health` 是构建和诊断入口。
- `scripts/deploy_personal.sh`：personal 分支的非 Docker 部署脚本；创建 venv、安装依赖、构建前端、渲染 systemd unit、重启并轮询 health。
- `deploy/daily-stock-analysis.service.in`：普通用户 systemd 服务模板，默认 `127.0.0.1:8000`，`NoNewPrivileges=true`、`PrivateTmp=true`。
- `.github/workflows/deploy-personal.yml`：部署前检查并通过 SSH 调用部署脚本；不替服务器完成 Codex/微信登录。
- `scripts/ci_gate.sh`、`scripts/codex_app_server_gate_a.py`、`scripts/check_ai_assets.py`、`scripts/check_supplier_bypasses.py`、`scripts/check_a_stock_data_notice.py`：确定性、Codex Gate A、AI 资产、供应商旁路和 a-stock-data notice 门禁。

## 7. 测试与验证覆盖

本轮新增/修改的测试覆盖四层：

1. **后端单元/服务**：Codex transport/account、Tool Surface、market runtime/evidence、provider contracts、Paper ledger/order/cycle/scheduler、Shadow、conversation/WeChat、alerts/outbox、feature flags 和 security negative cases。
2. **API 契约**：Agent account、Paper、Shadow、market、portfolio、screening 等 endpoint。
3. **前端组件/页面**：Agent 状态、Sidebar、Paper Workbench、Shadow Research、CandlestickChart、Alert rule。
4. **静态门禁**：syntax、deterministic、供应商 bypass、外部依赖隔离、a-stock-data notice。

推荐在本地按以下顺序复跑；真实 provider/Codex/微信 smoke 另行执行：

```bash
PATH="$PWD/.venv/bin:$PATH" ./scripts/ci_gate.sh syntax
PATH="$PWD/.venv/bin:$PATH" ./scripts/ci_gate.sh deterministic
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -m "not network"
npm run lint --prefix apps/dsa-web -- --max-warnings=0
npm run test --prefix apps/dsa-web -- --run
npm run build --prefix apps/dsa-web
```

## 8. 完整文件清单

下面是当前工作树相对基线的文件级清单。`修改`来自 `git diff --name-only`，`新增`来自 `git ls-files --others --exclude-standard`；文档本身新增后，重新运行这两个命令即可得到最新快照。清单按职责分组，未列出的文件没有因为本次集成而新增/修改。

### 8.1 修改文件（94 个，按路径）

```text
.env.example
.github/workflows/deploy-personal.yml
.gitignore
README.md
THIRD_PARTY_NOTICES.md
api/deps.py
api/v1/endpoints/__init__.py
api/v1/endpoints/agent.py
api/v1/endpoints/portfolio.py
api/v1/router.py
api/v1/schemas/__init__.py
apps/dsa-web/src/App.tsx
apps/dsa-web/src/api/__tests__/agent.test.ts
apps/dsa-web/src/api/agent.ts
apps/dsa-web/src/components/alerts/__tests__/AlertRuleForm.test.tsx
apps/dsa-web/src/components/layout/ShellHeader.tsx
apps/dsa-web/src/components/layout/SidebarNav.tsx
apps/dsa-web/src/components/layout/__tests__/SidebarNav.test.tsx
apps/dsa-web/src/components/settings/AgentBackendStatusPanel.tsx
apps/dsa-web/src/components/settings/__tests__/AgentBackendStatusPanel.test.tsx
apps/dsa-web/src/i18n/uiText.ts
apps/dsa-web/src/pages/HomePage.tsx
apps/dsa-web/src/pages/PortfolioPage.tsx
bot/commands/__init__.py
bot/commands/ask.py
bot/platforms/__init__.py
bot/platforms/dingtalk_stream.py
bot/platforms/feishu_stream.py
data_provider/__init__.py
data_provider/base.py
data_provider/fundamental_adapter.py
data_provider/realtime_types.py
deploy/daily-stock-analysis.service.in
docs/CHANGELOG.md
docs/INDEX.md
docs/alerts.md
docs/deploy-personal.md
docs/full-guide.md
docs/full-guide_EN.md
main.py
scripts/check_ai_assets.py
scripts/ci_gate.sh
scripts/codex_app_server_gate_a.py
scripts/deploy_personal.sh
src/agent/agent_backend.py
src/agent/codex_agent_backend.py
src/agent/codex_app_server_transport.py
src/agent/codex_tool_process.py
src/agent/events.py
src/agent/executor.py
src/agent/factory.py
src/agent/runner.py
src/agent/skills/base.py
src/agent/tool_surface.py
src/agent/tools/analysis_tools.py
src/agent/tools/data_tools.py
src/agent/tools/execution.py
src/agent/tools/market_tools.py
src/agent/tools/registry.py
src/agent/tools/search_tools.py
src/config.py
src/core/config_registry.py
src/repositories/alert_repo.py
src/repositories/portfolio_repo.py
src/search_service.py
src/services/alert_service.py
src/services/alert_worker.py
src/services/backtest_service.py
src/services/history_loader.py
src/services/market_hotspot_service.py
src/services/market_structure_service.py
src/services/portfolio_alerts.py
src/services/portfolio_import_service.py
src/services/portfolio_risk_service.py
src/services/portfolio_service.py
src/services/screening/candidate_context.py
src/services/screening/daily.py
src/services/screening/snapshot.py
src/services/screening_service.py
src/services/stock_service.py
src/storage.py
tests/conftest.py
tests/test_agent_backend.py
tests/test_agent_frozen_context.py
tests/test_agent_tool_surface.py
tests/test_alerts_docs.py
tests/test_ask_command.py
tests/test_codex_app_server_gate_a.py
tests/test_codex_tool_process.py
tests/test_portfolio_api.py
tests/test_portfolio_pr2.py
tests/test_portfolio_service.py
tests/test_screening_api.py
tests/test_yfinance_fundamental_adapter.py
```

### 8.2 新增文件（124 个，按路径；包含本文件）

```text
CONTEXT.md
api/v1/endpoints/market.py
api/v1/endpoints/paper.py
api/v1/endpoints/shadow.py
api/v1/schemas/agent.py
api/v1/schemas/market.py
api/v1/schemas/paper.py
api/v1/schemas/shadow.py
apps/dsa-web/src/api/market.ts
apps/dsa-web/src/api/paper.ts
apps/dsa-web/src/api/shadow.ts
apps/dsa-web/src/components/market/CandlestickChart.test.tsx
apps/dsa-web/src/components/market/CandlestickChart.tsx
apps/dsa-web/src/pages/PaperWorkbenchPage.tsx
apps/dsa-web/src/pages/ShadowResearchPage.tsx
apps/dsa-web/src/pages/__tests__/PaperWorkbenchPage.test.tsx
apps/dsa-web/src/pages/__tests__/ShadowResearchPage.test.tsx
apps/dsa-web/src/types/market.ts
apps/dsa-web/src/types/paper.ts
apps/dsa-web/src/types/shadow.ts
bot/commands/paper.py
bot/conversation.py
bot/credentials.py
bot/platforms/wechat_ilink.py
data_provider/eastmoney_client.py
data_provider/evidence_adapters.py
data_provider/extended_capabilities.py
data_provider/financial_types.py
data_provider/market_clock.py
data_provider/market_data_types.py
data_provider/market_evidence.py
data_provider/provider_fixtures.py
data_provider/runtime.py
data_provider/screening_sources.py
data_provider/security_id.py
data_provider/supplier_runtime.py
docs/adr/0001-use-official-codex-app-server.md
docs/adr/0002-deepen-tool-surface-and-market-data.md
docs/adr/0003-separate-shadow-paper-and-ledger.md
docs/adr/0004-adopt-a-stock-data-as-reference.md
docs/adr/0005-introduce-conversation-channel-before-weixin.md
docs/dsa-vibe-integration-changes.md
docs/market-data.md
docs/operations-local.md
docs/operations-server.md
docs/paper-account.md
scripts/check_a_stock_data_notice.py
scripts/check_supplier_bypasses.py
scripts/dsa-service.sh
src/agent/codex_account.py
src/agent/tools/extended_data_tools.py
src/agent/tools/paper_tools.py
src/paper_account/__init__.py
src/paper_account/controllers.py
src/paper_account/mandate.py
src/paper_account/observation.py
src/paper_account/order_service.py
src/paper_account/performance.py
src/paper_account/repository.py
src/paper_account/risk_context.py
src/paper_account/service.py
src/repositories/research_evidence_repo.py
src/services/codex_account_service.py
src/services/market_annotations_service.py
src/services/market_chart_service.py
src/services/market_stream_service.py
src/services/paper_decision_worker.py
src/services/portfolio_ledger_types.py
src/services/research_evidence_service.py
src/shadow_research/__init__.py
src/shadow_research/backtest.py
src/shadow_research/dsl.py
src/shadow_research/ledger.py
src/shadow_research/repository.py
src/shadow_research/service.py
src/shadow_research/snapshot.py
strategies/deep_company_research.yaml
strategies/financial_statement_quality.yaml
strategies/ownership_and_flow.yaml
strategies/research_discipline.yaml
strategies/shadow_account_review.yaml
strategies/trade_review.yaml
strategies/valuation_scenarios.yaml
tests/fixtures/provider_contract/cninfo_announcements.json
tests/fixtures/provider_contract/eastmoney_reports.json
tests/fixtures/provider_contract/sina_lrb.json
tests/fixtures/provider_contract/ths_eps.json
tests/provider_contract.py
tests/test_a_stock_data_notice.py
tests/test_alert_paper_cycle.py
tests/test_candidate_context.py
tests/test_codex_account.py
tests/test_codex_account_api.py
tests/test_conversation_adapters.py
tests/test_conversation_channel.py
tests/test_data_fetcher_manager_route.py
tests/test_evidence_adapters.py
tests/test_extended_capabilities.py
tests/test_extended_data_tools.py
tests/test_external_dependency_isolation.py
tests/test_feature_flags.py
tests/test_financial_types.py
tests/test_market_clock.py
tests/test_market_data_runtime.py
tests/test_market_data_types.py
tests/test_market_evidence.py
tests/test_market_workbench.py
tests/test_paper_account.py
tests/test_paper_api.py
tests/test_paper_decision_cycle.py
tests/test_paper_golden_flow.py
tests/test_paper_orders.py
tests/test_paper_scheduler.py
tests/test_portfolio_outbox.py
tests/test_provider_adapter_contract.py
tests/test_provider_fixtures.py
tests/test_research_evidence_storage.py
tests/test_security_id.py
tests/test_security_negative.py
tests/test_shadow_api.py
tests/test_shadow_research.py
tests/test_skill_capability.py
tests/test_supplier_bypass_gate.py
tests/test_supplier_runtime.py
```

本文件自身为本次新增文档；后续任何代码/文档变更都应重新运行清单命令，并同步更新本节的数量和路径。

## 9. 上线前的“已完成/待验收”判定

| 项目 | 当前状态 | 结论 |
| --- | --- | --- |
| 代码结构、API、前端路由、离线测试 | 已实现/已补测试 | 可进入本地回归 |
| 本地 LiteLLM Web/API | 具备启动脚本 | 配置 provider 后 smoke |
| 本地 Codex App Server | 具备登录/API/UI 闭环 | 同一用户安装登录后真实问股 |
| Paper/Shadow | 虚拟账户与研究能力已实现 | 先手动审批和小样本回放 |
| 个人微信 iLink | 适配器和凭据引用已实现 | token/allowlist/重连需真实验收 |
| 服务器 systemd | 脚本和 unit 模板已实现 | 目标服务器执行部署验收 |
| Caddy/域名/TLS | 文档给出配置方案 | 需要 DNS、80/443、防火墙和证书真实验收 |
| 实盘交易 | 不在本轮范围 | 当前没有券商下单授权，也不应宣称支持 |

如果要把“开发完成”升级为“可上线”，请按 [服务器操作手册](operations-server.md) 的验收清单逐项打勾，并把 Codex、provider、通知、Paper/Shadow、Caddy 的实际结果记录到实现审计文档，而不是只记录“服务 health 返回 200”。
