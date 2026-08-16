# DSA 服务器部署与运行操作手册

本文档描述当前仓库的非 Docker、Linux、systemd 部署路径，并补充服务器上的 Codex App Server 和 Caddy 公网反向代理。推荐的安全拓扑是：

先明确 Codex 的部署边界：当前 DSA 的 `codex_app_server` 是**服务器本地启动 App Server、远端使用 ChatGPT OAuth/模型服务**。服务器仍需安装 `codex` 可执行文件；仅把 OAuth URL 或 token 写进 `.env` 不能让现有 DSA 直接调用远程 App Server。官方文档中的 WebSocket remote mode 目前是 experimental，且当前 DSA transport 只实现 stdio，因此不应把它当作本生产方案。

```text
浏览器 ── HTTPS ──> Caddy :443/:80
                         │ reverse_proxy
                         ▼
                  127.0.0.1:8000
                         │
                 systemd → scripts/dsa-service.sh service
```

Uvicorn 只监听 `127.0.0.1`；不要开放 8000，也不要把 Codex App Server 的 stdio 子进程改成 TCP 服务。公网访问必须同时满足 DSA 登录认证、服务器防火墙和 Caddy TLS。

## 1. 先确认上线边界

当前仓库已经提供：

- `scripts/dsa-service.sh`：`web/full/service/build-web/doctor/health` 统一入口；
- `scripts/deploy_personal.sh`：personal 分支的 venv、依赖、前端构建和 systemd 安装/重启；
- `deploy/daily-stock-analysis.service.in`：普通用户运行、私有监听、自动重启的 unit 模板；
- Codex App Server Agent、Paper Account、Shadow Research、Market Chart、Evidence 和个人微信 iLink 的代码与离线测试。

仍需在目标服务器真实验收：provider/API 网络、Codex 登录和真实模型、行情/通知凭据、Paper/Shadow 业务 smoke、域名 DNS、80/443 防火墙和 Caddy 自动证书。`/api/health` 返回成功只证明 Web 进程可响应，不代表 scheduler、模型、Codex、微信或公网代理全部工作。

## 2. SSH 登录、服务器密码和权限

### 2.1 SSH 密码登录

使用普通服务用户连接服务器；不要把密码写入命令、脚本、GitHub Actions secret 以外的文档或 shell 历史：

```bash
ssh -o IdentitiesOnly=yes <dsa_user>@<server_ip_or_domain>
# SSH 客户端会交互式提示 Password；在提示处输入服务器密码
```

进入服务器后，先缓存本次 sudo 凭据。它会交互式提示 sudo 密码，不要使用 `sshpass` 或把密码拼进命令：

```bash
sudo -v
```

如果当前账号没有 sudo 权限，请让管理员授予部署所需的最小权限；不要用 root 直接运行 DSA。脚本默认需要写 `/etc/systemd/system`，安装 Caddy 需要写 apt keyring/软件源和 `/etc/caddy`。

### 2.2 服务用户原则

`SERVICE_USER` 必须是普通用户，并拥有应用目录中的 `.venv/`、`data/`、`logs/`、`reports/` 读写权限。重要的是：

- DSA systemd 进程以 `SERVICE_USER` 运行；
- Codex CLI 必须能被这个用户的 `PATH` 找到；
- Codex 登录态必须在这个用户（以及相同 `CODEX_HOME`）下完成；
- 在另一个 SSH 用户或桌面用户下登录 Codex，不会自动转移到 systemd 服务。

## 3. 服务器前置条件

