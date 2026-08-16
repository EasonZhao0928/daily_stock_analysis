# DSA 本地运行操作手册

本文档对应当前 checkout 的 FastAPI + React Web 工作台。长期 Web/API 进程使用 `scripts/dsa-service.sh`，不要把批处理入口 `python main.py` 当作长期服务守护。

本地操作手册可以帮助你把功能跑起来，但不把“进程启动成功”当作“模型、行情、Codex、微信和 Paper 全部验收成功”。第三方 provider、Codex OAuth、个人微信和公网部署都需要各自的真实 smoke。

先记住一个容易混淆的边界：`codex_cli` 和 `codex_app_server` 都会在**运行 DSA 的机器上启动 `codex` 可执行文件**；模型推理和 ChatGPT 账号服务在远端，但当前 DSA 不是直接调用一个远程 OAuth HTTP API。`codex_app_server` 使用官方 App Server 管理的 ChatGPT browser/device-code OAuth，服务器仍需要 Codex App Server 可执行文件。当前 DSA 没有实现远程 WebSocket App Server client，因此不能只靠 `.env` 把本地 `codex` 转发到另一台机器。

## 1. 本地运行模型

```text
浏览器 ──> 127.0.0.1:8000 ──> uvicorn server:app
                              ├─ LiteLLM / provider（普通生成）
                              ├─ Codex App Server（可选，Generation + Agent Chat）
                              ├─ Codex CLI（可选，generation-only）
                              ├─ Market / Evidence / Paper / Shadow API
                              └─ static/index.html（production Web）
```

| 命令 | 用途 | scheduler 行为 |
| --- | --- | --- |
| `scripts/dsa-service.sh web` | Web/API 工作台，默认入口 | **强制关闭** runtime scheduler |
| `scripts/dsa-service.sh full` | Web/API + runtime scheduler | 读取 `.env` 的 `SCHEDULE_ENABLED` |
| `scripts/dsa-service.sh service` | systemd 入口 | 读取 `DSA_SERVICE_MODE=web|full` |
| `scripts/dsa-service.sh build-web` | 安装前端依赖并构建 `static/` | 不启动服务 |
| `scripts/dsa-service.sh doctor` | 检查 `.env`、Python、目录和静态文件 | 不启动服务 |
| `scripts/dsa-service.sh health` | 请求 `/api/health` | 不启动服务 |

不要同时启动两个 `full` 进程，否则会重复调度分析、告警或 Paper cycle。

## 2. 环境准备

推荐 macOS/Linux、Python 3.10+、Node.js 20.19+（推荐 Node 22 LTS）和 npm 10+。如果启用 `GENERATION_BACKEND=codex_app_server` 或 `AGENT_BACKEND=codex_app_server`，还要求 `codex` CLI 能被运行 DSA 后端的用户找到；原生 Windows 后端不在本期 Codex App Server 支持范围内。

```bash
cd "/Users/coldenzyc/Trading Projects/code/daily_stock_analysis"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

npm ci --prefix apps/dsa-web --prefer-offline --no-audit --no-fund

.venv/bin/python --version
node --version
npm --version
```

如果 `python3`、Node 或 npm 不存在，请先安装对应运行时；不要把系统 Python 和项目 `.venv` 混用。前端构建必须在 `apps/dsa-web` 的 lockfile 约束下执行。

## 3. 创建 `.env` 与配置原则

```bash
cp .env.example .env
chmod 600 .env
${EDITOR:-vi} .env
```

`scripts/dsa-service.sh` 会在仓库根目录存在 `.env` 时自动设置 `ENV_FILE`。也可以显式选择配置文件：

```bash
ENV_FILE="$PWD/.env.local" ./scripts/dsa-service.sh web
```

首次加载时，进程环境变量通常优先于 `.env`；Web 设置保存会遵循配置管理器对 runtime key 的优先级。为避免“终端里临时 export 但文件没有记录”，上线前把最终值写入 `.env`，并用 `chmod 600` 保护文件。

