# DSA 本地运行操作手册

本文档对应当前 checkout 的 FastAPI + React Web 工作台。长期 Web/API 进程使用 `scripts/dsa-service.sh`，不要把批处理入口 `python main.py` 当作长期服务守护。

本地操作手册可以帮助你把功能跑起来，但不把“进程启动成功”当作“模型、行情、Codex、微信和 Paper 全部验收成功”。第三方 provider、Codex OAuth、个人微信和公网部署都需要各自的真实 smoke。

## 1. 本地运行模型

```text
浏览器 ──> 127.0.0.1:8000 ──> uvicorn server:app
                              ├─ LiteLLM / provider（普通生成）
                              ├─ Codex App Server（可选，Agent Chat）
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

推荐 macOS/Linux、Python 3.10+、Node.js 20.19+（推荐 Node 22 LTS）和 npm 10+。Codex App Server 还要求 `codex` CLI 能被运行 DSA 后端的用户找到；原生 Windows 后端不在本期 Codex App Server 支持范围内。

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
| 普通生成 | `GENERATION_BACKEND`、`GENERATION_FALLBACK_BACKEND`、`LITELLM_MODEL`、`LITELLM_CONFIG`、`LLM_CHANNELS` 和 provider key | 推荐先用 LiteLLM；确认 provider 连通后再开日报/调度。 |
| Agent（App Server） | `AGENT_BACKEND`、`AGENT_ARCH`、`AGENT_ORCHESTRATOR_TIMEOUT_S`、`AGENT_SKILLS`、`AGENT_SKILL_DIR` | `AGENT_BACKEND=codex_app_server` 影响问股 Chat；不会自动替换日报生成。 |
| Codex CLI generation | `GENERATION_BACKEND=codex_cli`、`GENERATION_BACKEND_TIMEOUT_SECONDS`、`GENERATION_FALLBACK_BACKEND` | **可以**替换普通日报、个股分析和大盘复盘；当前是 generation-only、experimental/limited。 |
| 纸面账户 | `PAPER_AUTO_MODE_ENABLED`、`PAPER_SCHEDULER_ENABLED`、`PAPER_SCHEDULER_INTERVAL_MINUTES` | 先人工审批和单次 `run`；不要一开始打开无人值守。 |
| scheduler | `SCHEDULE_ENABLED`、`SCHEDULE_TIME(S)`、`SCHEDULE_RUN_IMMEDIATELY`、`MARKET_REVIEW_ENABLED` | 本地默认关闭；`web` mode 即使 `.env` 写 true 也会抑制。 |
| Web/认证 | `WEBUI_HOST`、`WEBUI_PORT`、`WEBUI_AUTO_BUILD`、`ADMIN_AUTH_ENABLED`、`TRUST_X_FORWARDED_FOR` | 本机用 `127.0.0.1`；共享网络或公网前必须启用认证。 |
| 数据库/日志 | `DATABASE_PATH`、`SQLITE_WAL_ENABLED`、`SQLITE_BUSY_TIMEOUT_MS`、`LOG_DIR`、`LOG_LEVEL` | 确保 `data/`、`logs/`、`reports/` 可写；不要把数据库放到临时目录。 |
| 通知/微信 | DingTalk、Feishu、Telegram、email、custom webhook、`WECHAT_*` | 只配置要验证的渠道；微信 token 只存 credential store。 |

完整 provider 与 skill 配置参见 [LLM 配置指南](LLM_CONFIG_GUIDE.md)、[完整指南](full-guide.md)、[Paper Account 专题](paper-account.md) 和 [Market Data 专题](market-data.md)。

## 4. 在本地使用 Codex App Server

### 4.1 先理解两条模型路径

- `GENERATION_BACKEND=litellm`：日报、复盘、普通批处理的稳定路径。
- `AGENT_BACKEND=codex_app_server`：只切换 Web 问股 Agent；由 DSA 启动 `codex app-server --stdio`，通过官方 App Server 协议工作。
- `GENERATION_BACKEND=codex_cli`：generation-only 的本地 CLI 实验路径；它不是 App Server，也不是 Agent 工具 fallback。

Codex App Server 的官方协议说明见 [OpenAI Codex App Server 文档](https://developers.openai.com/codex/app-server/)。DSA 不读取或保存 Codex credential 文件；App Server/CLI 自己管理登录态。运行 DSA 的设备、用户、Docker 容器和远程服务器之间登录态互不共享。

### 4.2 用 Codex CLI 作为日报/分析 generation backend（可以）

如果你的目标是“让日报、个股分析、大盘复盘也使用已经登录的 Codex”，当前可以直接使用 `codex_cli`：

```dotenv
# 普通生成路径：日报、个股分析、大盘复盘、generate_text()
GENERATION_BACKEND=codex_cli
# 空值表示不自动回退到 LiteLLM；如果你配置了 API provider，也可以写 litellm
GENERATION_FALLBACK_BACKEND=
GENERATION_BACKEND_TIMEOUT_SECONDS=600
GENERATION_BACKEND_MAX_OUTPUT_BYTES=1048576
LOCAL_CLI_BACKEND_MAX_CONCURRENCY=1