以下以 Ubuntu/Debian 为例：

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv python3-pip nodejs npm
```

推荐 Python 3.10+、Node.js 20.19+（推荐 Node 22 LTS）、npm 10+。如果发行版自带 Node 版本过旧，先按发行版规范升级 Node，再执行 DSA 部署。

如果选择 `GENERATION_BACKEND=codex_app_server`、`AGENT_BACKEND=codex_app_server` 或 `GENERATION_BACKEND=codex_cli`，还要按官方 Codex CLI 安装方式给 `SERVICE_USER` 安装 `codex`；`scripts/deploy_personal.sh` 只安装 DSA/Python/前端依赖，不安装 Codex，也不会替你登录。

创建应用目录并确保权限：

```bash
sudo mkdir -p /opt/trading-projects/code
sudo chown "$USER":"$(id -gn)" /opt/trading-projects/code
```

## 4. 首次拉取和 `.env` 配置

```bash
cd /opt/trading-projects/code
REPO_URL="https://github.com/EasonZhao0928/daily_stock_analysis.git"
git clone "$REPO_URL" daily_stock_analysis
cd daily_stock_analysis
git switch personal

cp .env.example .env
chmod 600 .env
${EDITOR:-vi} .env
```

如果仓库来源不是上述 URL，请替换 `REPO_URL`；不要在服务器上用未审查的工作树直接 `git pull` 覆盖 `.env`、数据库或日志。

### 4.1 服务器安全基线

```dotenv
WEBUI_HOST=127.0.0.1
WEBUI_PORT=8000
WEBUI_AUTO_BUILD=false
ADMIN_AUTH_ENABLED=true
TRUST_X_FORWARDED_FOR=false

# 先以 Web/API-only 验证，确认业务后再切 full。
SCHEDULE_ENABLED=false
SCHEDULE_RUN_IMMEDIATELY=false
PAPER_AUTO_MODE_ENABLED=false
PAPER_SCHEDULER_ENABLED=false
EXTENDED_MARKET_DATA_ENABLED=false
WECHAT_CHANNEL_ENABLED=false

GENERATION_BACKEND=litellm
GENERATION_FALLBACK_BACKEND=litellm
AGENT_BACKEND=auto
AGENT_ARCH=single
AGENT_ORCHESTRATOR_TIMEOUT_S=600

# 如果使用 ChatGPT 订阅而不是 LiteLLM API，请改用第 7 节的统一 Codex 配置。
# GENERATION_BACKEND=codex_app_server
# GENERATION_FALLBACK_BACKEND=
# GENERATION_BACKEND_TIMEOUT_SECONDS=300
# GENERATION_BACKEND_MAX_OUTPUT_BYTES=1048576
# GENERATION_BACKEND_MAX_CONCURRENCY=1
# CODEX_MODEL=
# AGENT_BACKEND=codex_app_server
# AGENT_ARCH=single
# CODEX_HOME=/home/dsa_user/.codex
# PATH=/usr/local/bin:/usr/bin:/bin:/home/dsa_user/.local/bin