### 3.1 推荐的本地基线（LiteLLM）

下面配置只启动 Web/API，不启动 scheduler、Paper 无人值守 cycle、扩展数据和微信。provider key 只填写一个真实来源即可；不要把示例值提交到 Git。

```dotenv
STOCK_LIST=600519,300750,002594

# 普通日报、复盘、分析生成
GENERATION_BACKEND=litellm
GENERATION_FALLBACK_BACKEND=litellm
# 至少配置一种 provider：OPENAI_API_KEY / DEEPSEEK_API_KEY / GEMINI_API_KEY / ANSPIRE_API_KEYS / AIHUBMIX_KEY 等

# 问股 Agent 默认保持原有模型路径
AGENT_BACKEND=auto
AGENT_ARCH=single
AGENT_ORCHESTRATOR_TIMEOUT_S=600

WEBUI_HOST=127.0.0.1
WEBUI_PORT=8000
WEBUI_AUTO_BUILD=false
ADMIN_AUTH_ENABLED=false

SCHEDULE_ENABLED=false
SCHEDULE_RUN_IMMEDIATELY=false
PAPER_AUTO_MODE_ENABLED=false
PAPER_SCHEDULER_ENABLED=false
EXTENDED_MARKET_DATA_ENABLED=false
WECHAT_CHANNEL_ENABLED=false

DATABASE_PATH=./data/stock_analysis.db
LOG_DIR=./logs
LOG_LEVEL=INFO
```

`WEBUI_ENABLED` 是配置层兼容项，不取代 launcher；是否启动 Web/API 由 `dsa-service.sh` 的 mode 决定。历史文档里的 `SHADOW_BACKGROUND_SCAN_ENABLED` 已从当前配置契约移除，不要添加它。

### 3.2 环境变量分组

| 分组 | 主要变量 | 本地建议 |
| --- | --- | --- |
| 股票/数据 | `STOCK_LIST`、`TUSHARE_TOKEN`、`TICKFLOW_*`、`FUTU_OPEND_*`、Longbridge/Finnhub/AlphaVantage 等 | 只填实际要用的数据源；扩展能力要同时打开 `EXTENDED_MARKET_DATA_ENABLED`。 |
| 普通生成 | `GENERATION_BACKEND`、`GENERATION_FALLBACK_BACKEND`、`GENERATION_BACKEND_TIMEOUT_SECONDS`、`GENERATION_BACKEND_MAX_OUTPUT_BYTES`、`GENERATION_BACKEND_MAX_CONCURRENCY` | `litellm` 用 API provider；`codex_app_server` 用 ChatGPT 订阅登录态；`codex_cli` 是本地 CLI generation-only。 |
| Codex 账号/进程 | `CODEX_MODEL`、`CODEX_HOME`、`PATH` | `CODEX_MODEL` 可选；`CODEX_HOME` 只写账号目录；`PATH` 必须让 DSA 进程找到 `codex`，不要写 token。 |
| Agent（App Server） | `AGENT_BACKEND`、`AGENT_ARCH`、`AGENT_ORCHESTRATOR_TIMEOUT_S`、`AGENT_SKILLS`、`AGENT_SKILL_DIR` | `AGENT_BACKEND=codex_app_server` 影响问股 Chat/Paper proposal；不会自动替换日报，除非同时设置 Generation。 |
| Codex CLI generation | `GENERATION_BACKEND=codex_cli`、`GENERATION_BACKEND_TIMEOUT_SECONDS`、`GENERATION_FALLBACK_BACKEND` | **可以**替换普通日报、个股分析和大盘复盘；但需要本机 CLI，且是 generation-only、experimental/limited。 |
| 纸面账户 | `PAPER_AUTO_MODE_ENABLED`、`PAPER_SCHEDULER_ENABLED`、`PAPER_SCHEDULER_INTERVAL_MINUTES` | 先人工审批和单次 `run`；不要一开始打开无人值守。 |
| scheduler | `SCHEDULE_ENABLED`、`SCHEDULE_TIME(S)`、`SCHEDULE_RUN_IMMEDIATELY`、`MARKET_REVIEW_ENABLED` | 本地默认关闭；`web` mode 即使 `.env` 写 true 也会抑制。 |
| Web/认证 | `WEBUI_HOST`、`WEBUI_PORT`、`WEBUI_AUTO_BUILD`、`ADMIN_AUTH_ENABLED`、`TRUST_X_FORWARDED_FOR` | 本机用 `127.0.0.1`；共享网络或公网前必须启用认证。 |
| 数据库/日志 | `DATABASE_PATH`、`SQLITE_WAL_ENABLED`、`SQLITE_BUSY_TIMEOUT_MS`、`LOG_DIR`、`LOG_LEVEL` | 确保 `data/`、`logs/`、`reports/` 可写；不要把数据库放到临时目录。 |
| 通知/微信 | DingTalk、Feishu、Telegram、email、custom webhook、`WECHAT_*` | 只配置要验证的渠道；微信 token 只存 credential store。 |

