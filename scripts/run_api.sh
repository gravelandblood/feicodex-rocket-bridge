#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "Missing virtualenv. Run ./scripts/init.sh first." >&2
  exit 1
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "Missing .env. Run ./scripts/init.sh first." >&2
  exit 1
fi

HOST="${BRIDGE_API_HOST:-127.0.0.1}"
PORT="${BRIDGE_API_PORT:-18788}"
LOG_LEVEL="${UVICORN_LOG_LEVEL:-info}"

exec "$APP_DIR/.venv/bin/python" -m uvicorn app:APP --host "$HOST" --port "$PORT" --log-level "$LOG_LEVEL"