# 问股 Chat 是否使用 App Server 是另一条开关；这里保持默认模型，或单独设为 codex_app_server
AGENT_BACKEND=auto
```

此路径会为每次生成启动一个受限的 Codex CLI 子进程，向它传入 DSA 已准备好的行情/新闻/技能 prompt，再读取最终文本；日报的 JSON 解析、完整性校验和失败处理仍由 DSA 负责。因此它可以生成日报，但不是 Agent Tool Surface：Codex CLI 不会在这条路径里调用 DSA 的行情、Paper 或搜索工具。当前 preset 是只读、非交互、无 streaming（请求 stream 时降级为整段响应），usage telemetry 通常不可用，并标记为 experimental/limited。

### 4.3 安装并确认本机 Codex CLI

按照官方 Codex CLI 安装方式完成安装，再用**启动 DSA 的同一用户**检查：

```bash
command -v codex
codex --version
```

如果在终端中能找到但 Web/Desktop 启动找不到，说明后端进程的 `PATH` 不同；把 `codex` 放进后端可见的 PATH 后，完全重启 DSA。不要把 token、session 或 credential JSON 复制进仓库。

### 4.4 配置 Agent backend（App Server，问股 Chat）

在 `.env` 中显式 opt-in：

```dotenv
AGENT_BACKEND=codex_app_server
AGENT_ARCH=single
AGENT_ORCHESTRATOR_TIMEOUT_S=600
# 如果使用非默认 Codex 登录目录，只写目录路径；不要写 token
# CODEX_HOME=/Users/your-user/.codex
```

重启后打开 Web 的「设置 → Agent 设置」：

1. 选择「Codex 本地 Agent（实验）」并确认架构为「单 Agent」。
2. 查看账户状态；状态页的“可以尝试”只代表配置、命令和协议能力检查通过，不代表已经登录。
3. 选择浏览器登录，或选择 device-code 登录并在自己的浏览器完成授权。
4. 账户状态显示已登录后，回到问股页提出一个无副作用的历史分析问题。
5. 真实问题成功完成，才算模型、登录、工具、超时和回收链路通过。

Codex 当前工具范围是只读的历史分析上下文和回测汇总；实时行情、新闻、持仓、下单工具不会因为打开 Codex 而出现。`AGENT_ORCHESTRATOR_TIMEOUT_S=0` 对默认 LiteLLM 可能仍有旧语义，但 Codex 必须大于 0，否则配置会在保存/运行前拒绝。

### 4.5 恢复默认模型或清理登录态

```dotenv
AGENT_BACKEND=auto
```

重启服务后，问股回到 LiteLLM 路径。要退出 Codex 账户，在设置页使用退出登录；不要手动删除未知目录，除非你已经确认 `CODEX_HOME` 和 CLI 的账号管理方式。

如果只想恢复日报/分析的默认 provider：

```dotenv
GENERATION_BACKEND=litellm
GENERATION_FALLBACK_BACKEND=litellm
```

最常见的组合是：`GENERATION_BACKEND=codex_cli` 负责日报/分析，`AGENT_BACKEND=codex_app_server` 负责问股 Chat；两者可以共用同一个 Codex 登录态，但在 DSA 内是两个不同的执行适配器。

## 5. Paper Account、Shadow 和行情工作台

默认 feature flag 全部关闭。建议按以下顺序启用：

1. 保持 `PAPER_AUTO_MODE_ENABLED=false`，在 `/paper-workbench` 创建虚拟账户。
2. 选择一个股票和观察范围，运行单次 decision cycle，检查 observation cutoff、proposal、risk 和证据。
3. 手动 approve/reject proposal，检查订单 advance/cancel、fills 和绩效。
4. 在 `/shadow` 创建研究 profile，先回测/扫描再查看 signals；Shadow ledger 不等于 Paper ledger。
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

### 9.3 常见问题

- `缺少 fastapi/uvicorn`：使用 `.venv/bin/python -m pip install -r requirements.txt`，不要只安装系统 uvicorn。
- 页面空白或静态资源 404：重新运行 `./scripts/dsa-service.sh build-web`，确认 `static/index.html` 存在。
- API 正常但没有自动分析：确认启动的是 `full`，且 `SCHEDULE_ENABLED=true`；`web` 会有意抑制 scheduler。
- Codex 设置显示“可以尝试”但问股失败：这是预期的分层状态，检查同一用户的 `command -v codex`、`codex --version`、登录态和 `AGENT_ORCHESTRATOR_TIMEOUT_S`，再看服务日志。
- Codex 在终端可用但 Web 不可用：检查 Desktop/IDE 启动时的 PATH；完全重启后端，不要只重开浏览器。
- 模型调用失败：先用 LiteLLM `AGENT_BACKEND=auto` 验证 provider，再单独验证 Codex；不要一次打开所有新 feature flag。
- `database is locked`：确认没有两个 full/scheduler 进程，检查 `DATABASE_PATH`、WAL 和目录权限。

## 10. 本地安全边界

- 不提交 `.env`、API key、OAuth token、微信 token、session secret 或数据库凭据。
- 本地共享网络时也启用 `ADMIN_AUTH_ENABLED=true`；直连公网不要依赖 CORS 或隐藏路径。
- `TRUST_X_FORWARDED_FOR=true` 只适用于一层可信反向代理；本地直连保持 false。
- Codex 子进程由 DSA 以受控环境启动；不要为“方便登录”放宽 Tool Surface 或把 App Server 改成公网 TCP。
- Paper/Shadow 是虚拟/研究账户，不是实盘下单系统。