完整 provider 与 skill 配置参见 [LLM 配置指南](LLM_CONFIG_GUIDE.md)、[完整指南](full-guide.md)、[Paper Account 专题](paper-account.md) 和 [Market Data 专题](market-data.md)。

### 3.3 统一 Codex（ChatGPT 订阅）配置模板

如果希望日报/复盘/筛选/轻量生成和问股 Agent 都使用同一个 ChatGPT 订阅登录态，使用下面这组**现有变量**。`GENERATION_FALLBACK_BACKEND=` 必须显式留空，避免 Codex 失败时产生 LiteLLM API 费用；`CODEX_MODEL` 留空表示使用账号默认模型。

```dotenv
GENERATION_BACKEND=codex_app_server
GENERATION_FALLBACK_BACKEND=
GENERATION_BACKEND_TIMEOUT_SECONDS=300
GENERATION_BACKEND_MAX_OUTPUT_BYTES=1048576
GENERATION_BACKEND_MAX_CONCURRENCY=1
CODEX_MODEL=

AGENT_BACKEND=codex_app_server
AGENT_MODE=true
AGENT_ARCH=single
AGENT_ORCHESTRATOR_TIMEOUT_S=600

# 可选：只指定 Codex 自己管理登录态的目录；不要填 access/refresh token
# CODEX_HOME=/Users/<user>/.codex
# 可选：后端进程若找不到 CLI，补充其绝对路径目录（不要写 $PATH 展开式）
# PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
```

保存 `.env` 后必须完全重启 DSA。若只想让日报走 Codex、问股保留 LiteLLM，则只设置 `GENERATION_BACKEND=codex_app_server` 并保留 `AGENT_BACKEND=auto`；反过来也同理。

## 4. 在本地使用 Codex App Server

### 4.1 当前真实架构：远端模型，本地 App Server 进程

当前 DSA 支持三种普通 Generation 路径：

| 配置 | DSA 做什么 | 是否需要运行 DSA 的机器安装 `codex` | 是否使用 ChatGPT 订阅 OAuth |
| --- | --- | ---: | ---: |
| `GENERATION_BACKEND=litellm` | 直接调用配置的 API/provider | 否 | 否 |
| `GENERATION_BACKEND=codex_cli` | 启动 `codex exec` 类本地 CLI 子进程 | 是 | 取决于 CLI 登录方式 |
| `GENERATION_BACKEND=codex_app_server` | 启动 `codex app-server --stdio`，执行无工具 Generation | 是 | 是，官方 managed ChatGPT login |

因此“使用远程 OAuth”是可以的，但含义是：**OAuth、模型推理和额度在远端，App Server transport 进程仍在本机**。DSA 当前只实现 stdio transport，没有 `wss://` 远程 App Server client；不能只配置一个远程 URL 来省掉服务器上的 `codex` 安装。

