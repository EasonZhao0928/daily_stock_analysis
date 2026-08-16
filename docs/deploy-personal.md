# Personal 分支部署

> 完整操作请先阅读：[本地运行操作手册](operations-local.md) 和 [服务器部署与运行操作手册](operations-server.md)。长期 Web/API 服务统一通过 `scripts/dsa-service.sh` 启动；systemd 默认使用 `SERVICE_MODE=web`。

本仓库约定：

- `main` 只同步作者仓库的 `upstream/main`。
- `personal` 是服务器唯一部署分支。
- 新功能或修复从 `personal` 创建 `feat/*`、`fix/*` 分支，检查通过后向 `personal` 提 PR。
- `personal` 合并后，`.github/workflows/deploy-personal.yml` 会先运行后端与前端检查，再通过 SSH 将提交部署到服务器。

## 本地分支操作

```bash
git fetch upstream
git switch main
git merge --ff-only upstream/main
git push origin main

git switch personal
git merge --no-ff main
git push origin personal
```

开发改动使用独立分支：

```bash
git switch personal
git pull --ff-only origin personal
git switch -c feat/your-change
# 修改、检查、提交
git push -u origin feat/your-change
gh pr create --base personal --head feat/your-change
```

合并到 `personal` 后会自动部署。若只想重新部署当前提交，可在 GitHub Actions 中手动运行 **Deploy personal**。

## 服务器约定

应用目录默认是 `/opt/trading-projects/code/daily_stock_analysis`，systemd 服务名是 `daily-stock-analysis`，应用只监听 `127.0.0.1:8000`。首次部署会从 `.env.example` 生成服务器本地 `.env`，并启用 `ADMIN_AUTH_ENABLED=true`；之后部署不会覆盖该文件中的 API Key 或其他本地配置。

部署脚本支持两种明确模式：

```bash
# Web/API-only，默认且推荐先使用；不会启动 runtime scheduler
APP_DIR="$PWD" SERVICE_USER="$USER" SERVICE_MODE=web bash scripts/deploy_personal.sh

# Web/API + scheduler；只有 .env 中 SCHEDULE_ENABLED=true 时才会真正调度
APP_DIR="$PWD" SERVICE_USER="$USER" SERVICE_MODE=full bash scripts/deploy_personal.sh
```

systemd 的 `ExecStart` 现在调用 `scripts/dsa-service.sh service`，不再直接守护 `python main.py --serve-only`。一次性分析仍可使用 `.venv/bin/python main.py ...`，但不要与 `SERVICE_MODE=full` 同时启动第二个 scheduler。

常用检查命令：

```bash
sudo systemctl status daily-stock-analysis --no-pager
sudo journalctl -u daily-stock-analysis -f
curl -fsS http://127.0.0.1:8000/api/health
```

GitHub 仓库需要配置以下 Actions secrets：`DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_SSH_KEY`、`DEPLOY_KNOWN_HOSTS`。私钥只放在 GitHub Secrets 和本机安全位置，不提交到仓库。
