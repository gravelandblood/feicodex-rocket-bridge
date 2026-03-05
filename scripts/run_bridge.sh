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

exec "$APP_DIR/.venv/bin/python" "$APP_DIR/long_conn.py"
