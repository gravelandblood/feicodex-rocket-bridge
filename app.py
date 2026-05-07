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
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from appserver_client import AppServerDisconnected, AppServerError, AppServerTimeout, CodexAppServerClient, TurnRunResult
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
TURN_CHECKPOINT_INTERVAL_SEC = max(1, int(os.environ.get("BRIDGE_TURN_CHECKPOINT_INTERVAL_SEC", "3")))
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
AUTO_MEMORY_INJECT_ENABLED = str(os.environ.get("BRIDGE_AUTO_MEMORY_INJECT_ENABLED", "true")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTO_MEMORY_INJECT_LIMIT = max(1, min(100, int(os.environ.get("BRIDGE_AUTO_MEMORY_INJECT_LIMIT", "12"))))
AUTO_MEMORY_INJECT_MAX_CHARS = max(500, int(os.environ.get("BRIDGE_AUTO_MEMORY_INJECT_MAX_CHARS", "5000")))

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
AUTH_PENDING_PROFILES_DIR = _resolve_env_path(
    os.environ.get("BRIDGE_AUTH_PENDING_PROFILES_DIR", str(DATA_DIR / "auth_pending_profiles"))
)
AUTH_BACKUP_PROFILES_DIR = _resolve_env_path(
    os.environ.get("BRIDGE_AUTH_BACKUP_PROFILES_DIR", str(DATA_DIR / "auth_backup_profiles"))
)
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
_AUTH_REAUTH_LOCK = threading.Lock()
_AUTH_REAUTH_REQUESTS: Dict[str, Dict[str, Any]] = {}
AUTH_REAUTH_START_WAIT_SEC = max(5, min(30, int(os.environ.get("BRIDGE_AUTH_REAUTH_START_WAIT_SEC", "15"))))
AUTH_REAUTH_REQUEST_TTL_SEC = max(300, min(86400, int(os.environ.get("BRIDGE_AUTH_REAUTH_REQUEST_TTL_SEC", "3600"))))


class TurnRequest(BaseModel):
    text: str = Field(min_length=1, description="User input text")
    image_paths: list[str] = Field(default_factory=list, description="Optional local image paths")
    cwd: str = Field(default="")
    model: str = Field(default="")
    agent_provider: str = Field(default="")
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
    agent_provider: str = Field(default="")
    sandbox: str = Field(default="")
    approval_policy: str = Field(default="")
    personality: str = Field(default="")


class InterruptTurnRequest(BaseModel):
    turn_id: str = Field(default="")


class UpdateChatConfigRequest(BaseModel):
    cwd: str = Field(default="")
    model: str = Field(default="")
    agent_provider: str = Field(default="")
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
    profile: str = Field(default="")
    provider: str = Field(default="codex")
    auth_json: Any = Field(default_factory=dict)
    config_toml: str = Field(default="")
    label: str = Field(default="")
    notes: str = Field(default="")


class AuthControlBatchUploadItem(BaseModel):
    filename: str = Field(default="")
    auth_json: Any = Field(default_factory=dict)
    config_toml: str = Field(default="")


class AuthControlBatchUploadRequest(BaseModel):
    provider: str = Field(default="codex")
    notes: str = Field(default="")
    items: List[AuthControlBatchUploadItem] = Field(default_factory=list)


class AuthControlAssignRequest(BaseModel):
    profile: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    lease_sec: int = Field(default=AUTH_CONTROL_DEFAULT_LEASE_SEC, ge=60, le=604800)
    force: bool = Field(default=False)
    notes: str = Field(default="")


class AuthControlRevokeRequest(BaseModel):
    profile: str = Field(min_length=1)
    reason: str = Field(default="")


class AuthControlRemoveRemoteRequest(BaseModel):
    profile: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    reason: str = Field(default="")


class AuthControlRemoveLocalRequest(BaseModel):
    profile: str = Field(min_length=1)
    reason: str = Field(default="")


class AuthControlRemovePoolRequest(BaseModel):
    profile: str = Field(min_length=1)
    reason: str = Field(default="")


class AuthControlHealthCheckRequest(BaseModel):
    profile: str = Field(default="")
    node_id: str = Field(default="")
    mode: str = Field(default="status")
    prompt: str = Field(default="")
    timeout_sec: int = Field(default=0, ge=0, le=1800)


class AuthControlCheckOneRequest(BaseModel):
    profile: str = Field(min_length=1)
    mode: str = Field(default="status")
    prompt: str = Field(default="")
    timeout_sec: int = Field(default=0, ge=0, le=1800)


class AuthControlReauthStartRequest(BaseModel):
    profile: str = Field(default="")
    node_id: str = Field(default="")


class AuthControlReauthStatusRequest(BaseModel):
    request_id: str = Field(min_length=1)
    node_id: str = Field(default="")


class AuthControlReauthCancelRequest(BaseModel):
    request_id: str = Field(min_length=1)
    node_id: str = Field(default="")


class MemorySearchRequest(BaseModel):
    query: str = Field(default="")
    project: str = Field(default="")
    limit: int = Field(default=8, ge=1, le=100)
    include_turn_text: bool = Field(default=False)
    include_same_chat: bool = Field(default=False)


class AgentAdapter(Protocol):
    env: Dict[str, str]

    def start(self, experimental_api: bool = True) -> Dict[str, Any]:
        ...

    def stop(self) -> None:
        ...

    def is_running(self) -> bool:
        ...

    def thread_start(
        self,
        cwd: str = "",
        model: str = "",
        sandbox: str = "",
        approval_policy: str = "",
        personality: str = "",
    ) -> Dict[str, Any]:
        ...

    def thread_resume(
        self,
        thread_id: str,
        cwd: str = "",
        model: str = "",
        sandbox: str = "",
        approval_policy: str = "",
        personality: str = "",
    ) -> Dict[str, Any]:
        ...

    def turn_start(self, thread_id: str, text: str, image_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        ...

    def wait_for_turn_completion(self, thread_id: str, turn_id: str, timeout_sec: int = 600) -> Any:
        ...

    def get_thread_status(self, thread_id: str) -> Dict[str, Any]:
        ...

    def get_active_turn_id(self, thread_id: str) -> str:
        ...

    def get_thread_token_usage(self, thread_id: str) -> Dict[str, Any]:
        ...

    def get_turn_progress(self, thread_id: str) -> Dict[str, Any]:
        ...

    def get_account_rate_limits(self) -> Dict[str, Any]:
        ...

    def account_rate_limits_read(self) -> Dict[str, Any]:
        ...

    def get_turn_events(self, thread_id: str, turn_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        ...

    def turn_steer(self, thread_id: str, expected_turn_id: str, text: str, image_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        ...

    def turn_interrupt(self, thread_id: str, turn_id: str) -> Dict[str, Any]:
        ...


class CodexAgentAdapter:
    def __init__(self) -> None:
        self._client = CodexAppServerClient()

    @property
    def env(self) -> Dict[str, str]:
        return self._client.env

    def start(self, experimental_api: bool = True) -> Dict[str, Any]:
        return self._client.start(experimental_api=experimental_api)

    def stop(self) -> None:
        self._client.stop()

    def is_running(self) -> bool:
        return self._client.is_running()

    def thread_start(
        self,
        cwd: str = "",
        model: str = "",
        sandbox: str = "",
        approval_policy: str = "",
        personality: str = "",
    ) -> Dict[str, Any]:
        return self._client.thread_start(
            cwd=cwd,
            model=model,
            sandbox=sandbox,
            approval_policy=approval_policy,
            personality=personality,
        )

    def thread_resume(
        self,
        thread_id: str,
        cwd: str = "",
        model: str = "",
        sandbox: str = "",
        approval_policy: str = "",
        personality: str = "",
    ) -> Dict[str, Any]:
        return self._client.thread_resume(
            thread_id=thread_id,
            cwd=cwd,
            model=model,
            sandbox=sandbox,
            approval_policy=approval_policy,
        )

    def turn_start(self, thread_id: str, text: str, image_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        return self._client.turn_start(thread_id=thread_id, text=text, image_paths=image_paths)

    def wait_for_turn_completion(self, thread_id: str, turn_id: str, timeout_sec: int = 600) -> Any:
        return self._client.wait_for_turn_completion(thread_id=thread_id, turn_id=turn_id, timeout_sec=timeout_sec)

    def get_thread_status(self, thread_id: str) -> Dict[str, Any]:
        return self._client.get_thread_status(thread_id=thread_id)

    def get_active_turn_id(self, thread_id: str) -> str:
        return self._client.get_active_turn_id(thread_id=thread_id)

    def get_thread_token_usage(self, thread_id: str) -> Dict[str, Any]:
        return self._client.get_thread_token_usage(thread_id=thread_id)

    def get_turn_progress(self, thread_id: str) -> Dict[str, Any]:
        return self._client.get_turn_progress(thread_id=thread_id)

    def get_account_rate_limits(self) -> Dict[str, Any]:
        return self._client.get_account_rate_limits()

    def account_rate_limits_read(self) -> Dict[str, Any]:
        return self._client.account_rate_limits_read()

    def get_turn_events(self, thread_id: str, turn_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        return self._client.get_turn_events(thread_id=thread_id, turn_id=turn_id, limit=limit)

    def turn_steer(self, thread_id: str, expected_turn_id: str, text: str, image_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        return self._client.turn_steer(
            thread_id=thread_id,
            expected_turn_id=expected_turn_id,
            text=text,
            image_paths=image_paths,
        )

    def turn_interrupt(self, thread_id: str, turn_id: str) -> Dict[str, Any]:
        return self._client.turn_interrupt(thread_id=thread_id, turn_id=turn_id)


class ClaudeAgentAdapter:
    def __init__(self, provider: str = "claude") -> None:
        self.env: Dict[str, str] = {}
        self._running = False
        self._lock = threading.Lock()
        self._provider = _normalize_agent_provider(provider) or "claude"
        self._threads: Dict[str, Dict[str, Any]] = {}
        self._thread_status: Dict[str, Dict[str, Any]] = {}
        self._active_turn_by_thread: Dict[str, str] = {}
        self._turn_results: Dict[str, TurnRunResult] = {}
        self._token_usage_by_thread: Dict[str, Dict[str, Any]] = {}
        self._turn_events_by_thread: Dict[str, List[Dict[str, Any]]] = {}
        self._turn_preview_by_thread: Dict[str, str] = {}
        self._turn_started_at_by_thread: Dict[str, float] = {}
        self._turn_last_event_at_by_thread: Dict[str, float] = {}
        self._account_rate_limits: Dict[str, Any] = {}

    def _api_key(self) -> str:
        key = str(self.env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise AppServerError("claude adapter requires ANTHROPIC_API_KEY")
        return key

    def _base_url(self) -> str:
        return str(self.env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").strip().rstrip("/")

    def _default_model(self) -> str:
        return str(self.env.get("BRIDGE_CLAUDE_DEFAULT_MODEL") or os.environ.get("BRIDGE_CLAUDE_DEFAULT_MODEL") or "claude-sonnet-4-20250514").strip()

    def _thread(self, thread_id: str) -> Dict[str, Any]:
        with self._lock:
            data = self._threads.get(thread_id)
            if data is None:
                data = {"messages": [], "model": self._default_model(), "cwd": DEFAULT_CWD, "session_id": ""}
                self._threads[thread_id] = data
            return data

    def start(self, experimental_api: bool = True) -> Dict[str, Any]:
        if self._provider == "claude_code":
            self._claude_cli_bin()
        else:
            self._api_key()
        self._running = True
        if self._provider == "claude_code":
            return {"userAgent": "claude-code-adapter"}
        return {"userAgent": "claude-adapter"}

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return bool(self._running)

    def thread_start(
        self,
        cwd: str = "",
        model: str = "",
        sandbox: str = "",
        approval_policy: str = "",
        personality: str = "",
    ) -> Dict[str, Any]:
        if not self.is_running():
            self.start()
        thread_id = str(uuid.uuid4())
        with self._lock:
            self._threads[thread_id] = {
                "messages": [],
                "model": str(model or self._default_model()).strip() or self._default_model(),
                "cwd": str(cwd or DEFAULT_CWD),
                "session_id": "",
            }
            self._thread_status[thread_id] = {"type": "idle"}
            self._turn_events_by_thread[thread_id] = []
            self._turn_preview_by_thread[thread_id] = ""
            self._token_usage_by_thread[thread_id] = {}
        return {"thread": {"id": thread_id}}

    def thread_resume(
        self,
        thread_id: str,
        cwd: str = "",
        model: str = "",
        sandbox: str = "",
        approval_policy: str = "",
        personality: str = "",
    ) -> Dict[str, Any]:
        if not self.is_running():
            self.start()
        clean = str(thread_id or "").strip()
        if not clean:
            return self.thread_start(
                cwd=cwd,
                model=model,
                sandbox=sandbox,
                approval_policy=approval_policy,
                personality=personality,
            )
        with self._lock:
            if clean not in self._threads:
                self._threads[clean] = {
                    "messages": [],
                    "model": str(model or self._default_model()).strip() or self._default_model(),
                    "cwd": str(cwd or DEFAULT_CWD),
                    "session_id": "",
                }
            else:
                if model:
                    self._threads[clean]["model"] = str(model).strip()
                if cwd:
                    self._threads[clean]["cwd"] = str(cwd).strip()
                self._threads[clean]["session_id"] = str(self._threads[clean].get("session_id") or "").strip()
            self._thread_status[clean] = {"type": "idle"}
            self._turn_events_by_thread.setdefault(clean, [])
            self._turn_preview_by_thread.setdefault(clean, "")
            self._token_usage_by_thread.setdefault(clean, {})
        return {"thread": {"id": clean}}

    def _append_event(self, thread_id: str, text: str) -> None:
        now_ts = time.time()
        with self._lock:
            events = self._turn_events_by_thread.setdefault(thread_id, [])
            events.append({"ts": now_ts, "text": str(text or "")[:500]})
            if len(events) > 200:
                del events[:-200]
            self._turn_last_event_at_by_thread[thread_id] = now_ts

    def _claude_cli_bin(self) -> str:
        configured = str(self.env.get("CLAUDE_CLI_PATH") or os.environ.get("CLAUDE_CLI_PATH") or "claude").strip() or "claude"
        candidates: List[str] = [configured]
        if configured == "claude":
            candidates.extend(
                [
                    str(Path.home() / ".local" / "bin" / "claude"),
                    "/usr/local/bin/claude",
                    "/usr/bin/claude",
                ]
            )
        for raw in candidates:
            clean = str(raw or "").strip()
            if not clean:
                continue
            if "/" in clean:
                candidate = Path(clean).expanduser()
                if candidate.exists() and candidate.is_file():
                    return str(candidate)
            found = shutil.which(clean)
            if found:
                return found
        raise AppServerError("claude_code adapter requires `claude` CLI in PATH")

    def _parse_claude_cli_result(self, stdout_text: str) -> Dict[str, Any]:
        text = str(stdout_text or "").strip()
        if not text:
            raise AppServerError("claude code returned empty output")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict) and str(payload.get("type") or "") == "result":
                return payload
        try:
            payload = json.loads(text)
        except Exception as exc:
            raise AppServerError(f"claude code output is not valid json: {exc}") from exc
        if not isinstance(payload, dict):
            raise AppServerError("claude code output payload is invalid")
        return payload

    def _run_claude_code(self, cwd: str, model: str, prompt: str, resume_session_id: str = "") -> Dict[str, Any]:
        cli = self._claude_cli_bin()
        clean_cwd = str(cwd or DEFAULT_CWD).strip() or DEFAULT_CWD
        clean_model = str(model or self._default_model()).strip() or self._default_model()
        clean_prompt = str(prompt or "").strip()
        cmd: List[str] = [
            cli,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--add-dir",
            clean_cwd,
        ]
        env = os.environ.copy()
        for key, value in dict(self.env or {}).items():
            k = str(key or "").strip()
            v = str(value or "").strip()
            if k and v:
                env[k] = v
        if str(env.get("ANTHROPIC_API_KEY") or "").strip():
            # API key mode does not require interactive login.
            cmd.append("--bare")
        if clean_model:
            cmd.extend(["--model", clean_model])
        if resume_session_id:
            cmd.extend(["--resume", str(resume_session_id)])
        cmd.append(clean_prompt)
        proc = subprocess.run(
            cmd,
            cwd=clean_cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TURN_TIMEOUT_SEC,
            check=False,
        )
        result = self._parse_claude_cli_result(proc.stdout or "")
        is_error = bool(result.get("is_error"))
        if proc.returncode != 0 or is_error:
            err_text = str(result.get("result") or proc.stderr or proc.stdout or "").strip()
            raise AppServerError((err_text or "claude code call failed")[:1500])
        return result

    def _call_anthropic_messages(self, model: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        headers = {
            "x-api-key": self._api_key(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        url = f"{self._base_url()}/v1/messages"
        payload = {
            "model": str(model or self._default_model()),
            "max_tokens": 4096,
            "messages": messages,
        }
        with requests.Session() as session:
            session.trust_env = False
            resp = session.post(url, headers=headers, json=payload, timeout=120)
        if resp.status_code >= 400:
            detail = str(resp.text or "").strip()[:1500]
            raise AppServerError(f"claude messages api failed: http {resp.status_code} {detail}")
        try:
            data = resp.json()
        except Exception as exc:
            raise AppServerError(f"claude messages api invalid json: {exc}") from exc
        if not isinstance(data, dict):
            raise AppServerError("claude messages api invalid payload")
        return data

    def turn_start(self, thread_id: str, text: str, image_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        if not self.is_running():
            self.start()
        clean_thread_id = str(thread_id or "").strip()
        if not clean_thread_id:
            raise AppServerError("thread_id is required")
        user_text = str(text or "").strip()
        turn_id = str(uuid.uuid4())
        with self._lock:
            thread = self._threads.get(clean_thread_id)
            if thread is None:
                thread = {"messages": [], "model": self._default_model(), "cwd": DEFAULT_CWD, "session_id": ""}
                self._threads[clean_thread_id] = thread
            self._thread_status[clean_thread_id] = {"type": "running"}
            self._active_turn_by_thread[clean_thread_id] = turn_id
            self._turn_started_at_by_thread[clean_thread_id] = time.time()
            self._turn_preview_by_thread[clean_thread_id] = user_text[:200]

        self._append_event(clean_thread_id, f"user: {user_text[:200]}")
        provider_tag = "claude_code" if self._provider == "claude_code" else "claude"
        try:
            with self._lock:
                thread = dict(self._threads.get(clean_thread_id) or {"messages": [], "model": self._default_model()})
                model = str(thread.get("model") or self._default_model())
                cwd = str(thread.get("cwd") or self.env.get("BRIDGE_MCP_RUNTIME_CWD") or DEFAULT_CWD)
                session_id = str(thread.get("session_id") or "")
                history = list(thread.get("messages") or [])
            returned_session_id = session_id
            request_messages = history + [{"role": "user", "content": user_text}]
            if self._provider == "claude_code":
                result = self._run_claude_code(cwd=cwd, model=model, prompt=user_text, resume_session_id=session_id)
                assistant_text = str(result.get("result") or "").strip()
                returned_session_id = str(result.get("session_id") or session_id or "")
                usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                token_usage = {
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
                    "provider": "claude_code",
                }
                provider_tag = "claude_code"
            else:
                result = self._call_anthropic_messages(model=model, messages=request_messages)
                blocks = result.get("content") if isinstance(result.get("content"), list) else []
                parts: List[str] = []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if str(block.get("type") or "") == "text":
                        parts.append(str(block.get("text") or ""))
                assistant_text = "\n".join([p for p in parts if str(p).strip()]).strip()
                usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                token_usage = {
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
                    "provider": "claude",
                }
            with self._lock:
                self._threads[clean_thread_id] = {
                    **dict(self._threads.get(clean_thread_id) or {}),
                    "messages": request_messages + [{"role": "assistant", "content": assistant_text}],
                    "session_id": returned_session_id,
                }
                self._thread_status[clean_thread_id] = {"type": "idle"}
                self._active_turn_by_thread.pop(clean_thread_id, None)
                self._token_usage_by_thread[clean_thread_id] = token_usage
                self._turn_preview_by_thread[clean_thread_id] = assistant_text[:200]
                self._turn_results[turn_id] = TurnRunResult(
                    thread_id=clean_thread_id,
                    turn_id=turn_id,
                    turn_status="completed",
                    text=assistant_text,
                    error=None,
                )
            self._append_event(clean_thread_id, f"assistant: {assistant_text[:200]}")
        except Exception as exc:
            err = str(exc)[:1500]
            with self._lock:
                self._thread_status[clean_thread_id] = {"type": "idle"}
                self._active_turn_by_thread.pop(clean_thread_id, None)
                self._turn_results[turn_id] = TurnRunResult(
                    thread_id=clean_thread_id,
                    turn_id=turn_id,
                    turn_status="failed",
                    text="",
                    error={"message": err, "provider": provider_tag},
                )
            self._append_event(clean_thread_id, f"error: {err[:200]}")
        return {"turn": {"id": turn_id}}

    def wait_for_turn_completion(self, thread_id: str, turn_id: str, timeout_sec: int = 600) -> TurnRunResult:
        clean_turn = str(turn_id or "").strip()
        deadline = time.time() + max(1, int(timeout_sec))
        while time.time() < deadline:
            with self._lock:
                done = self._turn_results.get(clean_turn)
            if done:
                return done
            time.sleep(0.05)
        raise AppServerTimeout(f"claude wait timeout thread={thread_id} turn={turn_id}")

    def get_thread_status(self, thread_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._thread_status.get(str(thread_id or "").strip()) or {"type": "idle"})

    def get_active_turn_id(self, thread_id: str) -> str:
        with self._lock:
            return str(self._active_turn_by_thread.get(str(thread_id or "").strip()) or "")

    def get_thread_token_usage(self, thread_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._token_usage_by_thread.get(str(thread_id or "").strip()) or {})

    def get_turn_progress(self, thread_id: str) -> Dict[str, Any]:
        clean = str(thread_id or "").strip()
        with self._lock:
            started_at = float(self._turn_started_at_by_thread.get(clean) or 0.0)
            last_event_at = float(self._turn_last_event_at_by_thread.get(clean) or 0.0)
            preview = str(self._turn_preview_by_thread.get(clean) or "")
            active_turn = str(self._active_turn_by_thread.get(clean) or "")
        return {
            "activeTurnId": active_turn,
            "startedAt": started_at,
            "lastEventAt": last_event_at,
            "preview": preview,
        }

    def get_account_rate_limits(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._account_rate_limits or {})

    def account_rate_limits_read(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._account_rate_limits or {})

    def get_turn_events(self, thread_id: str, turn_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        clean = str(thread_id or "").strip()
        cap = max(1, min(200, int(limit or 20)))
        with self._lock:
            events = list(self._turn_events_by_thread.get(clean) or [])
        return events[-cap:]

    def turn_steer(self, thread_id: str, expected_turn_id: str, text: str, image_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        return self.turn_start(thread_id=thread_id, text=text, image_paths=image_paths)

    def turn_interrupt(self, thread_id: str, turn_id: str) -> Dict[str, Any]:
        clean_thread = str(thread_id or "").strip()
        with self._lock:
            active = str(self._active_turn_by_thread.get(clean_thread) or "")
            if active and (not turn_id or str(turn_id) == active):
                self._active_turn_by_thread.pop(clean_thread, None)
                self._thread_status[clean_thread] = {"type": "idle"}
                return {"ok": True, "interrupted": True, "turn_id": active}
        return {"ok": True, "interrupted": False, "turn_id": str(turn_id or "")}


def _normalize_agent_provider(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "codex"
    raw = raw.replace(" ", "_")
    aliases = {
        "codex": "codex",
        "openai": "codex",
        "claude": "claude",
        "claude_code": "claude_code",
        "claude-code": "claude_code",
        "anthropic": "claude",
        "openclaw": "openclaw",
    }
    return aliases.get(raw, raw)


def _build_agent_adapter(provider: str) -> AgentAdapter:
    clean = _normalize_agent_provider(provider)
    if clean == "codex":
        return CodexAgentAdapter()
    if clean in {"claude", "claude_code", "openclaw"}:
        return ClaudeAgentAdapter(provider=clean)
    raise ValueError(f"unsupported agent provider: {clean}")


@dataclass
class ChatRuntime:
    chat_id: str
    agent_provider: str = "codex"
    thread_id: str = ""
    active_turn_id: str = ""
    cwd: str = DEFAULT_CWD
    model: str = DEFAULT_MODEL
    sandbox: str = DEFAULT_SANDBOX
    approval_policy: str = DEFAULT_APPROVAL
    personality: str = DEFAULT_PERSONALITY
    auth_profile: str = ""
    profile_thread_ids: Dict[str, str] = field(default_factory=dict)
    last_input_at: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    client: AgentAdapter = field(default_factory=lambda: _build_agent_adapter("codex"))

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
            provider = _normalize_agent_provider(str(persisted.get("agent_provider") or "codex"))
            try:
                adapter = _build_agent_adapter(provider)
            except Exception as exc:
                LOG.warning("runtime provider fallback to codex chat_id=%s provider=%s err=%s", clean_chat_id, provider, exc)
                provider = "codex"
                adapter = _build_agent_adapter(provider)
            runtime = ChatRuntime(
                chat_id=clean_chat_id,
                agent_provider=provider,
                thread_id=str(persisted.get("thread_id") or ""),
                active_turn_id=str(persisted.get("active_turn_id") or ""),
                cwd=str(persisted.get("cwd") or DEFAULT_CWD),
                model=str(persisted.get("model") or DEFAULT_MODEL),
                sandbox=str(persisted.get("sandbox") or DEFAULT_SANDBOX),
                approval_policy=str(persisted.get("approval_policy") or DEFAULT_APPROVAL),
                personality=str(persisted.get("personality") or DEFAULT_PERSONALITY),
                auth_profile=str(persisted.get("auth_profile") or ""),
                profile_thread_ids=_normalize_profile_thread_ids(persisted.get("profile_thread_ids")),
                last_input_at=int(persisted.get("last_input_at") or persisted.get("updated_at") or 0),
                client=adapter,
            )
            if runtime.thread_id:
                current_key = _profile_thread_map_key(runtime.agent_provider, runtime.auth_profile)
                runtime.profile_thread_ids[current_key] = runtime.thread_id
            if not str(runtime.auth_profile or "").strip():
                try:
                    _ensure_runtime_preferred_auth_profile(runtime, reason="runtime bootstrap prefer recent active profile")
                except Exception as exc:
                    LOG.warning("runtime bootstrap auth auto-pick failed chat_id=%s err=%s", clean_chat_id, exc)
            _sync_runtime_model_from_profile(runtime, force=False)
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


def _profile_thread_map_key(provider: str, profile: str) -> str:
    clean_provider = _normalize_agent_provider(provider)
    clean_profile = str(profile or "").strip() or "__default__"
    return f"{clean_provider}::{clean_profile}"


def _normalize_profile_thread_ids(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in raw.items():
        clean_key = str(key or "").strip()
        clean_value = str(value or "").strip()
        if clean_key and clean_value:
            out[clean_key] = clean_value
    return out


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
    record_id: str = "",
) -> Dict[str, Any]:
    events = runtime.client.get_turn_events(thread_id=thread_id or runtime.thread_id, turn_id=turn_id, limit=80)
    current_cwd = str(cwd or runtime.cwd or DEFAULT_CWD)
    project = _project_label_for_cwd(current_cwd)
    start_ts = int(started_at or 0)
    end_ts = int(ended_at or time.time())
    duration_sec = max(0, end_ts - start_ts) if start_ts > 0 else 0
    stable_id = str(record_id or "").strip() or f"{end_ts}_{runtime.chat_id}_{turn_id or 'no_turn'}"
    return {
        "id": stable_id,
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
    requested_model = str(getattr(body, "model", "") or "").strip()
    runtime.model = str(requested_model or runtime.model or DEFAULT_MODEL)
    requested_provider = str(getattr(body, "agent_provider", "") or "").strip()
    provider_changed = False
    if requested_provider:
        normalized_provider = _normalize_agent_provider(requested_provider)
        current_provider = _normalize_agent_provider(runtime.agent_provider)
        if normalized_provider != current_provider:
            next_adapter = _build_agent_adapter(normalized_provider)
            try:
                runtime.client.stop()
            except Exception:
                pass
            runtime.client = next_adapter
            runtime.agent_provider = normalized_provider
            runtime.thread_id = ""
            runtime.active_turn_id = ""
            provider_changed = True
    if not requested_model:
        _sync_runtime_model_from_profile(runtime, force=provider_changed)
    runtime.sandbox = str(getattr(body, "sandbox", "") or runtime.sandbox or DEFAULT_SANDBOX)
    runtime.approval_policy = str(
        getattr(body, "approval_policy", "") or runtime.approval_policy or DEFAULT_APPROVAL
    )
    runtime.personality = str(getattr(body, "personality", "") or runtime.personality or DEFAULT_PERSONALITY)
    _apply_runtime_bridge_env(runtime)


def _persist_runtime(runtime: ChatRuntime, patch: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    current_key = _profile_thread_map_key(runtime.agent_provider, runtime.auth_profile)
    profile_thread_ids = _normalize_profile_thread_ids(runtime.profile_thread_ids)
    if runtime.thread_id:
        profile_thread_ids[current_key] = str(runtime.thread_id or "").strip()
        runtime.profile_thread_ids = dict(profile_thread_ids)

    data: Dict[str, Any] = {
        "runtime_id": runtime.chat_id,
        "source_chat_id": _runtime_actual_chat_id(runtime.chat_id),
        "project": _runtime_project_name(runtime.chat_id),
        "agent_provider": str(runtime.agent_provider or "codex"),
        "thread_id": runtime.thread_id,
        "active_turn_id": runtime.active_turn_id,
        "cwd": runtime.cwd,
        "model": runtime.model,
        "sandbox": runtime.sandbox,
        "approval_policy": runtime.approval_policy,
        "personality": runtime.personality,
        "auth_profile": runtime.auth_profile,
        "profile_thread_ids": profile_thread_ids,
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


def _looks_like_resume_prompt(text: str) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    keywords = {
        "continue",
        "resume",
        "继续",
        "接着",
        "接着做",
        "继续做",
        "继续刚才",
        "继续上次",
        "按这个继续",
    }
    if raw in keywords:
        return True
    if len(raw) <= 24 and ("继续" in raw or "接着" in raw):
        return True
    if len(raw) <= 32 and ("continue" in raw or "resume" in raw):
        return True
    return False


def _build_auto_memory_prefix(project: str, user_text: str, limit: int = 0) -> str:
    if not AUTO_MEMORY_INJECT_ENABLED:
        return ""
    target_project = str(project or "").strip()
    if not target_project:
        return ""
    query = str(user_text or "").strip()
    if _looks_like_resume_prompt(query):
        query = ""
    cap = max(1, min(100, int(limit or AUTO_MEMORY_INJECT_LIMIT)))
    items = HISTORY_STORE.search_project_memories(
        project=target_project,
        query=query,
        limit=cap,
        include_turn_text=True,
        exclude_chat_id="",
    )
    if not items:
        return ""
    lines: List[str] = []

    def _clip_text(text: Any, cap_chars: int) -> str:
        raw = str(text or "").strip()
        if len(raw) <= cap_chars:
            return raw
        return raw[: max(1, cap_chars - 3)].rstrip() + "..."

    def _memory_fields(memory_text: Any) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for line in str(memory_text or "").splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            k = str(key or "").strip().lower()
            if k in {"status", "user", "assistant", "error"}:
                out[k] = str(value or "").strip()
        return out

    for item in items:
        ts = int(item.get("started_at") or 0)
        stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts > 0 else "unknown-time"
        summary = str(item.get("summary") or "").strip()
        turn_id = str(item.get("turn_id") or "").strip()
        if not summary:
            continue
        lines.append(f"- [{stamp}] {summary} (turn_id={turn_id})")
        fields = _memory_fields(item.get("memory_text"))
        details: List[str] = []
        status = _clip_text(fields.get("status", ""), 32)
        user_part = _clip_text(fields.get("user", ""), 220)
        assistant_part = _clip_text(fields.get("assistant", ""), 260)
        error_part = _clip_text(fields.get("error", ""), 220)
        if status:
            details.append(f"status={status}")
        if user_part:
            details.append(f"user={user_part}")
        if assistant_part:
            details.append(f"assistant={assistant_part}")
        if error_part:
            details.append(f"error={error_part}")
        if details:
            lines.append(f"  detail: {' | '.join(details)}")
    if not lines:
        return ""
    header = (
        "[Bridge Auto Memory]\n"
        "你正在新线程里继续老会话。下面是同项目历史摘要与关键细节，请用于恢复上下文并继续解决当前问题。\n"
    )
    body = "\n".join(lines)
    payload = f"{header}{body}"
    if len(payload) > AUTO_MEMORY_INJECT_MAX_CHARS:
        payload = payload[: AUTO_MEMORY_INJECT_MAX_CHARS].rstrip()
    return payload


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


def _extract_rate_limits_payload(raw: Any) -> Dict[str, Any]:
    def _looks_like_limits(node: Any) -> bool:
        if not isinstance(node, dict):
            return False
        return isinstance(node.get("primary"), dict) or isinstance(node.get("secondary"), dict)

    if _looks_like_limits(raw):
        return dict(raw)
    if not isinstance(raw, dict):
        return {}
    direct = raw.get("rateLimits")
    if _looks_like_limits(direct):
        return dict(direct)
    by_id = raw.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for item in by_id.values():
            if _looks_like_limits(item):
                return dict(item)
    return {}


def _read_rate_limits(runtime: ChatRuntime, allow_request: bool = True) -> Dict[str, Any]:
    cached = _extract_rate_limits_payload(runtime.client.get_account_rate_limits())
    if cached:
        return cached
    if not allow_request:
        return {}
    try:
        read = _extract_rate_limits_payload(runtime.client.account_rate_limits_read())
        if read:
            return read
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

    # account/rateLimits/read may return token_expired even when session is still usable.
    # Treat it as a probe/read failure first, and let follow-up checks decide.
    if "account/ratelimits/read" in lower and "token_expired" in lower:
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
        "provider": _normalize_agent_provider(str(previous_meta.get("provider") or "codex")),
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
        "check_required": bool(previous_meta.get("check_required")),
        "last_health_check_at": int(previous_meta.get("last_health_check_at") or 0),
        "last_health_error": str(previous_meta.get("last_health_error") or "").strip(),
        "last_rate_limits": dict(previous_meta.get("last_rate_limits") or {})
        if isinstance(previous_meta.get("last_rate_limits"), dict)
        else {},
        "last_rate_limits_at": int(previous_meta.get("last_rate_limits_at") or 0),
        "last_used_at": int(previous_meta.get("last_used_at") or 0),
        "last_used_success_at": int(previous_meta.get("last_used_success_at") or 0),
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

    # Do not hard-stick to historical needs_reauth/deactivated flags here.
    # We still run the current auth payload validation and `codex login status`
    # so a successful re-login can recover immediately without requiring file hash change.

    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        meta["status"] = "invalid"
        meta["reason"] = f"invalid json: {exc}"
        meta["last_health_check_at"] = now_ts
        meta["last_health_error"] = meta["reason"]
        return meta

    provider = _infer_provider_from_auth_payload(data if isinstance(data, dict) else {}, fallback=str(meta.get("provider") or "codex"))
    meta["provider"] = provider
    ident = _auth_identity_from_auth_json(data if isinstance(data, dict) else {})
    meta["email"] = str(ident.get("email") or "").strip()
    meta["sub"] = str(ident.get("sub") or "").strip()
    if provider == "codex":
        auth_mode = str(data.get("auth_mode") or "").strip()
        meta["auth_mode"] = auth_mode

    cfg = source.with_name(f"{profile}.config.toml")
    if cfg.exists() and cfg.is_file():
        meta["source_config_toml"] = str(cfg)

    disabled_until = int(meta.get("disabled_until") or 0)
    if provider != "codex":
        claude_env = _extract_claude_profile_env(data if isinstance(data, dict) else {})
        if not claude_env.get("ANTHROPIC_API_KEY"):
            meta["status"] = "invalid"
            meta["reason"] = "missing ANTHROPIC_API_KEY/api_key"
            meta["last_health_check_at"] = now_ts
            meta["last_health_error"] = meta["reason"]
            return meta
        meta["last_health_check_at"] = now_ts
        if str(meta.get("status") or "").strip().lower() == "temp_disabled" and disabled_until > now_ts:
            meta["valid"] = False
            meta["reason"] = meta["disabled_reason"] or f"临时禁用中，预计 {time.strftime('%m-%d %H:%M', time.localtime(disabled_until))} 解禁"
            meta["last_health_error"] = meta["reason"]
        else:
            meta["valid"] = True
            meta["reason"] = ""
            meta["status"] = "active"
            meta["check_required"] = False
            meta["disabled_until"] = 0
            meta["disabled_reason"] = ""
            meta["needs_reauth"] = False
            meta["risk_deactivated"] = False
            meta["last_health_error"] = ""
        return meta

    if not _is_codex_auth_payload(data if isinstance(data, dict) else {}):
        meta["status"] = "invalid"
        meta["reason"] = "missing auth_mode/tokens"
        meta["last_health_check_at"] = now_ts
        meta["last_health_error"] = meta["reason"]
        return meta

    home_dir = _profile_home_dir(profile)
    home_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, home_dir / "auth.json")
    _ensure_bridge_mcp_server_installed(home_dir)
    if cfg.exists() and cfg.is_file():
        shutil.copy2(cfg, home_dir / "config.toml")

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
        if str(meta.get("status") or "").strip().lower() == "temp_disabled" and disabled_until > now_ts:
            meta["valid"] = False
            meta["reason"] = meta["disabled_reason"] or f"临时禁用中，预计 {time.strftime('%m-%d %H:%M', time.localtime(disabled_until))} 解禁"
            meta["last_health_error"] = meta["reason"]
        else:
            meta["valid"] = True
            meta["reason"] = ""
            meta["status"] = "active"
            meta["check_required"] = False
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


def _extract_claude_profile_env(payload: Dict[str, Any]) -> Dict[str, str]:
    data = payload if isinstance(payload, dict) else {}
    env_node = data.get("env") if isinstance(data.get("env"), dict) else {}
    anthropic_node = data.get("anthropic") if isinstance(data.get("anthropic"), dict) else {}

    def _pick(*keys: str) -> str:
        for key in keys:
            value = ""
            if isinstance(data.get(key), str):
                value = str(data.get(key) or "").strip()
            if (not value) and isinstance(env_node.get(key), str):
                value = str(env_node.get(key) or "").strip()
            if (not value) and isinstance(anthropic_node.get(key), str):
                value = str(anthropic_node.get(key) or "").strip()
            if value:
                return value
        return ""

    api_key = _pick("ANTHROPIC_API_KEY", "anthropic_api_key", "api_key")
    base_url = _pick("ANTHROPIC_BASE_URL", "anthropic_base_url", "base_url")
    model = _pick("BRIDGE_CLAUDE_DEFAULT_MODEL", "default_model", "model")
    out: Dict[str, str] = {}
    if api_key:
        out["ANTHROPIC_API_KEY"] = api_key
    if base_url:
        out["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    if model:
        out["BRIDGE_CLAUDE_DEFAULT_MODEL"] = model
    return out


def _is_codex_auth_payload(payload: Dict[str, Any]) -> bool:
    data = payload if isinstance(payload, dict) else {}
    auth_mode = str(data.get("auth_mode") or "").strip()
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    return bool(auth_mode and isinstance(tokens, dict))


def _infer_provider_from_auth_payload(payload: Dict[str, Any], fallback: str = "codex") -> str:
    fallback_provider = _normalize_agent_provider(fallback)
    if _is_codex_auth_payload(payload):
        return "codex"
    claude_env = _extract_claude_profile_env(payload)
    if claude_env.get("ANTHROPIC_API_KEY"):
        if fallback_provider in {"claude", "claude_code", "openclaw"}:
            return fallback_provider
        return "claude_code"
    return fallback_provider or "codex"


def _load_profile_auth_json(profile: str) -> Dict[str, Any]:
    clean = str(profile or "").strip()
    if not clean:
        return {}
    path = AUTH_PROFILES_DIR / f"{clean}.auth.json"
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _pending_auth_paths(profile: str) -> tuple[Path, Path]:
    name = _sanitize_auth_profile_name(profile)
    AUTH_PENDING_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    return (
        AUTH_PENDING_PROFILES_DIR / f"{name}.auth.json",
        AUTH_PENDING_PROFILES_DIR / f"{name}.config.toml",
    )


def _backup_auth_paths(profile: str) -> tuple[Path, Path]:
    name = _sanitize_auth_profile_name(profile)
    AUTH_BACKUP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    return (
        AUTH_BACKUP_PROFILES_DIR / f"{name}.auth.json",
        AUTH_BACKUP_PROFILES_DIR / f"{name}.config.toml",
    )


def _remove_pending_profile_artifacts(profile: str) -> bool:
    name = _sanitize_auth_profile_name(profile)
    src, cfg = _pending_auth_paths(name)
    changed = False
    try:
        if src.exists():
            src.unlink()
            changed = True
    except Exception as exc:
        LOG.warning("remove pending auth source failed profile=%s err=%s", name, exc)
    try:
        if cfg.exists():
            cfg.unlink()
            changed = True
    except Exception as exc:
        LOG.warning("remove pending auth config failed profile=%s err=%s", name, exc)
    return changed


def _remove_backup_profile_artifacts(profile: str) -> bool:
    name = _sanitize_auth_profile_name(profile)
    src, cfg = _backup_auth_paths(name)
    changed = False
    try:
        if src.exists():
            src.unlink()
            changed = True
    except Exception as exc:
        LOG.warning("remove backup auth source failed profile=%s err=%s", name, exc)
    try:
        if cfg.exists():
            cfg.unlink()
            changed = True
    except Exception as exc:
        LOG.warning("remove backup auth config failed profile=%s err=%s", name, exc)
    return changed


def _auth_identity_from_auth_json(payload: Dict[str, Any]) -> Dict[str, str]:
    data = payload if isinstance(payload, dict) else {}
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    token = str(tokens.get("id_token") or "").strip()
    jwt = _decode_jwt_payload(token)
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    email = (
        str(jwt.get("email") or "").strip()
        or str(data.get("email") or "").strip()
        or str(account.get("email") or "").strip()
    )
    sub = (
        str(jwt.get("sub") or "").strip()
        or str(data.get("sub") or "").strip()
        or str(data.get("account_id") or "").strip()
        or str(account.get("id") or "").strip()
    )
    return {
        "email": email,
        "sub": sub,
    }


def _pending_profile_item_from_file(path: Path) -> Dict[str, Any]:
    profile = str(path.name[: -len(".auth.json")] if path.name.endswith(".auth.json") else path.stem).strip()
    cfg = path.with_name(f"{profile}.config.toml")
    item: Dict[str, Any] = {
        "profile": profile,
        "email": "",
        "sub": "",
        "provider": "codex",
        "source_auth_json": str(path),
        "source_config_toml": str(cfg) if cfg.exists() and cfg.is_file() else "",
        "valid": False,
        "status": "pending",
        "reason": "",
        "updated_at": int(path.stat().st_mtime) if path.exists() else int(time.time()),
    }
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        item["status"] = "invalid"
        item["reason"] = f"invalid json: {exc}"
        return item
    if not isinstance(parsed, dict):
        item["status"] = "invalid"
        item["reason"] = "auth_json must be object"
        return item
    ident = _auth_identity_from_auth_json(parsed)
    item["email"] = str(ident.get("email") or "")
    item["sub"] = str(ident.get("sub") or "")
    provider = _infer_provider_from_auth_payload(parsed, fallback="codex")
    item["provider"] = provider
    if provider == "codex":
        if _is_codex_auth_payload(parsed):
            item["valid"] = True
            item["status"] = "pending"
        else:
            item["status"] = "invalid"
            item["reason"] = "missing auth_mode/tokens"
    else:
        if _extract_claude_profile_env(parsed).get("ANTHROPIC_API_KEY"):
            item["valid"] = True
            item["status"] = "pending"
        else:
            item["status"] = "invalid"
            item["reason"] = "missing ANTHROPIC_API_KEY/api_key"
    return item


def _list_pending_auth_profiles() -> List[Dict[str, Any]]:
    AUTH_PENDING_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    out: List[Dict[str, Any]] = []
    for path in sorted(AUTH_PENDING_PROFILES_DIR.glob("*.auth.json")):
        if not path.is_file():
            continue
        out.append(_pending_profile_item_from_file(path))
    return out


def _collect_known_profile_names() -> set[str]:
    names: set[str] = set()
    AUTH_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_PENDING_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_BACKUP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    for path in AUTH_PROFILES_DIR.glob("*.auth.json"):
        if path.is_file():
            names.add(str(path.name[: -len(".auth.json")]))
    for path in AUTH_PENDING_PROFILES_DIR.glob("*.auth.json"):
        if path.is_file():
            names.add(str(path.name[: -len(".auth.json")]))
    for path in AUTH_BACKUP_PROFILES_DIR.glob("*.auth.json"):
        if path.is_file():
            names.add(str(path.name[: -len(".auth.json")]))
    reg = _load_auth_control_registry()
    auths = reg.get("auths") if isinstance(reg.get("auths"), dict) else {}
    names.update(str(k or "").strip() for k in auths.keys())
    names.discard("")
    return names


def _auto_profile_alias(email: str = "", filename: str = "", used: Optional[set[str]] = None) -> str:
    used_set = set(used or set())
    base_source = str(email or "").strip()
    if not base_source:
        stem = str(Path(str(filename or "").strip()).stem or "").strip()
        base_source = stem
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base_source.lower()).strip("._-")
    if not base:
        base = f"auth_{int(time.time())}"
    if not re.match(r"^[A-Za-z0-9]", base):
        base = f"a_{base}"
    base = re.sub(r"_+", "_", base)[:64].rstrip("._-")
    if not base:
        base = "auth"
    try:
        clean_base = _sanitize_auth_profile_name(base)
    except Exception:
        clean_base = "auth"
    if clean_base not in used_set:
        return clean_base
    seq = 2
    while seq < 100000:
        suffix = f"_{seq}"
        candidate = (clean_base[: max(1, 64 - len(suffix))] + suffix).rstrip("._-")
        if not re.match(r"^[A-Za-z0-9]", candidate):
            candidate = f"a{candidate}"[:64]
        try:
            candidate = _sanitize_auth_profile_name(candidate)
        except Exception:
            seq += 1
            continue
        if candidate not in used_set:
            return candidate
        seq += 1
    raise HTTPException(status_code=500, detail="failed to generate unique profile alias")


def _auto_device_pending_profile(used: Optional[set[str]] = None) -> str:
    used_set = set(used or _collect_known_profile_names())
    base = f"device_login_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        candidate = _sanitize_auth_profile_name(base)
    except Exception:
        candidate = f"device_login_{int(time.time())}"
    if candidate not in used_set:
        return candidate
    seq = 2
    while seq < 100000:
        suffix = f"_{seq}"
        name = (candidate[: max(1, 64 - len(suffix))] + suffix).rstrip("._-")
        try:
            name = _sanitize_auth_profile_name(name)
        except Exception:
            seq += 1
            continue
        if name not in used_set:
            return name
        seq += 1
    raise HTTPException(status_code=500, detail="failed to generate pending profile name for device login")


def _merge_registry_profile_alias(source_profile: str, target_profile: str) -> None:
    src = str(source_profile or "").strip()
    dst = str(target_profile or "").strip()
    if (not src) or (not dst) or src == dst:
        return
    now_ts = int(time.time())
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}

        src_auth = auths.get(src) if isinstance(auths.get(src), dict) else {}
        dst_auth = auths.get(dst) if isinstance(auths.get(dst), dict) else {}
        merged = dict(dst_auth)
        if src_auth:
            if not merged:
                merged.update(src_auth)
            for key in ("provider", "label", "notes", "fingerprint", "email", "sub"):
                if (not str(merged.get(key) or "").strip()) and str(src_auth.get(key) or "").strip():
                    merged[key] = src_auth.get(key)
            merged["created_at"] = int(dst_auth.get("created_at") or src_auth.get("created_at") or now_ts)
        if merged:
            merged["profile"] = dst
            merged["updated_at"] = now_ts
            auths[dst] = merged
        auths.pop(src, None)

        src_asg = assignments.get(src) if isinstance(assignments.get(src), dict) else {}
        dst_asg = assignments.get(dst) if isinstance(assignments.get(dst), dict) else {}
        if src_asg and (not dst_asg):
            moved = dict(src_asg)
            moved["profile"] = dst
            moved["updated_at"] = now_ts
            assignments[dst] = moved
        assignments.pop(src, None)

        registry["auths"] = auths
        registry["assignments"] = assignments
        _audit_auth_control(
            registry,
            action="merge_profile_alias",
            profile=dst,
            ok=True,
            message=f"{src} -> {dst}",
        )
        _save_auth_control_registry(registry)


def _save_pending_profile(profile: str, auth_json: Any, config_toml: str = "") -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    src, cfg = _pending_auth_paths(name)
    auth_text = _normalize_auth_json_text(auth_json)
    tmp = src.with_name(f".{src.name}.tmp")
    tmp.write_text(auth_text, encoding="utf-8")
    tmp.replace(src)
    cfg_text = str(config_toml or "").strip()
    if cfg_text:
        cfg.write_text(cfg_text + ("" if cfg_text.endswith("\n") else "\n"), encoding="utf-8")
    elif cfg.exists():
        cfg.unlink()
    return _pending_profile_item_from_file(src)


def _save_backup_profile(profile: str, auth_json: Any, config_toml: str = "") -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    src, cfg = _backup_auth_paths(name)
    auth_text = _normalize_auth_json_text(auth_json)
    tmp = src.with_name(f".{src.name}.tmp")
    tmp.write_text(auth_text, encoding="utf-8")
    tmp.replace(src)
    cfg_text = str(config_toml or "").strip()
    if cfg_text:
        cfg.write_text(cfg_text + ("" if cfg_text.endswith("\n") else "\n"), encoding="utf-8")
    elif cfg.exists():
        cfg.unlink()
    return {
        "profile": name,
        "source_auth_json": str(src),
        "source_config_toml": str(cfg) if cfg.exists() and cfg.is_file() else "",
        "updated_at": int(time.time()),
    }


def _load_auth_payload_from_any_store(profile: str) -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    candidates = [
        ("local", AUTH_PROFILES_DIR / f"{name}.auth.json", AUTH_PROFILES_DIR / f"{name}.config.toml"),
        ("pending", _pending_auth_paths(name)[0], _pending_auth_paths(name)[1]),
        ("backup", _backup_auth_paths(name)[0], _backup_auth_paths(name)[1]),
    ]
    for source, auth_path, cfg_path in candidates:
        if not auth_path.exists() or (not auth_path.is_file()):
            continue
        try:
            payload = json.loads(auth_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"{source} auth invalid json for {name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail=f"{source} auth must be object for {name}")
        return {
            "source": source,
            "auth_json": payload,
            "config_toml": cfg_path.read_text(encoding="utf-8") if cfg_path.exists() and cfg_path.is_file() else "",
            "auth_path": str(auth_path),
            "cfg_path": str(cfg_path) if cfg_path.exists() and cfg_path.is_file() else "",
        }
    raise HTTPException(status_code=404, detail=f"auth source not found in local/pending/backup: {name}")


def _mark_auth_profile_unchecked(profile: str, reason: str = "未检测") -> None:
    _patch_auth_registry_profile(
        profile,
        {
            "check_required": True,
            "valid": False,
            "status": "unchecked",
            "reason": str(reason or "未检测").strip(),
            "last_health_check_at": 0,
            "last_health_error": "",
            "disabled_until": 0,
            "disabled_reason": "",
        },
    )


def _profile_name_rank(name: str) -> tuple[int, int, str]:
    clean = str(name or "").strip()
    has_seq_suffix = bool(re.search(r"_\d+$", clean))
    return (1 if has_seq_suffix else 0, len(clean), clean)


def _strip_ansi(text: str) -> str:
    raw = str(text or "")
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)


def _cleanup_reauth_requests(now_ts: Optional[int] = None) -> None:
    current = int(now_ts or time.time())
    stale: List[str] = []
    for rid, item in list(_AUTH_REAUTH_REQUESTS.items()):
        if not isinstance(item, dict):
            stale.append(str(rid))
            continue
        started_at = int(item.get("started_at") or 0)
        completed_at = int(item.get("completed_at") or 0)
        deadline = max(started_at, completed_at) + AUTH_REAUTH_REQUEST_TTL_SEC
        if deadline > 0 and deadline < current:
            proc = item.get("proc")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            stale.append(str(rid))
    for rid in stale:
        _AUTH_REAUTH_REQUESTS.pop(rid, None)


def _extract_device_auth_url_and_code(text: str) -> Dict[str, str]:
    clean = _strip_ansi(text)
    url_match = re.search(r"https://auth\.openai\.com/codex/device", clean)
    code_match = re.search(r"\b[A-Z0-9]{4,8}-[A-Z0-9]{4,8}\b", clean)
    return {
        "verification_uri": str(url_match.group(0)) if url_match else "",
        "user_code": str(code_match.group(0)) if code_match else "",
    }


def _collect_identity_candidates() -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    local_by_profile: Dict[str, Dict[str, Any]] = {}
    for item in _refresh_auth_profiles():
        if not isinstance(item, dict):
            continue
        profile = str(item.get("profile") or "").strip()
        if not profile:
            continue
        local_by_profile[profile] = dict(item)
    for item in _list_pending_auth_profiles():
        if not isinstance(item, dict):
            continue
        profile = str(item.get("profile") or "").strip()
        if not profile:
            continue
        pending = dict(item)
        if profile not in local_by_profile:
            local_by_profile[profile] = pending
    remote_by_profile: Dict[str, Dict[str, Any]] = {}
    try:
        nodes = _parse_auth_control_nodes()
        for snap in _collect_auth_control_node_snapshots(nodes):
            for item in list(snap.get("profiles") or []):
                if not isinstance(item, dict):
                    continue
                profile = str(item.get("profile") or "").strip()
                if not profile:
                    continue
                current = remote_by_profile.get(profile) if isinstance(remote_by_profile.get(profile), dict) else {}
                merged = dict(current)
                if not str(merged.get("email") or "").strip():
                    merged["email"] = str(item.get("email") or "").strip()
                if not str(merged.get("sub") or "").strip():
                    merged["sub"] = str(item.get("sub") or "").strip()
                remote_by_profile[profile] = merged
    except Exception:
        remote_by_profile = {}
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
    for profile, raw in auths.items():
        clean_profile = str(profile or "").strip()
        if not clean_profile:
            continue
        item = raw if isinstance(raw, dict) else {}
        current = local_by_profile.get(clean_profile, {})
        out.append(
            {
                "profile": clean_profile,
                "email": str(current.get("email") or item.get("email") or remote_by_profile.get(clean_profile, {}).get("email") or "").strip().lower(),
                "sub": str(current.get("sub") or item.get("sub") or remote_by_profile.get(clean_profile, {}).get("sub") or "").strip(),
            }
        )
    seen_profiles = {str(row.get("profile") or "").strip() for row in out}
    for profile, item in local_by_profile.items():
        clean_profile = str(profile or "").strip()
        if (not clean_profile) or clean_profile in seen_profiles:
            continue
        out.append(
            {
                "profile": clean_profile,
                "email": str(item.get("email") or "").strip().lower(),
                "sub": str(item.get("sub") or "").strip(),
            }
        )
    for profile, item in remote_by_profile.items():
        clean_profile = str(profile or "").strip()
        if (not clean_profile) or clean_profile in seen_profiles:
            continue
        out.append(
            {
                "profile": clean_profile,
                "email": str(item.get("email") or "").strip().lower(),
                "sub": str(item.get("sub") or "").strip(),
            }
        )
    return out


def _find_existing_profile_for_identity(email: str = "", sub: str = "") -> str:
    clean_email = str(email or "").strip().lower()
    clean_sub = str(sub or "").strip()
    if (not clean_email) and (not clean_sub):
        return ""
    candidates = _collect_identity_candidates()
    by_sub: List[str] = []
    by_email: List[str] = []
    for item in candidates:
        profile = str(item.get("profile") or "").strip()
        if not profile:
            continue
        item_sub = str(item.get("sub") or "").strip()
        item_email = str(item.get("email") or "").strip().lower()
        if clean_sub and item_sub and item_sub == clean_sub:
            by_sub.append(profile)
        if clean_email and item_email and item_email == clean_email:
            by_email.append(profile)
    if by_sub:
        return sorted(set(by_sub), key=_profile_name_rank)[0]
    if by_email:
        return sorted(set(by_email), key=_profile_name_rank)[0]
    return ""


def _watch_reauth_request(request_id: str) -> None:
    rid = str(request_id or "").strip()
    if not rid:
        return
    with _AUTH_REAUTH_LOCK:
        job = _AUTH_REAUTH_REQUESTS.get(rid) if isinstance(_AUTH_REAUTH_REQUESTS.get(rid), dict) else None
        if not job:
            return
        proc = job.get("proc")
        ready_event = job.get("ready_event")
    if not proc:
        return

    lines: List[str] = []
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                clean_line = _strip_ansi(str(line or "").rstrip("\n"))
                if clean_line:
                    lines.append(clean_line)
                joined = "\n".join(lines[-120:])
                parsed = _extract_device_auth_url_and_code(joined)
                with _AUTH_REAUTH_LOCK:
                    current = _AUTH_REAUTH_REQUESTS.get(rid) if isinstance(_AUTH_REAUTH_REQUESTS.get(rid), dict) else None
                    if not current:
                        return
                    if parsed.get("verification_uri") and not str(current.get("verification_uri") or "").strip():
                        current["verification_uri"] = str(parsed.get("verification_uri") or "").strip()
                    if parsed.get("user_code") and not str(current.get("user_code") or "").strip():
                        current["user_code"] = str(parsed.get("user_code") or "").strip()
                    current["output_tail"] = "\n".join(lines[-40:])[-4000:]
                    if current.get("verification_uri") and current.get("user_code"):
                        current["expires_at"] = int(current.get("started_at") or int(time.time())) + 900
                    if ready_event and (not ready_event.is_set()) and current.get("verification_uri") and current.get("user_code"):
                        ready_event.set()
        rc = int(proc.wait(timeout=1800))
    except subprocess.TimeoutExpired:
        try:
            proc.terminate()
        except Exception:
            pass
        rc = -9
        lines.append("device auth timeout")
    except Exception as exc:
        rc = -1
        lines.append(f"watch error: {exc}")

    with _AUTH_REAUTH_LOCK:
        current = _AUTH_REAUTH_REQUESTS.get(rid) if isinstance(_AUTH_REAUTH_REQUESTS.get(rid), dict) else None
        if not current:
            return
        current["exit_code"] = rc
        current["completed_at"] = int(time.time())
        current["output_tail"] = "\n".join(lines[-40:])[-4000:]
        if ready_event and (not ready_event.is_set()):
            ready_event.set()
        if str(current.get("status") or "").strip() in {"cancelled"}:
            current["error"] = str(current.get("error") or "cancelled").strip() or "cancelled"
            return
        if rc != 0:
            current["status"] = "failed"
            current["error"] = str(current.get("output_tail") or f"codex login exited with code {rc}")[-1000:]
            return

        profile = str(current.get("profile") or "").strip()
        if not profile:
            current["status"] = "failed"
            current["error"] = "missing profile"
            return
        home_auth = _profile_home_dir(profile) / "auth.json"
        if not home_auth.exists() or (not home_auth.is_file()):
            current["status"] = "failed"
            current["error"] = "login completed but auth.json not found in profile home"
            return
        try:
            payload = json.loads(home_auth.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("auth.json must be object")
            ident = _auth_identity_from_auth_json(payload)
            matched_profile = _find_existing_profile_for_identity(
                email=str(ident.get("email") or "").strip(),
                sub=str(ident.get("sub") or "").strip(),
            )
            auto_profile = bool(current.get("auto_profile"))
            profile_preexisted = bool(current.get("profile_preexisted"))
            final_profile = profile
            if auto_profile:
                if matched_profile:
                    final_profile = matched_profile
                else:
                    used = _collect_known_profile_names()
                    used.discard(profile)
                    final_profile = _auto_profile_alias(
                        email=str(ident.get("email") or "").strip(),
                        filename=f"{profile}.auth.json",
                        used=used,
                    )
            elif (not profile_preexisted) and matched_profile and matched_profile != profile:
                final_profile = matched_profile

            src = AUTH_PROFILES_DIR / f"{final_profile}.auth.json"
            if src.exists() and src.is_file():
                backup_name = f"{final_profile}.auth.json.{datetime.now().strftime('%Y%m%d_%H%M%S')}.reauth.bak"
                AUTH_BACKUP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, AUTH_BACKUP_PROFILES_DIR / backup_name)
            shutil.copy2(home_auth, src)
            if final_profile != profile:
                _remove_pending_profile_artifacts(profile)
                _remove_backup_profile_artifacts(profile)
                _remove_auth_profile_artifacts(profile)
                _merge_registry_profile_alias(profile, final_profile)
            _refresh_auth_profiles()
            current["profile"] = final_profile
            current["resolved_profile"] = final_profile
            current["status"] = "success"
            current["error"] = ""
        except Exception as exc:
            current["status"] = "failed"
            current["error"] = f"sync auth.json failed: {exc}"


def _auth_control_reauth_start(profile: str, node_id: str = "") -> Dict[str, Any]:
    requested_profile = str(profile or "").strip()
    auto_profile = not requested_profile
    if requested_profile:
        name = _sanitize_auth_profile_name(requested_profile)
    else:
        name = _auto_device_pending_profile(used=_collect_known_profile_names())
    target_node = str(node_id or "").strip()
    if target_node and target_node != "local":
        nodes = _auth_control_nodes_map()
        node = nodes.get(target_node)
        if not node:
            raise HTTPException(status_code=404, detail=f"node not found or disabled: {target_node}")
        try:
            payload = _call_node_api(node, "POST", "/auth/control/reauth/start", {"profile": name})
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"remote reauth start failed on {target_node}: {exc}") from exc
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            **dict(data),
            "node_id": target_node,
        }

    item = _get_auth_profile(name)
    profile_preexisted = bool(item)
    if item:
        provider = _normalize_agent_provider(str(item.get("provider") or "codex"))
        if provider != "codex":
            raise HTTPException(status_code=400, detail=f"profile provider not supported for device auth: {provider}")
    else:
        # Allow creating new codex profile via device-auth without pre-uploaded auth.json.
        now_ts = int(time.time())
        with _AUTH_CONTROL_LOCK:
            registry = _load_auth_control_registry()
            auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
            existing = auths.get(name) if isinstance(auths.get(name), dict) else {}
            auths[name] = {
                **existing,
                "profile": name,
                "provider": "codex",
                "label": str(existing.get("label") or name).strip()[:120],
                "notes": str(existing.get("notes") or "").strip()[:500],
                "updated_at": now_ts,
                "created_at": int(existing.get("created_at") or now_ts),
            }
            registry["auths"] = auths
            _audit_auth_control(registry, action="reauth_start", profile=name, node_id="local", ok=True, message="device auth start")
            _save_auth_control_registry(registry)

    with _AUTH_REAUTH_LOCK:
        _cleanup_reauth_requests(now_ts=int(time.time()))
        for rid, existing in list(_AUTH_REAUTH_REQUESTS.items()):
            if not isinstance(existing, dict):
                continue
            if str(existing.get("profile") or "").strip() != name:
                continue
            if str(existing.get("status") or "").strip() != "pending":
                continue
            return {
                "request_id": str(rid),
                "profile": name,
                "status": "pending",
                "verification_uri": str(existing.get("verification_uri") or "").strip(),
                "user_code": str(existing.get("user_code") or "").strip(),
                "expires_at": int(existing.get("expires_at") or int(time.time()) + 900),
            }

    home_dir = _profile_home_dir(name)
    home_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home_dir)
    try:
        proc = subprocess.Popen(
            ["codex", "login", "--device-auth"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"start device auth failed: {exc}") from exc

    now_ts = int(time.time())
    ready_event = threading.Event()
    request_id = str(uuid.uuid4())
    with _AUTH_REAUTH_LOCK:
        _cleanup_reauth_requests(now_ts=now_ts)
        job = {
            "request_id": request_id,
            "profile": name,
            "requested_profile": requested_profile,
            "auto_profile": bool(auto_profile),
            "profile_preexisted": bool(profile_preexisted),
            "status": "pending",
            "verification_uri": "",
            "user_code": "",
            "started_at": now_ts,
            "expires_at": now_ts + 900,
            "completed_at": 0,
            "error": "",
            "output_tail": "",
            "exit_code": None,
            "pid": int(proc.pid or 0),
            "home_dir": str(home_dir),
            "proc": proc,
            "ready_event": ready_event,
        }
        _AUTH_REAUTH_REQUESTS[request_id] = job

    threading.Thread(target=_watch_reauth_request, args=(request_id,), daemon=True, name=f"reauth-{request_id[:8]}").start()
    ready_event.wait(timeout=AUTH_REAUTH_START_WAIT_SEC)
    with _AUTH_REAUTH_LOCK:
        current = _AUTH_REAUTH_REQUESTS.get(request_id) if isinstance(_AUTH_REAUTH_REQUESTS.get(request_id), dict) else {}
        status = str(current.get("status") or "").strip()
        if status == "failed":
            raise HTTPException(status_code=500, detail=str(current.get("error") or "start device auth failed"))
        verification_uri = str(current.get("verification_uri") or "").strip()
        user_code = str(current.get("user_code") or "").strip()
        if (not verification_uri) or (not user_code):
            raise HTTPException(status_code=504, detail="device auth code not ready, retry")
        return {
            "request_id": request_id,
            "profile": name,
            "status": str(current.get("status") or "pending"),
            "verification_uri": verification_uri,
            "user_code": user_code,
            "expires_at": int(current.get("expires_at") or now_ts + 900),
            "node_id": "local",
        }


def _auth_control_reauth_status(request_id: str, node_id: str = "") -> Dict[str, Any]:
    rid = str(request_id or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="request_id is required")
    target_node = str(node_id or "").strip()
    if target_node and target_node != "local":
        nodes = _auth_control_nodes_map()
        node = nodes.get(target_node)
        if not node:
            raise HTTPException(status_code=404, detail=f"node not found or disabled: {target_node}")
        try:
            payload = _call_node_api(node, "POST", "/auth/control/reauth/status", {"request_id": rid})
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"remote reauth status failed on {target_node}: {exc}") from exc
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            **dict(data),
            "node_id": target_node,
        }
    now_ts = int(time.time())
    with _AUTH_REAUTH_LOCK:
        _cleanup_reauth_requests(now_ts=now_ts)
        current = _AUTH_REAUTH_REQUESTS.get(rid) if isinstance(_AUTH_REAUTH_REQUESTS.get(rid), dict) else None
        if not current:
            raise HTTPException(status_code=404, detail=f"reauth request not found: {rid}")
        proc = current.get("proc")
        if proc and proc.poll() is not None and str(current.get("status") or "").strip() == "pending":
            current["status"] = "failed"
            current["exit_code"] = int(proc.returncode or 0)
            current["completed_at"] = int(time.time())
            current["error"] = str(current.get("output_tail") or f"codex login exited with code {proc.returncode}")[-1000:]
        return {
            "request_id": rid,
            "profile": str(current.get("profile") or "").strip(),
            "status": str(current.get("status") or "").strip() or "pending",
            "verification_uri": str(current.get("verification_uri") or "").strip(),
            "user_code": str(current.get("user_code") or "").strip(),
            "started_at": int(current.get("started_at") or 0),
            "expires_at": int(current.get("expires_at") or 0),
            "completed_at": int(current.get("completed_at") or 0),
            "error": str(current.get("error") or "").strip(),
            "exit_code": current.get("exit_code"),
            "node_id": "local",
        }


def _auth_control_reauth_cancel(request_id: str, node_id: str = "") -> Dict[str, Any]:
    rid = str(request_id or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="request_id is required")
    target_node = str(node_id or "").strip()
    if target_node and target_node != "local":
        nodes = _auth_control_nodes_map()
        node = nodes.get(target_node)
        if not node:
            raise HTTPException(status_code=404, detail=f"node not found or disabled: {target_node}")
        try:
            payload = _call_node_api(node, "POST", "/auth/control/reauth/cancel", {"request_id": rid})
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"remote reauth cancel failed on {target_node}: {exc}") from exc
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            **dict(data),
            "node_id": target_node,
        }
    with _AUTH_REAUTH_LOCK:
        current = _AUTH_REAUTH_REQUESTS.get(rid) if isinstance(_AUTH_REAUTH_REQUESTS.get(rid), dict) else None
        if not current:
            raise HTTPException(status_code=404, detail=f"reauth request not found: {rid}")
        proc = current.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        current["status"] = "cancelled"
        current["completed_at"] = int(time.time())
        current["error"] = str(current.get("error") or "cancelled by user").strip()
        return {
            "request_id": rid,
            "profile": str(current.get("profile") or "").strip(),
            "status": "cancelled",
            "completed_at": int(current.get("completed_at") or 0),
            "node_id": "local",
        }


def _auth_control_batch_upload(provider: str, items: List[AuthControlBatchUploadItem], notes: str = "") -> Dict[str, Any]:
    clean_provider = _normalize_agent_provider(provider)
    uploads = [item for item in list(items or []) if isinstance(item, AuthControlBatchUploadItem)]
    if not uploads:
        raise HTTPException(status_code=400, detail="items is empty")
    used = _collect_known_profile_names()
    created: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
        for raw in uploads:
            filename = str(raw.filename or "").strip()
            try:
                auth_text = _normalize_auth_json_text(raw.auth_json)
                parsed = json.loads(auth_text)
                ident = _auth_identity_from_auth_json(parsed if isinstance(parsed, dict) else {})
                alias = _find_existing_profile_for_identity(
                    email=str(ident.get("email") or "").strip(),
                    sub=str(ident.get("sub") or "").strip(),
                )
                if not alias:
                    alias = _auto_profile_alias(email=str(ident.get("email") or ""), filename=filename, used=used)
                item = _save_pending_profile(alias, parsed if isinstance(parsed, dict) else {}, config_toml=raw.config_toml)
                used.add(alias)
                now_ts = int(time.time())
                existing = auths.get(alias) if isinstance(auths.get(alias), dict) else {}
                auths[alias] = {
                    **existing,
                    "profile": alias,
                    "provider": clean_provider,
                    "label": str(existing.get("label") or alias).strip()[:120],
                    "notes": str(notes or existing.get("notes") or "").strip()[:500],
                    "pool": "pending",
                    "updated_at": now_ts,
                    "created_at": int(existing.get("created_at") or now_ts),
                }
                created.append(
                    {
                        "profile": alias,
                        "email": str(item.get("email") or "").strip(),
                        "status": str(item.get("status") or "pending"),
                        "source": filename,
                    }
                )
                _audit_auth_control(registry, action="upload_pending", profile=alias, ok=True, message=f"source={filename}")
            except Exception as exc:
                failed.append({"source": filename, "error": str(exc)[:500]})
        registry["auths"] = auths
        _save_auth_control_registry(registry)
    return {"created": created, "failed": failed}


def _materialize_pending_profile_to_local(profile: str, provider: str = "codex", notes: str = "") -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    src, cfg = _pending_auth_paths(name)
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail=f"pending profile not found: {name}")
    try:
        auth_payload = json.loads(src.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"pending auth invalid json: {exc}") from exc
    cfg_text = cfg.read_text(encoding="utf-8") if cfg.exists() and cfg.is_file() else ""
    installed = _install_auth_profile(
        profile=name,
        provider=provider,
        auth_json=auth_payload if isinstance(auth_payload, dict) else {},
        config_toml=cfg_text,
        assignment_version=0,
        assignment_token="",
        assigned_server_id="",
        notes=notes,
    )
    _remove_pending_profile_artifacts(name)
    _mark_auth_profile_unchecked(name, reason="新分配，待检测")
    return installed


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
        "provider": _normalize_agent_provider(provider),
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
    # Node-to-node control calls must be direct; do not inherit process proxy env.
    # Otherwise HTTP(S)_PROXY may cause false 502 when talking to private nodes.
    with requests.Session() as session:
        session.trust_env = False
        resp = session.request(**kwargs)
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


def _extract_stale_assignment_current_version(message: str) -> int:
    text = str(message or "").strip().lower()
    if "stale assignment_version" not in text:
        return 0
    match = re.search(r"stale assignment_version:\s*\d+\s*<\s*current\s*(\d+)", text)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except Exception:
        return 0


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
        "provider": _normalize_agent_provider(str(provider or profile_item.get("provider") or "codex")),
        "label": str(label or profile).strip()[:120],
        "notes": str(notes or profile_item.get("notes") or "").strip()[:500],
        "fingerprint": sha,
        "updated_at": now_ts,
    }


def _safe_percent(value: Any) -> Optional[float]:
    try:
        val = float(value)
    except Exception:
        return None
    if val < 0:
        val = 0.0
    if val > 100:
        val = 100.0
    return round(val, 2)


def _quota_view_from_rate_limits(rate_limits: Dict[str, Any], updated_at: int = 0) -> Dict[str, Any]:
    limits = rate_limits if isinstance(rate_limits, dict) else {}

    def _one(key: str) -> Dict[str, Any]:
        node = limits.get(key) if isinstance(limits.get(key), dict) else {}
        used = _safe_percent(node.get("usedPercent"))
        remaining = round(max(0.0, 100.0 - used), 2) if used is not None else None
        try:
            resets_at = int(node.get("resetsAt") or 0)
        except Exception:
            resets_at = 0
        return {
            "used_pct": used,
            "remaining_pct": remaining,
            "resets_at": resets_at,
            "window_mins": int(node.get("windowDurationMins") or 0) if str(node.get("windowDurationMins") or "").strip() else 0,
        }

    primary = _one("primary")
    secondary = _one("secondary")
    return {
        "primary": primary,
        "secondary": secondary,
        "remaining_5h_pct": primary.get("remaining_pct"),
        "remaining_7d_pct": secondary.get("remaining_pct"),
        "updated_at": int(updated_at or 0),
        "plan_type": str(limits.get("planType") or "").strip(),
    }


def _quota_view_from_profile_item(item: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return _quota_view_from_rate_limits({}, updated_at=0)
    limits = item.get("last_rate_limits") if isinstance(item.get("last_rate_limits"), dict) else {}
    updated_at = int(item.get("last_rate_limits_at") or item.get("last_health_check_at") or 0)
    return _quota_view_from_rate_limits(limits, updated_at=updated_at)


def _collect_auth_control_node_snapshots(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    for node in nodes:
        node_id = str(node.get("id") or "").strip()
        node_label = str(node.get("label") or node_id).strip()
        try:
            payload = _call_node_api(node, "GET", "/auth/profiles", None)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            profiles = data.get("profiles") if isinstance(data.get("profiles"), list) else []
            items: List[Dict[str, Any]] = []
            for raw in profiles:
                if not isinstance(raw, dict):
                    continue
                profile = str(raw.get("profile") or "").strip()
                if not profile:
                    continue
                items.append(
                    {
                        "profile": profile,
                        "email": str(raw.get("email") or "").strip(),
                        "valid": bool(raw.get("valid")),
                        "status": str(raw.get("status") or "").strip(),
                        "reason": str(raw.get("reason") or "").strip(),
                        "check_required": bool(raw.get("check_required")),
                        "last_health_check_at": int(raw.get("last_health_check_at") or 0),
                        "quota": _quota_view_from_profile_item(raw),
                    }
                )
            out.append(
                {
                    "node_id": node_id,
                    "label": node_label,
                    "ok": True,
                    "error": "",
                    "checked_at": now_ts,
                    "profiles": items,
                    "profile_count": len(items),
                }
            )
        except Exception as exc:
            out.append(
                {
                    "node_id": node_id,
                    "label": node_label,
                    "ok": False,
                    "error": str(exc)[:1000],
                    "checked_at": now_ts,
                    "profiles": [],
                    "profile_count": 0,
                }
            )
    return out


def _auth_control_registry_state() -> Dict[str, Any]:
    nodes = _parse_auth_control_nodes()
    profiles = _refresh_auth_profiles()
    pending_profiles = _list_pending_auth_profiles()
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        audit = registry.get("audit") if isinstance(registry.get("audit"), list) else []
    now_ts = int(time.time())
    node_snapshots = _collect_auth_control_node_snapshots(nodes)
    snapshot_by_node: Dict[str, Dict[str, Any]] = {
        str(item.get("node_id") or ""): dict(item) for item in node_snapshots if isinstance(item, dict)
    }
    node_profiles_index: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for snap in node_snapshots:
        node_id = str(snap.get("node_id") or "").strip()
        pmap: Dict[str, Dict[str, Any]] = {}
        for profile_item in list(snap.get("profiles") or []):
            if not isinstance(profile_item, dict):
                continue
            profile = str(profile_item.get("profile") or "").strip()
            if profile:
                pmap[profile] = dict(profile_item)
        node_profiles_index[node_id] = pmap

    local_profiles: Dict[str, Dict[str, Any]] = {}
    for item in profiles:
        if not isinstance(item, dict):
            continue
        profile = str(item.get("profile") or "").strip()
        if profile:
            local_profiles[profile] = dict(item)

    pending_by_profile: Dict[str, Dict[str, Any]] = {}
    for item in pending_profiles:
        if not isinstance(item, dict):
            continue
        profile = str(item.get("profile") or "").strip()
        if profile:
            pending_by_profile[profile] = dict(item)

    all_profiles: set[str] = set(local_profiles.keys())
    all_profiles.update(str(k or "").strip() for k in pending_by_profile.keys())
    all_profiles.update(str(k or "").strip() for k in auths.keys())
    all_profiles.update(str(k or "").strip() for k in assignments.keys())
    for pmap in node_profiles_index.values():
        all_profiles.update(str(k or "").strip() for k in pmap.keys())
    all_profiles.discard("")

    merged_auths: List[Dict[str, Any]] = []
    node_public = [{k: v for k, v in node.items() if k != "api_token"} for node in nodes]
    environments = [{"id": "local", "label": "本机", "base_url": "", "enabled": True}] + node_public
    for profile in sorted(all_profiles):
        pending_item = pending_by_profile.get(profile) if isinstance(pending_by_profile.get(profile), dict) else {}
        local_item = local_profiles.get(profile) if isinstance(local_profiles.get(profile), dict) else {}
        merged = dict(auths.get(profile) if isinstance(auths.get(profile), dict) else {})
        if local_item:
            merged.update(
                _merge_auth_control_auth_meta(
                    local_item,
                    provider=str(merged.get("provider") or ""),
                    label=str(merged.get("label") or ""),
                    notes=str(merged.get("notes") or ""),
                )
            )
        merged["profile"] = profile
        merged["label"] = str(merged.get("label") or profile).strip()[:120]
        merged["provider"] = _normalize_agent_provider(str(merged.get("provider") or "codex"))
        merged["notes"] = str(merged.get("notes") or "").strip()[:500]
        merged["email"] = str((local_item or {}).get("email") or merged.get("email") or "").strip()
        merged["pool"] = {
            "exists": bool(pending_item),
            "status": str((pending_item or {}).get("status") or "pending").strip(),
            "reason": str((pending_item or {}).get("reason") or "").strip(),
            "source_auth_json": str((pending_item or {}).get("source_auth_json") or "").strip(),
            "source_config_toml": str((pending_item or {}).get("source_config_toml") or "").strip(),
        }
        if not str(merged.get("email") or "").strip():
            merged["email"] = str((pending_item or {}).get("email") or "").strip()
        merged["local"] = {
            "exists": bool(local_item),
            "valid": bool((local_item or {}).get("valid")),
            "status": str((local_item or {}).get("status") or ""),
            "reason": str((local_item or {}).get("reason") or ""),
            "check_required": bool((local_item or {}).get("check_required")),
            "last_health_check_at": int((local_item or {}).get("last_health_check_at") or 0),
            "quota": _quota_view_from_profile_item(local_item or {}),
        }

        assignment = assignments.get(profile) if isinstance(assignments.get(profile), dict) else {}
        merged["assignment"] = dict(assignment or {})
        lease_expire_at = int((assignment or {}).get("lease_expire_at") or 0)
        merged["lease_remaining_sec"] = max(0, lease_expire_at - now_ts) if lease_expire_at > 0 else 0

        node_rows: List[Dict[str, Any]] = []
        for node in node_public:
            node_id = str(node.get("id") or "").strip()
            node_label = str(node.get("label") or node_id).strip()
            snap = snapshot_by_node.get(node_id) if isinstance(snapshot_by_node.get(node_id), dict) else {}
            remote_item = (
                node_profiles_index.get(node_id, {}).get(profile)
                if isinstance(node_profiles_index.get(node_id), dict)
                else {}
            )
            present = bool(remote_item)
            row: Dict[str, Any] = {
                "node_id": node_id,
                "label": node_label,
                "ok": bool(snap.get("ok")),
                "error": str(snap.get("error") or "").strip(),
                "present": present,
            }
            if present:
                row.update(
                    {
                        "email": str(remote_item.get("email") or "").strip(),
                        "valid": bool(remote_item.get("valid")),
                        "status": str(remote_item.get("status") or "").strip(),
                        "reason": str(remote_item.get("reason") or "").strip(),
                        "check_required": bool(remote_item.get("check_required")),
                        "last_health_check_at": int(remote_item.get("last_health_check_at") or 0),
                        "quota": dict(remote_item.get("quota") or {}),
                    }
                )
            node_rows.append(row)
        if not str(merged.get("email") or "").strip():
            for row in node_rows:
                remote_email = str(row.get("email") or "").strip()
                if remote_email:
                    merged["email"] = remote_email
                    break
        merged["nodes"] = node_rows
        merged["discovered_remote"] = any(bool(item.get("present")) for item in node_rows)

        assigned_node = str((assignment or {}).get("node_id") or "").strip()
        merged["assigned_node"] = assigned_node
        if (not pending_item) and (not local_item) and (not merged.get("discovered_remote")) and (not assigned_node):
            continue
        if assigned_node:
            merged["group"] = "assigned"
        elif bool(local_item):
            merged["group"] = "assigned"
        elif bool(pending_item):
            merged["group"] = "pool"
        else:
            merged["group"] = "assigned"
        merged_auths.append(merged)
    return {
        "timestamp": now_ts,
        "nodes": node_public,
        "environments": environments,
        "pending_dir": str(AUTH_PENDING_PROFILES_DIR),
        "node_snapshots": node_snapshots,
        "auths": sorted(
            merged_auths,
            key=lambda x: (
                0 if str(x.get("group") or "") == "pool" else 1,
                str(x.get("profile") or ""),
            ),
        ),
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
    clean_provider = _normalize_agent_provider(provider)
    requested_profile = str(profile or "").strip()
    resolved_profile = requested_profile
    auth_text = _normalize_auth_json_text(auth_json)
    try:
        parsed_auth = json.loads(auth_text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"auth_json is not valid json: {exc}") from exc
    if not isinstance(parsed_auth, dict):
        raise HTTPException(status_code=400, detail="auth_json must be a JSON object")

    if clean_provider == "codex":
        ident = _auth_identity_from_auth_json(parsed_auth)
        existing = _find_existing_profile_for_identity(
            email=str(ident.get("email") or "").strip(),
            sub=str(ident.get("sub") or "").strip(),
        )
        if existing:
            resolved_profile = existing
        elif not resolved_profile:
            resolved_profile = _auto_profile_alias(
                email=str(ident.get("email") or "").strip(),
                filename="auth.json",
                used=_collect_known_profile_names(),
            )
    elif not resolved_profile:
        raise HTTPException(status_code=400, detail="profile is required for non-codex provider")

    if not resolved_profile:
        raise HTTPException(status_code=400, detail="profile is required")

    item = _install_auth_profile(
        profile=resolved_profile,
        provider=clean_provider,
        auth_json=parsed_auth,
        config_toml=config_toml,
        assignment_version=0,
        assignment_token="",
        assigned_server_id="",
        notes=notes,
    )
    meta = _merge_auth_control_auth_meta(item, provider=clean_provider, label=label, notes=notes)
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
        _audit_auth_control(
            registry,
            action="upload",
            profile=name,
            ok=True,
            message=f"profile uploaded/updated requested={requested_profile or '-'} resolved={name}",
        )
        _save_auth_control_registry(registry)
    return {
        "profile": name,
        "requested_profile": requested_profile,
        "resolved_profile": name,
        "meta": meta,
        "health": item,
    }


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
    target = nodes.get(target_node) if target_node != "local" else None
    if target_node != "local" and not target:
        raise HTTPException(status_code=404, detail=f"node not found or disabled: {target_node}")
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
        existing_auth = auths.get(name) if isinstance(auths.get(name), dict) else {}
        provider = _normalize_agent_provider(str(existing_auth.get("provider") or "codex"))
        merged_notes = str(notes or existing_auth.get("notes") or "").strip()[:500]

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

    source = _load_auth_payload_from_any_store(name)
    auth_payload = source.get("auth_json") if isinstance(source.get("auth_json"), dict) else {}
    cfg_text = str(source.get("config_toml") or "")
    if target_node != "local":
        tried_stale_recover = False
        while True:
            try:
                _call_node_api(
                    target,
                    "POST",
                    "/auth/profiles/upload",
                    {
                        "profile": name,
                        "provider": provider,
                        "auth_json": auth_payload,
                        "config_toml": cfg_text,
                        "assignment_version": version,
                        "assignment_token": token,
                        "assigned_server_id": target_node,
                        "notes": merged_notes,
                    },
                )
                break
            except Exception as exc:
                # Recover from remote fencing drift: bump local version and retry once.
                stale_current = _extract_stale_assignment_current_version(str(exc))
                if (not tried_stale_recover) and stale_current > 0 and stale_current >= int(version):
                    version = int(stale_current) + 1
                    token = secrets.token_hex(24)
                    tried_stale_recover = True
                    LOG.warning(
                        "auth assign stale fencing recovered profile=%s node=%s local_version_bumped_to=%s",
                        name,
                        target_node,
                        version,
                    )
                    continue
                raise
        _save_backup_profile(name, auth_payload, config_toml=cfg_text)
        _remove_pending_profile_artifacts(name)
        _remove_auth_profile_artifacts(name)
    else:
        _install_auth_profile(
            profile=name,
            provider=provider,
            auth_json=auth_payload,
            config_toml=cfg_text,
            assignment_version=0,
            assignment_token="",
            assigned_server_id="",
            notes=merged_notes,
        )
        _remove_pending_profile_artifacts(name)
        _save_backup_profile(name, auth_payload, config_toml=cfg_text)

    now_ts = int(time.time())
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        if not existing_auth:
            local_item = _get_auth_profile(name) if target_node == "local" else {}
            existing_auth = _merge_auth_control_auth_meta(local_item if isinstance(local_item, dict) else {}, provider=provider)
            existing_auth["created_at"] = now_ts
        existing_auth["profile"] = name
        existing_auth["updated_at"] = now_ts
        existing_auth["provider"] = provider
        existing_auth["notes"] = merged_notes
        auths[name] = existing_auth
        assignments[name] = {
            "profile": name,
            "node_id": target_node,
            "state": "assigned",
            "version": version,
            "token": token,
            "lease_sec": lease if target_node != "local" else 0,
            "lease_expire_at": (now_ts + lease) if target_node != "local" else 0,
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
    if target_node == "local":
        _mark_auth_profile_unchecked(name, reason="新分配，待检测")
    return {
        "profile": name,
        "node_id": target_node,
        "version": version,
        "lease_expire_at": (now_ts + lease) if target_node != "local" else 0,
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
    try:
        source = _load_auth_payload_from_any_store(name)
        auth_payload = source.get("auth_json") if isinstance(source.get("auth_json"), dict) else {}
        cfg_text = str(source.get("config_toml") or "")
        _save_pending_profile(name, auth_payload, config_toml=cfg_text)
        _save_backup_profile(name, auth_payload, config_toml=cfg_text)
    except Exception as exc:
        LOG.warning("revoke load-source failed profile=%s err=%s", name, exc)
    _remove_auth_profile_artifacts(name)
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


def _auth_control_remove_remote(profile: str, node_id: str, reason: str = "") -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    target_node = str(node_id or "").strip()
    if not target_node or target_node == "local":
        raise HTTPException(status_code=400, detail="node_id must be a remote node")
    nodes = _auth_control_nodes_map()
    node = nodes.get(target_node)
    if not node:
        raise HTTPException(status_code=404, detail=f"node not found or disabled: {target_node}")

    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        current = assignments.get(name) if isinstance(assignments.get(name), dict) else {}
        current_node = str(current.get("node_id") or "").strip()
        if current_node == target_node:
            version = int(current.get("version") or 0) + 1
            token = secrets.token_hex(24)
        else:
            version = 0
            token = ""

    _call_node_api(
        node,
        "POST",
        "/auth/profiles/remove",
        {
            "profile": name,
            "assignment_version": int(version),
            "assignment_token": str(token),
            "assigned_server_id": target_node,
            "reason": str(reason or "manual remove invalid"),
        },
    )
    now_ts = int(time.time())
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        current = assignments.get(name) if isinstance(assignments.get(name), dict) else {}
        if str(current.get("node_id") or "").strip() == target_node:
            assignments[name] = {
                "profile": name,
                "node_id": "",
                "state": "unassigned",
                "version": int(version),
                "token": str(token),
                "lease_sec": 0,
                "lease_expire_at": 0,
                "updated_at": now_ts,
                "last_error": str(reason or "remote removed").strip(),
            }
        registry["assignments"] = assignments
        _audit_auth_control(
            registry,
            action="remove_remote",
            profile=name,
            node_id=target_node,
            ok=True,
            message=str(reason or "manual remove invalid"),
        )
        _save_auth_control_registry(registry)
    return {"profile": name, "node_id": target_node, "removed": True, "updated_at": now_ts}


def _auth_control_remove_local(profile: str, reason: str = "") -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        current = assignments.get(name) if isinstance(assignments.get(name), dict) else {}
        current_node = str(current.get("node_id") or "").strip()
    if current_node == "local":
        return _auth_control_revoke(profile=name, reason=str(reason or "remove local assigned profile"))

    src = AUTH_PROFILES_DIR / f"{name}.auth.json"
    cfg = AUTH_PROFILES_DIR / f"{name}.config.toml"
    if src.exists() and src.is_file():
        try:
            payload = json.loads(src.read_text(encoding="utf-8"))
            cfg_text = cfg.read_text(encoding="utf-8") if cfg.exists() and cfg.is_file() else ""
            _save_backup_profile(name, payload if isinstance(payload, dict) else {}, config_toml=cfg_text)
        except Exception as exc:
            LOG.warning("remove local keep backup failed profile=%s err=%s", name, exc)

    removed = _remove_auth_profile_artifacts(name)
    now_ts = int(time.time())
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        _audit_auth_control(
            registry,
            action="remove_local",
            profile=name,
            node_id="local",
            ok=True,
            message=str(reason or "manual remove local"),
        )
        _save_auth_control_registry(registry)
    return {"profile": name, "removed_local": bool(removed), "updated_at": now_ts}


def _auth_control_remove_pool(profile: str, reason: str = "") -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        current = assignments.get(name) if isinstance(assignments.get(name), dict) else {}
        current_node = str(current.get("node_id") or "").strip()
    if current_node:
        raise HTTPException(status_code=409, detail=f"profile is assigned to {current_node}, move/revoke first")
    src = AUTH_PROFILES_DIR / f"{name}.auth.json"
    if src.exists() and src.is_file():
        raise HTTPException(status_code=409, detail="profile still exists on local node, remove local copy first")

    removed_pending = _remove_pending_profile_artifacts(name)
    removed_backup = _remove_backup_profile_artifacts(name)
    now_ts = int(time.time())
    with _AUTH_CONTROL_LOCK:
        registry = _load_auth_control_registry()
        assignments = registry.get("assignments") if isinstance(registry.get("assignments"), dict) else {}
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
        current = assignments.get(name) if isinstance(assignments.get(name), dict) else {}
        if str(current.get("node_id") or "").strip():
            raise HTTPException(status_code=409, detail=f"profile is assigned to {current.get('node_id')}, move/revoke first")
        assignments.pop(name, None)
        auths.pop(name, None)
        registry["assignments"] = assignments
        registry["auths"] = auths
        _audit_auth_control(
            registry,
            action="remove_pool",
            profile=name,
            node_id="pool",
            ok=True,
            message=str(reason or "manual remove pool profile"),
        )
        _save_auth_control_registry(registry)
    return {
        "profile": name,
        "removed_pending": bool(removed_pending),
        "removed_backup": bool(removed_backup),
        "updated_at": now_ts,
    }


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
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            profiles = data.get("profiles") if isinstance(data.get("profiles"), list) else []
            items: List[Dict[str, Any]] = []
            for item in profiles:
                if not isinstance(item, dict):
                    continue
                profile_name = str(item.get("profile") or "").strip()
                if (not profile_name) or (name and profile_name != name):
                    continue
                items.append(
                    {
                        "profile": profile_name,
                        "email": str(item.get("email") or "").strip(),
                        "valid": bool(item.get("valid")),
                        "status": str(item.get("status") or "").strip(),
                        "reason": str(item.get("reason") or "").strip(),
                        "check_required": bool(item.get("check_required")),
                        "last_health_check_at": int(item.get("last_health_check_at") or 0),
                        "quota": _quota_view_from_profile_item(item),
                    }
                )
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


def _probe_status_from_probe(probe: Dict[str, Any]) -> Dict[str, Any]:
    payload = probe if isinstance(probe, dict) else {}
    ok = bool(payload.get("ok"))
    status = str(payload.get("status") or "").strip().lower()
    message = str(payload.get("error") or "").strip()
    if ok:
        return {"ok": True, "status": "active", "reason": ""}
    classified = _classify_auth_error(message)
    if classified:
        return {"ok": False, "status": classified, "reason": message}
    if status and status not in {"completed", "active"}:
        return {"ok": False, "status": status, "reason": message or f"probe status={status}"}
    return {"ok": False, "status": "failed", "reason": message or "probe failed"}


def _probe_from_non_local_store(profile: str, mode: str, prompt: str = "", timeout_sec: int = 0) -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    source = _load_auth_payload_from_any_store(name)
    auth_payload = source.get("auth_json") if isinstance(source.get("auth_json"), dict) else {}
    cfg_text = str(source.get("config_toml") or "")
    source_type = str(source.get("source") or "").strip()
    identity = _auth_identity_from_auth_json(auth_payload if isinstance(auth_payload, dict) else {})
    temp_seed = f"probe_{name}_{int(time.time() * 1000)}_{secrets.token_hex(3)}"
    temp_profile = _sanitize_auth_profile_name(temp_seed)
    probe: Dict[str, Any] = {}
    try:
        _install_auth_profile(
            profile=temp_profile,
            provider="codex",
            auth_json=auth_payload,
            config_toml=cfg_text,
            assignment_version=0,
            assignment_token="",
            assigned_server_id="",
            notes="temp probe",
        )
        if _is_real_turn_health_mode(mode):
            probe = _real_turn_probe_auth_profile(profile=temp_profile, prompt=prompt, timeout_sec=timeout_sec)
        else:
            probe = _quick_quota_probe_auth_profile(profile=temp_profile)
    finally:
        _remove_auth_profile_artifacts(temp_profile)
        _refresh_auth_profiles()
    return {
        "source": source_type or "pending",
        "email": str(identity.get("email") or "").strip(),
        "probe": probe if isinstance(probe, dict) else {},
    }


def _auth_control_check_one(profile: str, mode: str = "status", prompt: str = "", timeout_sec: int = 0) -> Dict[str, Any]:
    name = _sanitize_auth_profile_name(profile)
    clean_mode = str(mode or "status").strip().lower() or "status"
    probe_prompt = str(prompt or "").strip()
    probe_timeout = int(timeout_sec or 0)
    now_ts = int(time.time())

    local_view: Dict[str, Any] = {"exists": False}
    pool_view: Dict[str, Any] = {"exists": False}

    local_item = _get_auth_profile(name)
    if isinstance(local_item, dict):
        checked = _health_check_auth_profile_item(item=local_item, mode=clean_mode, prompt=probe_prompt, timeout_sec=probe_timeout)
        local_status = str(checked.get("status") or "").strip().lower()
        local_view = {
            "exists": True,
            "email": str(checked.get("email") or "").strip(),
            "status": str(checked.get("status") or "").strip(),
            "reason": str(checked.get("reason") or checked.get("last_health_error") or "").strip(),
            "check_required": bool(checked.get("check_required")),
            "last_health_check_at": int(checked.get("last_health_check_at") or 0),
            "quota": _quota_view_from_profile_item(checked if isinstance(checked, dict) else {}),
            "ok": (not bool(checked.get("check_required"))) and (local_status == "active"),
            "source": "local",
        }
    else:
        pending_src, _ = _pending_auth_paths(name)
        # Do not probe backup copies in check-one:
        # backup may be stale and can produce false "failed" results that
        # override healthy remote-node status in UI summaries.
        if pending_src.exists() and pending_src.is_file():
            non_local = _probe_from_non_local_store(name, mode=clean_mode, prompt=probe_prompt, timeout_sec=probe_timeout)
            probe = non_local.get("probe") if isinstance(non_local.get("probe"), dict) else {}
            summary = _probe_status_from_probe(probe if isinstance(probe, dict) else {})
            checked_at = int(probe.get("checked_at") or now_ts)
            pool_view = {
                "exists": True,
                "source": str(non_local.get("source") or "pending"),
                "email": str(non_local.get("email") or "").strip(),
                "status": str(summary.get("status") or "").strip(),
                "reason": str(summary.get("reason") or "").strip(),
                "ok": bool(summary.get("ok")),
                "check_required": not bool(summary.get("ok")),
                "last_health_check_at": checked_at,
                "quota": _quota_view_from_rate_limits(
                    probe.get("rate_limits") if isinstance(probe.get("rate_limits"), dict) else {},
                    updated_at=checked_at,
                ),
                "probe": probe,
            }

    remote_probe = _auth_control_health_check(
        profile=name,
        node_id="",
        mode=clean_mode,
        prompt=probe_prompt,
        timeout_sec=probe_timeout,
    )
    remote_nodes_raw = remote_probe.get("nodes") if isinstance(remote_probe.get("nodes"), list) else []
    node_label_map: Dict[str, str] = {}
    for node in _parse_auth_control_nodes():
        if not isinstance(node, dict):
            continue
        nid = str(node.get("id") or "").strip()
        if nid:
            node_label_map[nid] = str(node.get("label") or nid).strip()
    remote_nodes: List[Dict[str, Any]] = []
    for node in remote_nodes_raw:
        if not isinstance(node, dict):
            continue
        nid = str(node.get("node_id") or "").strip()
        row: Dict[str, Any] = {
            "node_id": nid,
            "label": node_label_map.get(nid) or nid,
            "api_ok": bool(node.get("ok")),
            "error": str(node.get("error") or "").strip(),
            "present": False,
            "ok": False,
            "status": "",
            "reason": "",
            "check_required": False,
            "quota": _quota_view_from_rate_limits({}, updated_at=0),
            "email": "",
        }
        profiles = node.get("profiles") if isinstance(node.get("profiles"), list) else []
        for item in profiles:
            if not isinstance(item, dict):
                continue
            if str(item.get("profile") or "").strip() != name:
                continue
            row["present"] = True
            row["status"] = str(item.get("status") or "").strip()
            row["reason"] = str(item.get("reason") or "").strip()
            row["check_required"] = bool(item.get("check_required"))
            row["email"] = str(item.get("email") or "").strip()
            row["quota"] = dict(item.get("quota") or {})
            row["ok"] = (
                bool(node.get("ok"))
                and (not bool(item.get("check_required")))
                and str(item.get("status") or "").strip().lower() == "active"
            )
            break
        remote_nodes.append(row)

    if (not bool(local_view.get("exists"))) and (not bool(pool_view.get("exists"))):
        if not any(bool(item.get("present")) for item in remote_nodes):
            raise HTTPException(status_code=404, detail=f"profile not found in local/pending/remote: {name}")

    return {
        "timestamp": now_ts,
        "profile": name,
        "mode": clean_mode,
        "local": local_view,
        "pool": pool_view,
        "remote_nodes": remote_nodes,
    }


def _is_real_turn_health_mode(mode: str) -> bool:
    clean = str(mode or "").strip().lower()
    return clean in {"real", "real_turn", "turn", "conversation", "chat"}


def _quick_quota_probe_auth_profile(profile: str) -> Dict[str, Any]:
    clean = str(profile or "").strip()
    provider = _provider_for_profile(clean)
    if provider in {"claude", "claude_code", "openclaw"}:
        # Claude-like providers do not expose Codex-style quota APIs; use a lightweight real-turn probe.
        return _real_turn_probe_auth_profile(profile=clean, prompt="只回复OK", timeout_sec=min(45, AUTH_REAL_HEALTH_CHECK_TIMEOUT_SEC))
    runtime_id = f"diag_auth_quick_{clean or 'default'}_{int(time.time() * 1000)}"
    runtime = RUNTIMES.get(runtime_id)
    done_error = ""
    done_rate_limits: Dict[str, Any] = {}
    try:
        with runtime.lock:
            runtime.last_input_at = int(time.time())
            runtime.cwd = DEFAULT_CWD
            runtime.model = DEFAULT_MODEL
            runtime.sandbox = DEFAULT_SANDBOX
            runtime.approval_policy = DEFAULT_APPROVAL
            runtime.personality = DEFAULT_PERSONALITY
            _switch_runtime_auth_profile(
                runtime,
                profile=clean,
                reason="history auth quick quota-check",
                allow_invalid=True,
            )
            _ensure_thread(runtime, reset_thread=True)
        try:
            done_rate_limits = _extract_rate_limits_payload(runtime.client.get_account_rate_limits())
            if not done_rate_limits:
                done_rate_limits = _extract_rate_limits_payload(runtime.client.account_rate_limits_read())
        except Exception as exc:
            done_error = str(exc)
            done_rate_limits = {}
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
                    "last_turn_id": "",
                    "last_turn_at": int(time.time()),
                    "last_error": done_error[:1200] if done_error else "",
                },
            )
    ok = not bool(done_error)
    return {
        "ok": ok,
        "status": "completed" if ok else "failed",
        "error": done_error[:1200] if done_error else "",
        "assistant_text": "",
        "rate_limits": dict(done_rate_limits or {}),
        "thread_id": "",
        "turn_id": "",
        "checked_at": int(time.time()),
        "mode": "quick_quota",
    }


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
    done_rate_limits: Dict[str, Any] = {}
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
        try:
            done_rate_limits = _extract_rate_limits_payload(runtime.client.get_account_rate_limits())
            if not done_rate_limits:
                done_rate_limits = _extract_rate_limits_payload(runtime.client.account_rate_limits_read())
        except Exception:
            done_rate_limits = {}
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
        "rate_limits": dict(done_rate_limits or {}),
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
        "check_required": False,
    }
    probe_limits = probe.get("rate_limits")
    if isinstance(probe_limits, dict) and probe_limits:
        patch["last_rate_limits"] = dict(probe_limits)
        patch["last_rate_limits_at"] = now_ts
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
        elif classified == "temp_disabled":
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
        else:
            # Non-auth failures (for example transient transport disconnects) should not disable the profile.
            reason = message[:1200] if message else f"真实对话检测失败：status={status or 'failed'}"
            patch.update({"check_required": True, "last_health_error": reason})
    _patch_auth_registry_profile(clean, patch)
    refreshed = _auth_registry_by_profile().get(clean)
    payload = dict(refreshed or {"profile": clean})
    payload["last_probe"] = probe
    return payload


def _apply_quick_probe_result(profile: str, probe: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    clean = str(profile or "").strip()
    prior = dict(previous or {})
    now_ts = int(probe.get("checked_at") or time.time())
    patch: Dict[str, Any] = {
        "last_health_check_at": now_ts,
        "updated_at": now_ts,
    }
    probe_limits = probe.get("rate_limits")
    if isinstance(probe_limits, dict) and probe_limits:
        patch["last_rate_limits"] = dict(probe_limits)
        patch["last_rate_limits_at"] = now_ts

    if bool(probe.get("ok")):
        has_quota = isinstance(probe_limits, dict) and bool(probe_limits)
        patch["check_required"] = not has_quota
        if (not bool(prior.get("needs_reauth"))) and (not bool(prior.get("risk_deactivated"))):
            patch["valid"] = True
            patch["status"] = "active"
            patch["reason"] = "" if has_quota else "快检未刷新到额度，请稍后重试或改用慢检。"
            patch["disabled_until"] = 0
            patch["disabled_reason"] = ""
            patch["last_health_error"] = "" if has_quota else "quick_quota probe returned no rate_limits payload"
    else:
        message = str(probe.get("error") or "").strip()
        prior_status = str(prior.get("status") or "").strip().lower()
        patch["check_required"] = True
        if prior_status not in {"needs_reauth", "deactivated", "temp_disabled"}:
            patch["status"] = "unknown"
        if message:
            patch["reason"] = message[:1200]
            patch["last_health_error"] = message[:1200]

    _patch_auth_registry_profile(clean, patch)
    refreshed = _auth_registry_by_profile().get(clean)
    payload = dict(refreshed or {"profile": clean})
    payload["last_probe"] = probe
    return payload


def _quick_probe_should_escalate_to_real(probe: Dict[str, Any], previous: Dict[str, Any]) -> bool:
    payload = probe if isinstance(probe, dict) else {}
    prior = previous if isinstance(previous, dict) else {}
    message = str(payload.get("error") or "").strip().lower()
    has_limits = isinstance(payload.get("rate_limits"), dict) and bool(payload.get("rate_limits"))
    probe_ok = bool(payload.get("ok"))

    if probe_ok and (not has_limits):
        return True

    if (not probe_ok) and message:
        if (
            "token_expired" in message
            or "unauthorized" in message
            or "401" in message
            or "invalid_grant" in message
            or "refresh token has expired" in message
            or "refresh token was already used" in message
            or "access token could not be refreshed" in message
            or "login required" in message
            or "reauth" in message
        ):
            return True
        if _classify_auth_error(message) in {"needs_reauth", "deactivated"}:
            return True

    prior_status = str(prior.get("status") or "").strip().lower()
    if prior_status in {"needs_reauth", "deactivated"}:
        return False
    return False


def _health_check_auth_profile_item(item: Dict[str, Any], mode: str, prompt: str = "", timeout_sec: int = 0) -> Dict[str, Any]:
    profile = str((item or {}).get("profile") or "").strip()
    if not _is_real_turn_health_mode(mode):
        probe = _quick_quota_probe_auth_profile(profile=profile)
        previous = item if isinstance(item, dict) else {}
        if _quick_probe_should_escalate_to_real(probe, previous):
            real_probe = _real_turn_probe_auth_profile(profile=profile, prompt=prompt, timeout_sec=timeout_sec)
            result = _apply_health_probe_result(profile, real_probe)
            result["quick_probe"] = probe
            result["escalated_from_quick"] = True
            return result
        return _apply_quick_probe_result(profile, probe, previous)
    probe = _real_turn_probe_auth_profile(profile=profile, prompt=prompt, timeout_sec=timeout_sec)
    return _apply_health_probe_result(profile, probe)


def _get_auth_profile(profile: str) -> Optional[Dict[str, Any]]:
    target = str(profile or "").strip()
    for item in _refresh_auth_profiles():
        if str(item.get("profile") or "").strip() == target:
            return item
    return None


def _provider_for_profile(profile: str) -> str:
    clean = str(profile or "").strip()
    if not clean:
        return "codex"
    local = _get_auth_profile(clean)
    if isinstance(local, dict):
        provider = _normalize_agent_provider(str(local.get("provider") or ""))
        if provider:
            return provider
    try:
        registry = _load_auth_control_registry()
        auths = registry.get("auths") if isinstance(registry.get("auths"), dict) else {}
        item = auths.get(clean) if isinstance(auths.get(clean), dict) else {}
        provider = _normalize_agent_provider(str(item.get("provider") or ""))
        if provider:
            return provider
    except Exception:
        pass
    return "codex"


def _list_switchable_auth_profiles() -> List[Dict[str, Any]]:
    now_ts = int(time.time())
    items = [item for item in _refresh_auth_profiles() if _auth_profile_available(item, now_ts=now_ts)]
    if not items:
        return []
    with_headroom = []
    for item in items:
        limits = item.get("last_rate_limits") if isinstance(item.get("last_rate_limits"), dict) else {}
        if not _rate_limit_exhausted(limits):
            with_headroom.append(item)
    # Prefer accounts with remaining quota; fallback to general available list.
    return with_headroom or items


def _profile_recent_ready_ts(meta: Dict[str, Any]) -> int:
    if not isinstance(meta, dict):
        return 0
    success_ts = int(meta.get("last_used_success_at") or 0)
    status = str(meta.get("status") or "").strip().lower()
    health_ok_ts = int(meta.get("last_health_check_at") or 0) if status == "active" else 0
    return max(success_ts, health_ok_ts)


def _pick_preferred_auth_profile(exclude_profile: str = "") -> Optional[Dict[str, Any]]:
    excluded = str(exclude_profile or "").strip()
    candidates = [item for item in _list_switchable_auth_profiles() if str(item.get("profile") or "").strip()]
    if excluded:
        candidates = [item for item in candidates if str(item.get("profile") or "").strip() != excluded]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            _profile_recent_ready_ts(item),
            int(item.get("last_used_success_at") or 0),
            int(item.get("last_health_check_at") or 0),
            int(item.get("updated_at") or 0),
            str(item.get("profile") or ""),
        ),
        reverse=True,
    )
    return candidates[0]


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


def _claude_runtime_env_for_profile(profile: str) -> Dict[str, str]:
    payload = _load_profile_auth_json(profile)
    return _extract_claude_profile_env(payload if isinstance(payload, dict) else {})


def _profile_default_model(profile: str, provider: str) -> str:
    clean_provider = _normalize_agent_provider(provider)
    if clean_provider in {"claude", "claude_code", "openclaw"}:
        return str(_claude_runtime_env_for_profile(profile).get("BRIDGE_CLAUDE_DEFAULT_MODEL") or "").strip()
    return ""


def _looks_like_codex_or_openai_model(model: str) -> bool:
    clean = str(model or "").strip().lower()
    if not clean:
        return True
    if clean == str(DEFAULT_MODEL or "").strip().lower():
        return True
    if "codex" in clean:
        return True
    if clean.startswith("gpt-"):
        return True
    return False


def _looks_like_claude_model(model: str) -> bool:
    clean = str(model or "").strip().lower()
    if not clean:
        return False
    if clean.startswith("claude"):
        return True
    if clean.startswith("anthropic/claude"):
        return True
    if any(token in clean for token in ("sonnet", "haiku", "opus")):
        return True
    return False


def _sync_runtime_model_from_profile(runtime: ChatRuntime, force: bool = False) -> None:
    provider = _normalize_agent_provider(runtime.agent_provider)
    if provider == "codex":
        if force or _looks_like_claude_model(runtime.model):
            runtime.model = str(DEFAULT_MODEL or "").strip() or "gpt-5.3-codex"
        return
    if provider not in {"claude", "claude_code", "openclaw"}:
        return
    profile_model = _profile_default_model(runtime.auth_profile, provider)
    if not profile_model:
        profile_model = str(runtime.client.env.get("BRIDGE_CLAUDE_DEFAULT_MODEL") or os.environ.get("BRIDGE_CLAUDE_DEFAULT_MODEL") or "").strip()
    if not profile_model:
        profile_model = "claude-sonnet-4-20250514"
    if force or _looks_like_codex_or_openai_model(runtime.model):
        runtime.model = profile_model


def _apply_runtime_auth_profile(runtime: ChatRuntime) -> None:
    provider = _normalize_agent_provider(runtime.agent_provider)
    runtime.client.env.pop("CODEX_HOME", None)
    runtime.client.env.pop("ANTHROPIC_API_KEY", None)
    runtime.client.env.pop("ANTHROPIC_BASE_URL", None)
    runtime.client.env.pop("BRIDGE_CLAUDE_DEFAULT_MODEL", None)
    if provider == "codex":
        target_home = _sync_runtime_home(runtime)
        runtime.client.env["CODEX_HOME"] = str(target_home)
    elif provider in {"claude", "claude_code", "openclaw"}:
        claude_env = _claude_runtime_env_for_profile(runtime.auth_profile)
        for key, value in claude_env.items():
            clean = str(value or "").strip()
            if clean:
                runtime.client.env[key] = clean
    _apply_runtime_bridge_env(runtime)


def _switch_runtime_auth_profile(
    runtime: ChatRuntime,
    profile: str,
    reason: str = "",
    allow_invalid: bool = False,
) -> Dict[str, Any]:
    target = str(profile or "").strip()
    meta = _get_auth_profile(target) if target else {"profile": "", "email": "", "home_dir": ""}
    if target and (not meta or ((not bool(allow_invalid)) and (not bool(meta.get("valid"))))):
        raise HTTPException(status_code=400, detail=f"invalid auth profile: {target}")
    target_provider = _provider_for_profile(target)
    try:
        next_adapter = _build_agent_adapter(target_provider)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"unsupported provider for profile={target}: {target_provider}") from exc

    previous = str(runtime.auth_profile or "").strip()
    previous_provider = _normalize_agent_provider(runtime.agent_provider)
    previous_thread_id = str(runtime.thread_id or "").strip()
    profile_thread_ids = _normalize_profile_thread_ids(runtime.profile_thread_ids)
    if previous_thread_id:
        previous_key = _profile_thread_map_key(previous_provider, previous)
        profile_thread_ids[previous_key] = previous_thread_id
    target_key = _profile_thread_map_key(target_provider, target)
    # Switching profile should prioritize the current thread continuity.
    # Fallback to target profile's pinned thread only when current thread is empty.
    restored_thread_id = previous_thread_id or str(profile_thread_ids.get(target_key) or "").strip()
    try:
        runtime.client.stop()
    except Exception:
        pass
    runtime.client = next_adapter
    runtime.agent_provider = _normalize_agent_provider(target_provider)
    runtime.auth_profile = target
    runtime.profile_thread_ids = profile_thread_ids
    _sync_runtime_model_from_profile(runtime, force=True)
    runtime.thread_id = restored_thread_id
    runtime.active_turn_id = ""
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
            "last_auto_auth_switch_provider_from": previous_provider,
            "last_auto_auth_switch_provider_to": target_provider,
            "last_auto_auth_switch_restored_thread_id": restored_thread_id,
        },
    )
    LOG.info(
        "runtime auth switched chat_id=%s from_profile=%s to_profile=%s provider=%s model=%s restored_thread=%s reason=%s",
        runtime.chat_id,
        previous or "default",
        target or "default",
        _normalize_agent_provider(target_provider),
        str(runtime.model or ""),
        restored_thread_id or "-",
        str(reason or ""),
    )
    return {
        "from": previous,
        "to": target,
        "provider_from": previous_provider,
        "provider_to": target_provider,
        "identity": str((meta or {}).get("email") or (meta or {}).get("sub") or ""),
        "home_dir": str((meta or {}).get("home_dir") or ""),
        "restored_thread_id": restored_thread_id,
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


def _ensure_runtime_preferred_auth_profile(runtime: ChatRuntime, reason: str = "") -> Optional[Dict[str, Any]]:
    current = str(runtime.auth_profile or "").strip()
    if current:
        return None
    target = _pick_preferred_auth_profile()
    if not target:
        return None
    profile = str(target.get("profile") or "").strip()
    if not profile:
        return None
    return _switch_runtime_auth_profile(runtime, profile=profile, reason=reason or "bootstrap prefer recent active profile")


def _ensure_thread_info(runtime: ChatRuntime, reset_thread: bool = False) -> Dict[str, Any]:
    prev_thread_id = str(runtime.thread_id or "").strip()
    info: Dict[str, Any] = {
        "thread_id": "",
        "previous_thread_id": prev_thread_id,
        "reset_thread": bool(reset_thread),
        "client_started": False,
        "resume_attempted": False,
        "resumed": False,
        "resume_failed": False,
        "created_new": False,
    }

    if reset_thread:
        runtime.thread_id = ""
        runtime.active_turn_id = ""

    if runtime.thread_id and runtime.is_client_running():
        info["thread_id"] = str(runtime.thread_id or "")
        return info

    if not runtime.is_client_running():
        runtime.client.start()
        info["client_started"] = True

    if runtime.thread_id:
        info["resume_attempted"] = True
        try:
            runtime.client.thread_resume(
                thread_id=runtime.thread_id,
                cwd=runtime.cwd,
                model=runtime.model,
                sandbox=runtime.sandbox,
                approval_policy=runtime.approval_policy,
            )
            info["resumed"] = True
            info["thread_id"] = str(runtime.thread_id or "")
            return info
        except Exception as exc:
            info["resume_failed"] = True
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
    info["created_new"] = True
    info["thread_id"] = runtime.thread_id
    _persist_runtime(runtime)
    return info


def _ensure_thread(runtime: ChatRuntime, reset_thread: bool = False) -> str:
    return str(_ensure_thread_info(runtime, reset_thread=reset_thread).get("thread_id") or "")


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
            "agent_provider": _normalize_agent_provider(str(runtime.agent_provider or persisted.get("agent_provider") or "codex")),
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
            "profiles": profiles
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


@ROUTER.post("/auth/control/upload-batch", dependencies=[Depends(require_api_token)])
def auth_control_upload_batch(body: AuthControlBatchUploadRequest) -> Dict[str, Any]:
    data = _auth_control_batch_upload(provider=body.provider, items=body.items, notes=body.notes)
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


@ROUTER.post("/auth/control/remove-remote", dependencies=[Depends(require_api_token)])
def auth_control_remove_remote(body: AuthControlRemoveRemoteRequest) -> Dict[str, Any]:
    data = _auth_control_remove_remote(profile=body.profile, node_id=body.node_id, reason=body.reason)
    return {"ok": True, "data": data}


@ROUTER.post("/auth/control/remove-local", dependencies=[Depends(require_api_token)])
def auth_control_remove_local(body: AuthControlRemoveLocalRequest) -> Dict[str, Any]:
    data = _auth_control_remove_local(profile=body.profile, reason=body.reason)
    return {"ok": True, "data": data}


@ROUTER.post("/auth/control/remove-pool", dependencies=[Depends(require_api_token)])
def auth_control_remove_pool(body: AuthControlRemovePoolRequest) -> Dict[str, Any]:
    data = _auth_control_remove_pool(profile=body.profile, reason=body.reason)
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


@ROUTER.post("/auth/control/check-one", dependencies=[Depends(require_api_token)])
def auth_control_check_one(body: AuthControlCheckOneRequest) -> Dict[str, Any]:
    data = _auth_control_check_one(
        profile=body.profile,
        mode=body.mode,
        prompt=body.prompt,
        timeout_sec=int(body.timeout_sec or 0),
    )
    return {"ok": True, "data": data}


@ROUTER.post("/auth/control/reauth/start", dependencies=[Depends(require_api_token)])
def auth_control_reauth_start(body: AuthControlReauthStartRequest) -> Dict[str, Any]:
    data = _auth_control_reauth_start(profile=body.profile, node_id=body.node_id)
    return {"ok": True, "data": data}


@ROUTER.post("/auth/control/reauth/status", dependencies=[Depends(require_api_token)])
def auth_control_reauth_status(body: AuthControlReauthStatusRequest) -> Dict[str, Any]:
    data = _auth_control_reauth_status(request_id=body.request_id, node_id=body.node_id)
    return {"ok": True, "data": data}


@ROUTER.post("/auth/control/reauth/cancel", dependencies=[Depends(require_api_token)])
def auth_control_reauth_cancel(body: AuthControlReauthCancelRequest) -> Dict[str, Any]:
    data = _auth_control_reauth_cancel(request_id=body.request_id, node_id=body.node_id)
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
    turn_record_id = ""
    checkpoint_stop: Optional[threading.Event] = None
    checkpoint_thread: Optional[threading.Thread] = None
    turn_cwd = ""
    turn_model = ""
    turn_auth_profile = ""
    visible_user_text = str(body.text or "")
    turn_input_text = visible_user_text
    with runtime.lock:
        runtime.last_input_at = int(time.time())
        _resolve_chat_config(runtime, body)
        bootstrap_auth_switch = _ensure_runtime_preferred_auth_profile(
            runtime, reason="turn bootstrap prefer recent active profile"
        )
        if isinstance(bootstrap_auth_switch, dict):
            auto_auth_switch = dict(bootstrap_auth_switch)
        _apply_runtime_auth_profile(runtime)
        heal_info = _maybe_self_heal_disconnected_runtime(runtime)
        turn_cwd = str(runtime.cwd or DEFAULT_CWD)
        turn_model = str(runtime.model or DEFAULT_MODEL)
        turn_auth_profile = str(runtime.auth_profile or "")
        project_name = str(_runtime_project_name(runtime.chat_id) or _project_label_for_cwd(turn_cwd) or "").strip()
        is_resume_hint = _looks_like_resume_prompt(visible_user_text)
        had_live_client_before = runtime.is_client_running()
        memory_hint = bool(body.reset_thread) or is_resume_hint or bool((heal_info or {}).get("healed"))
        preflight_limits = _read_rate_limits(runtime, allow_request=runtime.is_client_running())
        if not preflight_limits:
            persisted = STORE.get_chat(chat_id)
            persisted_profile = str(persisted.get("last_rate_limits_profile") or "")
            if persisted_profile == turn_auth_profile and isinstance(persisted.get("last_rate_limits"), dict):
                preflight_limits = dict(persisted.get("last_rate_limits") or {})
        if _rate_limit_exhausted(preflight_limits):
            auto_auth_switch = _maybe_auto_switch_auth_profile(runtime, reason="preflight rate limit exhausted")
        try:
            ensure_info = _ensure_thread_info(runtime, reset_thread=bool(body.reset_thread))
            thread_id = str(ensure_info.get("thread_id") or "")
        except AppServerError as exc:
            state = _persist_runtime(runtime, {"last_error": str(exc), "last_turn_status": "failed"})
            raise HTTPException(
                status_code=502,
                detail={"ok": False, "error": str(exc), "thread_id": runtime.thread_id, "state": state},
            ) from exc
        created_new_thread = bool(ensure_info.get("created_new"))
        previous_thread_id = str(ensure_info.get("previous_thread_id") or "").strip()
        recovered_after_restart = bool(
            created_new_thread
            and (
                bool(previous_thread_id)
                or bool(ensure_info.get("resume_attempted"))
                or bool(ensure_info.get("resume_failed"))
                or bool(ensure_info.get("client_started"))
                or (not had_live_client_before)
            )
        )
        should_inject_memory = bool(
            AUTO_MEMORY_INJECT_ENABLED
            and (
                memory_hint
                or recovered_after_restart
                or (created_new_thread and not previous_thread_id)
            )
        )
        if recovered_after_restart:
            LOG.info(
                "auto memory inject trigger: rebuilt thread after restart/resume failure chat_id=%s prev_thread=%s new_thread=%s",
                runtime.chat_id,
                previous_thread_id or "",
                thread_id,
            )
        if should_inject_memory:
            prefix = _build_auto_memory_prefix(project=project_name, user_text=visible_user_text)
            if prefix:
                turn_input_text = (
                    f"{prefix}\n\n[Current User Message]\n{visible_user_text}\n\n"
                    "请基于以上历史信息继续，并优先延续之前未完成的工作。"
                )
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
        turn_record_id = f"{turn_started_at}_{runtime.chat_id}_{turn_id or 'no_turn'}"
        _persist_runtime(runtime, {"last_user_text": visible_user_text, "last_error": ""})

    def _flush_turn_checkpoint(
        status: str,
        assistant_text: str = "",
        error_text: str = "",
        ended_at: int = 0,
        token_usage: Optional[Dict[str, Any]] = None,
        rate_limits: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not turn_id:
            return
        try:
            HISTORY_STORE.append_turn(
                _build_turn_record(
                    runtime=runtime,
                    turn_id=turn_id,
                    status=status,
                    started_at=turn_started_at,
                    ended_at=(ended_at or int(time.time())),
                    user_text=visible_user_text,
                    assistant_text=assistant_text,
                    error_text=error_text,
                    thread_id=thread_id,
                    cwd=turn_cwd,
                    model=turn_model,
                    auth_profile=turn_auth_profile,
                    token_usage=token_usage,
                    rate_limits=rate_limits,
                    record_id=turn_record_id,
                )
            )
        except Exception as exc:
            LOG.warning("checkpoint turn persist failed chat_id=%s turn_id=%s err=%s", runtime.chat_id, turn_id, exc)

    checkpoint_stop = threading.Event()
    _flush_turn_checkpoint(status="running")

    def _checkpoint_loop() -> None:
        while checkpoint_stop and not checkpoint_stop.wait(TURN_CHECKPOINT_INTERVAL_SEC):
            _flush_turn_checkpoint(status="running")

    checkpoint_thread = threading.Thread(target=_checkpoint_loop, daemon=True)
    checkpoint_thread.start()

    try:
        done = runtime.client.wait_for_turn_completion(
            thread_id=thread_id,
            turn_id=turn_id,
            timeout_sec=int(body.timeout_sec),
        )
        if checkpoint_stop:
            checkpoint_stop.set()
        if checkpoint_thread:
            checkpoint_thread.join(timeout=0.3)
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
            if turn_auth_profile:
                profile_usage_patch: Dict[str, Any] = {"last_used_at": int(time.time())}
                if str(done.turn_status or "").strip().lower() == "completed":
                    profile_usage_patch["last_used_success_at"] = int(time.time())
                _patch_auth_registry_profile(turn_auth_profile, profile_usage_patch)
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
        _flush_turn_checkpoint(
            status=done.turn_status,
            assistant_text=done.text,
            error_text=json.dumps(done.error, ensure_ascii=False) if done.error else "",
            ended_at=int(time.time()),
            token_usage=token_usage,
            rate_limits=rate_limits,
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
        if checkpoint_stop:
            checkpoint_stop.set()
        if checkpoint_thread:
            checkpoint_thread.join(timeout=0.3)
        with runtime.lock:
            active_now = str(runtime.client.get_active_turn_id(thread_id) or runtime.active_turn_id or "")
            runtime.active_turn_id = active_now
            state = _persist_runtime(runtime, {"last_error": str(exc), "last_turn_status": "timeout"})
        _flush_turn_checkpoint(status="timeout", error_text=str(exc), ended_at=int(time.time()))
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
        if checkpoint_stop:
            checkpoint_stop.set()
        if checkpoint_thread:
            checkpoint_thread.join(timeout=0.3)
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
                # Preserve thread id across bridge/app-server disconnects so restart can resume.
                runtime.thread_id = str(runtime.thread_id or thread_id or "")
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
        _flush_turn_checkpoint(status="failed", error_text=err_text, ended_at=int(time.time()))
        raise HTTPException(
            status_code=502,
            detail={"ok": False, "error": err_text, "thread_id": runtime.thread_id, "state": state},
        ) from exc
    except Exception:
        if checkpoint_stop:
            checkpoint_stop.set()
        if checkpoint_thread:
            checkpoint_thread.join(timeout=0.3)
        raise


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
    limit: int = Query(default=8, ge=1, le=100),
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
    data_items: List[Dict[str, Any]] = []
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


@APP.post("/history/api/auth/control/upload-batch")
def history_auth_control_upload_batch_api(
    body: AuthControlBatchUploadRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_batch_upload(provider=body.provider, items=body.items, notes=body.notes)
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


@APP.post("/history/api/auth/control/remove-remote")
def history_auth_control_remove_remote_api(
    body: AuthControlRemoveRemoteRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_remove_remote(profile=body.profile, node_id=body.node_id, reason=body.reason)
    return JSONResponse({"ok": True, "data": data})


@APP.post("/history/api/auth/control/remove-local")
def history_auth_control_remove_local_api(
    body: AuthControlRemoveLocalRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_remove_local(profile=body.profile, reason=body.reason)
    return JSONResponse({"ok": True, "data": data})


@APP.post("/history/api/auth/control/remove-pool")
def history_auth_control_remove_pool_api(
    body: AuthControlRemovePoolRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_remove_pool(profile=body.profile, reason=body.reason)
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


@APP.post("/history/api/auth/control/check-one")
def history_auth_control_check_one_api(
    body: AuthControlCheckOneRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_check_one(
        profile=body.profile,
        mode=body.mode,
        prompt=body.prompt,
        timeout_sec=int(body.timeout_sec or 0),
    )
    return JSONResponse({"ok": True, "data": data})


@APP.post("/history/api/auth/control/reauth/start")
def history_auth_control_reauth_start_api(
    body: AuthControlReauthStartRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_reauth_start(profile=body.profile, node_id=body.node_id)
    return JSONResponse({"ok": True, "data": data})


@APP.post("/history/api/auth/control/reauth/status")
def history_auth_control_reauth_status_api(
    body: AuthControlReauthStatusRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_reauth_status(request_id=body.request_id, node_id=body.node_id)
    return JSONResponse({"ok": True, "data": data})


@APP.post("/history/api/auth/control/reauth/cancel")
def history_auth_control_reauth_cancel_api(
    body: AuthControlReauthCancelRequest,
    request: Request,
    token: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    _history_access_guard(request, token=token, authorization=authorization, require_session=False)
    data = _auth_control_reauth_cancel(request_id=body.request_id, node_id=body.node_id)
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
    .muted {{ color: #64748b; font-size: 12px; }}
    .drop-zone {{ border: 2px dashed #a9bdd8; border-radius: 10px; padding: 16px; background: #f8fbff; text-align: center; }}
    .drop-zone.dragover {{ border-color: #1d4ed8; background: #eef4ff; }}
    .pool-list {{ display: grid; gap: 8px; }}
    .pool-item {{ border: 1px solid #d7e3f2; border-radius: 8px; padding: 8px; background: #fbfdff; cursor: grab; }}
    .env-grid {{ display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-top: 8px; }}
    .env-drop {{ border: 1px dashed #b8c8df; border-radius: 8px; min-height: 72px; padding: 8px; background: #f9fbff; }}
    .env-drop.over {{ border-color: #1d4ed8; background: #eef4ff; }}
    .board-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .board-col {{ border: 1px solid #d9e2ec; border-radius: 10px; background: #fbfdff; min-height: 120px; }}
    .board-col.over {{ border-color: #1d4ed8; background: #eef4ff; }}
    .board-col h4 {{ margin: 0; padding: 10px; border-bottom: 1px solid #e6edf7; font-size: 14px; }}
    .board-body {{ padding: 8px; display: grid; gap: 8px; }}
    .auth-item {{ position: relative; overflow: hidden; border: 1px solid #d7e3f2; border-radius: 8px; padding: 8px; background: #fff; cursor: grab; }}
    .provider-ribbon {{
      position: absolute;
      top: 8px;
      right: -34px;
      width: 120px;
      transform: rotate(38deg);
      transform-origin: center;
      text-align: center;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.4px;
      line-height: 18px;
      color: #fff;
      pointer-events: none;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.28);
    }}
    .provider-ribbon-codex {{ background: linear-gradient(90deg, #2563eb, #1d4ed8); }}
    .provider-ribbon-claude {{ background: linear-gradient(90deg, #ea580c, #c2410c); }}
    .provider-ribbon-other {{ background: linear-gradient(90deg, #475569, #334155); }}
    .auth-head {{ display: flex; justify-content: space-between; gap: 6px; align-items: center; }}
    .tag-dup {{ font-size: 10px; border-radius: 999px; padding: 2px 6px; background: #fff1f2; color: #b91c1c; border: 1px solid #fecdd3; }}
    .status-pill {{ display: inline-block; font-size: 10px; border-radius: 999px; padding: 2px 6px; margin-top: 4px; }}
    .status-running {{ background: #dbeafe; color: #1d4ed8; }}
    .status-ok {{ background: #dcfce7; color: #166534; }}
    .status-bad {{ background: #fee2e2; color: #991b1b; }}
    .mini {{ font-size: 11px; padding: 4px 6px; border-radius: 6px; }}
    .node-chip {{ display: inline-block; margin: 2px 4px 2px 0; padding: 3px 7px; border-radius: 999px; font-size: 11px; border: 1px solid #d6e4ff; background: #f7faff; }}
    input, select, button {{ font: inherit; padding: 8px; border-radius: 8px; border: 1px solid #c5d2e0; }}
    button {{ background: #1d4ed8; color: #fff; border: 0; cursor: pointer; }}
    button:disabled {{ opacity: .65; cursor: not-allowed; }}
    button.alt {{ background: #64748b; }}
    button.danger {{ background: #b91c1c; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e5edf5; text-align: left; padding: 8px; vertical-align: top; }}
    code {{ background: #f2f7ff; padding: 2px 6px; border-radius: 6px; }}
    #log {{ white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; background: #09111f; color: #d6e2ff; padding: 10px; border-radius: 8px; max-height: 220px; overflow: auto; }}
    .flow-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .flow-field {{ display: grid; gap: 6px; }}
    .flow-field label {{ font-size: 12px; color: #475569; }}
    .device-code-panel {{ margin-top: 10px; border: 1px solid #cfe0ff; background: #f5f9ff; border-radius: 10px; padding: 10px; }}
    .device-code-value {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 18px; font-weight: 700; letter-spacing: 1px; color: #0f172a; }}
  </style>
</head>
<body>
  <header>Auth 集中管理（池中 / 已分配）</header>
  <main>
    <section class="card">
      <div class="row">
        <button onclick="refreshState()">刷新状态</button>
        <select id="hc_mode">
          <option value="status">快检：Codex 刷额度 / Claude 轻量对话</option>
          <option value="real">慢检：真实对话可用性（更慢）</option>
        </select>
        <button id="btn_health_check" onclick="healthCheck()">一键巡检</button>
        <span class="muted">待分配池目录：<code id="pending_dir">-</code></span>
      </div>
      <div id="summary" style="margin-top:8px;color:#4b5563;"></div>
    </section>

    <section class="card">
      <h3>账号接入流程（统一新增 / 重新登录）</h3>
      <div class="flow-grid">
        <div class="flow-field">
          <label for="flow_mode">目标操作</label>
          <select id="flow_mode" onchange="updateAuthFlowForm()">
            <option value="new">新增账号</option>
            <option value="existing">重新登录（更新已有账号）</option>
          </select>
        </div>
        <div class="flow-field">
          <label for="flow_method">授权方式</label>
          <select id="flow_method" onchange="updateAuthFlowForm()">
            <option value="device">设备码授权登录</option>
            <option value="upload">上传 auth.json</option>
            <option value="claude">Claude Code 配置</option>
          </select>
        </div>
        <div class="flow-field" id="flow_existing_wrap" style="display:none;">
          <label for="flow_target_profile">选择已有账号</label>
          <select id="flow_target_profile" onchange="updateAuthFlowForm()"></select>
        </div>
        <div class="flow-field" id="flow_new_profile_wrap">
          <label for="flow_new_profile">新账号 profile（可选）</label>
          <input id="flow_new_profile" placeholder="留空自动命名；若与已有账号同身份会自动覆盖已有账号" />
        </div>
        <div class="flow-field" id="flow_env_wrap">
          <label for="flow_env">登录执行环境</label>
          <select id="flow_env"></select>
          <div id="flow_env_auto" class="muted" style="display:none;">自动关联设备：-</div>
        </div>
        <div class="flow-field" id="flow_claude_wrap" style="display:none;">
          <label for="flow_claude_api_key">ANTHROPIC_API_KEY</label>
          <input id="flow_claude_api_key" type="password" placeholder="必填" />
          <label for="flow_claude_base_url">ANTHROPIC_BASE_URL（可选）</label>
          <input id="flow_claude_base_url" placeholder="可选" />
          <label for="flow_claude_model">默认模型（可选）</label>
          <input id="flow_claude_model" placeholder="可选，例如 claude-sonnet-4-20250514" />
          <label for="flow_claude_notes">备注（可选）</label>
          <input id="flow_claude_notes" placeholder="可选" />
        </div>
        <div class="flow-field" id="flow_upload_wrap" style="display:none;">
          <label for="flow_upload_file">auth.json 文件</label>
          <input id="flow_upload_file" type="file" accept=".json,.auth.json" />
          <label for="flow_upload_provider">Provider（可选）</label>
          <select id="flow_upload_provider">
            <option value="codex">codex</option>
            <option value="openclaw">openclaw</option>
            <option value="claude_code">claude code</option>
          </select>
          <label for="flow_upload_notes">备注（可选）</label>
          <input id="flow_upload_notes" placeholder="可选" />
        </div>
      </div>
      <div class="row" style="margin-top:10px;">
        <button onclick="submitAuthFlow()">提交</button>
      </div>
      <div class="muted" style="margin-top:8px;">新增与重登统一走同一流程；旧账号重登会自动关联到原设备环境。</div>
      <div id="device_code_panel" class="device-code-panel" style="display:none;">
        <div class="row" style="justify-content:space-between;">
          <strong>设备码</strong>
          <span id="device_code_status" class="muted"></span>
        </div>
        <div id="device_code_value" class="device-code-value">-</div>
        <div class="row" style="margin-top:8px;">
          <button class="alt mini" onclick="copyCurrentDeviceCode()">复制设备码</button>
          <button class="mini" onclick="openCurrentVerificationUri()">打开验证页面</button>
        </div>
        <div id="device_code_meta" class="muted" style="margin-top:6px;"></div>
      </div>
    </section>

    <section class="card">
      <h3>池中与各服务器（拖拽迁移）</h3>
      <div id="auth_board" class="board-grid"></div>
    </section>

    <section class="card"><h3>操作日志</h3><div id="log"></div></section>
  </main>
  <script>
    window.__AUTH_CONTROL_CONFIG__ = {page_config};
    const cfg = window.__AUTH_CONTROL_CONFIG__ || {{}};
    const q = cfg.authToken ? `?token=${{encodeURIComponent(cfg.authToken)}}` : '';
    let latestState = null;
    let dragProfile = '';
    let healthCheckRunning = false;
    const reauthPollers = {{}};
    let currentDeviceAuth = {{ requestId: '', profile: '', userCode: '', verificationUri: '', nodeId: 'local' }};

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

    function fmtPct(v) {{
      if (v === null || v === undefined || Number.isNaN(Number(v))) return '-';
      return `${{Number(v).toFixed(1)}}%`;
    }}

    function fmtResetAt(ts) {{
      const n = Number(ts || 0);
      if (!n) return '-';
      try {{
        return new Date(n * 1000).toLocaleString();
      }} catch (_) {{
        return '-';
      }}
    }}

    function fmtWindowMins(mins) {{
      const m = Number(mins || 0);
      if (!m || Number.isNaN(m)) return '';
      if (m % 1440 === 0) return `${{Math.round(m / 1440)}}d`;
      if (m % 60 === 0) return `${{Math.round(m / 60)}}h`;
      return `${{m}}m`;
    }}

    function quotaPiece(node, fallbackLabel) {{
      const n = node || {{}};
      const rem = fmtPct(n.remaining_pct);
      const label = fmtWindowMins(n.window_mins) || String(fallbackLabel || '').trim() || '窗口';
      const reset = fmtResetAt(n.resets_at);
      if (rem === '-' && reset === '-') return '';
      return `${{label}}剩余:${{rem}} 刷新:${{reset}}`;
    }}

    function quotaText(q) {{
      const primary = (q && q.primary) ? q.primary : {{}};
      const secondary = (q && q.secondary) ? q.secondary : {{}};
      const p1 = quotaPiece(primary, '主窗口');
      const p2 = quotaPiece(secondary, '副窗口');
      if (!p1 && !p2) return '额度: 未上报';
      if (p1 && p2) return `${{p1}} · ${{p2}}`;
      return p1 || p2;
    }}

    function localStatusText(local) {{
      const l = local || {{}};
      if (!l.exists) return '本机无';
      if (l.check_required) return '未检测';
      return l.status || 'unknown';
    }}

    function hasQuotaData(q) {{
      if (!q || typeof q !== 'object') return false;
      const p = q.primary || {{}};
      const s = q.secondary || {{}};
      const a = p.remaining_pct;
      const b = s.remaining_pct;
      return a !== null && a !== undefined || b !== null && b !== undefined;
    }}

    function quotaForEnv(auth, envId) {{
      if (!auth) return {{}};
      if (envId === 'local') return (auth.local && auth.local.quota) ? auth.local.quota : {{}};
      if (envId === 'pool') {{
        const lq = (auth.local && auth.local.quota) ? auth.local.quota : {{}};
        if (hasQuotaData(lq)) return lq;
        const rows = Array.isArray(auth.nodes) ? auth.nodes : [];
        for (const row of rows) {{
          const rq = (row && row.quota) ? row.quota : {{}};
          if (hasQuotaData(rq)) return rq;
        }}
        return lq;
      }}
      const row = findNode(auth, envId) || {{}};
      return (row && row.quota) ? row.quota : {{}};
    }}

    function setHealthCheckBusy(busy) {{
      const btn = document.getElementById('btn_health_check');
      if (!btn) return;
      btn.disabled = !!busy;
      btn.textContent = busy ? '巡检中...' : '一键巡检';
    }}

    const DEFAULT_LEASE_SEC = {AUTH_CONTROL_DEFAULT_LEASE_SEC};
    const opState = {{}};

    function findAuth(profile) {{
      const auths = (latestState && Array.isArray(latestState.auths)) ? latestState.auths : [];
      return auths.find(a => (a.profile || '') === (profile || '')) || null;
    }}

    function findNode(auth, nodeId) {{
      const nodes = (auth && Array.isArray(auth.nodes)) ? auth.nodes : [];
      return nodes.find(n => (n.node_id || '') === (nodeId || '')) || null;
    }}

    function setOp(profile, text, kind='running') {{
      if (!profile) return;
      opState[profile] = {{ text: String(text || ''), kind: String(kind || 'running'), ts: Date.now() }};
      if (latestState) renderBoard(latestState);
    }}

    function cleanupOps() {{
      const now = Date.now();
      for (const k of Object.keys(opState)) {{
        const ts = Number(opState[k]?.ts || 0);
        if (now - ts > 180000) delete opState[k];
      }}
    }}

    function reauthKey(profile, nodeId='local') {{
      return `${{String(profile || '').trim()}}@@${{String(nodeId || 'local').trim() || 'local'}}`;
    }}

    function clearReauthPoller(profile, nodeId='local') {{
      const key = reauthKey(profile, nodeId);
      if (!key) return;
      const timer = reauthPollers[key];
      if (timer) {{
        clearInterval(timer);
        delete reauthPollers[key];
      }}
    }}

    async function copyText(text) {{
      const raw = String(text || '').trim();
      if (!raw) return false;
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        try {{
          await navigator.clipboard.writeText(raw);
          return true;
        }} catch (_) {{}}
      }}
      try {{
        const ta = document.createElement('textarea');
        ta.value = raw;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return !!ok;
      }} catch (_) {{
        return false;
      }}
    }}

    function renderDeviceCodePanel() {{
      const panel = document.getElementById('device_code_panel');
      if (!panel) return;
      if (!currentDeviceAuth.requestId) {{
        panel.style.display = 'none';
        return;
      }}
      panel.style.display = 'block';
      const code = String(currentDeviceAuth.userCode || '').trim();
      const uri = String(currentDeviceAuth.verificationUri || '').trim();
      const profile = String(currentDeviceAuth.profile || '').trim();
      const node = String(currentDeviceAuth.nodeId || 'local').trim() || 'local';
      const status = String(currentDeviceAuth.statusText || '').trim();
      document.getElementById('device_code_value').textContent = code || '-';
      document.getElementById('device_code_status').textContent = status || '等待确认';
      document.getElementById('device_code_meta').textContent = `账号：${{profile || '-'}} | 节点：${{node}} | 验证地址：${{uri || '-'}}`;
    }}

    async function copyCurrentDeviceCode() {{
      const code = String(currentDeviceAuth.userCode || '').trim();
      if (!code) return;
      const ok = await copyText(code);
      appendLog(ok ? '设备码已复制到剪贴板' : '设备码复制失败，请手动复制');
    }}

    function openCurrentVerificationUri() {{
      const url = String(currentDeviceAuth.verificationUri || '').trim();
      if (!url) return;
      window.open(url, '_blank', 'noopener');
    }}

    function clearDeviceCodePanel() {{
      currentDeviceAuth = {{ requestId: '', profile: '', userCode: '', verificationUri: '', nodeId: 'local', statusText: '' }};
      renderDeviceCodePanel();
    }}

    async function startReauth(profile, nodeId='local') {{
      const p = String(profile || '').trim();
      const nid = String(nodeId || '').trim() || 'local';
      setOp(p || '__device_login__', '发起重登中', 'running');
      try {{
        const started = await api('/history/api/auth/control/reauth/start', 'POST', {{
          profile: p,
          node_id: nid === 'local' ? '' : nid,
        }});
        const authProfile = String(started.profile || p || '').trim();
        const url = String(started.verification_uri || '').trim();
        const code = String(started.user_code || '').trim();
        const rid = String(started.request_id || '').trim();
        currentDeviceAuth = {{
          requestId: rid,
          profile: authProfile,
          userCode: code,
          verificationUri: url,
          nodeId: nid,
          statusText: '等待确认',
        }};
        renderDeviceCodePanel();
        const copied = code ? await copyText(code) : false;
        appendLog(`reauth started: profile=${{authProfile}} code=${{code}} request=${{rid}}`);
        if (code) appendLog(copied ? `device code copied: ${{code}}` : `device code copy failed: ${{code}}`);
        setOp(authProfile || '__device_login__', '等待网页确认', 'running');
        clearReauthPoller(authProfile || p, nid);
        let rounds = 0;
        const pollKey = reauthKey(authProfile || p, nid);
        reauthPollers[pollKey] = setInterval(async () => {{
          rounds += 1;
          try {{
            const st = await api('/history/api/auth/control/reauth/status', 'POST', {{
              request_id: rid,
              node_id: nid === 'local' ? '' : nid,
            }});
            const status = String(st.status || '').trim();
            if (status === 'pending') {{
              currentDeviceAuth.statusText = '等待确认';
              renderDeviceCodePanel();
              setOp(authProfile || '__device_login__', '等待网页确认', 'running');
              if (rounds >= 300) {{
                clearReauthPoller(authProfile || p, nid);
                currentDeviceAuth.statusText = '已超时';
                renderDeviceCodePanel();
                setOp(authProfile || '__device_login__', '超时：请重试', 'bad');
              }}
              return;
            }}
            clearReauthPoller(authProfile || p, nid);
            if (status === 'success') {{
              const finalProfile = String(st.profile || authProfile || '').trim();
              currentDeviceAuth.profile = finalProfile || authProfile;
              currentDeviceAuth.statusText = '授权完成';
              renderDeviceCodePanel();
              const resolvedProfile = finalProfile || authProfile;
              setOp(resolvedProfile || '__device_login__', '完成：已更新账号，自动检测中', 'running');
              appendLog(`reauth success: profile=${{resolvedProfile}}`);
              try {{
                const one = await api('/history/api/auth/control/check-one', 'POST', {{
                  profile: resolvedProfile,
                  mode: 'status',
                }});
                const summary = summarizeCheckOne(one);
                setOp(
                  resolvedProfile || '__device_login__',
                  `完成：${{summary.text}}`,
                  summary.ok ? 'ok' : 'bad'
                );
                appendLog(
                  `reauth auto-check: profile=${{resolvedProfile}} result=${{summary.ok ? 'ok' : 'bad'}} detail=${{summary.text}}`
                );
              }} catch (e) {{
                setOp(resolvedProfile || '__device_login__', '完成：已更新账号（自动检测失败）', 'bad');
                appendLog(`reauth auto-check failed: profile=${{resolvedProfile}} err=${{String(e)}}`);
              }}
            }} else if (status === 'cancelled') {{
              currentDeviceAuth.statusText = '已取消';
              renderDeviceCodePanel();
              setOp(authProfile || '__device_login__', '已取消', 'bad');
              appendLog(`reauth cancelled: profile=${{authProfile}}`);
            }} else {{
              const err = String(st.error || '重登失败').trim();
              currentDeviceAuth.statusText = `失败：${{err}}`;
              renderDeviceCodePanel();
              setOp(authProfile || '__device_login__', `失败：${{err}}`, 'bad');
              appendLog(`reauth failed: profile=${{authProfile}} error=${{err}}`);
            }}
            try {{ await refreshState(false); }} catch (_) {{}}
          }} catch (e) {{
            clearReauthPoller(authProfile || p, nid);
            currentDeviceAuth.statusText = `失败：${{String(e)}}`;
            renderDeviceCodePanel();
            setOp(authProfile || '__device_login__', `失败：${{String(e)}}`, 'bad');
          }}
        }}, 2000);
      }} catch (e) {{
        clearDeviceCodePanel();
        setOp(p || '__device_login__', `失败：${{String(e)}}`, 'bad');
      }}
    }}

    function envList(data) {{
      return Array.isArray(data?.environments) ? data.environments : [];
    }}

    function envLabelById(envId) {{
      const id = String(envId || '').trim() || 'local';
      if (id === 'local') return '本机';
      const envs = envList(latestState || {{}});
      const found = envs.find(e => String(e?.id || '').trim() === id);
      return String(found?.label || id).trim() || id;
    }}

    function inEnv(auth, envId) {{
      if (!auth || !envId) return false;
      if (envId === 'pool') return (auth.group || '') === 'pool';
      if ((auth.group || '') === 'pool') return false;
      const asg = String((auth.assignment || {{}}).node_id || '').trim();
      if (envId === 'local') {{
        if (asg && asg !== 'local') return false;
        return !!(auth.local && auth.local.exists);
      }}
      if (asg) return asg === envId;
      const n = findNode(auth, envId);
      return !!(n && n.present);
    }}

    function remoteDupEmailMap(data) {{
      const out = {{}};
      const auths = Array.isArray(data?.auths) ? data.auths : [];
      const remoteEnvs = envList(data).filter(e => (e.id || '') !== 'local');
      for (const a of auths) {{
        const email = String(a?.email || '').trim().toLowerCase();
        if (!email) continue;
        let cnt = 0;
        for (const e of remoteEnvs) {{
          const n = findNode(a, e.id || '');
          if (n && n.present) cnt += 1;
        }}
        if (cnt > 1) out[email] = true;
      }}
      return out;
    }}

    function resolveExistingProfileNode(profile) {{
      const p = String(profile || '').trim();
      if (!p) return 'local';
      const auth = findAuth(p);
      if (!auth) return 'local';
      const assigned = String((auth.assignment || {{}}).node_id || '').trim();
      if (assigned) return assigned;
      if (auth.local && auth.local.exists) return 'local';
      const rows = Array.isArray(auth.nodes) ? auth.nodes : [];
      const present = rows.filter(r => !!r?.present && String(r?.node_id || '').trim());
      if (!present.length) return 'local';
      const active = present.find(r => String(r.status || '').trim().toLowerCase() === 'active');
      return String((active || present[0]).node_id || 'local').trim() || 'local';
    }}

    async function removeFromNode(profile, nodeId) {{
      if (!profile || !nodeId) return;
      setOp(profile, '删除中', 'running');
      try {{
        await api('/history/api/auth/control/remove-remote', 'POST', {{ profile, node_id: nodeId, reason: 'manual cleanup duplicate' }});
        await refreshState(false);
        setOp(profile, '完成：已删除', 'ok');
      }} catch (e) {{
        setOp(profile, `失败：${{String(e)}}`, 'bad');
      }}
    }}

    async function removeLocalCopy(profile) {{
      if (!profile) return;
      setOp(profile, '删除本机中', 'running');
      try {{
        await api('/history/api/auth/control/remove-local', 'POST', {{ profile, reason: 'manual remove local shadow' }});
        await refreshState(false);
        setOp(profile, '完成：本机已删', 'ok');
      }} catch (e) {{
        setOp(profile, `失败：${{String(e)}}`, 'bad');
      }}
    }}

    async function removePoolProfile(profile) {{
      if (!profile) return;
      setOp(profile, '删除池中中', 'running');
      try {{
        await api('/history/api/auth/control/remove-pool', 'POST', {{ profile, reason: 'manual remove pool profile' }});
        await refreshState(false);
        setOp(profile, '完成：池中已删', 'ok');
      }} catch (e) {{
        setOp(profile, `失败：${{String(e)}}`, 'bad');
      }}
    }}

    function summarizeCheckOne(data) {{
      const payload = data || {{}};
      const local = payload.local || {{}};
      const pool = payload.pool || {{}};
      const nodes = Array.isArray(payload.remote_nodes) ? payload.remote_nodes : [];
      if (pool.exists) {{
        return {{
          ok: !!pool.ok,
          text: pool.ok ? '检测通过' : (pool.status || '检测异常'),
        }};
      }}
      if (local.exists) {{
        const ok = (!local.check_required) && String(local.status || '').toLowerCase() === 'active';
        return {{
          ok,
          text: ok ? '检测通过' : (String(local.status || '').trim() || '检测异常'),
        }};
      }}
      const present = nodes.filter(n => !!n.present);
      if (!present.length) {{
        return {{ ok: false, text: '未发现可检测节点' }};
      }}
      const active = present.every(n => String(n.status || '').toLowerCase() === 'active');
      return {{
        ok: active,
        text: active ? '检测通过' : '检测异常',
      }};
    }}

    async function checkAuthOne(profile) {{
      if (!profile) return;
      const mode = (document.getElementById('hc_mode')?.value || 'status').trim();
      setOp(profile, '检测中', 'running');
      try {{
        const data = await api('/history/api/auth/control/check-one', 'POST', {{ profile, mode }});
        await refreshState(false);
        const summary = summarizeCheckOne(data);
        setOp(profile, `完成：${{summary.text}}`, summary.ok ? 'ok' : 'bad');
      }} catch (e) {{
        setOp(profile, `失败：${{String(e)}}`, 'bad');
      }}
    }}

    async function moveAuth(profile, sourceEnv, targetEnv) {{
      if (!profile || !targetEnv || sourceEnv === targetEnv) return;
      const auth = findAuth(profile);
      if (!auth) throw new Error(`未找到 auth: ${{profile}}`);
      setOp(profile, '迁移中', 'running');
      try {{
        if (targetEnv === 'pool') {{
          const asgNode = String((auth.assignment || {{}}).node_id || '').trim();
          if (asgNode && asgNode === sourceEnv) {{
            await api('/history/api/auth/control/revoke', 'POST', {{ profile, reason: `drag:${{sourceEnv}}->pool` }});
          }} else if (sourceEnv === 'local') {{
            await api('/history/api/auth/control/revoke', 'POST', {{ profile, reason: 'drag:local->pool' }});
          }} else {{
            await api('/history/api/auth/control/remove-remote', 'POST', {{ profile, node_id: sourceEnv, reason: 'drag_to_pool' }});
          }}
          setOp(profile, '检测中', 'running');
          try {{ await api('/history/api/auth/control/health-check', 'POST', {{ profile, mode: 'status' }}); }} catch (_) {{}}
          await refreshState(false);
          setOp(profile, '完成：已入池', 'ok');
          return;
        }}

        await api('/history/api/auth/control/assign', 'POST', {{
          profile,
          node_id: targetEnv,
          lease_sec: DEFAULT_LEASE_SEC,
          force: true
        }});
        setOp(profile, '检测中', 'running');
        try {{ await api('/history/api/auth/health-check', 'POST', {{ profile, mode: 'status' }}); }} catch (_) {{}}
        if (targetEnv !== 'local') {{
          try {{ await api('/history/api/auth/control/health-check', 'POST', {{ profile, node_id: targetEnv, mode: 'status' }}); }} catch (_) {{}}
        }}
        await refreshState(false);
        const fresh = findAuth(profile);
        let ok = false;
        if (targetEnv === 'local') {{
          const l = (fresh && fresh.local) ? fresh.local : {{}};
          ok = (!l.check_required) && String(l.status || '').toLowerCase() === 'active';
        }} else {{
          const n = findNode(fresh, targetEnv) || {{}};
          ok = String(n.status || '').toLowerCase() === 'active';
        }}
        setOp(profile, ok ? '完成：检测通过' : '完成：检测异常', ok ? 'ok' : 'bad');
      }} catch (e) {{
        setOp(profile, `失败：${{String(e)}}`, 'bad');
      }}
    }}

    function renderBoard(data) {{
      const board = document.getElementById('auth_board');
      board.innerHTML = '';
      const auths = Array.isArray(data?.auths) ? data.auths : [];
      const cols = [{{ id: 'pool', label: '池中' }}].concat(envList(data));
      const dupMap = remoteDupEmailMap(data);

      for (const col of cols) {{
        const colId = String(col.id || '').trim();
        const colLabel = String(col.label || colId || '').trim();
        const items = auths.filter(a => inEnv(a, colId)).sort((a, b) => String(a.profile||'').localeCompare(String(b.profile||'')));
        const wrap = document.createElement('div');
        wrap.className = 'board-col';
        wrap.innerHTML = `<h4>${{colLabel}} <span class="muted">(${{items.length}})</span></h4><div class="board-body"></div>`;
        const body = wrap.querySelector('.board-body');

        wrap.addEventListener('dragover', (e) => {{
          e.preventDefault();
          wrap.classList.add('over');
        }});
        wrap.addEventListener('dragleave', () => wrap.classList.remove('over'));
        wrap.addEventListener('drop', async (e) => {{
          e.preventDefault();
          wrap.classList.remove('over');
          let payload = null;
          try {{ payload = JSON.parse(e.dataTransfer.getData('text/plain') || '{{}}'); }} catch (_) {{ payload = null; }}
          if (!payload || !payload.profile) return;
          await moveAuth(String(payload.profile || ''), String(payload.source || ''), colId);
        }});

        if (!items.length) {{
          body.innerHTML = '<div class="muted">空</div>';
          board.appendChild(wrap);
          continue;
        }}

        for (const a of items) {{
          const item = document.createElement('div');
          item.className = 'auth-item';
          item.draggable = true;
          item.addEventListener('dragstart', (e) => {{
            dragProfile = String(a.profile || '');
            e.dataTransfer.setData('text/plain', JSON.stringify({{ profile: String(a.profile || ''), source: colId }}));
          }});

          const email = String(a.email || '').trim();
          const emailKey = email.toLowerCase();
          const dup = (colId !== 'pool' && colId !== 'local' && dupMap[emailKey]) ? '<span class="tag-dup">重复邮箱</span>' : '';
          let statusText = '';
          let reasonText = '';
          if (colId === 'pool') {{
            statusText = '待分配';
          }} else if (colId === 'local') {{
            const l = a.local || {{}};
            statusText = localStatusText(l);
            reasonText = String(l.reason || '').trim();
          }} else {{
            const n = findNode(a, colId) || {{}};
            statusText = n.present ? String(n.status || (n.valid ? 'active' : 'unknown')) : '未发现';
            reasonText = String(n.reason || n.error || '').trim();
          }}
          const op = opState[String(a.profile || '')] || null;
          const opHtml = op ? `<div class="status-pill status-${{op.kind||'running'}}">${{op.text||''}}</div>` : '';
          const quota = quotaText(quotaForEnv(a, colId));
          const provider = String(a.provider || 'codex').trim().toLowerCase();
          let providerLabel = 'OTHER';
          let providerTone = 'provider-ribbon-other';
          if (provider === 'codex') {{
            providerLabel = 'CODEX';
            providerTone = 'provider-ribbon-codex';
          }} else if (provider === 'claude_code' || provider === 'claude-code' || provider === 'claude') {{
            providerLabel = 'CLAUDE CODE';
            providerTone = 'provider-ribbon-claude';
          }}
          const providerRibbon = `<div class="provider-ribbon ${{providerTone}}">${{providerLabel}}</div>`;
          item.innerHTML =
            providerRibbon +
            `<div class="auth-head"><code>${{a.profile||''}}</code>${{dup}}</div>` +
            `<div class="muted">${{email||'无邮箱信息'}}</div>` +
            `<div class="muted">状态：${{statusText}}</div>` +
            `<div class="muted">额度：${{quota}}</div>` +
            `${{reasonText ? `<div class="muted">${{reasonText}}</div>` : ''}}` +
            opHtml;

          const act = document.createElement('div');
          act.className = 'row';
          act.style.marginTop = '6px';

          const checkBtn = document.createElement('button');
          checkBtn.className = 'mini';
          checkBtn.textContent = '检测';
          checkBtn.onclick = async () => {{
            await checkAuthOne(String(a.profile || ''));
          }};
          act.appendChild(checkBtn);

          if (colId !== 'pool') {{
            const provider = String(a.provider || 'codex').trim().toLowerCase();
            let needReauth = false;
            if (colId === 'local') {{
              needReauth = String((a.local || {{}}).status || '').trim().toLowerCase() === 'needs_reauth';
            }} else {{
              needReauth = String((findNode(a, colId) || {{}}).status || '').trim().toLowerCase() === 'needs_reauth';
            }}
            if (provider === 'codex' && needReauth) {{
              const reloginBtn = document.createElement('button');
              reloginBtn.className = 'mini';
              reloginBtn.textContent = '重新登录';
              reloginBtn.onclick = async () => {{
                await startReauth(String(a.profile || ''), colId);
              }};
              act.appendChild(reloginBtn);
            }}

            const backBtn = document.createElement('button');
            backBtn.className = 'alt mini';
            backBtn.textContent = '移到池中';
            backBtn.onclick = async () => {{
              await moveAuth(String(a.profile || ''), colId, 'pool');
            }};
            act.appendChild(backBtn);

            if (colId !== 'local') {{
              const delBtn = document.createElement('button');
              delBtn.className = 'danger mini';
              delBtn.textContent = '删除远程';
              delBtn.onclick = async () => {{
                if (!window.confirm(`确认删除远程节点[${{colLabel}}]上的账号 ${{a.profile||''}} 吗？`)) return;
                await removeFromNode(String(a.profile || ''), colId);
              }};
              act.appendChild(delBtn);

              if (a.local && a.local.exists) {{
                const delLocalBtn = document.createElement('button');
                delLocalBtn.className = 'alt mini';
                delLocalBtn.textContent = '删除本机副本';
                delLocalBtn.onclick = async () => {{
                  if (!window.confirm(`确认仅删除本机副本 ${{a.profile||''}} 吗？`)) return;
                  await removeLocalCopy(String(a.profile || ''));
                }};
                act.appendChild(delLocalBtn);
              }}
            }} else {{
              const delLocalBtn = document.createElement('button');
              delLocalBtn.className = 'danger mini';
              delLocalBtn.textContent = '删除本机节点';
              delLocalBtn.onclick = async () => {{
                if (!window.confirm(`确认删除本机节点账号 ${{a.profile||''}} 吗？`)) return;
                await removeLocalCopy(String(a.profile || ''));
              }};
              act.appendChild(delLocalBtn);
            }}
          }} else {{
            const delPoolBtn = document.createElement('button');
            delPoolBtn.className = 'danger mini';
            delPoolBtn.textContent = '删除池中';
            delPoolBtn.onclick = async () => {{
              if (!window.confirm(`确认删除池中账号 ${{a.profile||''}} 吗？`)) return;
              await removePoolProfile(String(a.profile || ''));
            }};
            act.appendChild(delPoolBtn);
          }}
          item.appendChild(act);
          body.appendChild(item);
        }}
        board.appendChild(wrap);
      }}
    }}

    function renderState(data) {{
      latestState = data;
      const auths = Array.isArray(data.auths) ? data.auths : [];
      const pool = auths.filter(a => (a.group || '') === 'pool');
      const assigned = auths.filter(a => (a.group || '') !== 'pool');
      const nodes = Array.isArray(data.nodes) ? data.nodes : [];
      document.getElementById('summary').textContent =
        `Auth总数: ${{auths.length}} | 池中: ${{pool.length}} | 已分配: ${{assigned.length}} | 节点: ${{nodes.length}} | 更新时间: ${{new Date((data.timestamp||0)*1000).toLocaleString()}}`;
      document.getElementById('pending_dir').textContent = data.pending_dir || '-';
      const envSel = document.getElementById('flow_env');
      if (envSel) {{
        const envs = envList(data);
        const options = [{{ id: 'local', label: '本机' }}].concat(envs.filter(e => (e.id || '') !== 'local'));
        const prev = String(envSel.value || 'local').trim() || 'local';
        envSel.innerHTML = options
          .map(e => `<option value="${{String(e.id || '').trim()}}">${{String(e.label || e.id || '').trim()}}</option>`)
          .join('');
        envSel.value = options.some(e => String(e.id||'') === prev) ? prev : 'local';
      }}
      renderFlowTargets(data);
      updateAuthFlowForm();
      renderBoard(data);
    }}

    async function refreshState(withLog=true) {{
      const data = await api('/history/api/auth/control/state');
      renderState(data);
      if (withLog) appendLog('state refreshed');
    }}

    function updateAuthFlowForm() {{
      const mode = String(document.getElementById('flow_mode')?.value || 'new').trim();
      const method = String(document.getElementById('flow_method')?.value || 'device').trim();
      const existingWrap = document.getElementById('flow_existing_wrap');
      const newWrap = document.getElementById('flow_new_profile_wrap');
      const envWrap = document.getElementById('flow_env_wrap');
      const envAuto = document.getElementById('flow_env_auto');
      const claudeWrap = document.getElementById('flow_claude_wrap');
      const uploadWrap = document.getElementById('flow_upload_wrap');
      const selectedExisting = String(document.getElementById('flow_target_profile')?.value || '').trim();
      const autoNode = resolveExistingProfileNode(selectedExisting);
      if (existingWrap) existingWrap.style.display = mode === 'existing' ? '' : 'none';
      if (newWrap) newWrap.style.display = mode === 'new' ? '' : 'none';
      if (envWrap) envWrap.style.display = method === 'device' ? '' : 'none';
      if (envAuto) {{
        if (method === 'device' && mode === 'existing') {{
          envAuto.style.display = '';
          envAuto.textContent = `自动关联设备：${{envLabelById(autoNode)}}`;
        }} else {{
          envAuto.style.display = 'none';
        }}
      }}
      const envSel = document.getElementById('flow_env');
      if (envSel) envSel.disabled = method === 'device' && mode === 'existing';
      if (claudeWrap) claudeWrap.style.display = method === 'claude' ? '' : 'none';
      if (uploadWrap) uploadWrap.style.display = method === 'upload' ? '' : 'none';
    }}

    function renderFlowTargets(data) {{
      const sel = document.getElementById('flow_target_profile');
      if (!sel) return;
      const auths = Array.isArray(data?.auths) ? data.auths : [];
      const options = auths
        .map(a => String(a?.profile || '').trim())
        .filter(Boolean)
        .sort((a, b) => a.localeCompare(b));
      const prev = String(sel.value || '').trim();
      sel.innerHTML = options.map(p => `<option value="${{p}}">${{p}}</option>`).join('');
      if (options.includes(prev)) {{
        sel.value = prev;
      }} else if (options.length) {{
        sel.value = options[0];
      }}
    }}

    async function submitAuthFlow() {{
      const mode = String(document.getElementById('flow_mode')?.value || 'new').trim();
      const method = String(document.getElementById('flow_method')?.value || 'device').trim();
      const existingProfile = String(document.getElementById('flow_target_profile')?.value || '').trim();
      const newProfile = String(document.getElementById('flow_new_profile')?.value || '').trim();
      const profile = mode === 'existing' ? existingProfile : newProfile;

      if (mode === 'existing' && !existingProfile) {{
        throw new Error('请选择要更新的已有账号');
      }}

      if (method === 'device') {{
        const env =
          mode === 'existing'
            ? resolveExistingProfileNode(existingProfile)
            : (String(document.getElementById('flow_env')?.value || 'local').trim() || 'local');
        await startReauth(profile, env);
        appendLog(`device auth started: mode=${{mode}} profile=${{profile || '(auto)'}} env=${{env}} (${{
          envLabelById(env)
        }})`);
        return;
      }}
      if (method === 'upload') {{
        const input = document.getElementById('flow_upload_file');
        const file = input && input.files ? input.files[0] : null;
        if (!file) throw new Error('请先选择 auth.json 文件');
        let parsed = null;
        try {{
          parsed = JSON.parse(await file.text());
        }} catch (e) {{
          throw new Error(`auth.json 解析失败: ${{String(e)}}`);
        }}
        const provider = String(document.getElementById('flow_upload_provider')?.value || 'codex').trim() || 'codex';
        const notes = String(document.getElementById('flow_upload_notes')?.value || '').trim();
        const result = await api('/history/api/auth/control/upload', 'POST', {{
          profile,
          provider,
          auth_json: parsed,
          config_toml: '',
          label: profile,
          notes,
        }});
        const resolved = String(result.profile || result.resolved_profile || profile || '').trim();
        setOp(resolved || '__upload__', '完成：已更新账号', 'ok');
        appendLog(`auth uploaded: mode=${{mode}} provider=${{provider}} requested=${{profile || '(auto)'}} resolved=${{resolved}}`);
        if (input) input.value = '';
        await refreshState(false);
        return;
      }}
      if (method !== 'claude') throw new Error(`不支持的授权方式: ${{method}}`);
      if (!profile) throw new Error('Claude Code 配置需要填写 profile');
      const apiKey = String(document.getElementById('flow_claude_api_key')?.value || '').trim();
      const baseUrl = String(document.getElementById('flow_claude_base_url')?.value || '').trim();
      const model = String(document.getElementById('flow_claude_model')?.value || '').trim();
      const notes = String(document.getElementById('flow_claude_notes')?.value || '').trim();
      if (!apiKey) throw new Error('ANTHROPIC_API_KEY 不能为空');
      const authJson = {{ api_key: apiKey }};
      if (baseUrl) authJson.base_url = baseUrl;
      if (model) authJson.model = model;
      const result = await api('/history/api/auth/control/upload', 'POST', {{
        profile,
        provider: 'claude_code',
        auth_json: authJson,
        config_toml: '',
        label: profile,
        notes,
      }});
      const resolved = String(result.profile || result.resolved_profile || profile || '').trim();
      setOp(resolved || '__upload__', '完成：已更新账号', 'ok');
      appendLog(`claude profile updated: mode=${{mode}} requested=${{profile}} resolved=${{resolved}}`);
      const keyInput = document.getElementById('flow_claude_api_key');
      if (keyInput) keyInput.value = '';
      await refreshState(false);
    }}

    async function healthCheck() {{
      if (healthCheckRunning) return;
      healthCheckRunning = true;
      setHealthCheckBusy(true);
      const mode = (document.getElementById('hc_mode')?.value || 'status').trim();
      try {{
        try {{
          appendLog(`health check started (mode=${{mode}})`);
          await api('/history/api/auth/health-check', 'POST', {{ mode }});
          appendLog('local health check done');
        }} catch (e) {{
          appendLog(`local health check failed: ${{String(e)}}`);
        }}
        try {{
          await api('/history/api/auth/control/health-check', 'POST', {{ mode }});
          appendLog('remote health check done');
        }} catch (e) {{
          appendLog(`remote health check failed: ${{String(e)}}`);
        }}
        try {{
          const auths = Array.isArray(latestState?.auths) ? latestState.auths : [];
          const poolItems = auths.filter(a => (a.group || '') === 'pool');
          for (const a of poolItems) {{
            const profile = String(a.profile || '').trim();
            if (!profile) continue;
            setOp(profile, '检测中', 'running');
            try {{
              const one = await api('/history/api/auth/control/check-one', 'POST', {{ profile, mode }});
              const summary = summarizeCheckOne(one);
              setOp(profile, `完成：${{summary.text}}`, summary.ok ? 'ok' : 'bad');
            }} catch (e) {{
              setOp(profile, `失败：${{String(e)}}`, 'bad');
            }}
          }}
          if (poolItems.length) appendLog(`pool health check done: ${{poolItems.length}}`);
        }} catch (e) {{
          appendLog(`pool health check failed: ${{String(e)}}`);
        }}
        try {{
          await refreshState(false);
        }} catch (e) {{
          appendLog(`refresh failed: ${{String(e)}}`);
        }}
        appendLog(`health check finished (mode=${{mode}})`);
      }} finally {{
        healthCheckRunning = false;
        setHealthCheckBusy(false);
      }}
    }}

    setInterval(() => {{
      cleanupOps();
      if (latestState) renderBoard(latestState);
    }}, 30000);

    (async () => {{
      updateAuthFlowForm();
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
    auth_control_href = "/history/auth-control"
    if str(token or "").strip():
        auth_control_href = f"{auth_control_href}?token={urllib.parse.quote(str(token or '').strip())}"
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
  <style>
    #auth-control-entry {{
      position: fixed;
      top: 12px;
      right: 16px;
      z-index: 9999;
      text-decoration: none;
      font-size: 12px;
      line-height: 1;
      font-weight: 600;
      color: #ffffff;
      background: #0f1b2a;
      border: 1px solid #1f2d40;
      border-radius: 999px;
      padding: 8px 12px;
      box-shadow: 0 4px 14px rgba(15, 27, 42, 0.22);
    }}
    #auth-control-entry:hover {{
      background: #1d4ed8;
      border-color: #1d4ed8;
    }}
  </style>
</head>
<body>
  <a id="auth-control-entry" href="{auth_control_href}">Auth 管理</a>
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