DATABASE_PATH=./data/stock_analysis.db
LOG_DIR=./logs
LOG_LEVEL=INFO
```

LiteLLM 基线至少补齐：

1. `STOCK_LIST` 或其他股票列表来源；
2. 一个经过验证的 LiteLLM/provider 渠道及 API key；
3. 需要推送时的通知渠道和目标；
4. 需要扩展数据时对应的 `TUSHARE_TOKEN`、TickFlow、Longbridge、Futu 等凭据。

不要把真实 key 写进 systemd unit、Git、命令行参数、日志或本操作手册。个人微信只在 credential store 保存 token，`.env` 只保存 `WECHAT_ILINK_TOKEN_REF` 等引用。历史文档中的 `SHADOW_BACKGROUND_SCAN_ENABLED` 已移除，不要在服务器配置中添加。

### 4.2 服务器环境变量分组

| 分组 | 主要变量 | 服务器建议 |
| --- | --- | --- |
| 普通生成 | `GENERATION_BACKEND`、`GENERATION_FALLBACK_BACKEND`、`GENERATION_BACKEND_TIMEOUT_SECONDS`、`GENERATION_BACKEND_MAX_OUTPUT_BYTES`、`GENERATION_BACKEND_MAX_CONCURRENCY`、`LITELLM_CONFIG`、`LLM_CHANNELS` | `litellm` 需要 provider/API key；`codex_app_server` 需要服务器本地 `codex` 和 ChatGPT managed login。 |
| Codex 账号/进程 | `CODEX_MODEL`、`CODEX_HOME`、`PATH` | 只写模型覆盖、账号目录和 CLI 目录，不写 access/refresh token。systemd 的 `User=` 必须能读取 `CODEX_HOME`。 |
| Agent App Server | `AGENT_BACKEND`、`AGENT_ARCH`、`AGENT_ORCHESTRATOR_TIMEOUT_S` | `AGENT_BACKEND=codex_app_server` 影响问股 Chat/Paper proposal；systemd 用户必须有自己的登录态。 |
| Codex CLI generation | `GENERATION_BACKEND=codex_cli`、`GENERATION_BACKEND_TIMEOUT_SECONDS`、`GENERATION_FALLBACK_BACKEND` | 可以替换日报、个股分析和大盘复盘；需要 CLI，generation-only、experimental/limited。 |
| Paper/Shadow | `PAPER_AUTO_MODE_ENABLED`、`PAPER_SCHEDULER_ENABLED`、`EXTENDED_MARKET_DATA_ENABLED`、`SHADOW_RESEARCH_SOURCE_PRIORITY`、`SHADOW_RESEARCH_SOURCE_TIMEOUT_SECONDS` | 先人工审批和单次 cycle；Shadow 默认跳过 Pytdx、每个日线源最多等待 15 秒；确认日志后再无人值守。 |
| Runtime scheduler | `SCHEDULE_ENABLED`、`SCHEDULE_TIME(S)`、`SCHEDULE_RUN_IMMEDIATELY` | `full` 才会读取；只运行一个 service 实例。 |
| Web/代理 | `WEBUI_HOST=127.0.0.1`、`WEBUI_PORT=8000`、`ADMIN_AUTH_ENABLED=true`、`TRUST_X_FORWARDED_FOR` | Caddy 是唯一公网入口；不开放 8000。 |
| 数据/日志 | `DATABASE_PATH`、SQLite WAL/重试、`LOG_DIR`、`LOG_LEVEL` | 做备份，保证服务用户可写。 |
| 微信/通知 | `WECHAT_*`、DingTalk/Feishu/Telegram/email/custom webhook | opt-in，小流量，单独 smoke。 |

## 5. 用部署脚本安装 systemd

部署脚本默认：

- `APP_DIR=/opt/trading-projects/code/daily_stock_analysis`；
- `SERVICE_NAME=daily-stock-analysis`；
- `SERVICE_HOST=127.0.0.1`、`SERVICE_PORT=8000`；
- `SERVICE_MODE=web`；
- Python 包默认使用清华 PyPI 镜像，可用 `PIP_INDEX` 覆盖。

先确保当前连接仍在服务器、`sudo -v` 已通过，再执行：

```bash
cd /opt/trading-projects/code/daily_stock_analysis

APP_DIR="$PWD" \
SERVICE_USER="$USER" \
SERVICE_GROUP="$(id -gn)" \
SERVICE_MODE=web \
bash scripts/deploy_personal.sh
```

如果使用专门用户，例如 `dsa_user`，明确写出：

```bash
APP_DIR="$PWD" \
SERVICE_USER=dsa_user \
SERVICE_GROUP=dsa_user \
SERVICE_MODE=web \
bash scripts/deploy_personal.sh
```

脚本会检查 personal 分支和已跟踪修改、创建 `.venv`、安装 `requirements.txt`、执行前端 `npm ci`/build、渲染并安装 `/etc/systemd/system/daily-stock-analysis.service`、daemon-reload/enable/restart，然后轮询 `http://127.0.0.1:8000/api/health`。

部署后检查：

```bash
sudo systemctl status daily-stock-analysis --no-pager
curl --fail http://127.0.0.1:8000/api/health
```

