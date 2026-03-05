# FeiCodex Rocket Bridge

Independent Feishu bot bridge based on `codex app-server`.

This repository does not depend on `feishu_codex_bridge` source code.

## Features

- Text messages are sent directly to Codex.
- Attachments are downloaded and staged for the next turn.
- Menu actions open management cards.
- Card actions support project/session/model workflows.

## Prerequisites

- Linux host with `python3` and `venv`
- Codex CLI available in `PATH` (`codex`) and logged in (`codex login`)
- Feishu app credentials (`FEISHU_APP_ID`, `FEISHU_APP_SECRET`)

## Quick Start

```bash
./scripts/init.sh
```

Then edit `.env` and set at least:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

Start services in two terminals:

```bash
./scripts/run_api.sh
```

```bash
./scripts/run_bridge.sh
```

## Feishu Console Setup

In app event/callback configuration:

- Subscribe event: `im.message.receive_v1`
- Subscribe event: `application.botmenu.v6`
- Configure callback: `card.action.trigger`

In app menu, keep keys:

- `menu_project_manage`
- `menu_session_manage`

These keys map via `BRIDGE_MENU_ACTIONS_JSON` in `.env`.

## Environment

See [.env.example](./.env.example). Defaults are repository-local:

- state file: `./data/state.json`
- uploads dir: `./data/uploads`
- default cwd: `.`

Both `app.py` and `long_conn.py` auto-load `.env`.

## HTTP Control API

Prefix: `/appbridge/api` (configurable by `BRIDGE_API_PREFIX`).

- `GET /chat/{chat_id}/status`
- `POST /chat/{chat_id}/thread/reset`
- `POST /chat/{chat_id}/turn`
- `POST /chat/{chat_id}/interrupt`

Auth header:

- `Authorization: Bearer <BRIDGE_API_TOKEN>`

## Smoke Test

```bash
./.venv/bin/python smoke_test.py
```

## systemd (optional)

Template files:

- `feicodex-rocket-api.service.example`
- `feicodex-rocket-bridge.service.example`

Replace `__APP_DIR__` with absolute path before installing.

Example:

```bash
APP_DIR="$(pwd)"
sed "s|__APP_DIR__|$APP_DIR|g" feicodex-rocket-api.service.example > /etc/systemd/system/feicodex-rocket-api.service
sed "s|__APP_DIR__|$APP_DIR|g" feicodex-rocket-bridge.service.example > /etc/systemd/system/feicodex-rocket-bridge.service
systemctl daemon-reload
systemctl enable --now feicodex-rocket-api.service feicodex-rocket-bridge.service
```