官方 App Server 文档列出了 browser/device-code ChatGPT 登录、账号状态和 rate limits；其中通过 WebSocket 远程连接的能力目前标为 experimental，不适合作为当前 DSA 的生产部署方案。详见 [OpenAI Codex App Server 文档](https://developers.openai.com/codex/app-server/)。

DSA 不读取、复制或保存 Codex credential 文件；App Server 自己管理登录态。运行 DSA 的设备、用户、容器和远程服务器之间登录态互不共享。

### 4.2 用 Codex App Server 处理日报、复盘和 Agent（推荐的订阅路径）

如果目标是“所有首期受支持的普通生成和问股都使用 ChatGPT 订阅”，在 `.env` 中配置：

```dotenv
GENERATION_BACKEND=codex_app_server
GENERATION_FALLBACK_BACKEND=
GENERATION_BACKEND_TIMEOUT_SECONDS=300
GENERATION_BACKEND_MAX_OUTPUT_BYTES=1048576
GENERATION_BACKEND_MAX_CONCURRENCY=1
CODEX_MODEL=

AGENT_BACKEND=codex_app_server
AGENT_MODE=true
AGENT_ARCH=single
AGENT_ORCHESTRATOR_TIMEOUT_S=600
```

这会让日报、个股分析、市场复盘、screening LLM ranking、轻量 Bot 文本走 Generation adapter，让问股 Chat/Paper proposal 走 Agent adapter。普通问股 Chat 使用 `portfolio_readonly` 只读工具；Paper proposal 使用冻结 Observation 的独立 profile，输出后仍需人工审批。两者共享账号和 App Server Runtime 基础，但普通 Generation 永远没有动态工具、MCP、Apps、Plugins 或网络访问。

### 4.3 账号登录与本地真实调用

1. 用同一个启动 DSA 的用户确认 `command -v codex`、`codex --version`。
2. 启动 Web：`./scripts/dsa-service.sh web`，打开「设置 → Generation/Agent 状态」。
3. 先运行 Codex **quick check**：它只检查配置、binary、协议、账号和 rate limits，不发送模型 turn。
4. 在本机优先使用 browser login；如果浏览器回调不稳定，使用 device-code，在浏览器打开返回的 URL 并输入验证码。
5. 账号显示 authenticated 后，运行一次显式 Generation JSON smoke（页面会先展示额度风险，必须确认后才发送真实模型请求）。
6. 再发送一个无副作用问股问题；检查结果中的 `effective_backend=codex_app_server`、日志和 rate-limit 变化。

本地 App Server 的 browser flow 可以由同一台机器完成；如果 DSA 运行在远程服务器，请优先使用 device-code，详见服务器手册。

页面对应的后端接口是 `POST /api/v1/system/config/generation-backends/quick-check`（无模型）和 `POST /api/v1/system/config/generation-backends/smoke-test`（真实 text/JSON turn）；这两个 POST 需要管理员会话和同源 CSRF，优先从设置页操作，不要把 cookie/token 写进命令历史。

### 4.4 Codex CLI generation（仅作备用）

若要使用本机 CLI 的 generation-only 路径：

```dotenv
GENERATION_BACKEND=codex_cli
GENERATION_FALLBACK_BACKEND=
GENERATION_BACKEND_TIMEOUT_SECONDS=600
GENERATION_BACKEND_MAX_OUTPUT_BYTES=1048576
LOCAL_CLI_BACKEND_MAX_CONCURRENCY=1
AGENT_BACKEND=auto
```

它每次启动受限 CLI 子进程，把 DSA 已准备好的 prompt 交给 CLI，再读取最终文本；不会调用 DSA ToolSurface，usage telemetry 通常不可用，且属于 experimental/limited。

### 4.5 安装、检查和清理 Codex

```bash
command -v codex
codex --version
printf 'CODEX_HOME=%s\n' "${CODEX_HOME:-<default>}"
```

如果终端能找到但 Web 找不到，检查启动进程的 `PATH`；`.env` 中只写目录，不写 token：

```dotenv
CODEX_HOME=/Users/<user>/.codex
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
```

退出账号请使用设置页 Logout；不要手动复制或删除 credential 文件。

恢复默认 provider：

```dotenv
GENERATION_BACKEND=litellm
GENERATION_FALLBACK_BACKEND=litellm
AGENT_BACKEND=auto
```

## 5. Paper Account、Shadow 和行情工作台

默认 feature flag 全部关闭。建议按以下顺序启用：

1. 保持 `PAPER_AUTO_MODE_ENABLED=false`，在 `/paper-workbench` 创建虚拟账户。
2. 选择一个股票和观察范围，运行单次 decision cycle，检查 observation cutoff、proposal、risk 和证据。
3. 手动 approve/reject proposal，检查订单 advance/cancel、fills 和绩效。
4. 在 `/shadow` 创建研究 profile，先回测/扫描再查看 signals；Shadow ledger 不等于 Paper ledger。
   留空观测 JSON 时，后端会按日期范围从统一 Market Data 路由取日线；适配器收到的
   日期为 `YYYY-MM-DD`，默认每个数据源最多等待 15 秒。网络慢时可在 `.env` 调整
   `SHADOW_RESEARCH_SOURCE_TIMEOUT_SECONDS`（1–60 秒），并用
   `SHADOW_RESEARCH_SOURCE_PRIORITY` 显式指定顺序。
5. 需要公告/三表/研报等能力时，再配置数据源并打开：

   ```dotenv
   EXTENDED_MARKET_DATA_ENABLED=true
   ```

6. 确认单次流程稳定后，才考虑：

   ```dotenv
   PAPER_AUTO_MODE_ENABLED=true
   PAPER_SCHEDULER_ENABLED=true
   PAPER_SCHEDULER_INTERVAL_MINUTES=60
   ```

`PAPER_AUTO_MODE_ENABLED=true` 只允许满足 mandate/risk 条件的自动审批；它不会连接券商。无人值守 cycle 需要 `full` mode，并且只能有一个 scheduler 实例。

行情工作台使用 `/api/v1/market/{symbol}/candles|snapshot|annotations|stream`；图表会显示 stale/metadata，不要将 stale 数据当成实时成交价。

## 6. 个人微信 iLink（可选）

个人微信通道是单用户、小流量适配，不是群发服务。默认关闭，启用前先阅读 [Bot 命令与接入](bot-command.md) 和凭据实现。

```dotenv
WECHAT_CHANNEL_ENABLED=true
WECHAT_ILINK_BASE_URL=https://<your-ilink-endpoint>
WECHAT_ILINK_TOKEN_REF=wechat-ilink-token
WECHAT_ALLOWLIST=<your-openid-or-user-id>
WECHAT_POLL_TIMEOUT_MS=30000
```

`WECHAT_ILINK_TOKEN_REF` 只是 credential-store 的引用；token 本体不要写 `.env`、命令行、日志或 Git。启用后先验证 allowlist、poll、重连、错误和停止行为，再让它触发 Agent/Paper 功能。

## 7. 构建、诊断和启动

### 7.1 构建和环境检查

```bash
./scripts/dsa-service.sh build-web
./scripts/dsa-service.sh doctor
```

`build-web` 执行前端 lockfile 安装和 production build，必须生成根目录 `static/index.html`。`doctor` 会检查 `.env`、当前 Python 是否能导入 `fastapi/uvicorn`、`data/logs/reports` 写权限和静态文件。

### 7.2 只启动 Web/API（本地默认）

```bash
./scripts/dsa-service.sh web
```

访问：

- Web：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/docs>
- 健康检查：

  ```bash
  ./scripts/dsa-service.sh health
  ```

指定绑定地址或端口：

```bash
./scripts/dsa-service.sh web --host 127.0.0.1 --port 18000
```

`--reload` 只建议本地开发使用：

```bash
./scripts/dsa-service.sh web --reload
```

如果把 host 改为 `0.0.0.0`，脚本会告警；这只适合临时局域网诊断，不能代替认证和反向代理。

### 7.3 启动 Web/API + scheduler

先确认时间、交易日和通知配置，再运行：

```bash
SCHEDULE_ENABLED=true \
SCHEDULE_RUN_IMMEDIATELY=false \
./scripts/dsa-service.sh full
```

停止服务使用 `Ctrl-C`。`full` 模式的 scheduler 由 FastAPI lifespan 管理，退出时会停止；不要再另起 `python main.py --schedule`。

### 7.4 Vite 前端热更新（可选）

终端 A：

```bash
./scripts/dsa-service.sh web
```

终端 B：

```bash
npm run dev --prefix apps/dsa-web -- --host 127.0.0.1 --port 5173
```

访问 <http://127.0.0.1:5173>。Vite 将 `/api` 代理到 `http://127.0.0.1:8000`；不要把 Vite 端口暴露到公网。

## 8. 一次性 CLI 与常用操作

`main.py` 仍用于一次性分析/批处理，不用于守护 Web/API：

```bash
.venv/bin/python main.py --stocks 600519,300750 --no-notify
.venv/bin/python main.py --dry-run
.venv/bin/python main.py --market-review --no-notify
.venv/bin/python main.py --check-notify
```

切换端口、配置文件或 Python：

```bash
DSA_PORT=18000 ./scripts/dsa-service.sh web
ENV_FILE="$PWD/.env.codex" ./scripts/dsa-service.sh web
DSA_PYTHON="$PWD/.venv/bin/python" ./scripts/dsa-service.sh doctor
```

## 9. 本地验收

### 9.1 静态/离线回归

```bash
PATH="$PWD/.venv/bin:$PATH" ./scripts/ci_gate.sh syntax
PATH="$PWD/.venv/bin:$PATH" ./scripts/ci_gate.sh deterministic
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -m "not network"

npm run lint --prefix apps/dsa-web -- --max-warnings=0
npm run test --prefix apps/dsa-web -- --run
npm run build --prefix apps/dsa-web
```

`flake8` 是 CI 依赖，不一定随生产 `.venv` 安装；需要本地补跑时执行 `.venv/bin/python -m pip install -r .github/requirements-ci.txt` 后再运行 `PATH="$PWD/.venv/bin:$PATH" ./scripts/ci_gate.sh flake8`。离线测试不能证明真实 OAuth、远程模型、在线行情或公网代理可用。

### 9.2 功能 smoke 顺序

```text
[ ] /api/health 返回成功
[ ] 首页、登录（如启用）和 /docs 可访问
[ ] 普通 LiteLLM 问股成功
[ ] Codex 设置状态 + 浏览器/device login + 真实历史问股（如启用）
[ ] Market candle/snapshot/annotation/stale 展示
[ ] Paper 单次 cycle、risk、人工 approve/reject、order/fill
[ ] Shadow profile、backtest、scan、signals
[ ] 需要时扩展数据源的 evidence/cutoff
[ ] 需要时微信 allowlist/poll/reconnect
[ ] full 模式只有一个 scheduler，且没有意外立即执行
```

### 9.3 真实 OAuth / 模型调用验收

按“无额度 → 可能消耗额度”的顺序执行：

```bash
./scripts/dsa-service.sh doctor
./scripts/dsa-service.sh health
command -v codex
codex --version
```

然后在 Web「设置」中：

1. 确认 Generation/Agent 的 effective backend 是 `codex_app_server`。
2. 点击 **quick check**，确认 binary、protocol、account 和 rate limits；这一步不发模型 turn。
3. 完成 browser/device-code OAuth，确认账号显示 authenticated、plan 和 rate limits，且页面不显示 token。
4. 点击 JSON smoke；第一次只会返回额度风险提示，必须显式确认 `confirm_quota_risk=true` 才执行一次真实请求。
5. 对日报或市场复盘运行一个单标的、关闭通知的最小任务，并在报告/运行详情中确认 `effective_backend=codex_app_server`。
6. 对 Agent 发送“只读取历史上下文，不执行交易”的问题，再验证取消、超时和工具 profile；不要把真实下单意图作为首次 smoke。

失败判断：`command_not_found` 是 CLI/PATH 问题，`login_required` 是 OAuth 未完成，`rate_limit_exceeded` 是额度窗口问题；在 `GENERATION_FALLBACK_BACKEND=` 空值时这些失败必须 fail closed，不能静默调用 LiteLLM。

### 9.4 在线数据源验收

在线数据源必须单独验收，不会因 Codex smoke 通过而自动通过。服务运行后至少验证一个行情、一个复盘/分析上下文和一个需要配置凭据的 provider：

```bash
curl --fail 'http://127.0.0.1:8000/api/v1/market/600519/candles?period=daily&limit=5'
curl --fail 'http://127.0.0.1:8000/api/v1/market/600519/snapshot'
```

在 Web 的 Market 页面确认响应包含来源、时间和 stale/limitations 语义；若配置了 Tushare/TickFlow/Longbridge，则在相应设置或分析任务中执行一次真实调用，并记录实际 source、HTTP 错误、fallback 与权限结果。不要只用 `/api/health` 作为数据源证据。

### 9.5 CI 与生产验收

本地静态门禁通过后，推送分支并在 GitHub Actions 中确认 `backend-tests` 的 3 个 shard、`backend-gate`、`web-gate`（以及变更触发时的 Docker gate）均为 success：

```bash
gh run list --workflow ci.yml --branch personal --limit 5
gh run view <run-id> --log-failed
```

生产验收按 [服务器操作手册](operations-server.md) 执行：systemd active、localhost health、Codex/account/model smoke、在线数据源、Caddy TLS/公网访问、SSE、scheduler 单实例和备份恢复都要分别记录。CI 通过不等于生产 OAuth、行情或代理已通过。

### 9.6 常见问题

- `缺少 fastapi/uvicorn`：使用 `.venv/bin/python -m pip install -r requirements.txt`，不要只安装系统 uvicorn。
- 页面空白或静态资源 404：重新运行 `./scripts/dsa-service.sh build-web`，确认 `static/index.html` 存在。
- API 正常但没有自动分析：确认启动的是 `full`，且 `SCHEDULE_ENABLED=true`；`web` 会有意抑制 scheduler。
- Codex 设置显示“可以尝试”但问股失败：这是预期的分层状态，检查同一用户的 `command -v codex`、`codex --version`、`CODEX_HOME`、登录态和 `AGENT_ORCHESTRATOR_TIMEOUT_S`，再看服务日志。
- Codex 在终端可用但 Web 不可用：检查 Desktop/IDE 启动时的 PATH；完全重启后端，不要只重开浏览器。
- 模型调用失败：先用 quick check，再用显式 Codex JSON smoke；若要对比 provider，临时切换 LiteLLM 并明确记录 effective backend。不要一次打开所有新 feature flag。
- `database is locked`：确认没有两个 full/scheduler 进程，检查 `DATABASE_PATH`、WAL 和目录权限。

## 10. 本地安全边界

- 不提交 `.env`、API key、OAuth token、微信 token、session secret 或数据库凭据。
- 本地共享网络时也启用 `ADMIN_AUTH_ENABLED=true`；直连公网不要依赖 CORS 或隐藏路径。
- `TRUST_X_FORWARDED_FOR=true` 只适用于一层可信反向代理；本地直连保持 false。
- Codex 子进程由 DSA 以受控环境启动；不要为“方便登录”放宽 Tool Surface、复制 credential 文件或把 App Server 改成公网 TCP。当前 DSA 不支持远程 App Server URL。
- Paper/Shadow 是虚拟/研究账户，不是实盘下单系统。