脚本只负责 DSA 服务，不安装或登录 Codex，不配置域名，也不会替你创建 Caddy 公网代理。

## 6. systemd 日常管理

```bash
sudo systemctl start daily-stock-analysis
sudo systemctl stop daily-stock-analysis
sudo systemctl restart daily-stock-analysis
sudo systemctl status daily-stock-analysis --no-pager
sudo systemctl enable daily-stock-analysis
sudo systemctl disable daily-stock-analysis

sudo journalctl -u daily-stock-analysis -f
sudo journalctl -u daily-stock-analysis --since "1 hour ago" --no-pager

curl --fail http://127.0.0.1:8000/api/health
```

修改 `.env` 后必须重启：

```bash
sudo systemctl restart daily-stock-analysis
```

### 6.1 启用 runtime scheduler

先保持 Web/API-only 验证成功，再编辑 `.env`：

```dotenv
SCHEDULE_ENABLED=true
SCHEDULE_TIME=18:00
SCHEDULE_TIMES=
SCHEDULE_RUN_IMMEDIATELY=false
```

重新以 `SERVICE_MODE=full` 安装/重启 unit：

```bash
cd /opt/trading-projects/code/daily_stock_analysis
APP_DIR="$PWD" SERVICE_USER="$USER" SERVICE_GROUP="$(id -gn)" SERVICE_MODE=full \
bash scripts/deploy_personal.sh
```

`full` 只是允许 runtime scheduler 按配置工作；`SCHEDULE_ENABLED=false` 仍不会调度。不要再启动 `python main.py --schedule`，否则会产生第二个 scheduler。

## 7. 在服务器上使用 Codex

### 7.1 当前支持的服务器模式

服务器上有三种选择：

| 模式 | 服务器是否安装 `codex` | 账号/模型来源 | 适用范围 |
| --- | ---: | --- | --- |
| `GENERATION_BACKEND=litellm` | 否 | API provider/key | 不使用 ChatGPT 订阅，最容易自动化 |
| `GENERATION_BACKEND=codex_cli` | 是 | CLI 当前登录态 | 普通 Generation，实验性、无 Agent ToolSurface |
| `GENERATION_BACKEND=codex_app_server` | 是 | 官方 App Server managed ChatGPT OAuth/订阅 | 日报/复盘/筛选/轻量 Generation + Agent Chat/Paper |

`codex_app_server` 不是把 DSA 直接连到一个公开“Codex OAuth API”；DSA 会在服务器上启动 `codex app-server --stdio`，由该进程与 ChatGPT 服务通信。因此服务器不安装 `codex` 时，当前实现无法运行 Generation 或 Agent 的 App Server 路径。

### 7.2 统一 Codex（服务器推荐配置）

如果服务器使用 ChatGPT 订阅而不是 LiteLLM API，在 `.env` 中写：

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

