#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found" >&2
  exit 1
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI not found in PATH" >&2
  echo "Install Codex CLI first, then run this script again." >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r requirements.txt

mkdir -p "$APP_DIR/data/uploads"

if [[ ! -f .env ]]; then
  cp .env.example .env
  "$APP_DIR/.venv/bin/python" - <<'PY'
import secrets
from pathlib import Path
p = Path('.env')
text = p.read_text(encoding='utf-8')
placeholder = 'BRIDGE_API_TOKEN=replace_with_long_random_token'
if placeholder in text:
    text = text.replace(placeholder, f"BRIDGE_API_TOKEN={secrets.token_urlsafe(32)}", 1)
    p.write_text(text, encoding='utf-8')
PY
  echo "Created .env from .env.example"
fi

echo "Init complete."
echo "Next steps:"
echo "1) Edit .env and set FEISHU_APP_ID / FEISHU_APP_SECRET"
echo "2) Ensure Codex is logged in: codex login"
echo "3) Start API:    ./scripts/run_api.sh"
echo "4) Start bridge: ./scripts/run_bridge.sh"
