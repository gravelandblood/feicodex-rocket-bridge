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
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from appserver_client import AppServerError, AppServerTimeout, CodexAppServerClient
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


def _validate_auth_profile_file(source: Path) -> Dict[str, Any]:
    profile = str(source.name[: -len(".auth.json")] if source.name.endswith(".auth.json") else source.stem).strip()
    meta: Dict[str, Any] = {
        "profile": profile,
        "source_auth_json": str(source),
        "source_config_toml": "",
        "valid": False,
        "reason": "",
        "auth_mode": "",
        "email": "",
        "sub": "",
        "home_dir": str(_profile_home_dir(profile)),
    }
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        meta["reason"] = f"invalid json: {exc}"
        return meta

    auth_mode = str(data.get("auth_mode") or "").strip()
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    payload = _decode_jwt_payload(str(tokens.get("id_token") or ""))
    meta["auth_mode"] = auth_mode
    meta["email"] = str(payload.get("email") or "").strip()
    meta["sub"] = str(payload.get("sub") or "").strip()

    if not auth_mode or not isinstance(tokens, dict):
        meta["reason"] = "missing auth_mode/tokens"
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
        meta["reason"] = f"status check failed: {exc}"
        return meta

    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode == 0 and "Logged in" in output:
        meta["valid"] = True
        return meta
    meta["reason"] = output.strip() or f"status code {proc.returncode}"
    return meta


def _refresh_auth_profiles() -> List[Dict[str, Any]]:
    AUTH_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profiles: List[Dict[str, Any]] = []
    for path in sorted(AUTH_PROFILES_DIR.glob("*.auth.json")):
        if not path.is_file():
            continue
        profile = str(path.name[: -len(".auth.json")] if path.name.endswith(".auth.json") else path.stem).strip()
        if not profile:
            continue
        profiles.append(_validate_auth_profile_file(path))
    _save_auth_registry(profiles)
    return profiles


def _get_auth_profile(profile: str) -> Optional[Dict[str, Any]]:
    target = str(profile or "").strip()
    for item in _refresh_auth_profiles():
        if str(item.get("profile") or "").strip() == target:
            return item
    return None


def _list_switchable_auth_profiles() -> List[Dict[str, Any]]:
    return [{"profile": "", "label": "default", "valid": True, "email": ""}] + [
        item for item in _refresh_auth_profiles() if bool(item.get("valid"))
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
    active_turn_id = str(runtime.active_turn_id or persisted.get("active_turn_id") or "")
    auth_profile = str(runtime.auth_profile or persisted.get("auth_profile") or "")
    auth_meta = _get_auth_profile(auth_profile) if auth_profile else None

    if thread_id and runtime.is_client_running():
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
    if not token_usage and isinstance(persisted.get("last_token_usage"), dict):
        token_usage = dict(persisted.get("last_token_usage") or {})
    if not rate_limits and isinstance(persisted.get("last_rate_limits"), dict):
        rate_limits = dict(persisted.get("last_rate_limits") or {})
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
                }
            ]
            + profiles
        },
    }


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
        turn_cwd = str(runtime.cwd or DEFAULT_CWD)
        turn_model = str(runtime.model or DEFAULT_MODEL)
        turn_auth_profile = str(runtime.auth_profile or "")
        preflight_limits = _read_rate_limits(runtime, allow_request=runtime.is_client_running())
        if not preflight_limits:
            persisted = STORE.get_chat(chat_id)
            preflight_limits = dict(persisted.get("last_rate_limits") or {}) if isinstance(persisted.get("last_rate_limits"), dict) else {}
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
            if _is_auth_limit_error(str(exc)) and not auto_auth_switch:
                auto_auth_switch = _maybe_auto_switch_auth_profile(runtime, reason=str(exc))
                if auto_auth_switch:
                    thread_id = _ensure_thread(runtime, reset_thread=True)
                    turn_start = runtime.client.turn_start(
                        thread_id=thread_id,
                        text=turn_input_text,
                        image_paths=[str(p) for p in list(body.image_paths or []) if str(p).strip()],
                    )
                else:
                    state = _persist_runtime(runtime, {"last_error": str(exc), "last_turn_status": "failed"})
                    raise HTTPException(
                        status_code=502,
                        detail={"ok": False, "error": str(exc), "thread_id": runtime.thread_id, "state": state},
                    ) from exc
            else:
                state = _persist_runtime(runtime, {"last_error": str(exc), "last_turn_status": "failed"})
                raise HTTPException(
                    status_code=502,
                    detail={"ok": False, "error": str(exc), "thread_id": runtime.thread_id, "state": state},
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
                },
            )
            if _rate_limit_exhausted(rate_limits) and not auto_auth_switch:
                auto_auth_switch = _maybe_auto_switch_auth_profile(runtime, reason="post-turn rate limit exhausted")
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
        with runtime.lock:
            runtime.active_turn_id = str(runtime.client.get_active_turn_id(thread_id) or runtime.active_turn_id or "")
            state = _persist_runtime(runtime, {"last_error": str(exc), "last_turn_status": "failed"})
        HISTORY_STORE.append_turn(
            _build_turn_record(
                runtime=runtime,
                turn_id=turn_id or str(runtime.active_turn_id or ""),
                status="failed",
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
            status_code=502,
            detail={"ok": False, "error": str(exc), "thread_id": runtime.thread_id, "state": state},
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