# 只在 CLI 不在 systemd 默认 PATH 时设置；不要写 $PATH 或 token
PATH=/usr/local/bin:/usr/bin:/bin:/home/dsa_user/.local/bin
CODEX_HOME=/home/dsa_user/.codex
```

这组配置使受支持的普通生成走 `CodexAppServerGenerationBackend`，问股/Paper 走 `CodexAgentBackend`。普通问股 Chat 使用 `portfolio_readonly` 只读工具；Paper proposal 使用冻结 Observation 的独立 profile，输出后仍需人工审批。`GENERATION_FALLBACK_BACKEND=` 显式为空时 Codex 失败会 fail closed；只有明确接受 API 费用时才把它改为 `litellm`。

### 7.3 安装并验证服务器 Codex（必须与 systemd 用户一致）

按照官方 Codex CLI 安装方式安装到服务器，然后用 unit 中的 `User=` 验证。不要在桌面用户登录后复制 credential 文件：

```bash
SERVICE_USER="$(sudo systemctl show -p User --value daily-stock-analysis)"
sudo -u "$SERVICE_USER" -H sh -lc 'command -v codex && codex --version'
sudo -u "$SERVICE_USER" -H sh -lc 'printf "CODEX_HOME=%s\n" "${CODEX_HOME:-<default>}"'
```

如果 CLI 位于用户目录，确认 `/etc/systemd/system/daily-stock-analysis.service` 的 `EnvironmentFile` 能读到 `.env` 中的绝对 `PATH`/`CODEX_HOME`，然后：

```bash
sudo systemctl daemon-reload
sudo systemctl restart daily-stock-analysis
sudo systemctl show daily-stock-analysis -p User -p Environment --no-pager
sudo -u "$SERVICE_USER" -H env PATH="/usr/local/bin:/usr/bin:/bin:/home/dsa_user/.local/bin" codex --version
```

### 7.4 从公网 Web 完成远程 OAuth 登录

OAuth 授权页面可以在你自己的电脑完成，但账号最终保存在服务器 `SERVICE_USER` 对应的 App Server/Codex 登录目录：

1. 先完成 Caddy、DSA 管理员登录和 `/api/health` 验证。
2. 打开 `https://<your-domain>/`，进入「设置 → Generation/Agent 状态」。
3. 点击 **quick check**；确认 `codex` binary、protocol、account/rate-limit 检查通过。它不发送模型 turn。
4. 公网服务器优先选择 **device-code login**，在自己的浏览器打开返回的 `verification_url` 并输入 `user_code`。browser flow 需要 App Server 的本地 callback，跨机器时可能因 `localhost` 回调位置不一致而失败。
5. 账号显示 authenticated 后，运行显式 Generation JSON smoke；页面会先提示额度风险，必须确认后才发送一次真实模型请求。
6. 再发起无副作用的历史问股，确认 Agent SSE 成功、工具 profile 正确、日志无 token。

App Server 是 DSA 后端启动的 `codex app-server --stdio` 子进程，不监听额外 TCP 端口。Caddy 只代理 DSA 的 HTTP/SSE API，绝不直接代理 Codex 进程。

设置页对应 `POST /api/v1/system/config/generation-backends/quick-check`（无模型）和 `POST /api/v1/system/config/generation-backends/smoke-test`（真实 text/JSON turn）；接口要求管理员会话和同源 CSRF，公网环境不要用未保护的裸 curl 代替设置页操作。

### 7.5 “服务器不安装 Codex”方案的当前结论

当前不能通过环境变量实现。要实现它，需要新增一层远程 App Server transport：在另一台已登录机器运行 App Server WebSocket listener，DSA 通过 `wss://` 连接，并增加 TLS、bearer/capability token、重连、并发、断线和账号归属设计。

官方文档确实描述了 `codex app-server --listen` / `codex --remote`，但同时明确 WebSocket transport 仍是 experimental、并不支持 production workloads；当前 DSA 代码也没有该 client。不要把 Vibe 的内部 OAuth/refresh-token/Responses endpoint 复制到服务器，这违反 ADR-0001/0006 的安全边界。

### 7.6 服务器 Codex 排错和验收

```bash
sudo journalctl -u daily-stock-analysis -n 200 --no-pager
sudo systemctl show daily-stock-analysis -p User -p WorkingDirectory -p Environment --no-pager
SERVICE_USER="$(sudo systemctl show -p User --value daily-stock-analysis)"
sudo ss -ltnp | grep -E ':(8000|4500)\b' || true
sudo -u "$SERVICE_USER" -H sh -lc 'command -v codex; codex --version; printf "CODEX_HOME=%s\n" "${CODEX_HOME:-<default>}"'
```

常见原因：

- 在桌面用户登录了 Codex，但 systemd 用的是 `SERVICE_USER`；
- `codex` 在 `/home/<user>/.local/bin`，systemd 的 PATH 找不到；
- 服务器无出站网络/DNS，或 ChatGPT 登录回调被防火墙拦截；
- `AGENT_ARCH` 不是 `single`，或 `AGENT_ORCHESTRATOR_TIMEOUT_S=0`；
- 只看了设置页“可以尝试”，没有完成 device-code 和真实 Generation/Agent smoke。

