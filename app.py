#!/usr/bin/env python3
from __future__ import annotations

import atexit
import base64
import html
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from appserver_client import AppServerDisconnected, AppServerError, AppServerTimeout, CodexAppServerClient
from history_store import BridgeHistoryStore
from state_store import BridgeStateStore

LOG = logging.getLogger("feicodex_rocket_bridge")

APP_DIR = Path(__file__).resolve().parent
HISTORY_WEB_DIST_DIR = APP_DIR / "web" / "history-dashboard" / "dist"
load_dotenv(APP_DIR / ".env", override=False)
DATA_DIR = APP_DIR / "data"
STATE_PATH = os.environ.get("BRIDGE_STATE_PATH", str(DATA_DIR / "state.json"))
API_TOKEN = os.environ.get("BRIDGE_API_TOKEN", "")
API_PREFIX = os.environ.get("BRIDGE_API_PREFIX", "/appbridge/api")
DEFAULT_CWD = os.environ.get("BRIDGE_DEFAULT_CWD", str(APP_DIR))
DEFAULT_MODEL = os.environ.get("BRIDGE_DEFAULT_MODEL", "gpt-5.3-codex")
DEFAULT_SANDBOX = os.environ.get("BRIDGE_DEFAULT_SANDBOX", "danger-full-access")
DEFAULT_APPROVAL = os.environ.get("BRIDGE_DEFAULT_APPROVAL_POLICY", "never")
DEFAULT_PERSONALITY = os.environ.get("BRIDGE_DEFAULT_PERSONALITY", "pragmatic")
DEFAULT_TURN_TIMEOUT_SEC = int(os.environ.get("BRIDGE_TURN_TIMEOUT_SEC", "21600"))
IDLE_EVICT_SEC = max(0, int(os.environ.get("BRIDGE_IDLE_EVICT_SEC", "600")))
IDLE_SWEEP_INTERVAL_SEC = max(10, int(os.environ.get("BRIDGE_IDLE_SWEEP_INTERVAL_SEC", "60")))
AUTO_AUTH_SWITCH_ENABLED = str(os.environ.get("BRIDGE_AUTO_AUTH_SWITCH_ENABLED", "true")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTO_AUTH_SWITCH_THRESHOLD_PCT = max(1, min(100, int(os.environ.get("BRIDGE_AUTO_AUTH_SWITCH_THRESHOLD_PCT", "100"))))
AUTH_HEALTH_CHECK_DEFAULT_MODE = str(os.environ.get("BRIDGE_AUTH_HEALTH_CHECK_DEFAULT_MODE", "real_turn")).strip().lower() or "real_turn"
AUTH_REAL_HEALTH_CHECK_PROMPT = str(os.environ.get("BRIDGE_AUTH_REAL_HEALTH_CHECK_PROMPT", "只回复OK")).strip() or "只回复OK"
AUTH_REAL_HEALTH_CHECK_TIMEOUT_SEC = max(
    10,
    min(300, int(os.environ.get("BRIDGE_AUTH_REAL_HEALTH_CHECK_TIMEOUT_SEC", "90"))),
)
AUTH_REAL_HEALTH_CHECK_FAIL_DISABLE_SEC = max(60, int(os.environ.get("BRIDGE_AUTH_REAL_HEALTH_CHECK_FAIL_DISABLE_SEC", "900")))
DISCONNECT_SELF_HEAL_ENABLED = str(os.environ.get("BRIDGE_DISCONNECT_SELF_HEAL_ENABLED", "true")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DISCONNECT_SELF_HEAL_THRESHOLD = max(1, int(os.environ.get("BRIDGE_DISCONNECT_SELF_HEAL_THRESHOLD", "2")))
DISCONNECT_SELF_HEAL_WINDOW_SEC = max(30, int(os.environ.get("BRIDGE_DISCONNECT_SELF_HEAL_WINDOW_SEC", "900")))
DISCONNECT_SELF_HEAL_REBUILD_ENABLED = str(
    os.environ.get("BRIDGE_DISCONNECT_SELF_HEAL_REBUILD_ENABLED", "true")
).strip().lower() in {"1", "true", "yes", "on"}
DISCONNECT_SELF_HEAL_FORCE_REBUILD_ON_AUTH_HEADER_ERROR = str(
    os.environ.get("BRIDGE_DISCONNECT_SELF_HEAL_FORCE_REBUILD_ON_AUTH_HEADER_ERROR", "true")
).strip().lower() in {"1", "true", "yes", "on"}
DISCONNECT_SELF_HEAL_BACKUP_LIMIT = max(1, int(os.environ.get("BRIDGE_DISCONNECT_SELF_HEAL_BACKUP_LIMIT", "6")))

_state_path = Path(STATE_PATH).expanduser()
if not _state_path.is_absolute():
    _state_path = APP_DIR / _state_path
STORE = BridgeStateStore(str(_state_path.resolve()))


def _resolve_env_path(raw: str) -> Path:
    p = Path(str(raw or "")).expanduser()
    if not p.is_absolute():
        p = APP_DIR / p
    return p.resolve()


AUTH_PROFILES_DIR = _resolve_env_path(os.environ.get("BRIDGE_AUTH_PROFILES_DIR", str(DATA_DIR / "auth_profiles")))
AUTH_HOMES_DIR = _resolve_env_path(os.environ.get("BRIDGE_AUTH_HOMES_DIR", str(DATA_DIR / "auth_homes")))
RUNTIME_HOMES_DIR = _resolve_env_path(os.environ.get("BRIDGE_RUNTIME_HOMES_DIR", str(DATA_DIR / "runtime_homes")))
AUTH_REGISTRY_PATH = _resolve_env_path(
    os.environ.get("BRIDGE_AUTH_REGISTRY_PATH", str(DATA_DIR / "auth_profiles_registry.json"))
)
AUTH_DISPATCH_FENCE_PATH = _resolve_env_path(
    os.environ.get("BRIDGE_AUTH_DISPATCH_FENCE_PATH", str(DATA_DIR / "auth_dispatch_fence.json"))
)
AUTH_CONTROL_REGISTRY_PATH = _resolve_env_path(
    os.environ.get("BRIDGE_AUTH_CONTROL_REGISTRY_PATH", str(DATA_DIR / "auth_control_registry.json"))
)
AUTH_CONTROL_NODES_JSON = str(os.environ.get("BRIDGE_AUTH_CONTROL_NODES_JSON", "[]")).strip() or "[]"
AUTH_CONTROL_DEFAULT_LEASE_SEC = max(60, int(os.environ.get("BRIDGE_AUTH_CONTROL_DEFAULT_LEASE_SEC", "86400")))
AUTH_CONTROL_MAX_LEASE_SEC = max(
    AUTH_CONTROL_DEFAULT_LEASE_SEC,
    int(os.environ.get("BRIDGE_AUTH_CONTROL_MAX_LEASE_SEC", "604800")),
)
AUTH_CONTROL_MAX_AUDIT = max(100, int(os.environ.get("BRIDGE_AUTH_CONTROL_MAX_AUDIT", "2000")))
DEFAULT_CODEX_HOME = _resolve_env_path(os.environ.get("BRIDGE_DEFAULT_CODEX_HOME", str(Path.home() / ".codex")))
BRIDGE_MCP_SERVER_NAME = str(os.environ.get("BRIDGE_MCP_SERVER_NAME", "feishu-bridge-files")).strip() or "feishu-bridge-files"
BRIDGE_MCP_SERVER_PATH = _resolve_env_path(os.environ.get("BRIDGE_MCP_SERVER_PATH", str(APP_DIR / "bridge_mcp_server.py")))
BRIDGE_MCP_PYTHON = str(Path(os.environ.get("BRIDGE_MCP_PYTHON", sys.executable)).expanduser())
BRIDGE_MCP_REPLY_CONTEXT_PATH = _resolve_env_path(
    os.environ.get("BRIDGE_MCP_REPLY_CONTEXT_PATH", str(DATA_DIR / "reply_context.json"))
)
PROJECTS_STORE_PATH = _resolve_env_path(os.environ.get("BRIDGE_PROJECTS_STORE_PATH", str(DATA_DIR / "projects.json")))
HISTORY_PATH = _resolve_env_path(os.environ.get("BRIDGE_HISTORY_PATH", str(DATA_DIR / "history.json")))
HISTORY_DB_PATH = _resolve_env_path(os.environ.get("BRIDGE_HISTORY_DB_PATH", str(DATA_DIR / "history.db")))
HISTORY_MAX_TURNS = max(100, int(os.environ.get("BRIDGE_HISTORY_MAX_TURNS", "2000")))
HISTORY_STORE = BridgeHistoryStore(str(HISTORY_DB_PATH), max_turns=HISTORY_MAX_TURNS, legacy_json_path=str(HISTORY_PATH))
USER_CHAT_MAP_PATH = _resolve_env_path(os.environ.get("BRIDGE_USER_CHAT_MAP_PATH", str(DATA_DIR / "user_chat_map.json")))
FEISHU_APP_ID = str(os.environ.get("FEISHU_APP_ID", "")).strip()
FEISHU_APP_SECRET = str(os.environ.get("FEISHU_APP_SECRET", "")).strip()
HISTORY_ALLOWED_OPEN_IDS_RAW = str(os.environ.get("HISTORY_ALLOWED_OPEN_IDS", "")).strip()
HISTORY_SESSION_SECRET = str(os.environ.get("HISTORY_SESSION_SECRET", API_TOKEN or "history-session-secret")).strip()
HISTORY_SESSION_TTL_SEC = max(300, int(os.environ.get("HISTORY_SESSION_TTL_SEC", "604800")))
HISTORY_COOKIE_NAME = str(os.environ.get("HISTORY_COOKIE_NAME", "feicodex_history_session")).strip() or "feicodex_history_session"
FEISHU_OAUTH_AUTHORIZE_URL = str(
    os.environ.get("FEISHU_OAUTH_AUTHORIZE_URL", "https://accounts.feishu.cn/open-apis/authen/v1/authorize")
).strip()
FEISHU_OAUTH_TOKEN_URLS = [
    str(os.environ.get("FEISHU_OAUTH_TOKEN_URL", "https://open.feishu.cn/open-apis/authen/v1/access_token")).strip(),
    "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
]
FEISHU_OAUTH_USERINFO_URL = str(
    os.environ.get("FEISHU_OAUTH_USERINFO_URL", "https://open.feishu.cn/open-apis/authen/v1/user_info")
).strip()

_AUTH_DISPATCH_FENCE_LOCK = threading.Lock()
_AUTH_CONTROL_LOCK = threading.Lock()


class TurnRequest(BaseModel):
    text: str = Field(min_length=1, description="User input text")
    image_paths: list[str] = Field(default_factory=list, description="Optional local image paths")
    cwd: str = Field(default="")
    model: str = Field(default="")
    sandbox: str = Field(default="")
    approval_policy: str = Field(default="")
    personality: str = Field(default="")
    timeout_sec: int = Field(default=DEFAULT_TURN_TIMEOUT_SEC, ge=5, le=86400)
    reset_thread: bool = Field(default=False)


class SteerTurnRequest(BaseModel):
    text: str = Field(min_length=1, description="Steer text")
    image_paths: list[str] = Field(default_factory=list, description="Optional local image paths")
    expected_turn_id: str = Field(default="")


class ResetThreadRequest(BaseModel):
    cwd: str = Field(default="")
    model: str = Field(default="")
    sandbox: str = Field(default="")
    approval_policy: str = Field(default="")
    personality: str = Field(default="")


class InterruptTurnRequest(BaseModel):
    turn_id: str = Field(default="")


class UpdateChatConfigRequest(BaseModel):
    cwd: str = Field(default="")
    model: str = Field(default="")
    sandbox: str = Field(default="")
    approval_policy: str = Field(default="")
    personality: str = Field(default="")


class UpdateChatAuthProfileRequest(BaseModel):
    profile: str = Field(default="")


class HistoryAuthSwitchRequest(BaseModel):
    project: str = Field(default="")
    chat_id: str = Field(default="")
    profile: str = Field(default="")


class HistoryAuthHealthCheckRequest(BaseModel):
    profile: str = Field(default="")
    mode: str = Field(default="")
    prompt: str = Field(default="")
    timeout_sec: int = Field(default=0, ge=0, le=1800)


class AuthProfileUploadRequest(BaseModel):
    profile: str = Field(min_length=1)
    provider: str = Field(default="codex")
    auth_json: Any = Field(default_factory=dict)
    config_toml: str = Field(default="")
    assignment_version: int = Field(default=0, ge=0)
    assignment_token: str = Field(default="")
    assigned_server_id: str = Field(default="")
    notes: str = Field(default="")


class AuthProfileRemoveRequest(BaseModel):
    profile: str = Field(min_length=1)
    assignment_version: int = Field(default=0, ge=0)
    assignment_token: str = Field(default="")
    assigned_server_id: str = Field(default="")
    reason: str = Field(default="")


class AuthApiHealthCheckRequest(BaseModel):
    profile: str = Field(default="")
    mode: str = Field(default="")
    prompt: str = Field(default="")
    timeout_sec: int = Field(default=0, ge=0, le=1800)


class AuthControlUploadRequest(BaseModel):
    profile: str = Field(min_length=1)
    provider: str = Field(default="codex")
    auth_json: Any = Field(default_factory=dict)
    config_toml: str = Field(default="")
    label: str = Field(default="")
    notes: str = Field(default="")


class AuthControlAssignRequest(BaseModel):
    profile: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    lease_sec: int = Field(default=AUTH_CONTROL_DEFAULT_LEASE_SEC, ge=60, le=604800)
    force: bool = Field(default=False)
    notes: str = Field(default="")


class AuthControlRevokeRequest(BaseModel):
    profile: str = Field(min_length=1)
    reason: str = Field(default="")


class AuthControlHealthCheckRequest(BaseModel):
    profile: str = Field(default="")
    node_id: str = Field(default="")
    mode: str = Field(default="status")
    prompt: str = Field(default="")
    timeout_sec: int = Field(default=0, ge=0, le=1800)


class MemorySearchRequest(BaseModel):
    query: str = Field(default="")
    project: str = Field(default="")
    limit: int = Field(default=8, ge=1, le=20)
    include_turn_text: bool = Field(default=False)
    include_same_chat: bool = Field(default=False)


@dataclass
class ChatRuntime:
    chat_id: str
    thread_id: str = ""
    active_turn_id: str = ""
    cwd: str = DEFAULT_CWD
    model: str = DEFAULT_MODEL
    sandbox: str = DEFAULT_SANDBOX
    approval_policy: str = DEFAULT_APPROVAL
    personality: str = DEFAULT_PERSONALITY
    auth_profile: str = ""
    last_input_at: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    client: CodexAppServerClient = field(default_factory=CodexAppServerClient)

    def is_client_running(self) -> bool:
        return self.client.is_running()


class BridgeRuntimeManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._runtimes: Dict[str, ChatRuntime] = {}

    def get(self, chat_id: str) -> ChatRuntime:
        clean_chat_id = str(chat_id)
        with self._lock:
            runtime = self._runtimes.get(clean_chat_id)
            if runtime:
                return runtime

            persisted = STORE.get_chat(clean_chat_id)
            if (not persisted) and ("::" in clean_chat_id):
                legacy = STORE.get_chat(_runtime_actual_chat_id(clean_chat_id))
                legacy_cwd = str(legacy.get("cwd") or "")
                target_project = _runtime_project_name(clean_chat_id)
                if legacy and (not target_project or _project_label_for_cwd(legacy_cwd) == target_project):
                    persisted = dict(legacy)
            runtime = ChatRuntime(
                chat_id=clean_chat_id,
                thread_id=str(persisted.get("thread_id") or ""),
                active_turn_id=str(persisted.get("active_turn_id") or ""),
                cwd=str(persisted.get("cwd") or DEFAULT_CWD),
                model=str(persisted.get("model") or DEFAULT_MODEL),
                sandbox=str(persisted.get("sandbox") or DEFAULT_SANDBOX),
                approval_policy=str(persisted.get("approval_policy") or DEFAULT_APPROVAL),
                personality=str(persisted.get("personality") or DEFAULT_PERSONALITY),
                auth_profile=str(persisted.get("auth_profile") or ""),
                last_input_at=int(persisted.get("last_input_at") or persisted.get("updated_at") or 0),
            )
            _apply_runtime_auth_profile(runtime)
            self._runtimes[clean_chat_id] = runtime
            return runtime

    def runtimes_count(self) -> int:
        with self._lock:
            return len(self._runtimes)

    def evict_idle(self, idle_sec: int) -> int:
        if idle_sec <= 0:
            return 0
        now = int(time.time())
        with self._lock:
            items = list(self._runtimes.items())
        evicted = 0
        for chat_id, runtime in items:
            if not runtime.lock.acquire(blocking=False):
                continue
            try:
                if runtime.thread_id and runtime.is_client_running():
                    active_turn = str(runtime.client.get_active_turn_id(runtime.thread_id) or "")
                    runtime.active_turn_id = active_turn
                else:
                    active_turn = ""
                    if runtime.active_turn_id:
                        runtime.active_turn_id = ""
                        STORE.upsert_chat(chat_id, {"active_turn_id": ""})
                if active_turn:
                    continue

                last_input_at = int(runtime.last_input_at or 0)
                if last_input_at <= 0:
                    persisted = STORE.get_chat(chat_id)
                    last_input_at = int(persisted.get("last_input_at") or persisted.get("updated_at") or 0)
                    runtime.last_input_at = last_input_at
                if last_input_at <= 0:
                    continue
                if (now - last_input_at) < int(idle_sec):
                    continue

                if runtime.is_client_running():
                    runtime.client.stop()
                with self._lock:
                    cur = self._runtimes.get(chat_id)
                    if cur is runtime:
                        self._runtimes.pop(chat_id, None)
                        evicted += 1
                LOG.info("evicted idle runtime chat_id=%s idle_sec=%s", chat_id, now - last_input_at)
            except Exception as exc:
                LOG.warning("evict idle failed chat_id=%s err=%s", chat_id, exc)
            finally:
                runtime.lock.release()
        return evicted

    def stop_all(self) -> None:
        with self._lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            try:
                runtime.client.stop()
            except Exception as exc:
                LOG.warning("stop runtime failed chat_id=%s err=%s", runtime.chat_id, exc)


RUNTIMES = BridgeRuntimeManager()
atexit.register(RUNTIMES.stop_all)
_IDLE_SWEEPER_STOP = threading.Event()
_IDLE_SWEEPER_THREAD: Optional[threading.Thread] = None


def _idle_sweeper_loop() -> None:
    LOG.info(
        "idle sweeper started idle_evict_sec=%s interval_sec=%s",
        IDLE_EVICT_SEC,
        IDLE_SWEEP_INTERVAL_SEC,
    )
    while not _IDLE_SWEEPER_STOP.wait(IDLE_SWEEP_INTERVAL_SEC):
        try:
            evicted = RUNTIMES.evict_idle(IDLE_EVICT_SEC)
            if evicted > 0:
                LOG.info("idle sweeper evicted=%s active_runtime_chats=%s", evicted, RUNTIMES.runtimes_count())
        except Exception as exc:
            LOG.warning("idle sweeper iteration failed err=%s", exc)
    LOG.info("idle sweeper stopped")


def _extract_bearer_token(authorization: str) -> str:
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def require_api_token(authorization: Optional[str] = Header(default=None)) -> None:
    if not API_TOKEN:
        raise HTTPException(status_code=503, detail="bridge api disabled: BRIDGE_API_TOKEN not set")
    auth = str(authorization or "")
    token = _extract_bearer_token(auth)
    if not token or token != API_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")


def _check_api_token(token: str = "", authorization: Optional[str] = None) -> None:
    supplied = str(token or "").strip()
    if not supplied and authorization:
        supplied = _extract_bearer_token(str(authorization or ""))
    if not API_TOKEN:
        raise HTTPException(status_code=503, detail="bridge api disabled: BRIDGE_API_TOKEN not set")
    if not supplied or supplied != API_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _urlsafe_b64decode(raw: str) -> bytes:
    text = str(raw or "").strip()
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign_history_payload(payload: Dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = _urlsafe_b64encode(body)
    sig = hmac.new(HISTORY_SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_urlsafe_b64encode(sig)}"


def _decode_history_payload(token: str) -> Dict[str, Any]:
    raw = str(token or "").strip()
    encoded, sep, sig = raw.partition(".")
    if not encoded or not sep or not sig:
        raise ValueError("invalid token")
    expected = hmac.new(HISTORY_SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    actual = _urlsafe_b64decode(sig)
    if not hmac.compare_digest(expected, actual):
        raise ValueError("invalid signature")
    payload = json.loads(_urlsafe_b64decode(encoded).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid payload")
    exp = int(payload.get("exp") or 0)
    if exp > 0 and exp < int(time.time()):
        raise ValueError("expired token")
    return payload


def _history_allowed_open_ids() -> List[str]:
    values = [item.strip() for item in HISTORY_ALLOWED_OPEN_IDS_RAW.split(",") if item.strip()]
    if values:
        return values
    if USER_CHAT_MAP_PATH.exists():
        try:
            data = json.loads(USER_CHAT_MAP_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                discovered = sorted(
                    {
                        key.strip()
                        for key in data.keys()
                        if isinstance(key, str) and key.startswith("ou_") and ":" not in key
                    }
                )
                if len(discovered) == 1:
                    return discovered
        except Exception:
            pass
    return []


def _history_public_base(request: Request) -> str:
    proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme or "https").strip()
    host = str(request.headers.get("host") or request.url.netloc or "").strip()
    return f"{proto}://{host}".rstrip("/")


def _history_redirect_uri(request: Request) -> str:
    return f"{_history_public_base(request)}/history/auth/callback"


def _history_cookie_payload(request: Request) -> Optional[Dict[str, Any]]:
    raw = str(request.cookies.get(HISTORY_COOKIE_NAME) or "").strip()
    if not raw:
        return None
    try:
        payload = _decode_history_payload(raw)
    except Exception:
        return None
    open_id = str(payload.get("open_id") or "").strip()
    if not open_id:
        return None
    allowed = _history_allowed_open_ids()
    if allowed and open_id not in allowed:
        return None
    return payload


def _history_access_guard(
    request: Request,
    token: str = "",
    authorization: Optional[str] = None,
    require_session: bool = False,
) -> Dict[str, Any]:
    payload = _history_cookie_payload(request)
    if payload:
        return payload
    if not require_session:
        _check_api_token(token=token, authorization=authorization)
        return {"mode": "api_token"}
    raise HTTPException(status_code=401, detail="history login required")


def _history_feishu_user_info(code: str, redirect_uri: str) -> Dict[str, Any]:
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET not set")
    payload = {
        "grant_type": "authorization_code",
        "code": str(code or "").strip(),
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET,
        "redirect_uri": redirect_uri,
    }
    last_error = "oauth token exchange failed"
    for token_url in FEISHU_OAUTH_TOKEN_URLS:
        if not token_url:
            continue
        try:
            resp = requests.post(token_url, json=payload, timeout=20)
            data = resp.json()
        except Exception as exc:
            last_error = str(exc)
            continue
        if resp.status_code >= 400:
            last_error = json.dumps(data, ensure_ascii=False)
            continue
        access_token = (
            str(data.get("access_token") or "")
            or str((data.get("data") or {}).get("access_token") or "")
            or str((data.get("data") or {}).get("user_access_token") or "")
        ).strip()
        if not access_token:
            last_error = json.dumps(data, ensure_ascii=False)
            continue
        info_resp = requests.get(
            FEISHU_OAUTH_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        info = info_resp.json()
        if info_resp.status_code >= 400:
            last_error = json.dumps(info, ensure_ascii=False)
            continue
        user = info.get("data") if isinstance(info.get("data"), dict) else info
        if not isinstance(user, dict):
            last_error = json.dumps(info, ensure_ascii=False)
            continue
        return user
    raise RuntimeError(last_error)


def _load_projects_map() -> Dict[str, str]:
    if not PROJECTS_STORE_PATH.exists():
        return {}
    try:
        data = json.loads(PROJECTS_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for name, raw_path in data.items():
        if not isinstance(name, str) or not isinstance(raw_path, str):
            continue
        try:
            out[name] = str(Path(raw_path).expanduser().resolve())
        except Exception:
            continue
    return out


def _project_label_for_cwd(cwd: str) -> str:
    try:
        resolved = str(Path(str(cwd or "")).expanduser().resolve())
    except Exception:
        resolved = str(cwd or "").strip()
    for name, proj_path in _load_projects_map().items():
        if resolved == proj_path:
            return name
    if resolved:
        return Path(resolved).name or resolved
    return "未命名项目"


def _load_reply_context_map() -> Dict[str, Dict[str, Any]]:
    if not BRIDGE_MCP_REPLY_CONTEXT_PATH.exists():
        return {}
    try:
        data = json.loads(BRIDGE_MCP_REPLY_CONTEXT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    runtimes = data.get("runtimes") if isinstance(data, dict) else {}
    if not isinstance(runtimes, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for runtime_id, item in runtimes.items():
        key = str(runtime_id or "").strip()
        if key and isinstance(item, dict):
            out[key] = dict(item)
    return out


def _reply_anchor_for_runtime(runtime_id: str) -> Dict[str, str]:
    item = _load_reply_context_map().get(str(runtime_id or "").strip())
    if not isinstance(item, dict):
        return {"chat_id": "", "reply_to_message_id": ""}
    return {
        "chat_id": str(item.get("chat_id") or "").strip(),
        "reply_to_message_id": str(item.get("message_id") or "").strip(),
    }


def _runtime_actual_chat_id(runtime_id: str) -> str:
    raw = str(runtime_id or "").strip()
    if "::" in raw:
        return raw.split("::", 1)[0].strip()
    return raw


def _runtime_project_name(runtime_id: str) -> str:
    raw = str(runtime_id or "").strip()
    if "::" not in raw:
        return ""
    return raw.split("::", 1)[1].strip()


def _runtime_id_from_chat_project(chat_id: str, project: str = "") -> str:
    base = str(chat_id or "").strip()
    proj = str(project or "").strip()
    if not base:
        return ""
    return f"{base}::{proj}" if proj else base


def _build_turn_record(
    runtime: ChatRuntime,
    turn_id: str,
    status: str,
    started_at: int = 0,
    ended_at: int = 0,
    user_text: str = "",
    assistant_text: str = "",
    error_text: str = "",
    thread_id: str = "",
    cwd: str = "",
    model: str = "",
    auth_profile: Optional[str] = None,
    token_usage: Optional[Dict[str, Any]] = None,
    rate_limits: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    events = runtime.client.get_turn_events(thread_id=thread_id or runtime.thread_id, turn_id=turn_id, limit=80)
    current_cwd = str(cwd or runtime.cwd or DEFAULT_CWD)
    project = _project_label_for_cwd(current_cwd)
    start_ts = int(started_at or 0)
    end_ts = int(ended_at or time.time())
    duration_sec = max(0, end_ts - start_ts) if start_ts > 0 else 0
    return {
        "id": f"{end_ts}_{runtime.chat_id}_{turn_id or 'no_turn'}",
        "project": project,
        "chat_id": _runtime_actual_chat_id(runtime.chat_id),
        "runtime_id": runtime.chat_id,
        "thread_id": str(thread_id or runtime.thread_id or ""),
        "turn_id": str(turn_id or ""),
        "cwd": current_cwd,
        "model": str(model or runtime.model or DEFAULT_MODEL),
        "auth_profile": str(runtime.auth_profile or "") if auth_profile is None else str(auth_profile or ""),
        "status": str(status or ""),
        "started_at": start_ts,
        "ended_at": end_ts,
        "duration_sec": duration_sec,
        "user_text": str(user_text or ""),
        "assistant_text": str(assistant_text or ""),
        "error_text": str(error_text or ""),
        "events": events,
        "token_usage": dict(token_usage or {}),
        "rate_limits": dict(rate_limits or {}),
    }


def _resolve_chat_config(runtime: ChatRuntime, body: Any) -> None:
    runtime.cwd = str(getattr(body, "cwd", "") or runtime.cwd or DEFAULT_CWD)
    runtime.model = str(getattr(body, "model", "") or runtime.model or DEFAULT_MODEL)
    runtime.sandbox = str(getattr(body, "sandbox", "") or runtime.sandbox or DEFAULT_SANDBOX)
    runtime.approval_policy = str(
        getattr(body, "approval_policy", "") or runtime.approval_policy or DEFAULT_APPROVAL
    )
    runtime.personality = str(getattr(body, "personality", "") or runtime.personality or DEFAULT_PERSONALITY)
    _apply_runtime_bridge_env(runtime)


def _persist_runtime(runtime: ChatRuntime, patch: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "runtime_id": runtime.chat_id,
        "source_chat_id": _runtime_actual_chat_id(runtime.chat_id),
        "project": _runtime_project_name(runtime.chat_id),
        "thread_id": runtime.thread_id,
        "active_turn_id": runtime.active_turn_id,
        "cwd": runtime.cwd,
        "model": runtime.model,
        "sandbox": runtime.sandbox,
        "approval_policy": runtime.approval_policy,
        "personality": runtime.personality,
        "auth_profile": runtime.auth_profile,
        "last_input_at": int(runtime.last_input_at or 0),
    }
    if patch:
        data.update(patch)
    return STORE.upsert_chat(runtime.chat_id, data)


def _is_disconnect_wait_error(message: str) -> bool:
    return "app-server disconnected while waiting for turn completion" in str(message or "")


def _disconnect_streak_patch(runtime: ChatRuntime) -> Dict[str, Any]:
    now_ts = int(time.time())
    persisted = STORE.get_chat(runtime.chat_id)
    prev_ts = int(persisted.get("last_disconnect_at") or 0)
    prev_streak = int(persisted.get("disconnect_fail_streak") or 0)
    streak = prev_streak + 1 if prev_ts > 0 and (now_ts - prev_ts) <= DISCONNECT_SELF_HEAL_WINDOW_SEC else 1
    return {
        "disconnect_fail_streak": int(streak),
        "last_disconnect_at": now_ts,
    }


def _error_text(value: Any) -> str:
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value or "")


def _runtime_backup_root() -> Path:
    return RUNTIME_HOMES_DIR / "_self_heal_backups"


def _cleanup_runtime_backups(runtime_id: str, limit: int = 0) -> None:
    keep = max(1, int(limit or DISCONNECT_SELF_HEAL_BACKUP_LIMIT))
    root = _runtime_backup_root()
    if not root.exists():
        return
    home_name = _runtime_home_name(runtime_id)
    prefix = f"{home_name}."
    candidates: List[Path] = []
    for path in root.iterdir():
        if path.name.startswith(prefix):
            candidates.append(path)
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for stale in candidates[keep:]:
        try:
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
            else:
                stale.unlink(missing_ok=True)
        except Exception:
            pass


def _archive_runtime_home(runtime_id: str, reason: str = "") -> str:
    src = _runtime_home_dir(runtime_id)
    if not src.exists():
        return ""
    root = _runtime_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    reason_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", str(reason or "").strip()).strip("._")
    reason_tag = (reason_tag[:28] if reason_tag else "heal")
    base_name = f"{src.name}.{time.strftime('%Y%m%d-%H%M%S', time.localtime())}.{reason_tag}"
    dst = root / base_name
    index = 1
    while dst.exists():
        dst = root / f"{base_name}.{index}"
        index += 1
    shutil.move(str(src), str(dst))
    _cleanup_runtime_backups(runtime_id)
    return str(dst)


def _should_force_runtime_rebuild(persisted: Dict[str, Any]) -> bool:
    if not DISCONNECT_SELF_HEAL_FORCE_REBUILD_ON_AUTH_HEADER_ERROR:
        return False
    last_turn_error_text = _error_text((persisted or {}).get("last_turn_error")).lower()
    if not last_turn_error_text:
        return False
    if "missing bearer or basic authentication in header" in last_turn_error_text:
        return True
    if "unexpected status 401 unauthorized" in last_turn_error_text and "/v1/responses" in last_turn_error_text:
        return True
    return False


def _rebuild_runtime_after_disconnect(runtime: ChatRuntime, reason: str, streak: int, force: bool = False) -> Dict[str, Any]:
    now_ts = int(time.time())
    backup_home = ""
    if DISCONNECT_SELF_HEAL_REBUILD_ENABLED:
        try:
            backup_home = _archive_runtime_home(runtime.chat_id, reason=reason)
        except Exception as exc:
            LOG.warning("archive runtime home failed chat_id=%s err=%s", runtime.chat_id, exc)
    runtime.thread_id = ""
    runtime.active_turn_id = ""
    try:
        runtime.client.stop()
    except Exception:
        pass
    _apply_runtime_auth_profile(runtime)
    _persist_runtime(
        runtime,
        {
            "disconnect_fail_streak": 0,
            "last_self_heal_at": now_ts,
            "last_self_heal_reason": reason,
            "last_self_heal_strategy": "runtime_rebuild",
            "last_self_heal_backup_home": backup_home,
            "last_self_heal_force": bool(force),
            "last_error": "",
        },
    )
    LOG.warning(
        "disconnect self-heal rebuild chat_id=%s streak=%s force=%s backup=%s",
        runtime.chat_id,
        streak,
        force,
        backup_home,
    )
    return {"healed": True, "streak": streak, "reason": reason, "strategy": "runtime_rebuild", "forced": bool(force)}


def _maybe_self_heal_disconnected_runtime(runtime: ChatRuntime) -> Dict[str, Any]:
    if not DISCONNECT_SELF_HEAL_ENABLED:
        return {"healed": False, "streak": 0, "reason": ""}
    persisted = STORE.get_chat(runtime.chat_id)
    last_err = str(persisted.get("last_error") or "")
    if not _is_disconnect_wait_error(last_err):
        return {"healed": False, "streak": 0, "reason": ""}
    streak = int(persisted.get("disconnect_fail_streak") or 0)
    last_ts = int(persisted.get("last_disconnect_at") or 0)
    now_ts = int(time.time())
    within_window = last_ts > 0 and (now_ts - last_ts) <= DISCONNECT_SELF_HEAL_WINDOW_SEC
    force_rebuild = _should_force_runtime_rebuild(persisted)
    if not force_rebuild and streak < DISCONNECT_SELF_HEAL_THRESHOLD:
        return {"healed": False, "streak": streak, "reason": ""}
    if not force_rebuild and not within_window:
        return {"healed": False, "streak": streak, "reason": ""}

    reason = f"disconnect self-heal (streak={streak}, force={int(bool(force_rebuild))})"
    if DISCONNECT_SELF_HEAL_REBUILD_ENABLED:
        try:
            return _rebuild_runtime_after_disconnect(runtime, reason=reason, streak=streak, force=force_rebuild)
        except Exception as exc:
            LOG.warning("disconnect self-heal rebuild failed chat_id=%s err=%s", runtime.chat_id, exc)
    if str(runtime.auth_profile or "").strip():
        _switch_runtime_auth_profile(runtime, profile=str(runtime.auth_profile or "").strip(), reason=reason)
    else:
        runtime.thread_id = ""
        runtime.active_turn_id = ""
        try:
            runtime.client.stop()
        except Exception:
            pass
        _persist_runtime(runtime)
    _persist_runtime(
        runtime,
        {
            "disconnect_fail_streak": 0,
            "last_self_heal_at": now_ts,
            "last_self_heal_reason": reason,
            "last_self_heal_strategy": "auth_reapply",
            "last_self_heal_force": bool(force_rebuild),
            "last_error": "",
        },
    )
    return {"healed": True, "streak": streak, "reason": reason, "strategy": "auth_reapply", "forced": bool(force_rebuild)}


def _read_rate_limits(runtime: ChatRuntime, allow_request: bool = True) -> Dict[str, Any]:
    cached = runtime.client.get_account_rate_limits()
    if cached:
        return cached
    if not allow_request:
        return {}
    try:
        read = runtime.client.account_rate_limits_read()
        if isinstance(read.get("rateLimits"), dict):
            return dict(read.get("rateLimits") or {})
    except Exception:
        return {}
    return {}


def _load_auth_registry() -> Dict[str, Any]:
    if not AUTH_REGISTRY_PATH.exists():
        return {"profiles": []}
    try:
        data = json.loads(AUTH_REGISTRY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("profiles"), list):
            return data
    except Exception:
        pass
    return {"profiles": []}


def _save_auth_registry(profiles: List[Dict[str, Any]]) -> None:
    AUTH_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"profiles": profiles, "updated_at": int(time.time())}
    AUTH_REGISTRY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _profile_home_dir(profile: str) -> Path:
    return AUTH_HOMES_DIR / str(profile or "").strip()


def _codex_home_for_profile(profile: str) -> Path:
    clean = str(profile or "").strip()
    return _profile_home_dir(clean) if clean else DEFAULT_CODEX_HOME


def _runtime_home_name(runtime_id: str) -> str:
    raw = str(runtime_id or "").strip() or "runtime"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._") or "runtime"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{safe[:64]}-{digest}"


def _runtime_home_dir(runtime_id: str) -> Path:
    return RUNTIME_HOMES_DIR / _runtime_home_name(runtime_id)


def _run_codex_mcp(home_dir: Path, args: List[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home_dir)
    return subprocess.run(
        ["codex", "mcp", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _ensure_bridge_mcp_server_installed(home_dir: Path, server_env: Optional[Dict[str, str]] = None) -> None:
    target_home = Path(home_dir).expanduser()
    target_home.mkdir(parents=True, exist_ok=True)
    if not BRIDGE_MCP_SERVER_PATH.exists():
        LOG.warning("bridge MCP server script missing path=%s", BRIDGE_MCP_SERVER_PATH)
        return
    expected_env = {
        str(k or "").strip(): str(v or "").strip()
        for k, v in dict(server_env or {}).items()
        if str(k or "").strip() and str(v or "").strip()
    }
    need_reinstall = False
    try:
        current = _run_codex_mcp(target_home, ["get", BRIDGE_MCP_SERVER_NAME, "--json"])
    except Exception as exc:
        LOG.warning("codex mcp get failed home=%s err=%s", target_home, exc)
        return
    if current.returncode == 0:
        try:
            current_info = json.loads(current.stdout or "{}")
        except Exception:
            current_info = {}
        transport = current_info.get("transport") if isinstance(current_info, dict) else {}
        if not isinstance(transport, dict):
            transport = {}
        current_env = transport.get("env") if isinstance(transport.get("env"), dict) else {}
        current_args = [str(x) for x in list(transport.get("args") or [])]
        current_command = str(transport.get("command") or "")
        need_reinstall = (
            current_command != BRIDGE_MCP_PYTHON
            or current_args != [str(BRIDGE_MCP_SERVER_PATH)]
            or current_env != expected_env
        )
        if not need_reinstall:
            return
        removed = _run_codex_mcp(target_home, ["remove", BRIDGE_MCP_SERVER_NAME])
        if removed.returncode != 0:
            LOG.warning(
                "codex mcp remove failed home=%s rc=%s stdout=%s stderr=%s",
                target_home,
                removed.returncode,
                (removed.stdout or "").strip(),
                (removed.stderr or "").strip(),
            )
            return
    try:
        add_args: List[str] = ["add", BRIDGE_MCP_SERVER_NAME]
        for key, value in sorted(expected_env.items()):
            add_args.extend(["--env", f"{key}={value}"])
        add_args.extend(["--", BRIDGE_MCP_PYTHON, str(BRIDGE_MCP_SERVER_PATH)])
        added = _run_codex_mcp(target_home, add_args)
    except Exception as exc:
        LOG.warning("codex mcp add failed home=%s err=%s", target_home, exc)
        return
    if added.returncode != 0:
        LOG.warning(
            "codex mcp add failed home=%s rc=%s stdout=%s stderr=%s",
            target_home,
            added.returncode,
            (added.stdout or "").strip(),
            (added.stderr or "").strip(),
        )


def _bridge_mcp_env_for_runtime(runtime: ChatRuntime) -> Dict[str, str]:
    current_cwd = str(Path(runtime.cwd or DEFAULT_CWD).expanduser().resolve())
    anchor = _reply_anchor_for_runtime(runtime.chat_id)
    env = {
        "FEISHU_APP_ID": FEISHU_APP_ID,
        "FEISHU_APP_SECRET": FEISHU_APP_SECRET,
        "BRIDGE_STATE_PATH": str(_state_path.resolve()),
        "BRIDGE_MCP_RUNTIME_ID": str(runtime.chat_id or ""),
        "BRIDGE_MCP_DEFAULT_CHAT_ID": str(anchor.get("chat_id") or _runtime_actual_chat_id(runtime.chat_id) or ""),
        "BRIDGE_MCP_DEFAULT_PROJECT": _runtime_project_name(runtime.chat_id),
        "BRIDGE_MCP_RUNTIME_CWD": current_cwd,
        "BRIDGE_MCP_REPLY_CONTEXT_PATH": str(BRIDGE_MCP_REPLY_CONTEXT_PATH),
        "BRIDGE_MCP_FILE_ALLOWED_DIRS": current_cwd,
    }
    return {k: v for k, v in env.items() if str(v or "").strip()}


def _sync_runtime_home(runtime: ChatRuntime) -> Path:
    source_home = _codex_home_for_profile(runtime.auth_profile)
    target_home = _runtime_home_dir(runtime.chat_id)
    target_home.mkdir(parents=True, exist_ok=True)
    for filename in ("auth.json", "config.toml"):
        src = source_home / filename
        dst = target_home / filename
        if src.exists() and src.is_file():
            shutil.copy2(src, dst)
        elif dst.exists():
            try:
                dst.unlink()
            except Exception:
                pass
    _ensure_bridge_mcp_server_installed(target_home, server_env=_bridge_mcp_env_for_runtime(runtime))
    return target_home


def _ensure_bridge_mcp_server_for_known_homes() -> None:
    homes: List[Path] = [DEFAULT_CODEX_HOME]
    if AUTH_HOMES_DIR.exists():
        for path in sorted(AUTH_HOMES_DIR.iterdir()):
            if path.is_dir():
                homes.append(path)
    seen: set[str] = set()
    for home in homes:
        key = str(home.resolve())
        if key in seen:
            continue
        seen.add(key)
        _ensure_bridge_mcp_server_installed(home)


def _apply_runtime_bridge_env(runtime: ChatRuntime) -> None:
    runtime.client.env["BRIDGE_STATE_PATH"] = str(_state_path.resolve())
    runtime.client.env["BRIDGE_MCP_RUNTIME_ID"] = str(runtime.chat_id or "")
    runtime.client.env["BRIDGE_MCP_DEFAULT_CHAT_ID"] = _runtime_actual_chat_id(runtime.chat_id)
    runtime.client.env["BRIDGE_MCP_DEFAULT_PROJECT"] = _runtime_project_name(runtime.chat_id)
    runtime.client.env["BRIDGE_MCP_RUNTIME_CWD"] = str(Path(runtime.cwd or DEFAULT_CWD).expanduser().resolve())
    runtime.client.env["BRIDGE_MCP_REPLY_CONTEXT_PATH"] = str(BRIDGE_MCP_REPLY_CONTEXT_PATH)


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    raw = parts[1].replace("-", "+").replace("_", "/")
    raw += "=" * ((4 - len(raw) % 4) % 4)
    try:
        return json.loads(base64.b64decode(raw.encode("utf-8")).decode("utf-8"))
    except Exception:
        return {}


def _auth_file_sha1(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _parse_disable_seconds_from_message(message: str) -> int:
    text = str(message or "")
    lower = text.lower()
    now = time.time()

    # Example: "try again in 04:59:10"
    for hh, mm, ss in re.findall(r"(?i)\b(?:in|after|reset(?:s)?\s+in)\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\b", lower):
        try:
            total = int(hh) * 3600 + int(mm) * 60 + int(ss or 0)
            if total > 0:
                return total
        except Exception:
            pass

    # Example: "in 5 hours", "7 days", "5小时", "7天"
    for value, unit in re.findall(r"(?i)(\d+)\s*(hours?|hrs?|hr|h|days?|d|小时|天)", text):
        try:
            num = int(value)
        except Exception:
            continue
        unit_low = unit.lower()
        if unit_low in {"h", "hr", "hrs", "hour", "hours", "小时"}:
            return max(300, num * 3600)
        if unit_low in {"d", "day", "days", "天"}:
            return max(300, num * 86400)

    # Example: "until 2026-03-25 12:34:56" or ISO forms
    for token in re.findall(r"\b20\d{2}-\d{2}-\d{2}[T\s]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?\b", text):
        try:
            dt = datetime.fromisoformat(token.replace("Z", "+00:00"))
            ts = dt.timestamp()
            if ts > now:
                return max(300, int(ts - now))
        except Exception:
            pass

    return 0


def _classify_auth_error(message: str) -> str:
    text = str(message or "").strip()
    lower = text.lower()
    if not lower:
        return ""

    if re.search(r"(disactivat|deactivat|suspend|account.+disabled|account.+banned|risk)", lower):
        return "deactivated"

    if (
        "refresh token was already used" in lower
        or "access token could not be refreshed" in lower
        or "invalid_grant" in lower
        or "refresh token has expired" in lower
        or "reauth" in lower
        or "login required" in lower
    ):
        return "needs_reauth"

    if _is_auth_limit_error(text) or re.search(r"(try again in|retry after|too many requests|rate limit)", lower):
        return "temp_disabled"

    return ""


def _auth_profile_available(meta: Dict[str, Any], now_ts: Optional[int] = None) -> bool:
    now = int(now_ts if now_ts is not None else time.time())
    if not bool(meta.get("valid")):
        return False
    status = str(meta.get("status") or "").strip().lower()
    if status in {"needs_reauth", "deactivated"}:
        return False
    disabled_until = int(meta.get("disabled_until") or 0)
    if status == "temp_disabled" and disabled_until > now:
        return False
    return True


def _auth_registry_by_profile() -> Dict[str, Dict[str, Any]]:
    data = _load_auth_registry()
    items = data.get("profiles") if isinstance(data.get("profiles"), list) else []
    out: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        profile = str(item.get("profile") or "").strip()
        if profile:
            out[profile] = dict(item)
    return out


def _patch_auth_registry_profile(profile: str, patch: Dict[str, Any]) -> None:
    name = str(profile or "").strip()
    if not name:
        return
    data = _load_auth_registry()
    items = data.get("profiles") if isinstance(data.get("profiles"), list) else []
    changed = False
    found = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("profile") or "").strip() != name:
            continue
        item.update(dict(patch or {}))
        item["updated_at"] = int(time.time())
        found = True
        changed = True
    if not found:
        source = AUTH_PROFILES_DIR / f"{name}.auth.json"
        if source.exists() and source.is_file():
            entry = _validate_auth_profile_file(source)
            entry.update(dict(patch or {}))
            entry["updated_at"] = int(time.time())
            items.append(entry)
            changed = True
    if changed:
        _save_auth_registry([item for item in items if isinstance(item, dict)])


def _remove_auth_profile_artifacts(profile: str) -> bool:
    clean = str(profile or "").strip()
    if not clean:
        return False
    changed = False
    src = AUTH_PROFILES_DIR / f"{clean}.auth.json"
    cfg = AUTH_PROFILES_DIR / f"{clean}.config.toml"
    home = _profile_home_dir(clean)
    try:
        if src.exists():
            src.unlink()
            changed = True
    except Exception as exc:
        LOG.warning("remove auth profile source failed profile=%s err=%s", clean, exc)
    try:
        if cfg.exists():
            cfg.unlink()
            changed = True
    except Exception as exc:
        LOG.warning("remove auth profile config failed profile=%s err=%s", clean, exc)
    try:
        if home.exists() and home.is_dir():
            shutil.rmtree(home, ignore_errors=False)
            changed = True
    except Exception as exc:
        LOG.warning("remove auth profile home failed profile=%s err=%s", clean, exc)
    if changed:
        _refresh_auth_profiles()
    return changed


def _validate_auth_profile_file(source: Path, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    profile = str(source.name[: -len(".auth.json")] if source.name.endswith(".auth.json") else source.stem).strip()
    previous_meta = dict(previous or {})
    now_ts = int(time.time())
    source_hash = _auth_file_sha1(source)
    prev_hash = str(previous_meta.get("source_auth_sha1") or "").strip()
    file_changed = bool(source_hash and prev_hash and source_hash != prev_hash)

    meta: Dict[str, Any] = {
        "profile": profile,
        "source_auth_json": str(source),
        "source_config_toml": str(previous_meta.get("source_config_toml") or "").strip(),
        "valid": False,
        "reason": "",
        "auth_mode": str(previous_meta.get("auth_mode") or "").strip(),
        "email": str(previous_meta.get("email") or "").strip(),
        "sub": str(previous_meta.get("sub") or "").strip(),
        "home_dir": str(_profile_home_dir(profile)),
        "status": str(previous_meta.get("status") or "").strip() or "unknown",
        "disabled_until": int(previous_meta.get("disabled_until") or 0),
        "disabled_reason": str(previous_meta.get("disabled_reason") or "").strip(),
        "needs_reauth": bool(previous_meta.get("needs_reauth")),
        "risk_deactivated": bool(previous_meta.get("risk_deactivated")),
        "last_health_check_at": int(previous_meta.get("last_health_check_at") or 0),
        "last_health_error": str(previous_meta.get("last_health_error") or "").strip(),
        "source_auth_sha1": source_hash,
        "updated_at": now_ts,
    }
    if file_changed:
        meta["disabled_until"] = 0
        meta["disabled_reason"] = ""
        meta["needs_reauth"] = False
        meta["risk_deactivated"] = False
        meta["last_health_error"] = ""
        meta["status"] = "unknown"

    if bool(meta.get("needs_reauth")) and (not file_changed):
        meta["status"] = "needs_reauth"
        meta["valid"] = False
        meta["reason"] = "auth.json 登录态已失效，请重新获取并替换该文件。"
        return meta
    if bool(meta.get("risk_deactivated")) and (not file_changed):
        meta["status"] = "deactivated"
        meta["valid"] = False
        meta["reason"] = "账号疑似被风控/停用，已禁止继续使用。"
        return meta

    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        meta["status"] = "invalid"
        meta["reason"] = f"invalid json: {exc}"
        meta["last_health_check_at"] = now_ts
        meta["last_health_error"] = meta["reason"]
        return meta

    auth_mode = str(data.get("auth_mode") or "").strip()
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    payload = _decode_jwt_payload(str(tokens.get("id_token") or ""))
    meta["auth_mode"] = auth_mode
    meta["email"] = str(payload.get("email") or "").strip()
    meta["sub"] = str(payload.get("sub") or "").strip()

    if not auth_mode or not isinstance(tokens, dict):
        meta["status"] = "invalid"
        meta["reason"] = "missing auth_mode/tokens"
        meta["last_health_check_at"] = now_ts
        meta["last_health_error"] = meta["reason"]
        return meta

    home_dir = _profile_home_dir(profile)
    home_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, home_dir / "auth.json")
    _ensure_bridge_mcp_server_installed(home_dir)

    cfg = source.with_name(f"{profile}.config.toml")
    if cfg.exists() and cfg.is_file():
        shutil.copy2(cfg, home_dir / "config.toml")
        meta["source_config_toml"] = str(cfg)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(home_dir)
    try:
        proc = subprocess.run(
            ["codex", "login", "status"],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        meta["status"] = "invalid"
        meta["reason"] = f"status check failed: {exc}"
        meta["last_health_check_at"] = now_ts
        meta["last_health_error"] = meta["reason"]
        return meta

    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    meta["last_health_check_at"] = now_ts
    if proc.returncode == 0 and "Logged in" in output:
        disabled_until = int(meta.get("disabled_until") or 0)
        if str(meta.get("status") or "").strip().lower() == "temp_disabled" and disabled_until > now_ts:
            meta["valid"] = False
            meta["reason"] = meta["disabled_reason"] or f"临时禁用中，预计 {time.strftime('%m-%d %H:%M', time.localtime(disabled_until))} 解禁"
            meta["last_health_error"] = meta["reason"]
        else:
            meta["valid"] = True
            meta["reason"] = ""
            meta["status"] = "active"
            meta["disabled_until"] = 0
            meta["disabled_reason"] = ""
            meta["needs_reauth"] = False
            meta["risk_deactivated"] = False
            meta["last_health_error"] = ""
        return meta
    raw_reason = output.strip() or f"status code {proc.returncode}"
    classified = _classify_auth_error(raw_reason)
    meta["reason"] = raw_reason
    meta["last_health_error"] = raw_reason
    if classified == "needs_reauth":
        meta["status"] = "needs_reauth"
        meta["needs_reauth"] = True
        meta["disabled_until"] = 0
        meta["disabled_reason"] = "refresh token 已失效，需替换 auth.json"
    elif classified == "deactivated":
        meta["status"] = "deactivated"
        meta["risk_deactivated"] = True
        meta["disabled_until"] = 0
        meta["disabled_reason"] = "账号疑似被停用/风控"
    else:
        meta["status"] = "invalid"
    return meta


def _refresh_auth_profiles() -> List[Dict[str, Any]]:
    AUTH_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    previous_by_profile = _auth_registry_by_profile()
    profiles: List[Dict[str, Any]] = []
    for path in sorted(AUTH_PROFILES_DIR.glob("*.auth.json")):
        if not path.is_file():
            continue
        profile = str(path.name[: -len(".auth.json")] if path.name.endswith(".auth.json") else path.stem).strip()
        if not profile:
            continue
        profiles.append(_validate_auth_profile_file(path, previous=previous_by_profile.get(profile)))
    _save_auth_registry(profiles)
    return profiles


def _sanitize_auth_profile_name(raw: str) -> str:
    clean = str(raw or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", clean):
        raise HTTPException(
            status_code=400,
            detail="invalid profile, only [A-Za-z0-9._-], max 64 chars, and must start with alnum",
        )
    return clean


def _normalize_auth_json_text(payload: Any) -> str:
    if isinstance(payload, str):
        source = str(payload or "").strip()
        if not source:
            raise HTTPException(status_code=400, detail="auth_json is empty")
        try:
            parsed = json.loads(source)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"auth_json is not valid json: {exc}") from exc
        return json.dumps(parsed, ensure_ascii=False, indent=2) + "\n"
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="auth_json must be a JSON object or string")
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _load_dispatch_fence() -> Dict[str, Any]:
    if not AUTH_DISPATCH_FENCE_PATH.exists():
        return {"profiles": {}, "updated_at": 0}
    try:
        data = json.loads(AUTH_DISPATCH_FENCE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"profiles": {}, "updated_at": 0}
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    return {"profiles": dict(profiles), "updated_at": int(data.get("updated_at") or 0)}


def _save_dispatch_fence(data: Dict[str, Any]) -> None:
    AUTH_DISPATCH_FENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "profiles": dict((data or {}).get("profiles") or {}),
        "updated_at": int(time.time()),
    }
    AUTH_DISPATCH_FENCE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _apply_dispatch_fence(profile: str, version: int, token: str, server_id: str, state: str) -> None:
    if version <= 0:
        return
    name = _sanitize_auth_profile_name(profile)
    clean_token = str(token or "").strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="assignment_token is required when assignment_version > 0")
    with _AUTH_DISPATCH_FENCE_LOCK:
        store = _load_dispatch_fence()
        profiles = store.get("profiles") if isinstance(store.get("profiles"), dict) else {}
        previous = profiles.get(name) if isinstance(profiles.get(name), dict) else {}
        prev_version = int(previous.get("version") or 0)
        prev_token = str(previous.get("token") or "").strip()
        if version < prev_version:
            raise HTTPException(
                status_code=409,
                detail=f"stale assignment_version: {version} < current {prev_version}",
            )
        if version == prev_version and prev_token and prev_token != clean_token:
            raise HTTPException(
                status_code=409,
                detail=f"assignment token mismatch for same version={version}",
            )
        profiles[name] = {
            "version": int(version),
            "token": clean_token,
            "server_id": str(server_id or "").strip(),
            "state": str(state or "").strip() or "assigned",
            "updated_at": int(time.time()),
        }
        _save_dispatch_fence({"profiles": profiles})


def _install_auth_profile(
    profile: str,
    provider: str,
    auth_json: Any,
    config_toml: str = "",
    assignment_version: int = 0,
    assignment_token: str = "",
    assigned_server_id: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    _apply_dispatch_fence(
        profile=name,
        version=int(assignment_version or 0),
        token=str(assignment_token or "").strip(),
        server_id=str(assigned_server_id or "").strip(),
        state="assigned",
    )
    AUTH_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    auth_text = _normalize_auth_json_text(auth_json)
    src = AUTH_PROFILES_DIR / f"{name}.auth.json"
    tmp = AUTH_PROFILES_DIR / f".{name}.auth.json.tmp"
    tmp.write_text(auth_text, encoding="utf-8")
    tmp.replace(src)
    cfg_text = str(config_toml or "").strip()
    cfg = AUTH_PROFILES_DIR / f"{name}.config.toml"
    if cfg_text:
        cfg.write_text(cfg_text + ("\n" if not cfg_text.endswith("\n") else ""), encoding="utf-8")
    elif cfg.exists():
        cfg.unlink()

    meta = _get_auth_profile(name)
    if not meta:
        raise HTTPException(status_code=500, detail=f"profile saved but refresh failed: {name}")
    patch = {
        "provider": str(provider or "codex").strip() or "codex",
        "notes": str(notes or "").strip()[:500],
    }
    if int(assignment_version or 0) > 0:
        patch.update(
            {
                "dispatch_server_id": str(assigned_server_id or "").strip(),
                "dispatch_version": int(assignment_version),
                "dispatch_token": str(assignment_token or "").strip(),
                "dispatch_updated_at": int(time.time()),
            }
        )
    _patch_auth_registry_profile(name, patch)
    refreshed = _get_auth_profile(name) or meta
    return refreshed


def _remove_auth_profile(
    profile: str,
    assignment_version: int = 0,
    assignment_token: str = "",
    assigned_server_id: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    _apply_dispatch_fence(
        profile=name,
        version=int(assignment_version or 0),
        token=str(assignment_token or "").strip(),
        server_id=str(assigned_server_id or "").strip(),
        state="removed",
    )
    changed = _remove_auth_profile_artifacts(name)
    return {
        "profile": name,
        "removed": bool(changed),
        "reason": str(reason or "").strip(),
        "assignment_version": int(assignment_version or 0),
        "assigned_server_id": str(assigned_server_id or "").strip(),
    }


def _load_auth_control_registry() -> Dict[str, Any]:
    if not AUTH_CONTROL_REGISTRY_PATH.exists():
        return {"auths": {}, "assignments": {}, "audit": [], "updated_at": 0}
    try:
        data = json.loads(AUTH_CONTROL_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"auths": {}, "assignments": {}, "audit": [], "updated_at": 0}
    auths = data.get("auths") if isinstance(data.get("auths"), dict) else {}
    assignments = data.get("assignments") if isinstance(data.get("assignments"), dict) else {}
    audit = data.get("audit") if isinstance(data.get("audit"), list) else []
    return {
        "auths": dict(auths),
        "assignments": dict(assignments),
        "audit": [item for item in audit if isinstance(item, dict)][-AUTH_CONTROL_MAX_AUDIT:],
        "updated_at": int(data.get("updated_at") or 0),
    }


def _save_auth_control_registry(data: Dict[str, Any]) -> None:
    AUTH_CONTROL_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "auths": dict((data or {}).get("auths") or {}),
        "assignments": dict((data or {}).get("assignments") or {}),
        "audit": [item for item in list((data or {}).get("audit") or []) if isinstance(item, dict)][
            -AUTH_CONTROL_MAX_AUDIT:
        ],
        "updated_at": int(time.time()),
    }
    AUTH_CONTROL_REGISTRY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _audit_auth_control(
    registry: Dict[str, Any],
    action: str,
    profile: str,
    node_id: str = "",
    ok: bool = True,
    message: str = "",
) -> None:
    audit = registry.get("audit") if isinstance(registry.get("audit"), list) else []
    audit.append(
        {
            "ts": int(time.time()),
            "action": str(action or "").strip(),
            "profile": str(profile or "").strip(),
            "node_id": str(node_id or "").strip(),
            "ok": bool(ok),
            "message": str(message or "").strip()[:2000],
        }
    )
    registry["audit"] = audit[-AUTH_CONTROL_MAX_AUDIT:]


def _parse_auth_control_nodes() -> List[Dict[str, Any]]:
    try:
        raw = json.loads(AUTH_CONTROL_NODES_JSON)
    except Exception:
        raw = []
    items = raw if isinstance(raw, list) else []
    nodes: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "").strip()
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        if (not node_id) or (not base_url) or node_id in seen:
            continue
        nodes.append(
            {
                "id": node_id,
                "label": str(item.get("label") or node_id),
                "base_url": base_url,
                "api_token": str(item.get("api_token") or API_TOKEN or "").strip(),
                "timeout_sec": max(5, min(300, int(item.get("timeout_sec") or 45))),
                "enabled": bool(item.get("enabled", True)),
            }
        )
        seen.add(node_id)
    return nodes


def _auth_control_nodes_map() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in _parse_auth_control_nodes():
        if not bool(item.get("enabled")):
            continue
        out[str(item.get("id") or "")] = dict(item)
    return out


def _call_node_api(node: Dict[str, Any], method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = str(node.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError(f"invalid node base_url for node={node.get('id')}")
    url = f"{base}{API_PREFIX}{path}"
    headers = {"Accept": "application/json"}
    token = str(node.get("api_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    kwargs: Dict[str, Any] = {
        "method": str(method or "GET").upper(),
        "url": url,
        "headers": headers,
        "timeout": max(5, int(node.get("timeout_sec") or 45)),
    }
    if body is not None:
        kwargs["json"] = body
    resp = requests.request(**kwargs)
    text = str(resp.text or "").strip()
    try:
        payload = resp.json()
    except Exception:
        payload = {"ok": False, "error": text[:1000] or f"http {resp.status_code}"}
    if resp.status_code >= 400:
        detail = str(payload.get("error") or payload.get("detail") or text or f"http {resp.status_code}")
        raise RuntimeError(f"node={node.get('id')} api={path} failed: {detail}")
    if not bool(payload.get("ok", True)):
        raise RuntimeError(f"node={node.get('id')} api={path} failed: {payload.get('error')}")
    return payload


def _merge_auth_control_auth_meta(profile_item: Dict[str, Any], provider: str = "", label: str = "", notes: str = "") -> Dict[str, Any]:
    profile = str(profile_item.get("profile") or "").strip()
    source = str(profile_item.get("source_auth_json") or "").strip()
    sha = ""
    if source:
        path = Path(source)
        if path.exists() and path.is_file():
            sha = _auth_file_sha1(path)
    now_ts = int(time.time())
    return {
        "profile": profile,
        "provider": str(provider or profile_item.get("provider") or "codex").strip() or "codex",
        "label": str(label or profile).strip()[:120],
        "notes": str(notes or profile_item.get("notes") or "").strip()[:500],
        "fingerprint": sha,
        "updated_at": now_ts,
    }


def _auth_control_registry_state() -> Dict[str, Any]:
    nodes = _parse_auth_control_nodes()
    profiles = _refresh_auth_profiles()
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        audit = registry.get("audit") if isinstance(registry.get("audit"), list) else []
    now_ts = int(time.time())
    merged_auths: List[Dict[str, Any]] = []
    for item in profiles:
        profile = str(item.get("profile") or "").strip()
        if not profile:
            continue
        merged = dict(auths.get(profile) if isinstance(auths.get(profile), dict) else {})
        merged.update(_merge_auth_control_auth_meta(item, provider=str(merged.get("provider") or "")))
        merged["health"] = {
            "valid": bool(item.get("valid")),
            "status": str(item.get("status") or ""),
            "reason": str(item.get("reason") or ""),
            "last_health_check_at": int(item.get("last_health_check_at") or 0),
        }
        assignment = assignments.get(profile) if isinstance(assignments.get(profile), dict) else {}
        merged["assignment"] = dict(assignment or {})
        lease_expire_at = int((assignment or {}).get("lease_expire_at") or 0)
        merged["lease_remaining_sec"] = max(0, lease_expire_at - now_ts) if lease_expire_at > 0 else 0
        merged_auths.append(merged)
    return {
        "timestamp": now_ts,
        "nodes": [{k: v for k, v in node.items() if k != "api_token"} for node in nodes],
        "auths": sorted(merged_auths, key=lambda x: str(x.get("profile") or "")),
        "audit": [item for item in audit if isinstance(item, dict)][-200:],
    }


def _auth_control_upload(
    profile: str,
    provider: str,
    auth_json: Any,
    config_toml: str = "",
    label: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    item = _install_auth_profile(
        profile=profile,
        provider=provider,
        auth_json=auth_json,
        config_toml=config_toml,
        assignment_version=0,
        assignment_token="",
        assigned_server_id="",
        notes=notes,
    )
    meta = _merge_auth_control_auth_meta(item, provider=provider, label=label, notes=notes)
    name = str(meta.get("profile") or "")
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
        existing = auths.get(name) if isinstance(auths.get(name), dict) else {}
        created_at = int(existing.get("created_at") or time.time())
        merged = dict(existing)
        merged.update(meta)
        merged["created_at"] = created_at
        auths[name] = merged
        registry["auths"] = auths
        _audit_auth_control(registry, action="upload", profile=name, ok=True, message="profile uploaded/updated")
        _save_auth_control_registry(registry)
    return {"profile": name, "meta": meta, "health": item}


def _auth_control_assign(
    profile: str,
    node_id: str,
    lease_sec: int,
    force: bool = False,
    notes: str = "",
) -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    target_node = str(node_id or "").strip()
    if not target_node:
        raise HTTPException(status_code=400, detail="node_id is required")
    nodes = _auth_control_nodes_map()
    target = nodes.get(target_node)
    if not target:
        raise HTTPException(status_code=404, detail=f"node not found or disabled: {target_node}")
    auth_item = _get_auth_profile(name)
    if not auth_item:
        raise HTTPException(status_code=404, detail=f"profile not found locally: {name}")
    if not bool(auth_item.get("valid")):
        raise HTTPException(status_code=400, detail=f"profile is not healthy locally: {name}")
    lease = max(60, min(AUTH_CONTROL_MAX_LEASE_SEC, int(lease_sec or AUTH_CONTROL_DEFAULT_LEASE_SEC)))

    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        current = assignments.get(name) if isinstance(assignments.get(name), dict) else {}
        current_node = str(current.get("node_id") or "").strip()
        if current_node and current_node != target_node and (not force):
            raise HTTPException(
                status_code=409,
                detail=f"profile already assigned to {current_node}, set force=true to migrate",
            )
        version = int(current.get("version") or 0) + 1
        token = secrets.token_hex(24)

    if current_node and current_node != target_node:
        old = nodes.get(current_node)
        if old:
            _call_node_api(
                old,
                "POST",
                "/auth/profiles/remove",
                {
                    "profile": name,
                    "assignment_version": version,
                    "assignment_token": token,
                    "assigned_server_id": target_node,
                    "reason": f"migrate_to:{target_node}",
                },
            )
    if force:
        for nid, node in nodes.items():
            if nid == target_node:
                continue
            try:
                _call_node_api(
                    node,
                    "POST",
                    "/auth/profiles/remove",
                    {
                        "profile": name,
                        "assignment_version": version,
                        "assignment_token": token,
                        "assigned_server_id": target_node,
                        "reason": "force_deduplicate",
                    },
                )
            except Exception:
                pass

    source_auth = AUTH_PROFILES_DIR / f"{name}.auth.json"
    source_cfg = AUTH_PROFILES_DIR / f"{name}.config.toml"
    if not source_auth.exists():
        raise HTTPException(status_code=404, detail=f"local auth file missing: {source_auth}")
    _call_node_api(
        target,
        "POST",
        "/auth/profiles/upload",
        {
            "profile": name,
            "provider": str(auths.get(name, {}).get("provider") if isinstance(auths.get(name), dict) else "codex"),
            "auth_json": json.loads(source_auth.read_text(encoding="utf-8")),
            "config_toml": source_cfg.read_text(encoding="utf-8") if source_cfg.exists() else "",
            "assignment_version": version,
            "assignment_token": token,
            "assigned_server_id": target_node,
            "notes": notes,
        },
    )

    now_ts = int(time.time())
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        existing_auth = auths.get(name) if isinstance(auths.get(name), dict) else {}
        if not existing_auth:
            existing_auth = _merge_auth_control_auth_meta(auth_item)
            existing_auth["created_at"] = now_ts
        existing_auth["updated_at"] = now_ts
        auths[name] = existing_auth
        assignments[name] = {
            "profile": name,
            "node_id": target_node,
            "state": "assigned",
            "version": version,
            "token": token,
            "lease_sec": lease,
            "lease_expire_at": now_ts + lease,
            "updated_at": now_ts,
            "last_error": "",
        }
        registry["auths"] = auths
        registry["assignments"] = assignments
        _audit_auth_control(
            registry,
            action="assign",
            profile=name,
            node_id=target_node,
            ok=True,
            message=f"assigned with lease={lease}s version={version}",
        )
        _save_auth_control_registry(registry)
    return {
        "profile": name,
        "node_id": target_node,
        "version": version,
        "lease_expire_at": now_ts + lease,
    }


def _auth_control_revoke(profile: str, reason: str = "") -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    nodes = _auth_control_nodes_map()
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        current = assignments.get(name) if isinstance(assignments.get(name), dict) else {}
        current_node = str(current.get("node_id") or "").strip()
        version = int(current.get("version") or 0) + 1
        token = secrets.token_hex(24)
    if current_node:
        node = nodes.get(current_node)
        if node:
            _call_node_api(
                node,
                "POST",
                "/auth/profiles/remove",
                {
                    "profile": name,
                    "assignment_version": version,
                    "assignment_token": token,
                    "assigned_server_id": current_node,
                    "reason": str(reason or "manual revoke"),
                },
            )
    now_ts = int(time.time())
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        assignments[name] = {
            "profile": name,
            "node_id": "",
            "state": "unassigned",
            "version": version,
            "token": token,
            "lease_sec": 0,
            "lease_expire_at": 0,
            "updated_at": now_ts,
            "last_error": str(reason or "").strip(),
        }
        registry["assignments"] = assignments
        _audit_auth_control(
            registry,
            action="revoke",
            profile=name,
            node_id=current_node,
            ok=True,
            message=str(reason or "manual revoke"),
        )
        _save_auth_control_registry(registry)
    return {"profile": name, "revoked_from": current_node, "version": version, "updated_at": now_ts}


def _auth_control_health_check(profile: str = "", node_id: str = "", mode: str = "status", prompt: str = "", timeout_sec: int = 0) -> Dict[str, Any]:
    name = str(profile or "").strip()
    target_node_id = str(node_id or "").strip()
    clean_mode = str(mode or "status").strip().lower() or "status"
    nodes = _auth_control_nodes_map()
    selected: List[Dict[str, Any]] = []
    for nid, node in nodes.items():
        if target_node_id and nid != target_node_id:
            continue
        selected.append(node)
    if target_node_id and not selected:
        raise HTTPException(status_code=404, detail=f"node not found or disabled: {target_node_id}")
    out_nodes: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    for node in selected:
        nid = str(node.get("id") or "")
        try:
            if _is_real_turn_health_mode(clean_mode):
                payload = _call_node_api(
                    node,
                    "POST",
                    "/auth/health-check",
                    {
                        "profile": name,
                        "mode": clean_mode,
                        "prompt": str(prompt or "").strip(),
                        "timeout_sec": int(timeout_sec or 0),
                    },
                )
            else:
                payload = _call_node_api(node, "GET", "/auth/profiles", None)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            profiles = data.get("profiles") if isinstance(data.get("profiles"), list) else []
            items = [
                item
                for item in profiles
                if isinstance(item, dict) and ((not name) or str(item.get("profile") or "").strip() == name)
            ]
            out_nodes.append({"node_id": nid, "ok": True, "profiles": items, "error": ""})
        except Exception as exc:
            out_nodes.append({"node_id": nid, "ok": False, "profiles": [], "error": str(exc)})
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        for item in out_nodes:
            nid = str(item.get("node_id") or "")
            if not bool(item.get("ok")):
                continue
            for p in list(assignments.values()):
                if not isinstance(p, dict):
                    continue
                if str(p.get("node_id") or "").strip() != nid:
                    continue
                p["last_health_check_at"] = now_ts
                p["last_health_ok"] = True
        registry["assignments"] = assignments
        _audit_auth_control(
            registry,
            action="health_check",
            profile=name,
            node_id=target_node_id,
            ok=True,
            message=f"mode={clean_mode}",
        )
        _save_auth_control_registry(registry)
    return {"timestamp": now_ts, "mode": clean_mode, "profile": name, "nodes": out_nodes}


def _is_real_turn_health_mode(mode: str) -> bool:
    clean = str(mode or "").strip().lower()
    return clean in {"real", "real_turn", "turn", "conversation", "chat"}


def _real_turn_probe_auth_profile(profile: str, prompt: str = "", timeout_sec: int = 0) -> Dict[str, Any]:
    clean = str(profile or "").strip()
    probe_prompt = str(prompt or AUTH_REAL_HEALTH_CHECK_PROMPT).strip() or AUTH_REAL_HEALTH_CHECK_PROMPT
    probe_timeout = int(timeout_sec or AUTH_REAL_HEALTH_CHECK_TIMEOUT_SEC)
    probe_timeout = max(10, min(300, probe_timeout))
    runtime_id = f"diag_auth_real_{clean or 'default'}_{int(time.time() * 1000)}"
    runtime = RUNTIMES.get(runtime_id)
    turn_id = ""
    thread_id = ""
    done_error = ""
    done_status = ""
    done_text = ""
    try:
        with runtime.lock:
            runtime.last_input_at = int(time.time())
            runtime.cwd = DEFAULT_CWD
            runtime.model = DEFAULT_MODEL
            runtime.sandbox = DEFAULT_SANDBOX
            runtime.approval_policy = DEFAULT_APPROVAL
            runtime.personality = DEFAULT_PERSONALITY
            _switch_runtime_auth_profile(runtime, profile=clean, reason="history auth real health-check")
            thread_id = _ensure_thread(runtime, reset_thread=True)
            started = runtime.client.turn_start(thread_id=thread_id, text=probe_prompt, image_paths=[])
            turn = started.get("turn") if isinstance(started.get("turn"), dict) else {}
            turn_id = str(turn.get("id") or "")
            if not turn_id:
                raise AppServerError(f"turn/start returned no turn id: {started}")
            runtime.active_turn_id = turn_id
        done = runtime.client.wait_for_turn_completion(thread_id=thread_id, turn_id=turn_id, timeout_sec=probe_timeout)
        done_status = str(done.turn_status or "")
        done_text = str(done.text or "")
        if isinstance(done.error, dict) and done.error:
            done_error = json.dumps(done.error, ensure_ascii=False)
    except HTTPException as exc:
        done_error = str(exc.detail or exc)
    except Exception as exc:
        done_error = str(exc)
    finally:
        with runtime.lock:
            runtime.active_turn_id = ""
            runtime.thread_id = ""
            try:
                runtime.client.stop()
            except Exception:
                pass
            _persist_runtime(
                runtime,
                {
                    "thread_id": "",
                    "active_turn_id": "",
                    "last_turn_status": "completed" if not done_error else "failed",
                    "last_turn_id": turn_id,
                    "last_turn_at": int(time.time()),
                    "last_error": done_error[:1200] if done_error else "",
                },
            )
    ok = (not done_error) and (done_status.lower() == "completed")
    if (not done_error) and (not ok):
        done_error = f"turn ended with status={done_status or 'unknown'}"
    return {
        "ok": ok,
        "status": done_status or ("completed" if ok else "failed"),
        "error": done_error[:1200] if done_error else "",
        "assistant_text": done_text[:200],
        "thread_id": thread_id,
        "turn_id": turn_id,
        "checked_at": int(time.time()),
        "mode": "real_turn",
    }


def _apply_health_probe_result(profile: str, probe: Dict[str, Any]) -> Dict[str, Any]:
    clean = str(profile or "").strip()
    now_ts = int(probe.get("checked_at") or time.time())
    ok = bool(probe.get("ok"))
    message = str(probe.get("error") or "").strip()
    status = str(probe.get("status") or "").strip()
    patch: Dict[str, Any] = {
        "last_health_check_at": now_ts,
        "updated_at": now_ts,
    }
    if ok:
        patch.update(
            {
                "valid": True,
                "status": "active",
                "reason": "",
                "disabled_until": 0,
                "disabled_reason": "",
                "needs_reauth": False,
                "risk_deactivated": False,
                "last_health_error": "",
            }
        )
    else:
        classified = _classify_auth_error(message)
        if classified == "needs_reauth":
            patch.update(
                {
                    "valid": False,
                    "status": "needs_reauth",
                    "reason": message[:1200] or "真实对话检测失败：登录态失效",
                    "needs_reauth": True,
                    "risk_deactivated": False,
                    "disabled_until": 0,
                    "disabled_reason": "refresh token 已失效，需替换 auth.json",
                    "last_health_error": message[:1200],
                }
            )
        elif classified == "deactivated":
            patch.update(
                {
                    "valid": False,
                    "status": "deactivated",
                    "reason": message[:1200] or "真实对话检测失败：账号疑似被停用/风控",
                    "needs_reauth": False,
                    "risk_deactivated": True,
                    "disabled_until": 0,
                    "disabled_reason": "账号疑似被停用/风控",
                    "last_health_error": message[:1200],
                }
            )
        else:
            disable_sec = _parse_disable_seconds_from_message(message)
            if disable_sec <= 0:
                disable_sec = AUTH_REAL_HEALTH_CHECK_FAIL_DISABLE_SEC
            disabled_until = now_ts + max(60, int(disable_sec))
            reason = message[:1200] if message else f"真实对话检测失败：status={status or 'failed'}"
            patch.update(
                {
                    "valid": False,
                    "status": "temp_disabled",
                    "reason": reason,
                    "needs_reauth": False,
                    "risk_deactivated": False,
                    "disabled_until": disabled_until,
                    "disabled_reason": f"真实对话检测失败，暂时禁用到 {time.strftime('%m-%d %H:%M', time.localtime(disabled_until))}",
                    "last_health_error": reason,
                }
            )
    _patch_auth_registry_profile(clean, patch)
    refreshed = _auth_registry_by_profile().get(clean)
    payload = dict(refreshed or {"profile": clean})
    payload["last_probe"] = probe
    return payload


def _health_check_auth_profile_item(item: Dict[str, Any], mode: str, prompt: str = "", timeout_sec: int = 0) -> Dict[str, Any]:
    profile = str((item or {}).get("profile") or "").strip()
    if not _is_real_turn_health_mode(mode):
        payload = dict(item or {})
        payload["last_probe"] = {"mode": "status", "ok": bool(payload.get("valid")), "checked_at": int(time.time())}
        return payload
    probe = _real_turn_probe_auth_profile(profile=profile, prompt=prompt, timeout_sec=timeout_sec)
    return _apply_health_probe_result(profile, probe)


def _get_auth_profile(profile: str) -> Optional[Dict[str, Any]]:
    target = str(profile or "").strip()
    for item in _refresh_auth_profiles():
        if str(item.get("profile") or "").strip() == target:
            return item
    return None


def _list_switchable_auth_profiles() -> List[Dict[str, Any]]:
    now_ts = int(time.time())
    return [{"profile": "", "label": "default", "valid": True, "email": "", "status": "active"}] + [
        item for item in _refresh_auth_profiles() if _auth_profile_available(item, now_ts=now_ts)
    ]


def _pick_next_auth_profile(current_profile: str) -> Optional[Dict[str, Any]]:
    items = _list_switchable_auth_profiles()
    current = str(current_profile or "").strip()
    named_items = [item for item in items if str(item.get("profile") or "").strip()]
    if current:
        items = named_items
    if not items:
        return None
    keys = [str(item.get("profile") or "").strip() for item in items]
    try:
        start = keys.index(current)
    except ValueError:
        start = -1
    for idx in range(1, len(items) + 1):
        item = items[(start + idx) % len(items)]
        if str(item.get("profile") or "").strip() != current:
            return item
    return None


def _is_auth_limit_error(message: str) -> bool:
    return bool(re.search(r"(429|rate[\s_-]*limit|quota|insufficient[\s_-]*quota|usage limit)", str(message or ""), re.I))


def _rate_limit_exhausted(rate_limits: Dict[str, Any]) -> bool:
    if not isinstance(rate_limits, dict):
        return False
    for key in ("primary", "secondary"):
        node = rate_limits.get(key) if isinstance(rate_limits.get(key), dict) else {}
        try:
            used_percent = float(node.get("usedPercent"))
        except Exception:
            continue
        if used_percent >= float(AUTO_AUTH_SWITCH_THRESHOLD_PCT):
            return True
    return False


def _apply_auth_error_policy(
    runtime: ChatRuntime,
    failed_profile: str,
    error_message: str,
    allow_switch: bool = True,
) -> Dict[str, Any]:
    profile = str(failed_profile or "").strip()
    message = str(error_message or "").strip()
    if not profile or not message:
        return {"classification": "", "switch": None, "note": "", "deleted": False}

    classification = _classify_auth_error(message)
    now_ts = int(time.time())
    note = ""
    deleted = False
    patch: Dict[str, Any] = {
        "valid": False,
        "reason": message[:1200],
        "last_health_check_at": now_ts,
        "last_health_error": message[:1200],
    }
    if classification == "temp_disabled":
        disable_sec = _parse_disable_seconds_from_message(message) or 5 * 3600
        disabled_until = now_ts + max(300, int(disable_sec))
        patch.update(
            {
                "status": "temp_disabled",
                "disabled_until": disabled_until,
                "disabled_reason": f"触发额度/频控上限：{message[:300]}",
                "needs_reauth": False,
                "risk_deactivated": False,
            }
        )
        note = f"账号 `{profile}` 触发额度上限，已临时禁用至 {time.strftime('%m-%d %H:%M', time.localtime(disabled_until))}"
    elif classification == "needs_reauth":
        patch.update(
            {
                "status": "needs_reauth",
                "disabled_until": 0,
                "disabled_reason": "refresh token 已失效，需替换 auth.json",
                "needs_reauth": True,
                "risk_deactivated": False,
            }
        )
        note = f"账号 `{profile}` 登录态失效，需要重新获取并替换 `auth.json`"
    elif classification == "deactivated":
        patch.update(
            {
                "status": "deactivated",
                "disabled_until": 0,
                "disabled_reason": "账号疑似被停用/风控",
                "needs_reauth": False,
                "risk_deactivated": True,
            }
        )
        note = f"账号 `{profile}` 疑似被停用/风控，已从账号池移除"
    else:
        return {"classification": "", "switch": None, "note": "", "deleted": False}

    _patch_auth_registry_profile(profile, patch)
    if classification == "deactivated":
        deleted = _remove_auth_profile_artifacts(profile)

    switched: Optional[Dict[str, Any]] = None
    if allow_switch:
        target = _pick_next_auth_profile(runtime.auth_profile)
        if target:
            target_profile = str(target.get("profile") or "").strip()
            if target_profile != str(runtime.auth_profile or "").strip():
                switched = _switch_runtime_auth_profile(
                    runtime,
                    profile=target_profile,
                    reason=f"auth policy {classification}: {message[:200]}",
                )
    return {"classification": classification, "switch": switched, "note": note, "deleted": deleted}


def _apply_runtime_auth_profile(runtime: ChatRuntime) -> None:
    target_home = _sync_runtime_home(runtime)
    runtime.client.env["CODEX_HOME"] = str(target_home)
    _apply_runtime_bridge_env(runtime)


def _switch_runtime_auth_profile(runtime: ChatRuntime, profile: str, reason: str = "") -> Dict[str, Any]:
    target = str(profile or "").strip()
    meta = _get_auth_profile(target) if target else {"profile": "", "email": "", "home_dir": ""}
    if target and (not meta or not bool(meta.get("valid"))):
        raise HTTPException(status_code=400, detail=f"invalid auth profile: {target}")
    previous = str(runtime.auth_profile or "").strip()
    runtime.auth_profile = target
    runtime.thread_id = ""
    runtime.active_turn_id = ""
    try:
        runtime.client.stop()
    except Exception:
        pass
    _apply_runtime_auth_profile(runtime)
    _persist_runtime(
        runtime,
        {
            "last_error": "",
            "last_token_usage": {},
            "last_rate_limits": {},
            "last_token_usage_profile": "",
            "last_rate_limits_profile": "",
            "last_auto_auth_switch_from": previous,
            "last_auto_auth_switch_to": target,
            "last_auto_auth_switch_reason": str(reason or ""),
            "last_auto_auth_switch_at": int(time.time()),
        },
    )
    return {
        "from": previous,
        "to": target,
        "identity": str((meta or {}).get("email") or (meta or {}).get("sub") or ""),
        "home_dir": str((meta or {}).get("home_dir") or ""),
    }


def _maybe_auto_switch_auth_profile(runtime: ChatRuntime, reason: str = "") -> Optional[Dict[str, Any]]:
    if not AUTO_AUTH_SWITCH_ENABLED:
        return None
    target = _pick_next_auth_profile(runtime.auth_profile)
    if not target:
        return None
    profile = str(target.get("profile") or "").strip()
    if profile == str(runtime.auth_profile or "").strip():
        return None
    info = _switch_runtime_auth_profile(runtime, profile=profile, reason=reason)
    LOG.warning(
        "auto auth switch chat_id=%s from=%s to=%s reason=%s",
        runtime.chat_id,
        info.get("from") or "default",
        info.get("to") or "default",
        reason,
    )
    return info


def _ensure_thread(runtime: ChatRuntime, reset_thread: bool = False) -> str:
    if reset_thread:
        runtime.thread_id = ""
        runtime.active_turn_id = ""

    if runtime.thread_id and runtime.is_client_running():
        return runtime.thread_id

    if not runtime.is_client_running():
        runtime.client.start()

    if runtime.thread_id:
        try:
            runtime.client.thread_resume(
                thread_id=runtime.thread_id,
                cwd=runtime.cwd,
                model=runtime.model,
                sandbox=runtime.sandbox,
                approval_policy=runtime.approval_policy,
            )
            return runtime.thread_id
        except Exception as exc:
            LOG.warning("thread resume failed, creating new thread chat_id=%s err=%s", runtime.chat_id, exc)
            runtime.thread_id = ""

    started = runtime.client.thread_start(
        cwd=runtime.cwd,
        model=runtime.model,
        sandbox=runtime.sandbox,
        approval_policy=runtime.approval_policy,
        personality=runtime.personality,
    )
    thread = started.get("thread") if isinstance(started.get("thread"), dict) else {}
    runtime.thread_id = str(thread.get("id") or "")
    if not runtime.thread_id:
        raise AppServerError(f"thread/start returned no thread id: {started}")
    _persist_runtime(runtime)
    return runtime.thread_id


APP = FastAPI(title="feicodex-rocket-bridge", version="0.2.0")
APP.mount("/history-static", StaticFiles(directory=HISTORY_WEB_DIST_DIR), name="history_static")
ROUTER = APIRouter(prefix=API_PREFIX)


@APP.get("/healthz")
def healthz() -> Dict[str, Any]:
    sweeper_alive = bool(_IDLE_SWEEPER_THREAD and _IDLE_SWEEPER_THREAD.is_alive())
    return {
        "ok": True,
        "service": "feicodex-rocket-bridge",
        "api_prefix": API_PREFIX,
        "active_runtime_chats": RUNTIMES.runtimes_count(),
        "idle_evict_sec": IDLE_EVICT_SEC,
        "idle_sweep_interval_sec": IDLE_SWEEP_INTERVAL_SEC,
        "idle_sweeper_enabled": bool(IDLE_EVICT_SEC > 0),
        "idle_sweeper_alive": sweeper_alive,
        "timestamp": int(time.time()),
    }


@ROUTER.get("/chat/{chat_id}/status", dependencies=[Depends(require_api_token)])
def chat_status(chat_id: str) -> Dict[str, Any]:
    runtime = RUNTIMES.get(chat_id)
    persisted = STORE.get_chat(chat_id)
    source_chat_id = _runtime_actual_chat_id(chat_id)
    thread_id = str(runtime.thread_id or persisted.get("thread_id") or "")
    thread_status: Dict[str, Any] = {}
    token_usage: Dict[str, Any] = {}
    rate_limits: Dict[str, Any] = {}
    turn_progress: Dict[str, Any] = {}
    turn_events: List[Dict[str, Any]] = []
    runtime_active_turn_id = str(runtime.active_turn_id or "")
    active_turn_id = str(runtime_active_turn_id or persisted.get("active_turn_id") or "")
    auth_profile = str(runtime.auth_profile or persisted.get("auth_profile") or "")
    auth_meta = _get_auth_profile(auth_profile) if auth_profile else None

    has_live_client = bool(thread_id and runtime.is_client_running())
    if has_live_client:
        thread_status = runtime.client.get_thread_status(thread_id)
        token_usage = runtime.client.get_thread_token_usage(thread_id)
        rate_limits = _read_rate_limits(runtime)
        turn_progress = runtime.client.get_turn_progress(thread_id)
        client_active_turn_id = str(runtime.client.get_active_turn_id(thread_id) or "")
        if client_active_turn_id:
            active_turn_id = client_active_turn_id
        elif str(thread_status.get("type") or "").lower() == "idle":
            # app-server reports idle, so persisted active turn id is stale.
            active_turn_id = ""
        turn_events = runtime.client.get_turn_events(thread_id=thread_id, turn_id=active_turn_id, limit=8)
    if not rate_limits:
        rate_limits = _read_rate_limits(runtime, allow_request=False)
    if (
        not token_usage
        and isinstance(persisted.get("last_token_usage"), dict)
        and str(persisted.get("last_token_usage_profile") or "") == auth_profile
    ):
        token_usage = dict(persisted.get("last_token_usage") or {})
    if (
        not rate_limits
        and isinstance(persisted.get("last_rate_limits"), dict)
        and str(persisted.get("last_rate_limits_profile") or "") == auth_profile
    ):
        rate_limits = dict(persisted.get("last_rate_limits") or {})
    status_type = str(thread_status.get("type") or "").strip().lower()
    state_patch: Dict[str, Any] = {}
    persisted_active_turn = str(persisted.get("active_turn_id") or "")
    if persisted_active_turn:
        stale_without_live_runtime = (not has_live_client) and (not runtime_active_turn_id)
        if stale_without_live_runtime or not active_turn_id:
            active_turn_id = ""
            state_patch["active_turn_id"] = ""
        elif active_turn_id != persisted_active_turn:
            state_patch["active_turn_id"] = active_turn_id
    if str(persisted.get("last_turn_status") or "").strip().lower() == "running" and not active_turn_id:
        if status_type in {"", "idle", "systemerror"}:
            state_patch["last_turn_status"] = "failed"
            if not str(persisted.get("last_error") or "").strip():
                state_patch["last_error"] = "stale running state cleared after app-server disconnect/restart"
    if state_patch:
        persisted = _persist_runtime(runtime, state_patch)
    last_auto_auth_switch = {
        "from": str(persisted.get("last_auto_auth_switch_from") or "").strip(),
        "to": str(persisted.get("last_auto_auth_switch_to") or "").strip(),
        "reason": str(persisted.get("last_auto_auth_switch_reason") or "").strip(),
        "at": int(persisted.get("last_auto_auth_switch_at") or 0),
    }

    return {
        "ok": True,
        "data": {
            "chat_id": source_chat_id,
            "runtime_id": chat_id,
            "project": _runtime_project_name(chat_id) or _project_label_for_cwd(str(runtime.cwd or persisted.get("cwd") or DEFAULT_CWD)),
            "thread_id": thread_id,
            "active_turn_id": active_turn_id,
            "thread_status": thread_status,
            "token_usage": token_usage,
            "rate_limits": rate_limits,
            "turn_progress": turn_progress,
            "turn_events": turn_events,
            "cwd": str(runtime.cwd or persisted.get("cwd") or DEFAULT_CWD),
            "model": str(runtime.model or persisted.get("model") or DEFAULT_MODEL),
            "sandbox": str(runtime.sandbox or persisted.get("sandbox") or DEFAULT_SANDBOX),
            "approval_policy": str(runtime.approval_policy or persisted.get("approval_policy") or DEFAULT_APPROVAL),
            "personality": str(runtime.personality or persisted.get("personality") or DEFAULT_PERSONALITY),
            "auth_profile": auth_profile,
            "auth_identity": str((auth_meta or {}).get("email") or (auth_meta or {}).get("sub") or ""),
            "auto_auth_switch_enabled": AUTO_AUTH_SWITCH_ENABLED,
            "auto_auth_switch_threshold_pct": AUTO_AUTH_SWITCH_THRESHOLD_PCT,
            "last_auto_auth_switch": last_auto_auth_switch,
            "state": persisted,
        },
    }


@ROUTER.get("/auth/profiles", dependencies=[Depends(require_api_token)])
def auth_profiles_list() -> Dict[str, Any]:
    profiles = _refresh_auth_profiles()
    return {
        "ok": True,
        "data": {
            "profiles": [
                {
                    "profile": "",
                    "label": "default",
                    "email": "",
                    "valid": True,
                    "reason": "",
                    "home_dir": "",
                    "source_auth_json": "",
                    "status": "active",
                    "disabled_until": 0,
                    "disabled_reason": "",
                    "needs_reauth": False,
                    "risk_deactivated": False,
                    "last_health_check_at": 0,
                    "last_health_error": "",
                }
            ]
            + profiles
        },
    }


@ROUTER.post("/auth/profiles/upload", dependencies=[Depends(require_api_token)])
def auth_profiles_upload(body: AuthProfileUploadRequest) -> Dict[str, Any]:
    installed = _install_auth_profile(
        profile=body.profile,
        provider=body.provider,
        auth_json=body.auth_json,
        config_toml=body.config_toml,
        assignment_version=int(body.assignment_version or 0),
        assignment_token=str(body.assignment_token or "").strip(),
        assigned_server_id=str(body.assigned_server_id or "").strip(),
        notes=body.notes,
    )
    return {"ok": True, "data": {"profile": installed}}


@ROUTER.post("/auth/profiles/remove", dependencies=[Depends(require_api_token)])
def auth_profiles_remove(body: AuthProfileRemoveRequest) -> Dict[str, Any]:
    payload = _remove_auth_profile(
        profile=body.profile,
        assignment_version=int(body.assignment_version or 0),
        assignment_token=str(body.assignment_token or "").strip(),
        assigned_server_id=str(body.assigned_server_id or "").strip(),
        reason=body.reason,
    )
    return {"ok": True, "data": payload}


@ROUTER.post("/auth/health-check", dependencies=[Depends(require_api_token)])
def auth_profiles_health_check(body: AuthApiHealthCheckRequest) -> Dict[str, Any]:
    target = str(body.profile or "").strip()
    mode = str(body.mode or AUTH_HEALTH_CHECK_DEFAULT_MODE).strip().lower() or AUTH_HEALTH_CHECK_DEFAULT_MODE
    probe_prompt = str(body.prompt or "").strip()
    probe_timeout = int(body.timeout_sec or 0)
    if target:
        item = _get_auth_profile(target)
        if not item:
            raise HTTPException(status_code=404, detail=f"profile not found: {target}")
        profiles = [_health_check_auth_profile_item(item=item, mode=mode, prompt=probe_prompt, timeout_sec=probe_timeout)]
    else:
        seed = _refresh_auth_profiles()
        profiles = [
            _health_check_auth_profile_item(item=item, mode=mode, prompt=probe_prompt, timeout_sec=probe_timeout)
            for item in seed
        ]
    now_ts = int(time.time())
    normalized: List[Dict[str, Any]] = []
    for item in profiles:
        payload = dict(item)
        disabled_until = int(payload.get("disabled_until") or 0)
        payload["label"] = str(payload.get("profile") or "").strip() or "default"
        payload["available"] = _auth_profile_available(payload, now_ts=now_ts)
        payload["disabled_remaining_sec"] = max(0, disabled_until - now_ts) if disabled_until > 0 else 0
        normalized.append(payload)
    return {"ok": True, "data": {"profiles": normalized, "timestamp": now_ts, "mode": mode}}


@ROUTER.get("/auth/control/state", dependencies=[Depends(require_api_token)])
def auth_control_state() -> Dict[str, Any]:
    return {"ok": True, "data": _auth_control_registry_state()}


@ROUTER.post("/auth/control/upload", dependencies=[Depends(require_api_token)])
def auth_control_upload(body: AuthControlUploadRequest) -> Dict[str, Any]:
    data = _auth_control_upload(
        profile=body.profile,
        provider=body.provider,
        auth_json=body.auth_json,
        config_toml=body.config_toml,
        label=body.label,
        notes=body.notes,
    )
    return {"ok": True, "data": data}


@ROUTER.post("/auth/control/assign", dependencies=[Depends(require_api_token)])
def auth_control_assign(body: AuthControlAssignRequest) -> Dict[str, Any]:
    data = _auth_control_assign(
        profile=body.profile,
        node_id=body.node_id,
        lease_sec=int(body.lease_sec or AUTH_CONTROL_DEFAULT_LEASE_SEC),
        force=bool(body.force),
        notes=body.notes,
    )
    return {"ok": True, "data": data}


@ROUTER.post("/auth/control/revoke", dependencies=[Depends(require_api_token)])
def auth_control_revoke(body: AuthControlRevokeRequest) -> Dict[str, Any]:
    data = _auth_control_revoke(profile=body.profile, reason=body.reason)
    return {"ok": True, "data": data}


@ROUTER.post("/auth/control/health-check", dependencies=[Depends(require_api_token)])
def auth_control_health_check(body: AuthControlHealthCheckRequest) -> Dict[str, Any]:
    data = _auth_control_health_check(
        profile=body.profile,
        node_id=body.node_id,
        mode=body.mode,
        prompt=body.prompt,
        timeout_sec=int(body.timeout_sec or 0),
    )
    return {"ok": True, "data": data}


@ROUTER.post("/chat/{chat_id}/config", dependencies=[Depends(require_api_token)])
def chat_config_update(chat_id: str, body: UpdateChatConfigRequest) -> Dict[str, Any]:
    runtime = RUNTIMES.get(chat_id)
    with runtime.lock:
        _resolve_chat_config(runtime, body)
        state = _persist_runtime(runtime, {"last_error": ""})
        return {
            "ok": True,
            "data": {
                "chat_id": chat_id,
                "cwd": runtime.cwd,
                "model": runtime.model,
                "sandbox": runtime.sandbox,
                "approval_policy": runtime.approval_policy,
                "personality": runtime.personality,
                "state": state,
            },
        }


@ROUTER.post("/chat/{chat_id}/auth-profile", dependencies=[Depends(require_api_token)])
def chat_auth_profile_update(chat_id: str, body: UpdateChatAuthProfileRequest) -> Dict[str, Any]:
    runtime = RUNTIMES.get(chat_id)
    profile = str(body.profile or "").strip()

    with runtime.lock:
        info = _switch_runtime_auth_profile(runtime, profile=profile, reason="manual")
        state = STORE.get_chat(chat_id)
        return {
            "ok": True,
            "data": {
                "chat_id": chat_id,
                "auth_profile": profile,
                "auth_identity": str(info.get("identity") or ""),
                "home_dir": str(info.get("home_dir") or ""),
                "state": state,
            },
        }


@ROUTER.post("/chat/{chat_id}/thread/reset", dependencies=[Depends(require_api_token)])
def chat_thread_reset(chat_id: str, body: ResetThreadRequest) -> Dict[str, Any]:
    runtime = RUNTIMES.get(chat_id)
    with runtime.lock:
        runtime.last_input_at = int(time.time())
        _resolve_chat_config(runtime, body)
        _apply_runtime_auth_profile(runtime)
        runtime.active_turn_id = ""
        try:
            runtime.client.stop()
        except Exception:
            pass
        runtime.client.start()
        started = runtime.client.thread_start(
            cwd=runtime.cwd,
            model=runtime.model,
            sandbox=runtime.sandbox,
            approval_policy=runtime.approval_policy,
            personality=runtime.personality,
        )
        thread = started.get("thread") if isinstance(started.get("thread"), dict) else {}
        runtime.thread_id = str(thread.get("id") or "")
        if not runtime.thread_id:
            raise HTTPException(status_code=502, detail="thread/start returned no thread id")
        state = _persist_runtime(runtime, {"last_error": ""})

    return {"ok": True, "data": {"thread_id": runtime.thread_id, "state": state}}


@ROUTER.post("/chat/{chat_id}/turn", dependencies=[Depends(require_api_token)])
def chat_turn(chat_id: str, body: TurnRequest) -> Dict[str, Any]:
    runtime = RUNTIMES.get(chat_id)
    auto_auth_switch: Optional[Dict[str, Any]] = None
    turn_started_at = 0
    thread_id = ""
    turn_id = ""
    turn_cwd = ""
    turn_model = ""
    turn_auth_profile = ""
    visible_user_text = str(body.text or "")
    turn_input_text = visible_user_text
    with runtime.lock:
        runtime.last_input_at = int(time.time())
        _resolve_chat_config(runtime, body)
        _apply_runtime_auth_profile(runtime)
        _maybe_self_heal_disconnected_runtime(runtime)
        turn_cwd = str(runtime.cwd or DEFAULT_CWD)
        turn_model = str(runtime.model or DEFAULT_MODEL)
        turn_auth_profile = str(runtime.auth_profile or "")
        preflight_limits = _read_rate_limits(runtime, allow_request=runtime.is_client_running())
        if not preflight_limits:
            persisted = STORE.get_chat(chat_id)
            persisted_profile = str(persisted.get("last_rate_limits_profile") or "")
            if persisted_profile == turn_auth_profile and isinstance(persisted.get("last_rate_limits"), dict):
                preflight_limits = dict(persisted.get("last_rate_limits") or {})
        if _rate_limit_exhausted(preflight_limits):
            auto_auth_switch = _maybe_auto_switch_auth_profile(runtime, reason="preflight rate limit exhausted")
        try:
            thread_id = _ensure_thread(runtime, reset_thread=bool(body.reset_thread))
        except AppServerError as exc:
            state = _persist_runtime(runtime, {"last_error": str(exc), "last_turn_status": "failed"})
            raise HTTPException(
                status_code=502,
                detail={"ok": False, "error": str(exc), "thread_id": runtime.thread_id, "state": state},
            ) from exc
        active_now = str(runtime.client.get_active_turn_id(thread_id) or "")
        if active_now != str(runtime.active_turn_id or ""):
            runtime.active_turn_id = active_now
            _persist_runtime(runtime)
        if active_now and not bool(body.reset_thread):
            thread_status = runtime.client.get_thread_status(thread_id)
            if str(thread_status.get("type") or "").lower() == "idle":
                # get_active_turn_id can lag briefly after completion; trust idle status.
                active_now = ""
                runtime.active_turn_id = ""
                _persist_runtime(runtime, {"last_error": ""})
        if active_now and not bool(body.reset_thread):
            state = _persist_runtime(runtime, {"last_error": "turn already running", "last_turn_status": "running"})
            raise HTTPException(
                status_code=409,
                detail={
                    "ok": False,
                    "error": "turn already running",
                    "thread_id": thread_id,
                    "active_turn_id": active_now,
                    "state": state,
                },
            )

        try:
            turn_start = runtime.client.turn_start(
                thread_id=thread_id,
                text=turn_input_text,
                image_paths=[str(p) for p in list(body.image_paths or []) if str(p).strip()],
            )
        except AppServerError as exc:
            raw_err = str(exc)
            handled = _apply_auth_error_policy(runtime, failed_profile=turn_auth_profile, error_message=raw_err, allow_switch=True)
            switched = handled.get("switch") if isinstance(handled, dict) else None
            note = str((handled or {}).get("note") or "").strip()
            if isinstance(switched, dict):
                auto_auth_switch = switched
                if note:
                    auto_auth_switch["note"] = note
                thread_id = _ensure_thread(runtime, reset_thread=True)
                turn_start = runtime.client.turn_start(
                    thread_id=thread_id,
                    text=turn_input_text,
                    image_paths=[str(p) for p in list(body.image_paths or []) if str(p).strip()],
                )
            elif _is_auth_limit_error(raw_err) and not auto_auth_switch:
                auto_auth_switch = _maybe_auto_switch_auth_profile(runtime, reason=raw_err)
                if auto_auth_switch:
                    thread_id = _ensure_thread(runtime, reset_thread=True)
                    turn_start = runtime.client.turn_start(
                        thread_id=thread_id,
                        text=turn_input_text,
                        image_paths=[str(p) for p in list(body.image_paths or []) if str(p).strip()],
                    )
                else:
                    err_text = raw_err + (f"\n{note}" if note else "")
                    state = _persist_runtime(runtime, {"last_error": err_text, "last_turn_status": "failed"})
                    raise HTTPException(
                        status_code=502,
                        detail={"ok": False, "error": err_text, "thread_id": runtime.thread_id, "state": state},
                    ) from exc
            else:
                err_text = raw_err + (f"\n{note}" if note else "")
                state = _persist_runtime(runtime, {"last_error": err_text, "last_turn_status": "failed"})
                raise HTTPException(
                    status_code=502,
                    detail={"ok": False, "error": err_text, "thread_id": runtime.thread_id, "state": state},
                ) from exc

        turn = turn_start.get("turn") if isinstance(turn_start.get("turn"), dict) else {}
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            state = _persist_runtime(runtime, {"last_error": f"turn/start returned no turn id: {turn_start}"})
            raise HTTPException(
                status_code=502,
                detail={"ok": False, "error": "turn/start returned no turn id", "thread_id": runtime.thread_id, "state": state},
            )

        runtime.active_turn_id = turn_id
        turn_started_at = int(time.time())
        _persist_runtime(runtime, {"last_user_text": visible_user_text, "last_error": ""})

    try:
        done = runtime.client.wait_for_turn_completion(
            thread_id=thread_id,
            turn_id=turn_id,
            timeout_sec=int(body.timeout_sec),
        )
        thread_status = runtime.client.get_thread_status(thread_id)
        token_usage = runtime.client.get_thread_token_usage(thread_id)
        rate_limits = _read_rate_limits(runtime)
        with runtime.lock:
            if str(runtime.active_turn_id) == str(turn_id):
                runtime.active_turn_id = ""
            state = _persist_runtime(
                runtime,
                {
                    "last_turn_id": done.turn_id,
                    "last_turn_status": done.turn_status,
                    "last_assistant_text": done.text,
                    "last_turn_error": done.error or None,
                    "last_turn_at": int(time.time()),
                    "last_error": "",
                    "last_token_usage": token_usage,
                    "last_rate_limits": rate_limits,
                    "last_token_usage_profile": turn_auth_profile,
                    "last_rate_limits_profile": turn_auth_profile,
                    "disconnect_fail_streak": 0,
                },
            )
            if _rate_limit_exhausted(rate_limits) and not auto_auth_switch:
                auto_auth_switch = _maybe_auto_switch_auth_profile(runtime, reason="post-turn rate limit exhausted")
            done_error_text = json.dumps(done.error, ensure_ascii=False) if done.error else ""
            if done_error_text:
                handled = _apply_auth_error_policy(
                    runtime,
                    failed_profile=turn_auth_profile,
                    error_message=done_error_text,
                    allow_switch=True,
                )
                switched = handled.get("switch") if isinstance(handled, dict) else None
                note = str((handled or {}).get("note") or "").strip()
                if isinstance(switched, dict):
                    auto_auth_switch = switched
                if note:
                    if not isinstance(auto_auth_switch, dict):
                        auto_auth_switch = {}
                    auto_auth_switch["note"] = note
        HISTORY_STORE.append_turn(
            _build_turn_record(
                runtime=runtime,
                turn_id=done.turn_id,
                status=done.turn_status,
                started_at=turn_started_at,
                ended_at=int(time.time()),
                user_text=visible_user_text,
                assistant_text=done.text,
                error_text=json.dumps(done.error, ensure_ascii=False) if done.error else "",
                thread_id=thread_id,
                cwd=turn_cwd,
                model=turn_model,
                auth_profile=turn_auth_profile,
                token_usage=token_usage,
                rate_limits=rate_limits,
            )
        )
        return {
            "ok": True,
            "data": {
                "thread_id": thread_id,
                "turn_id": done.turn_id,
                "turn_status": done.turn_status,
                "assistant_text": done.text,
                "turn_error": done.error,
                "thread_status": thread_status,
                "token_usage": token_usage,
                "rate_limits": rate_limits,
                "auto_auth_switch": auto_auth_switch,
                "state": state,
            },
        }
    except AppServerTimeout as exc:
        with runtime.lock:
            active_now = str(runtime.client.get_active_turn_id(thread_id) or runtime.active_turn_id or "")
            runtime.active_turn_id = active_now
            state = _persist_runtime(runtime, {"last_error": str(exc), "last_turn_status": "timeout"})
        HISTORY_STORE.append_turn(
            _build_turn_record(
                runtime=runtime,
                turn_id=turn_id or active_now,
                status="timeout",
                started_at=turn_started_at,
                ended_at=int(time.time()),
                user_text=visible_user_text,
                assistant_text="",
                error_text=str(exc),
                thread_id=thread_id,
                cwd=turn_cwd,
                model=turn_model,
                auth_profile=turn_auth_profile,
            )
        )
        raise HTTPException(
            status_code=504,
            detail={
                "ok": False,
                "error": str(exc),
                "thread_id": runtime.thread_id,
                "active_turn_id": active_now,
                "state": state,
            },
        ) from exc
    except AppServerError as exc:
        disconnected = isinstance(exc, AppServerDisconnected)
        handled = _apply_auth_error_policy(
            runtime,
            failed_profile=turn_auth_profile,
            error_message=str(exc),
            allow_switch=False,
        )
        note = str((handled or {}).get("note") or "").strip()
        err_text = str(exc) + (f"\n{note}" if note else "")
        active_for_record = ""
        with runtime.lock:
            disconnect_patch: Dict[str, Any] = {}
            if disconnected and _is_disconnect_wait_error(str(exc)):
                disconnect_patch = _disconnect_streak_patch(runtime)
            active_for_record = str(runtime.client.get_active_turn_id(thread_id) or runtime.active_turn_id or "")
            if disconnected:
                runtime.active_turn_id = ""
                runtime.thread_id = ""
                try:
                    runtime.client.stop()
                except Exception:
                    pass
                active_for_record = ""
            else:
                runtime.active_turn_id = active_for_record
            state_patch: Dict[str, Any] = {"last_error": err_text, "last_turn_status": "failed"}
            state_patch.update(disconnect_patch)
            state = _persist_runtime(runtime, state_patch)
        HISTORY_STORE.append_turn(
            _build_turn_record(
                runtime=runtime,
                turn_id=turn_id or active_for_record,
                status="failed",
                started_at=turn_started_at,
                ended_at=int(time.time()),
                user_text=visible_user_text,
                assistant_text="",
                error_text=err_text,
                thread_id=thread_id,
                cwd=turn_cwd,
                model=turn_model,
                auth_profile=turn_auth_profile,
            )
        )
        raise HTTPException(
            status_code=502,
            detail={"ok": False, "error": err_text, "thread_id": runtime.thread_id, "state": state},
        ) from exc


@ROUTER.post("/chat/{chat_id}/turn/steer", dependencies=[Depends(require_api_token)])
def chat_turn_steer(chat_id: str, body: SteerTurnRequest) -> Dict[str, Any]:
    runtime = RUNTIMES.get(chat_id)
    visible_user_text = str(body.text or "")
    steer_input_text = visible_user_text
    with runtime.lock:
        runtime.last_input_at = int(time.time())
        try:
            thread_id = _ensure_thread(runtime, reset_thread=False)
        except AppServerError as exc:
            state = _persist_runtime(runtime, {"last_error": str(exc)})
            raise HTTPException(
                status_code=502,
                detail={"ok": False, "error": str(exc), "thread_id": runtime.thread_id, "state": state},
            ) from exc
        active_turn_id = str(runtime.active_turn_id or runtime.client.get_active_turn_id(thread_id))
        if not active_turn_id:
            state = _persist_runtime(runtime, {"last_error": "no running turn"})
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "error": "no running turn", "thread_id": thread_id, "state": state},
            )

        expected_turn_id = str(body.expected_turn_id or active_turn_id)
        if expected_turn_id != active_turn_id:
            state = _persist_runtime(runtime, {"last_error": "expected_turn_id mismatch"})
            raise HTTPException(
                status_code=409,
                detail={
                    "ok": False,
                    "error": "expected_turn_id mismatch",
                    "thread_id": thread_id,
                    "active_turn_id": active_turn_id,
                    "state": state,
                },
            )

        try:
            steer = runtime.client.turn_steer(
                thread_id=thread_id,
                expected_turn_id=expected_turn_id,
                text=steer_input_text,
                image_paths=[str(p) for p in list(body.image_paths or []) if str(p).strip()],
            )
        except AppServerError as exc:
            state = _persist_runtime(runtime, {"last_error": str(exc)})
            raise HTTPException(
                status_code=502,
                detail={"ok": False, "error": str(exc), "thread_id": thread_id, "state": state},
            ) from exc

        steer_turn_id = str(steer.get("turnId") or active_turn_id)
        runtime.active_turn_id = steer_turn_id
        state = _persist_runtime(runtime, {"last_user_text": visible_user_text, "last_error": ""})
        return {
            "ok": True,
            "data": {
                "thread_id": thread_id,
                "turn_id": steer_turn_id,
                "state": state,
            },
        }


@ROUTER.post("/chat/{chat_id}/interrupt", dependencies=[Depends(require_api_token)])
def chat_interrupt(chat_id: str, body: InterruptTurnRequest) -> Dict[str, Any]:
    runtime = RUNTIMES.get(chat_id)
    with runtime.lock:
        runtime.last_input_at = int(time.time())
        thread_id = str(runtime.thread_id or "")
        if not thread_id:
            return {"ok": True, "message": "no active thread"}
        turn_id = str(body.turn_id or runtime.active_turn_id or runtime.client.get_active_turn_id(thread_id))
        if not turn_id:
            return {"ok": True, "message": "no running turn"}
        try:
            result = runtime.client.turn_interrupt(thread_id=thread_id, turn_id=turn_id)
            runtime.active_turn_id = ""
            state = _persist_runtime(runtime, {"last_error": "", "last_interrupt_turn_id": turn_id})
            return {"ok": True, "data": {"thread_id": thread_id, "turn_id": turn_id, "result": result, "state": state}}
        except AppServerError as exc:
            runtime.active_turn_id = ""
            state = _persist_runtime(runtime, {"last_error": str(exc)})
            raise HTTPException(
                status_code=502,
                detail={
                    "ok": False,
                    "error": str(exc),
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "state": state,
                },
            ) from exc


@ROUTER.post("/chat/{chat_id}/memory/search", dependencies=[Depends(require_api_token)])
def chat_memory_search(chat_id: str, body: MemorySearchRequest) -> Dict[str, Any]:
    requested_project = str(body.project or "").strip()
    runtime_id = _runtime_id_from_chat_project(chat_id=chat_id, project=requested_project)
    runtime = RUNTIMES.get(runtime_id)
    with runtime.lock:
        project_name = (
            requested_project
            or str(_runtime_project_name(runtime.chat_id) or "").strip()
            or _project_label_for_cwd(runtime.cwd)
        )
        current_chat_id = _runtime_actual_chat_id(runtime.chat_id)
    exclude_chat = "" if bool(body.include_same_chat) else current_chat_id
    items = HISTORY_STORE.search_project_memories(
        project=project_name,
        query=str(body.query or "").strip(),
        limit=int(body.limit or 8),
        include_turn_text=bool(body.include_turn_text),
        exclude_chat_id=exclude_chat,
    )
    return {
        "ok": True,
        "data": {
            "runtime_id": runtime_id,
            "chat_id": current_chat_id,
            "project": project_name,
            "query": str(body.query or "").strip(),
            "items": items,
            "count": len(items),
        },
    }


@ROUTER.get("/history", dependencies=[Depends(require_api_token)])
def history_json(offset: int = 0, limit: int = 50) -> Dict[str, Any]:
    page = HISTORY_STORE.project_summaries(offset=offset, limit=limit)
    return {"ok": True, "data": {"projects": page["items"], "pagination": page["pagination"]}}


@APP.get("/history/entry")
def history_entry(request: Request, next: str = Query(default="/history")) -> RedirectResponse:
    payload = _history_cookie_payload(request)
    safe_next = next if str(next or "").startswith("/") else "/history"
    if payload:
        return RedirectResponse(url=safe_next, status_code=302)
    state = _sign_history_payload(
        {
            "exp": int(time.time()) + 600,
            "next": safe_next,
        }
    )
    query = urllib.parse.urlencode(
        {
            "app_id": FEISHU_APP_ID,
            "redirect_uri": _history_redirect_uri(request),
            "response_type": "code",
            "state": state,
        }
    )
    return RedirectResponse(url=f"{FEISHU_OAUTH_AUTHORIZE_URL}?{query}", status_code=302)


@APP.get("/history/auth/callback")
def history_auth_callback(request: Request, code: str = Query(default=""), state: str = Query(default="")) -> RedirectResponse:
    try:
        state_payload = _decode_history_payload(state)
    except Exception as exc:
        return RedirectResponse(url=f"/history/auth/failed?reason={urllib.parse.quote(str(exc))}", status_code=302)
    try:
        user = _history_feishu_user_info(code=code, redirect_uri=_history_redirect_uri(request))
    except Exception as exc:
        return RedirectResponse(url=f"/history/auth/failed?reason={urllib.parse.quote(str(exc))}", status_code=302)
    open_id = str(user.get("open_id") or "").strip()
    allowed = _history_allowed_open_ids()
    if not open_id or (allowed and open_id not in allowed):
        return RedirectResponse(url="/history/auth/failed?reason=forbidden", status_code=302)
    resp = RedirectResponse(
        url=str(state_payload.get("next") or "/history"),
        status_code=302,
    )
    resp.set_cookie(
        key=HISTORY_COOKIE_NAME,
        value=_sign_history_payload({"open_id": open_id, "exp": int(time.time()) + HISTORY_SESSION_TTL_SEC}),
        max_age=HISTORY_SESSION_TTL_SEC,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp


@APP.get("/history/auth/failed", response_class=HTMLResponse)
def history_auth_failed(reason: str = Query(default="")) -> HTMLResponse:
    message = html.escape(str(reason or "登录失败"))
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>访问失败</title>
<style>body{{font-family:"Noto Serif SC","Source Han Serif SC","Songti SC",serif;background:#f7f2e8;color:#1d1b18;padding:32px;}}main{{max-width:720px;margin:0 auto;background:#fffdf8;border:1px solid #d9d0c2;border-radius:18px;padding:24px;}}a{{color:#146356;}}</style>
</head><body><main><h1>无法访问历史页</h1><p>{message}</p><p>如果你是应用拥有者，请检查网页应用授权、回调地址和允许访问的 open_id 配置。</p><p><a href="/history/entry">重新尝试登录</a></p></main></body></html>"""
    )


@APP.get("/history/logout")
def history_logout() -> RedirectResponse:
    resp = RedirectResponse(url="/history/entry", status_code=302)
    resp.delete_cookie(HISTORY_COOKIE_NAME, path="/")
    return resp


@APP.get("/history/api/projects")
def history_projects_api(
    request: Request,
    offset: int = 0,
    limit: int = 50,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    page = HISTORY_STORE.project_summaries(offset=offset, limit=limit)
    return JSONResponse({"ok": True, "data": {"projects": page["items"], "pagination": page["pagination"]}})


@APP.get("/history/api/sessions")
def history_sessions_api(
    request: Request,
    project: str = Query(default=""),
    offset: int = 0,
    limit: int = 50,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    page = HISTORY_STORE.session_summaries(project=project, offset=offset, limit=limit)
    return JSONResponse({"ok": True, "data": {"project": project, "sessions": page["items"], "pagination": page["pagination"]}})


@APP.get("/history/api/turns")
def history_turns_api(
    request: Request,
    project: str = Query(default=""),
    chat_id: str = Query(default=""),
    offset: int = 0,
    limit: int = 50,
    include_events: bool = Query(default=False),
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    page = HISTORY_STORE.turn_items(
        project=project,
        chat_id=chat_id,
        offset=offset,
        limit=limit,
        include_events=include_events,
    )
    return JSONResponse(
        {
            "ok": True,
            "data": {
                "project": project,
                "chat_id": chat_id,
                "turns": page["items"],
                "pagination": page["pagination"],
            },
        }
    )


@APP.get("/history/api/turn")
def history_turn_api(
    request: Request,
    turn_id: str = Query(default=""),
    include_events: bool = Query(default=True),
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    item = HISTORY_STORE.turn_detail(turn_id=turn_id, include_events=include_events)
    if not item:
        return JSONResponse(status_code=404, content={"ok": False, "error": "turn not found"})
    return JSONResponse({"ok": True, "data": {"turn": item}})


@APP.get("/history/api/memory/search")
def history_memory_search_api(
    request: Request,
    project: str = Query(default=""),
    query: str = Query(default=""),
    limit: int = Query(default=8, ge=1, le=20),
    include_turn_text: bool = Query(default=False),
    exclude_chat_id: str = Query(default=""),
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    items = HISTORY_STORE.search_project_memories(
        project=str(project or "").strip(),
        query=str(query or "").strip(),
        limit=int(limit or 8),
        include_turn_text=bool(include_turn_text),
        exclude_chat_id=str(exclude_chat_id or "").strip(),
    )
    return JSONResponse(
        {
            "ok": True,
            "data": {
                "project": str(project or "").strip(),
                "query": str(query or "").strip(),
                "items": items,
                "count": len(items),
            },
        }
    )


@APP.get("/history/api/auth/profiles")
def history_auth_profiles_api(
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    now_ts = int(time.time())
    profiles = _refresh_auth_profiles()
    data_items: List[Dict[str, Any]] = [
        {
            "profile": "",
            "label": "default",
            "email": "",
            "valid": True,
            "reason": "",
            "home_dir": str(DEFAULT_CODEX_HOME),
            "source_auth_json": "",
            "status": "active",
            "disabled_until": 0,
            "disabled_reason": "",
            "needs_reauth": False,
            "risk_deactivated": False,
            "last_health_check_at": 0,
            "last_health_error": "",
            "available": True,
            "disabled_remaining_sec": 0,
        }
    ]
    for item in profiles:
        profile_item = dict(item)
        disabled_until = int(profile_item.get("disabled_until") or 0)
        profile_item["label"] = str(profile_item.get("profile") or "").strip() or "default"
        profile_item["available"] = _auth_profile_available(profile_item, now_ts=now_ts)
        profile_item["disabled_remaining_sec"] = max(0, disabled_until - now_ts) if disabled_until > 0 else 0
        data_items.append(profile_item)
    return JSONResponse({"ok": True, "data": {"profiles": data_items, "timestamp": now_ts}})


@APP.post("/history/api/auth/health-check")
def history_auth_health_check_api(
    body: HistoryAuthHealthCheckRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    target = str(body.profile or "").strip()
    mode = str(body.mode or AUTH_HEALTH_CHECK_DEFAULT_MODE).strip().lower() or AUTH_HEALTH_CHECK_DEFAULT_MODE
    probe_prompt = str(body.prompt or "").strip()
    probe_timeout = int(body.timeout_sec or 0)
    if target:
        item = _get_auth_profile(target)
        if not item:
            return JSONResponse(status_code=404, content={"ok": False, "error": f"profile not found: {target}"})
        profiles = [_health_check_auth_profile_item(item=item, mode=mode, prompt=probe_prompt, timeout_sec=probe_timeout)]
    else:
        seed = _refresh_auth_profiles()
        profiles = [
            _health_check_auth_profile_item(item=item, mode=mode, prompt=probe_prompt, timeout_sec=probe_timeout)
            for item in seed
        ]
    now_ts = int(time.time())
    normalized: List[Dict[str, Any]] = []
    for item in profiles:
        payload = dict(item)
        disabled_until = int(payload.get("disabled_until") or 0)
        payload["label"] = str(payload.get("profile") or "").strip() or "default"
        payload["available"] = _auth_profile_available(payload, now_ts=now_ts)
        payload["disabled_remaining_sec"] = max(0, disabled_until - now_ts) if disabled_until > 0 else 0
        normalized.append(payload)
    return JSONResponse({"ok": True, "data": {"profiles": normalized, "timestamp": now_ts, "mode": mode}})


@APP.post("/history/api/auth/switch")
def history_auth_switch_api(
    body: HistoryAuthSwitchRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    chat_id = str(body.chat_id or "").strip()
    profile = str(body.profile or "").strip()
    project = str(body.project or "").strip()
    if not chat_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "chat_id is required"})
    runtime_id = _runtime_id_from_chat_project(chat_id=chat_id, project=project)
    runtime = RUNTIMES.get(runtime_id)
    with runtime.lock:
        info = _switch_runtime_auth_profile(runtime, profile=profile, reason="history dashboard manual switch")
        state = STORE.get_chat(runtime_id)
    return JSONResponse(
        {
            "ok": True,
            "data": {
                "chat_id": chat_id,
                "runtime_id": runtime_id,
                "project": project,
                "auth_profile": str(info.get("to") or "").strip(),
                "auth_identity": str(info.get("identity") or "").strip(),
                "state": state,
            },
        }
    )


@APP.get("/history/api/auth/control/state")
def history_auth_control_state_api(
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    return JSONResponse({"ok": True, "data": _auth_control_registry_state()})


@APP.post("/history/api/auth/control/upload")
def history_auth_control_upload_api(
    body: AuthControlUploadRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_upload(
        profile=body.profile,
        provider=body.provider,
        auth_json=body.auth_json,
        config_toml=body.config_toml,
        label=body.label,
        notes=body.notes,
    )
    return JSONResponse({"ok": True, "data": data})


@APP.post("/history/api/auth/control/assign")
def history_auth_control_assign_api(
    body: AuthControlAssignRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_assign(
        profile=body.profile,
        node_id=body.node_id,
        lease_sec=int(body.lease_sec or AUTH_CONTROL_DEFAULT_LEASE_SEC),
        force=bool(body.force),
        notes=body.notes,
    )
    return JSONResponse({"ok": True, "data": data})


@APP.post("/history/api/auth/control/revoke")
def history_auth_control_revoke_api(
    body: AuthControlRevokeRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_revoke(profile=body.profile, reason=body.reason)
    return JSONResponse({"ok": True, "data": data})


@APP.post("/history/api/auth/control/health-check")
def history_auth_control_health_check_api(
    body: AuthControlHealthCheckRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_health_check(
        profile=body.profile,
        node_id=body.node_id,
        mode=body.mode,
        prompt=body.prompt,
        timeout_sec=int(body.timeout_sec or 0),
    )
    return JSONResponse({"ok": True, "data": data})


@APP.get("/history/auth-control", response_class=HTMLResponse)
def history_auth_control_page(
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> HTMLResponse:
    session_payload = _history_cookie_payload(request)
    if not session_payload:
        has_api_token = bool(str(token or "").strip() or str(authorization or "").strip())
        if has_api_token:
            _check_api_token(token=token, authorization=authorization)
        else:
            return RedirectResponse(url="/history/entry?next=/history/auth-control", status_code=302)
    page_config = json.dumps({"authToken": str(token or "").strip()}, ensure_ascii=False)
    html_page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Auth 集中管理</title>
  <style>
    body {{ margin: 0; font-family: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif; background: #f4f7fb; color: #1b2430; }}
    header {{ padding: 16px 20px; background: #0f1b2a; color: #fff; font-weight: 600; }}
    main {{ padding: 16px; display: grid; gap: 16px; }}
    .card {{ background: #fff; border: 1px solid #d9e2ec; border-radius: 10px; padding: 12px; }}
    .row {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    input, select, textarea, button {{ font: inherit; padding: 8px; border-radius: 8px; border: 1px solid #c5d2e0; }}
    textarea {{ width: 100%; min-height: 120px; }}
    button {{ background: #1d4ed8; color: #fff; border: 0; cursor: pointer; }}
    button.alt {{ background: #64748b; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e5edf5; text-align: left; padding: 8px; vertical-align: top; }}
    code {{ background: #f2f7ff; padding: 2px 6px; border-radius: 6px; }}
    #log {{ white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; background: #09111f; color: #d6e2ff; padding: 10px; border-radius: 8px; max-height: 240px; overflow: auto; }}
  </style>
</head>
<body>
  <header>Auth 集中管理（上传 / 分配 / 巡检）</header>
  <main>
    <section class="card">
      <div class="row">
        <button onclick="refreshState()">刷新状态</button>
        <button class="alt" onclick="healthCheck()">一键巡检</button>
      </div>
      <div id="summary" style="margin-top:8px;color:#4b5563;"></div>
    </section>
    <section class="card">
      <h3>上传 Auth</h3>
      <div class="row">
        <input id="up_profile" placeholder="profile 名称，例如 work_01" style="min-width: 220px;" />
        <select id="up_provider"><option value="codex">codex</option><option value="openclaw">openclaw</option></select>
        <input id="up_label" placeholder="label（可选）" style="min-width: 180px;" />
      </div>
      <div style="margin-top:8px;"><textarea id="up_auth_json" placeholder='粘贴 auth.json 内容（JSON）'></textarea></div>
      <div style="margin-top:8px;"><textarea id="up_config_toml" placeholder='config.toml（可选）'></textarea></div>
      <div class="row" style="margin-top:8px;"><input id="up_notes" placeholder="备注（可选）" style="min-width: 320px;" /><button onclick="uploadAuth()">上传</button></div>
    </section>
    <section class="card">
      <h3>分配 / 回收</h3>
      <div class="row">
        <select id="assign_profile"></select>
        <select id="assign_node"></select>
        <input id="assign_lease" type="number" min="60" step="60" value="{AUTH_CONTROL_DEFAULT_LEASE_SEC}" />
        <label><input id="assign_force" type="checkbox" />force 迁移</label>
        <button onclick="assignAuth()">分配</button>
        <button class="alt" onclick="revokeAuth()">回收</button>
      </div>
    </section>
    <section class="card">
      <h3>Auth 列表</h3>
      <table>
        <thead><tr><th>Profile</th><th>Provider</th><th>本地健康</th><th>分配状态</th><th>租约</th></tr></thead>
        <tbody id="auth_rows"></tbody>
      </table>
    </section>
    <section class="card">
      <h3>节点巡检结果</h3>
      <div id="node_health"></div>
    </section>
    <section class="card"><h3>操作日志</h3><div id="log"></div></section>
  </main>
  <script>
    window.__AUTH_CONTROL_CONFIG__ = {page_config};
    const cfg = window.__AUTH_CONTROL_CONFIG__ || {{}};
    const q = cfg.authToken ? `?token=${{encodeURIComponent(cfg.authToken)}}` : '';
    let latestState = null;
    function appendLog(msg) {{
      const el = document.getElementById('log');
      const ts = new Date().toLocaleString();
      el.textContent = `[${{ts}}] ${{msg}}\\n` + el.textContent;
    }}
    async function api(path, method='GET', body=null) {{
      const resp = await fetch(path + q, {{
        method,
        headers: {{ 'Content-Type': 'application/json' }},
        body: body ? JSON.stringify(body) : null,
      }});
      const data = await resp.json();
      if (!resp.ok || data.ok === false) throw new Error(data.error || data.detail || resp.statusText);
      return data.data;
    }}
    function renderState(data) {{
      latestState = data;
      const auths = Array.isArray(data.auths) ? data.auths : [];
      const nodes = Array.isArray(data.nodes) ? data.nodes : [];
      document.getElementById('summary').textContent = `Auth: ${{auths.length}} | Nodes: ${{nodes.length}} | 更新时间: ${{new Date((data.timestamp||0)*1000).toLocaleString()}}`;
      const tbody = document.getElementById('auth_rows');
      tbody.innerHTML = '';
      const pSel = document.getElementById('assign_profile');
      pSel.innerHTML = '';
      for (const a of auths) {{
        const tr = document.createElement('tr');
        const h = a.health || {{}};
        const asg = a.assignment || {{}};
        tr.innerHTML = `<td><code>${{a.profile||''}}</code></td><td>${{a.provider||''}}</td><td>${{h.status||''}} ${{h.valid?'(ok)':'(bad)'}}<br/>${{h.reason||''}}</td><td>${{asg.node_id||'未分配'}}<br/>v${{asg.version||0}}</td><td>${{a.lease_remaining_sec||0}}s</td>`;
        tbody.appendChild(tr);
        const op = document.createElement('option');
        op.value = a.profile || '';
        op.textContent = a.profile || '';
        pSel.appendChild(op);
      }}
      const nSel = document.getElementById('assign_node');
      nSel.innerHTML = '';
      for (const n of nodes) {{
        const op = document.createElement('option');
        op.value = n.id || '';
        op.textContent = `${{n.id||''}} - ${{n.label||''}}`;
        nSel.appendChild(op);
      }}
    }}
    async function refreshState() {{
      const data = await api('/history/api/auth/control/state');
      renderState(data);
      appendLog('state refreshed');
    }}
    async function uploadAuth() {{
      const profile = document.getElementById('up_profile').value.trim();
      const provider = document.getElementById('up_provider').value.trim();
      const label = document.getElementById('up_label').value.trim();
      const notes = document.getElementById('up_notes').value.trim();
      const authRaw = document.getElementById('up_auth_json').value.trim();
      const cfgToml = document.getElementById('up_config_toml').value;
      if (!profile || !authRaw) throw new Error('profile 和 auth_json 不能为空');
      let authJson;
      try {{ authJson = JSON.parse(authRaw); }} catch (e) {{ throw new Error('auth_json 不是合法 JSON'); }}
      await api('/history/api/auth/control/upload', 'POST', {{ profile, provider, label, notes, auth_json: authJson, config_toml: cfgToml }});
      appendLog(`upload ok: ${{profile}}`);
      await refreshState();
    }}
    async function assignAuth() {{
      const profile = document.getElementById('assign_profile').value.trim();
      const node_id = document.getElementById('assign_node').value.trim();
      const lease_sec = Number(document.getElementById('assign_lease').value || 0);
      const force = document.getElementById('assign_force').checked;
      if (!profile || !node_id) throw new Error('请选择 profile 和 node');
      await api('/history/api/auth/control/assign', 'POST', {{ profile, node_id, lease_sec, force }});
      appendLog(`assign ok: ${{profile}} -> ${{node_id}}`);
      await refreshState();
    }}
    async function revokeAuth() {{
      const profile = document.getElementById('assign_profile').value.trim();
      if (!profile) throw new Error('请选择 profile');
      await api('/history/api/auth/control/revoke', 'POST', {{ profile, reason: 'manual' }});
      appendLog(`revoke ok: ${{profile}}`);
      await refreshState();
    }}
    async function healthCheck() {{
      const data = await api('/history/api/auth/control/health-check', 'POST', {{ mode: 'status' }});
      const el = document.getElementById('node_health');
      el.innerHTML = '';
      for (const n of (data.nodes||[])) {{
        const d = document.createElement('div');
        d.style.padding = '6px 0';
        d.textContent = `node=${{n.node_id}} ok=${{n.ok}} profiles=${{(n.profiles||[]).length}} error=${{n.error||''}}`;
        el.appendChild(d);
      }}
      appendLog('health check finished');
    }}
    (async () => {{
      try {{ await refreshState(); }} catch (e) {{ appendLog(String(e)); }}
    }})();
  </script>
</body>
</html>"""
    return HTMLResponse(html_page)


@APP.get("/history", response_class=HTMLResponse)
def history_page(
    request: Request,
    token: str = Query(default=""),
    limit: int = 300,
    project: str = Query(default=""),
    chat_id: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> HTMLResponse:
    session_payload = _history_cookie_payload(request)
    if not session_payload:
        has_api_token = bool(str(token or "").strip() or str(authorization or "").strip())
        if has_api_token:
            _check_api_token(token=token, authorization=authorization)
        else:
            return RedirectResponse(url="/history/entry?next=/history", status_code=302)
    page_config = json.dumps(
        {
            "authToken": str(token or "").strip(),
            "initialTurnLimit": max(20, min(100, int(limit or 50))),
            "initialProject": str(project or "").strip(),
            "initialChatId": str(chat_id or "").strip(),
        },
        ensure_ascii=False,
    )
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>FeiCodex 项目看板</title>
  <link rel="stylesheet" href="/history-static/assets/history-dashboard.css" />
</head>
<body>
  <div id="root"></div>
  <script>window.__HISTORY_PAGE_CONFIG__ = {page_config};</script>
  <script type="module" src="/history-static/assets/history-dashboard.js"></script>
</body>
</html>"""
    return HTMLResponse(page)


APP.include_router(ROUTER)


@APP.on_event("startup")
def _on_startup() -> None:
    global _IDLE_SWEEPER_THREAD
    try:
        _ensure_bridge_mcp_server_for_known_homes()
    except Exception as exc:
        LOG.warning("ensure bridge MCP server on startup failed err=%s", exc)
    if IDLE_EVICT_SEC <= 0:
        LOG.info("idle sweeper disabled idle_evict_sec=%s", IDLE_EVICT_SEC)
        return
    _IDLE_SWEEPER_STOP.clear()
    t = _IDLE_SWEEPER_THREAD
    if t and t.is_alive():
        return
    _IDLE_SWEEPER_THREAD = threading.Thread(target=_idle_sweeper_loop, name="idle-sweeper", daemon=True)
    _IDLE_SWEEPER_THREAD.start()


@APP.on_event("shutdown")
def _on_shutdown() -> None:
    _IDLE_SWEEPER_STOP.set()
    t = _IDLE_SWEEPER_THREAD
    if t and t.is_alive():
        t.join(timeout=2.0)
    RUNTIMES.stop_all()
