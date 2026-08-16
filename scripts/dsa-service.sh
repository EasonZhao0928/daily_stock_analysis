#!/usr/bin/env bash

# DSA service launcher.
#
# This wrapper deliberately starts the ASGI application through uvicorn
# (server:app) instead of routing long-running Web/API processes through the
# batch-oriented main.py CLI.  ``server.py`` still owns environment loading,
# logging setup, and the FastAPI application import.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"

usage() {
  cat <<'EOF'
DSA service launcher

Usage:
  scripts/dsa-service.sh web [--host HOST] [--port PORT] [--reload]
  scripts/dsa-service.sh full [--host HOST] [--port PORT]
  scripts/dsa-service.sh service [--host HOST] [--port PORT]
  scripts/dsa-service.sh build-web
  scripts/dsa-service.sh doctor
  scripts/dsa-service.sh health [--host HOST] [--port PORT]

Modes:
  web      Start FastAPI/WebUI only; scheduler is explicitly suppressed.
  full     Start FastAPI/WebUI and let SCHEDULE_ENABLED control the runtime scheduler.
  service  Systemd entrypoint; uses DSA_SERVICE_MODE (web|full, default web).
  build-web Install frontend lockfile dependencies and build static assets.
  doctor   Validate .env, Python/uvicorn, writable runtime directories and frontend build.
  health   Query /api/health without starting a process.

Environment overrides:
  DSA_PYTHON   Python executable (default: .venv/bin/python, then python3).
  ENV_FILE     dotenv file; defaults to the repository .env when present.
  DSA_HOST     Bind host (fallback: WEBUI_HOST, then 127.0.0.1).
  DSA_PORT     Bind port (fallback: WEBUI_PORT, then 8000).
  UVICORN_LOG_LEVEL  Uvicorn log level (default: info).
  DSA_SERVICE_MODE   Systemd mode: web or full (default: web).
EOF
}

die() {
  echo "dsa-service: $*" >&2
  exit 1
}

if [[ -z "${ENV_FILE:-}" && -f "$APP_DIR/.env" ]]; then
  export ENV_FILE="$APP_DIR/.env"
fi

resolve_python() {
  if [[ -n "${DSA_PYTHON:-}" ]]; then
    printf '%s\n' "$DSA_PYTHON"
    return
  fi
  if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
    printf '%s\n' "$APP_DIR/.venv/bin/python"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  die "未找到 Python；请创建 .venv 或设置 DSA_PYTHON。"
}

PYTHON_BIN="$(resolve_python)"

require_python_runtime() {
  if ! "$PYTHON_BIN" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
    die "当前 Python 缺少 fastapi/uvicorn：$PYTHON_BIN；请先安装 requirements.txt。"
  fi
}

parse_common_options() {
  HOST="${DSA_HOST:-${WEBUI_HOST:-127.0.0.1}}"
  PORT="${DSA_PORT:-${WEBUI_PORT:-8000}}"
  RELOAD=false

  while (($# > 0)); do
    case "$1" in
      --host)
        (($# >= 2)) || die "--host 需要参数"
        HOST="$2"
        shift 2
        ;;
      --port)
        (($# >= 2)) || die "--port 需要参数"
        PORT="$2"
        shift 2
        ;;
      --reload)
        RELOAD=true
        shift
        ;;
      --no-reload)
        RELOAD=false
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "未知参数：$1"
        ;;
    esac
  done
}

build_web() {
  command -v npm >/dev/null 2>&1 || die "未找到 npm；请安装 Node.js 22+。"
  npm ci --prefix "$APP_DIR/apps/dsa-web" --prefer-offline --no-audit --no-fund
  npm run build --prefix "$APP_DIR/apps/dsa-web"
  [[ -s "$APP_DIR/static/index.html" ]] || die "前端构建未生成 static/index.html。"
}

doctor() {
  [[ -f "$APP_DIR/.env" ]] || die "缺少 .env；请先 cp .env.example .env 并填写配置。"
  require_python_runtime
  for runtime_dir in data logs reports; do
    mkdir -p "$APP_DIR/$runtime_dir"
    [[ -w "$APP_DIR/$runtime_dir" ]] || die "目录不可写：$APP_DIR/$runtime_dir"
  done
  [[ -s "$APP_DIR/static/index.html" ]] || die "缺少静态前端；请先运行 scripts/dsa-service.sh build-web。"
  echo "DSA doctor: OK"
  echo "  app_dir=$APP_DIR"
  echo "  python=$PYTHON_BIN"
  echo "  env_file=${ENV_FILE:-<default .env> }"
  echo "  frontend=$APP_DIR/static/index.html"
}

health() {
  parse_common_options "$@"
  command -v curl >/dev/null 2>&1 || die "未找到 curl。"
  curl --fail --show-error --silent "http://${HOST}:${PORT}/api/health"
  printf '\n'
}

serve() {
  local mode="$1"
  shift
  parse_common_options "$@"
  require_python_runtime

  mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/reports"
  cd "$APP_DIR"

  HOST_NORMALIZED="$(printf '%s' "$HOST" | tr '[:upper:]' '[:lower:]')"
  case "$HOST_NORMALIZED" in
    0.0.0.0|::|'[::]'|'*')
      echo "WARNING: DSA is binding a public interface ($HOST); verify ADMIN_AUTH_ENABLED=true and use a trusted proxy/firewall." >&2
      ;;
  esac

  if [[ "$mode" == "web" ]]; then
    # api.app's lifespan consumes this flag and will not start the runtime
    # scheduler, even if a shared .env has SCHEDULE_ENABLED=true.
    export RUNTIME_SCHEDULER_SUPPRESS_START=true
  else
    unset RUNTIME_SCHEDULER_SUPPRESS_START || true
  fi

  uvicorn_args=(
    -m uvicorn server:app
    --host "$HOST"
    --port "$PORT"
    --log-level "${UVICORN_LOG_LEVEL:-info}"
  )
  if [[ "$RELOAD" == true ]]; then
    uvicorn_args+=(--reload)
  fi

  echo "DSA ${mode} service: http://${HOST}:${PORT}"
  exec "$PYTHON_BIN" "${uvicorn_args[@]}"
}

COMMAND="${1:-web}"
shift || true

case "$COMMAND" in
  web)
    serve web "$@"
    ;;
  full)
    serve full "$@"
    ;;
  service)
    SERVICE_MODE="${DSA_SERVICE_MODE:-web}"
    case "$SERVICE_MODE" in
      web|full) serve "$SERVICE_MODE" "$@" ;;
      *) die "DSA_SERVICE_MODE 必须是 web 或 full，当前为：$SERVICE_MODE" ;;
    esac
    ;;
  build-web)
    (($# == 0)) || die "build-web 不接受额外参数"
    build_web
    ;;
  doctor)
    (($# == 0)) || die "doctor 不接受额外参数"
    doctor
    ;;
  health)
    health "$@"
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    die "未知命令：$COMMAND"
    ;;
esac