## 8. 安装并配置 Caddy 公网反向代理

### 8.1 DNS 和网络前提

先把域名的 A/AAAA 记录指向服务器，并确认云厂商安全组允许 TCP 80/443。DNS 验证：

```bash
dig +short <your-domain>
```

若使用 UFW，先允许 SSH，再允许 HTTP/HTTPS；确认不会断开当前 SSH 后再启用：

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw status
```

### 8.2 安装 Caddy（Ubuntu/Debian 官方包）

先执行 `sudo -v`，下面命令会多次使用 sudo。完整安装说明见 [Caddy Install](https://caddyserver.com/docs/install)。

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo apt update
sudo apt install -y caddy
```

Caddy 的 systemd 服务通常由包安装提供；不要让 Caddy 以 root 运行 DSA。Caddy 只需要读取自己的配置并代理到 localhost。

### 8.3 Caddyfile

将 `<your-domain>` 替换为真实域名。以下配置将所有 HTTP/API/SSE 请求代理到 DSA，开启压缩；不需要额外把 `/api` 分段代理：

```caddyfile
<your-domain> {
    encode gzip zstd
    reverse_proxy 127.0.0.1:8000
}
```

编辑 `/etc/caddy/Caddyfile`（可以使用 `sudoedit`，避免把配置写入错误路径）：

```bash
sudoedit /etc/caddy/Caddyfile
```

校验、格式化并加载：

```bash
sudo caddy fmt --overwrite /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable caddy
sudo systemctl restart caddy
sudo systemctl status caddy --no-pager
```

