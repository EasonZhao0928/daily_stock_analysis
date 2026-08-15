# DSA 服务器部署与运行操作手册

本文档描述当前仓库的非 Docker、Linux、systemd 部署路径，并补充服务器上的 Codex App Server 和 Caddy 公网反向代理。推荐的安全拓扑是：

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

仍需在目标服务器真实验收：provider/API 网络、Codex 登录、行情/通知凭据、Paper/Shadow 业务 smoke、域名 DNS、80/443 防火墙和 Caddy 自动证书。`/api/health` 返回成功只证明 Web 进程可响应，不代表 scheduler、模型、Codex、微信或公网代理全部工作。

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

DATABASE_PATH=./data/stock_analysis.db
LOG_DIR=./logs
LOG_LEVEL=INFO
```

至少补齐：

1. `STOCK_LIST` 或其他股票列表来源；
2. 一个经过验证的 LiteLLM/provider 渠道及 API key；
3. 需要推送时的通知渠道和目标；
4. 需要扩展数据时对应的 `TUSHARE_TOKEN`、TickFlow、Longbridge、Futu 等凭据。

不要把真实 key 写进 systemd unit、Git、命令行参数、日志或本操作手册。个人微信只在 credential store 保存 token，`.env` 只保存 `WECHAT_ILINK_TOKEN_REF` 等引用。历史文档中的 `SHADOW_BACKGROUND_SCAN_ENABLED` 已移除，不要在服务器配置中添加。

### 4.2 服务器环境变量分组

| 分组 | 主要变量 | 服务器建议 |
| --- | --- | --- |
| 普通生成 | `GENERATION_BACKEND`、`GENERATION_FALLBACK_BACKEND`、`LITELLM_CONFIG`、`LLM_CHANNELS`、provider key | 先验证单个 provider，再启用 scheduler。 |
| Agent App Server | `AGENT_BACKEND`、`AGENT_ARCH`、`AGENT_ORCHESTRATOR_TIMEOUT_S`、可选 `CODEX_HOME`/`PATH` | `AGENT_BACKEND=codex_app_server` 影响问股 Chat/Paper proposal；systemd 用户必须有自己的登录态。 |
| Codex CLI generation | `GENERATION_BACKEND=codex_cli`、`GENERATION_BACKEND_TIMEOUT_SECONDS`、`GENERATION_FALLBACK_BACKEND` | 可以替换日报、个股分析和大盘复盘；generation-only、experimental/limited。 |
| Paper/Shadow | `PAPER_AUTO_MODE_ENABLED`、`PAPER_SCHEDULER_ENABLED`、`EXTENDED_MARKET_DATA_ENABLED` | 先人工审批和单次 cycle；确认日志后再无人值守。 |
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

### 7.1 用 Codex CLI 生成日报/分析（可以）

如果服务器上的目标是让普通日报、个股分析和大盘复盘也走 Codex，可以在 `.env` 中使用 generation backend：

```dotenv
GENERATION_BACKEND=codex_cli
GENERATION_FALLBACK_BACKEND=
GENERATION_BACKEND_TIMEOUT_SECONDS=600
GENERATION_BACKEND_MAX_OUTPUT_BYTES=1048576
LOCAL_CLI_BACKEND_MAX_CONCURRENCY=1

