#!/usr/bin/env bash

set -Eeuo pipefail

# Override these variables when deploying to a different server layout.
APP_DIR="${APP_DIR:-/opt/trading-projects/code/daily_stock_analysis}"
SERVICE_NAME="${SERVICE_NAME:-daily-stock-analysis}"
SERVICE_HOST="${SERVICE_HOST:-127.0.0.1}"
SERVICE_PORT="${SERVICE_PORT:-8000}"
# The server's outbound route to pypi.org is bandwidth-constrained; use a
# HTTPS mirror by default. Override PIP_INDEX when deploying elsewhere.
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

cd "$APP_DIR"

if [[ "$(git branch --show-current)" != "personal" ]]; then
  echo "STOP: deployment must run from the personal branch" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "STOP: tracked changes exist on the server" >&2
  git status --short
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
fi
chmod 600 .env

set_env() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" .env; then
    sed -i -E "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '\n%s=%s\n' "$key" "$value" >> .env
  fi
}

# Keep the app private behind Caddy and require the app's own login before a
# future public reverse-proxy route is added. Existing API keys are untouched.
set_env WEBUI_HOST "$SERVICE_HOST"
set_env WEBUI_PORT "$SERVICE_PORT"
set_env WEBUI_AUTO_BUILD false
set_env ADMIN_AUTH_ENABLED true

mkdir -p data logs

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

for attempt in 1 2 3; do
  if .venv/bin/python -m pip install \
    --index-url "$PIP_INDEX" \
    --timeout 120 \
    --retries 5 \
    -r requirements.txt; then
    break
  fi
  if [[ "$attempt" -eq 3 ]]; then
    echo "Python dependency installation failed after ${attempt} attempts" >&2
    exit 1
  fi
  echo "Python dependency installation attempt ${attempt} failed; retrying..." >&2
  sleep 10
done

npm --prefix apps/dsa-web ci --prefer-offline --no-audit --no-fund
npm --prefix apps/dsa-web run build

if [[ ! -s static/index.html ]]; then
  echo "STOP: frontend build did not produce static/index.html" >&2
  exit 1
fi

SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
unit_file="$(mktemp)"
trap 'rm -f "$unit_file"' EXIT

sed \
  -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
  -e "s|@SERVICE_GROUP@|$SERVICE_GROUP|g" \
  -e "s|@APP_DIR@|$APP_DIR|g" \
  -e "s|@SERVICE_HOST@|$SERVICE_HOST|g" \
  -e "s|@SERVICE_PORT@|$SERVICE_PORT|g" \
  deploy/daily-stock-analysis.service.in > "$unit_file"

sudo install -m 0644 "$unit_file" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

for attempt in $(seq 1 60); do
  if curl -fsS --max-time 5 "http://${SERVICE_HOST}:${SERVICE_PORT}/api/health" >/dev/null; then
    echo "${SERVICE_NAME} is healthy on ${SERVICE_HOST}:${SERVICE_PORT}"
    exit 0
  fi
  if ! sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    sudo journalctl -u "$SERVICE_NAME" -n 80 --no-pager
    exit 1
  fi
  sleep 2
done

echo "STOP: ${SERVICE_NAME} did not become healthy in time" >&2
sudo systemctl status "$SERVICE_NAME" --no-pager || true
sudo journalctl -u "$SERVICE_NAME" -n 80 --no-pager || true
exit 1