Caddy 在域名解析正确且 80/443 可达时会自动申请/续期 HTTPS 证书，并把 HTTP 重定向到 HTTPS。具体行为和 `reverse_proxy` 语义见 [Caddy HTTPS quick start](https://caddyserver.com/docs/quick-starts/https)、[reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) 和 [Caddy running](https://caddyserver.com/docs/running)。

### 8.4 公网验收

先在服务器本机确认上游，再从外部网络测试：

```bash
curl --fail http://127.0.0.1:8000/api/health
curl -I https://<your-domain>/
curl --fail https://<your-domain>/api/health
sudo journalctl -u caddy -n 200 --no-pager
```

浏览器验收：

```text
[ ] HTTPS 证书有效且 HTTP 自动跳转
[ ] 首页和静态资源加载
[ ] /api/health 成功
[ ] ADMIN_AUTH_ENABLED=true 时未登录不能访问受保护 API
[ ] 登录、设置、Agent Chat/SSE 正常
[ ] Paper/Shadow/Market 页面和 SSE 不超时
[ ] 8000 端口只绑定 localhost，公网扫描不可直连
[ ] Codex app-server 没有任何公网 TCP 监听
```

不要把 `/docs`、`.env`、数据库文件、日志目录或 Codex credential 目录配置成静态文件目录；Caddyfile 只代理 DSA upstream。

## 9. 密码、配置更新和回滚

### 9.1 Web 管理员密码

首次访问启用认证的 DSA 时，在页面设置初始密码。忘记密码时，使用 systemd 同一个服务用户执行：

```bash
cd /opt/trading-projects/code/daily_stock_analysis
sudo -u dsa_user -H .venv/bin/python -m src.auth reset_password
sudo systemctl restart daily-stock-analysis
```

密码哈希、session secret 和数据库都位于 `DATABASE_PATH` 所在目录，备份时按 secret 处理；不要把明文密码写入命令历史或文档。

### 9.2 代码更新

```bash
cd /opt/trading-projects/code/daily_stock_analysis
git status --short
git switch personal
git pull --ff-only origin personal

APP_DIR="$PWD" SERVICE_USER="$USER" SERVICE_GROUP="$(id -gn)" SERVICE_MODE=web \
bash scripts/deploy_personal.sh
```

脚本会拒绝覆盖服务器上已跟踪的本地修改；不要用 `git reset --hard` 直接覆盖配置和数据。更新后重新执行 health、Web、登录和需要的 feature smoke。

回滚前先确认目标提交并备份：

```bash
tar -czf "/tmp/dsa-backup-$(date +%Y%m%d-%H%M%S).tgz" .env data logs reports
git log --oneline -10
git reset --hard <已验证提交>
APP_DIR="$PWD" SERVICE_USER="$USER" SERVICE_GROUP="$(id -gn)" SERVICE_MODE=web \
bash scripts/deploy_personal.sh
```

`.env`、`data/`、`logs/` 和 `reports/` 不应通过 Git 回滚；备份文件包含敏感数据，按服务器权限保护。

## 10. GitHub Actions 自动部署（可选）

仓库的 `Deploy personal` workflow 会在 `personal` 分支 push 后执行检查，再通过 SSH 调用 `scripts/deploy_personal.sh`。需要配置：

- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_SSH_KEY`
- `DEPLOY_KNOWN_HOSTS`

workflow 默认 `SERVICE_MODE=web`，不会因为 `.env` 误写 `SCHEDULE_ENABLED=true` 就自动启动 scheduler。要切 full，请在服务器明确执行一次 `SERVICE_MODE=full` 部署并记录变更。

Actions 不会替服务器安装/登录 Codex，不会替个人微信扫码，也不会替你创建 Caddy/TLS；这些都必须在服务器上按本手册完成。

## 11. 服务器外部验收与上线清单

### 11.1 真实 OAuth / 模型调用

按以下顺序验收，不要把 `/api/health` 当作模型验收：

```bash
sudo systemctl is-active --quiet daily-stock-analysis
curl --fail http://127.0.0.1:8000/api/health
SERVICE_USER="$(sudo systemctl show -p User --value daily-stock-analysis)"
sudo -u "$SERVICE_USER" -H sh -lc 'command -v codex && codex --version'
```

然后在公网 Web「设置」中：

1. 运行 Codex **quick check**，确认 binary、protocol、account 和 rate limits；这一步不消耗模型额度。
2. 用 device-code 完成 ChatGPT OAuth（跨机器优先使用 device-code；browser flow 可能把 localhost callback 指向错误的机器）。
3. 确认 account 为 authenticated、plan/rate limits 可读，且 UI/日志不出现 token。
4. 运行显式 JSON smoke，先确认额度风险，再执行一次真实模型请求。
5. 运行一个关闭通知、单标的的日报/复盘，检查 effective backend 为 `codex_app_server`。
6. 发送一个只读历史问股，检查 Agent SSE、profile 工具、取消和超时清理。

失败时查看：

```bash
sudo journalctl -u daily-stock-analysis -n 200 --no-pager
sudo systemctl show daily-stock-analysis -p User -p WorkingDirectory -p Environment --no-pager
```

`GENERATION_FALLBACK_BACKEND=` 为空时，未登录、额度耗尽、进程失败和不支持能力都必须 fail closed；若日志出现 LiteLLM 调用，说明配置或路由验收失败。

### 11.2 在线数据源

在服务器本机执行至少一条行情接口，并在 Web Market 页面检查 source/stale/limitations：

```bash
curl --fail 'http://127.0.0.1:8000/api/v1/market/600519/candles?period=daily&limit=5'
curl --fail 'http://127.0.0.1:8000/api/v1/market/600519/snapshot'
```

若启用了 Tushare、TickFlow、Longbridge、Futu 或搜索 provider，再执行一个真实日报/市场复盘并记录实际 source、权限、超时、fallback 和 stale 状态。认证中间件返回 401 时，在已登录 Web 中重复相同操作；不要用 health 代替数据源证据。

### 11.3 CI 验收

从开发机或有 GitHub CLI 权限的环境确认分支对应的 workflow 全部成功：

```bash
gh run list --workflow ci.yml --branch personal --limit 5
gh run view <run-id> --log-failed
gh run list --workflow deploy-personal.yml --branch personal --limit 5
```

至少确认 `backend-tests` 三个 shard、`backend-gate`、`web-gate`（以及本次变更触发的 Docker gate）为 `success`。CI 默认不需要真实 ChatGPT 凭据；部署 workflow 也不会代服务器安装/登录 Codex、完成微信扫码或配置 Caddy。

### 11.4 systemd/Caddy/生产访问

```bash
sudo systemctl status daily-stock-analysis --no-pager
sudo systemctl status caddy --no-pager
curl --fail http://127.0.0.1:8000/api/health
curl -I https://<your-domain>/
curl --fail https://<your-domain>/api/health
sudo ss -ltnp | grep -E ':(8000|4500)\b' || true
```

预期：DSA 只监听 `127.0.0.1:8000`，公网只看到 Caddy `:80/:443`，没有 Codex `:4500` 或其他 App Server TCP 监听；浏览器还需验证管理员认证、登录、Generation/Agent、SSE、Paper/Shadow、数据源和 scheduler 单实例。

```text
[ ] SSH 普通用户登录成功；sudo -v 可以交互式验证密码
[ ] personal 分支和目标提交已确认，工作树没有意外 tracked 修改
[ ] .env 权限为 0600，真实 key/token 未进入 Git
[ ] .venv、Python 依赖、Node/npm 和 static/index.html 存在
[ ] systemd User/Group、WorkingDirectory、SERVICE_MODE 已确认
[ ] /api/health 本机返回成功，journal 无持续异常
[ ] ADMIN_AUTH_ENABLED=true，初始密码已设置并能修改/重置
[ ] 选择 LiteLLM 时 provider 做过最小真实生成；选择 Codex 时 `codex` 由同一 SERVICE_USER 可执行
[ ] Codex quick check、OAuth/device-code、额度确认 JSON smoke 和真实问股/日报均成功（如启用）
[ ] `GENERATION_BACKEND` 与 `AGENT_BACKEND` 的 effective backend 与预期一致
[ ] 在线行情/搜索/扩展数据按需做过真实调用，并记录 source/stale/fallback
[ ] Paper/Shadow/Market/扩展数据按需做过最小 smoke
[ ] CI 的 backend shards、backend-gate、web-gate 和相关 Docker gate 均 success
[ ] full 模式只有一个 scheduler，SCHEDULE_RUN_IMMEDIATELY 符合预期
[ ] DNS A/AAAA 指向服务器，云安全组和 UFW 允许 80/443/SSH
[ ] Caddyfile validate 通过，Caddy active，HTTPS 证书有效
[ ] Caddy 公网访问首页、登录、API、SSE 正常
[ ] 8000 未对公网开放；Codex app-server 无额外 TCP 监听
[ ] .env/data/logs/reports 有备份和恢复方案
```

## 12. 安全边界与官方参考

- DSA 公网入口是 Caddy HTTPS + DSA 管理员认证；不要仅依赖 CORS、隐藏路径或 `/docs` 关闭。
- `TRUST_X_FORWARDED_FOR=true` 只在“一层可信 Caddy → DSA”时考虑；不要在直连公网时打开。
- Codex App Server 登录态由 Codex 自己管理；不要上传/复制 credential 文件，不要在 Caddy 中暴露 App Server stdio。当前 DSA 不支持远程 `wss://` App Server transport。
- Paper/Shadow 是虚拟/研究账户，不提供实盘下单授权。
- Codex 协议：[OpenAI Codex App Server](https://developers.openai.com/codex/app-server/)
- Caddy 安装：[Install](https://caddyserver.com/docs/install)
- Caddy HTTPS：[HTTPS quick start](https://caddyserver.com/docs/quick-starts/https)
- Caddy 反代：[reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
- Caddy systemd：[Running Caddy](https://caddyserver.com/docs/running)
