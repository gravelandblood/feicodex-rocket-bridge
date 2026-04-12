# AGENTS

This file stores repository-local operating rules for code and ops actions.

## Remote Server Mapping
- `想法运维` means remote host `xiangfa-ops` (alias `tx-legacy-1`).
- `赤兔AI` means remote host `chitu-ai` (alias `redhare-ai-1`).
- For requests like "更新想法运维/赤兔AI服务", default target repo is:
  - `/root/bridgespace/feicodex-rocket-bridge`
  - update command: `git pull --ff-only origin main`
  - then restart: `feicodex-rocket-api.service` and `feicodex-rocket-bridge.service`

## SSH Quoting Rules
- Avoid deeply nested shell quotes for remote execution.
- Prefer one of these patterns:
  - `ssh <host> 'bash -lc '"'"'<command>'"'"''`
  - `ssh <host> <<'EOF' ... EOF` for multi-line commands.
- Do not embed local command substitution into remote command strings by accident.
- If a command is complex, write a short local script and run it remotely, or split into multiple small remote calls.

## Delivery Rules
- After finishing any feature/code change, commit the code locally first before any deployment/update action.
- For remote server updates, always use git-based update flow in target repo:
  - `git pull --ff-only origin main`
  - then restart required services.