# 问股 Chat 仍是独立开关；需要 App Server Chat 时再设为 codex_app_server
AGENT_BACKEND=auto
```

这条路径每次启动受限的 Codex CLI 子进程，读取 DSA 已准备好的分析 prompt，返回文本/JSON 后由 DSA 继续解析和落库。它不会在生成过程中调用 DSA Tool Surface，不等同于 App Server Agent；当前没有完整 streaming/usage telemetry，并且 CLI 参数和版本属于 experimental/limited。服务器服务用户必须能找到并登录 `codex`，因此仍需完成下面的 CLI 可执行文件、PATH 和登录态检查。

`GENERATION_FALLBACK_BACKEND=` 为空表示失败时不切回 LiteLLM；如果服务器同时配置了可用 API provider，可以写 `litellm` 作为兜底。修改后重启 systemd。

### 7.2 服务器与本机是两个登录域

Codex 必须安装、登录在**运行 DSA systemd 服务的服务器用户**上。桌面电脑的 Codex 登录态不会因为 Caddy、SSH 或 `.env` 自动复制到服务器；不要复制 credential 文件或 OAuth token。

按照官方 Codex CLI 安装方式安装后，用服务用户验证：

```bash
sudo -u dsa_user -H sh -lc 'command -v codex && codex --version'
```

如果 systemd 用户不是 `dsa_user`，替换为 unit 中的 `User=`。`codex` 必须在该用户和 systemd 进程的 PATH 中；例如 CLI 装在用户目录时，可以在 `.env` 写目录路径（不要写 token）：

```dotenv
# 仅在 command -v codex 显示的路径不在 systemd 默认 PATH 时设置
PATH=/usr/local/bin:/usr/bin:/bin:/home/dsa_user/.local/bin
# 仅指定 Codex 登录目录；token 仍由 Codex 自己保存
CODEX_HOME=/home/dsa_user/.codex
```

改动 PATH/CODEX_HOME 后重启 systemd，并再次验证：

```bash
sudo systemctl restart daily-stock-analysis
sudo -u dsa_user -H env PATH="/usr/local/bin:/usr/bin:/bin:/home/dsa_user/.local/bin" codex --version
```

### 7.3 启用 App Server 配置

在服务器 `.env` 中加入：

```dotenv
AGENT_BACKEND=codex_app_server
AGENT_ARCH=single
AGENT_ORCHESTRATOR_TIMEOUT_S=600
```

这只改变 Agent Chat；日报、复盘和 scheduler 仍由 `GENERATION_BACKEND` 控制。不要写 `GENERATION_BACKEND=codex_app_server`。

### 7.4 从公网 Web 设置 App Server 登录

Caddy 和 DSA 登录可用后，在自己的电脑浏览器完成：

1. 打开 `https://<your-domain>/`，首次访问按页面提示设置管理员密码并登录。
2. 进入「设置 → Agent 设置」，选择 Codex 本地 Agent，确认 single-agent 和 timeout。
3. 查看账户状态；“可以尝试”不是“已登录”。
4. 点击浏览器登录，若服务器不能打开浏览器则使用 device-code；把页面返回的授权 URL/验证码在自己的电脑完成。
5. 登录成功后回到问股页发送一个无副作用的历史分析问题；成功完成才算真实链路通过。

App Server 是 DSA 后端启动的 `codex app-server --stdio` 子进程，不监听额外 TCP 端口。Caddy 只代理 DSA 的 HTTP/SSE API，绝不直接代理 Codex 进程。

Codex 工具范围、状态语义、停止/超时清理和 single-agent 限制见 [LLM 配置指南](LLM_CONFIG_GUIDE.md)；官方协议见 [Codex App Server 文档](https://developers.openai.com/codex/app-server/)。

### 7.5 登录态排错

```bash
sudo journalctl -u daily-stock-analysis -n 200 --no-pager
sudo systemctl show daily-stock-analysis -p User -p WorkingDirectory
sudo -u dsa_user -H sh -lc 'command -v codex; codex --version; printf "CODEX_HOME=%s\n" "$CODEX_HOME"'
```

常见原因：

- 在桌面用户登录了 Codex，但 systemd 用的是 `dsa_user`；
- `codex` 在 `/home/<user>/.local/bin`，systemd 的 PATH 找不到；
- 服务器无出站网络/DNS，或 Codex 需要的网络策略被阻断；
- `AGENT_ARCH` 不是 single，或 `AGENT_ORCHESTRATOR_TIMEOUT_S=0`；
- 只看了设置页状态，没有发送真实问题验证模型/工具。

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

## 11. 服务器上线验收清单

```text
[ ] SSH 普通用户登录成功；sudo -v 可以交互式验证密码
[ ] personal 分支和目标提交已确认，工作树没有意外 tracked 修改
[ ] .env 权限为 0600，真实 key/token 未进入 Git
[ ] .venv、Python 依赖、Node/npm 和 static/index.html 存在
[ ] systemd User/Group、WorkingDirectory、SERVICE_MODE 已确认
[ ] /api/health 本机返回成功，journal 无持续异常
[ ] ADMIN_AUTH_ENABLED=true，初始密码已设置并能修改/重置
[ ] LiteLLM/provider 做过最小真实问股或生成 smoke
[ ] Codex（如启用）由同一 SERVICE_USER 登录，真实问股成功
[ ] Paper/Shadow/Market/扩展数据按需做过最小 smoke
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
- Codex App Server 登录态由 Codex 自己管理；不要上传/复制 credential 文件，不要在 Caddy 中暴露 App Server stdio。
- Paper/Shadow 是虚拟/研究账户，不提供实盘下单授权。
- Codex 协议：[OpenAI Codex App Server](https://developers.openai.com/codex/app-server/)
- Caddy 安装：[Install](https://caddyserver.com/docs/install)
- Caddy HTTPS：[HTTPS quick start](https://caddyserver.com/docs/quick-starts/https)
- Caddy 反代：[reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
- Caddy systemd：[Running Caddy](https://caddyserver.com/docs/running)
