#!/usr/bin/env python3
from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from appserver_client import AppServerError, AppServerTimeout, CodexAppServerClient
from state_store import BridgeStateStore

LOG = logging.getLogger("feicodex_rocket_bridge")

APP_DIR = Path(__file__).resolve().parent
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
AUTH_REGISTRY_PATH = _resolve_env_path(
    os.environ.get("BRIDGE_AUTH_REGISTRY_PATH", str(DATA_DIR / "auth_profiles_registry.json"))
)


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


def _resolve_chat_config(runtime: ChatRuntime, body: Any) -> None:
    runtime.cwd = str(getattr(body, "cwd", "") or runtime.cwd or DEFAULT_CWD)
    runtime.model = str(getattr(body, "model", "") or runtime.model or DEFAULT_MODEL)
    runtime.sandbox = str(getattr(body, "sandbox", "") or runtime.sandbox or DEFAULT_SANDBOX)
    runtime.approval_policy = str(
        getattr(body, "approval_policy", "") or runtime.approval_policy or DEFAULT_APPROVAL
    )
    runtime.personality = str(getattr(body, "personality", "") or runtime.personality or DEFAULT_PERSONALITY)


def _persist_runtime(runtime: ChatRuntime, patch: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {
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


def _apply_runtime_auth_profile(runtime: ChatRuntime) -> None:
    profile = str(runtime.auth_profile or "").strip()
    if profile:
        runtime.client.env["CODEX_HOME"] = str(_profile_home_dir(profile))
    else:
        runtime.client.env.pop("CODEX_HOME", None)


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
    thread_id = str(runtime.thread_id or persisted.get("thread_id") or "")
    thread_status: Dict[str, Any] = {}
    token_usage: Dict[str, Any] = {}
    rate_limits: Dict[str, Any] = {}
    turn_progress: Dict[str, Any] = {}
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
    if not rate_limits:
        rate_limits = _read_rate_limits(runtime, allow_request=False)
    if not token_usage and isinstance(persisted.get("last_token_usage"), dict):
        token_usage = dict(persisted.get("last_token_usage") or {})
    if not rate_limits and isinstance(persisted.get("last_rate_limits"), dict):
        rate_limits = dict(persisted.get("last_rate_limits") or {})

    return {
        "ok": True,
        "data": {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "active_turn_id": active_turn_id,
            "thread_status": thread_status,
            "token_usage": token_usage,
            "rate_limits": rate_limits,
            "turn_progress": turn_progress,
            "cwd": str(runtime.cwd or persisted.get("cwd") or DEFAULT_CWD),
            "model": str(runtime.model or persisted.get("model") or DEFAULT_MODEL),
            "sandbox": str(runtime.sandbox or persisted.get("sandbox") or DEFAULT_SANDBOX),
            "approval_policy": str(runtime.approval_policy or persisted.get("approval_policy") or DEFAULT_APPROVAL),
            "personality": str(runtime.personality or persisted.get("personality") or DEFAULT_PERSONALITY),
            "auth_profile": auth_profile,
            "auth_identity": str((auth_meta or {}).get("email") or (auth_meta or {}).get("sub") or ""),
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
    meta = _get_auth_profile(profile) if profile else None
    if profile and (not meta or not bool(meta.get("valid"))):
        raise HTTPException(status_code=400, detail=f"invalid auth profile: {profile}")

    with runtime.lock:
        runtime.auth_profile = profile
        runtime.thread_id = ""
        runtime.active_turn_id = ""
        try:
            runtime.client.stop()
        except Exception:
            pass
        _apply_runtime_auth_profile(runtime)
        state = _persist_runtime(runtime, {"last_error": ""})
        return {
            "ok": True,
            "data": {
                "chat_id": chat_id,
                "auth_profile": profile,
                "auth_identity": str((meta or {}).get("email") or (meta or {}).get("sub") or ""),
                "home_dir": str((meta or {}).get("home_dir") or ""),
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
    with runtime.lock:
        runtime.last_input_at = int(time.time())
        _resolve_chat_config(runtime, body)
        _apply_runtime_auth_profile(runtime)
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
                text=body.text,
                image_paths=[str(p) for p in list(body.image_paths or []) if str(p).strip()],
            )
        except AppServerError as exc:
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
        _persist_runtime(runtime, {"last_user_text": str(body.text), "last_error": ""})

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
                "state": state,
            },
        }
    except AppServerTimeout as exc:
        with runtime.lock:
            active_now = str(runtime.client.get_active_turn_id(thread_id) or runtime.active_turn_id or "")
            runtime.active_turn_id = active_now
            state = _persist_runtime(runtime, {"last_error": str(exc), "last_turn_status": "timeout"})
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
        raise HTTPException(
            status_code=502,
            detail={"ok": False, "error": str(exc), "thread_id": runtime.thread_id, "state": state},
        ) from exc


@ROUTER.post("/chat/{chat_id}/turn/steer", dependencies=[Depends(require_api_token)])
def chat_turn_steer(chat_id: str, body: SteerTurnRequest) -> Dict[str, Any]:
    runtime = RUNTIMES.get(chat_id)
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
                text=body.text,
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
        state = _persist_runtime(runtime, {"last_user_text": str(body.text), "last_error": ""})
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


APP.include_router(ROUTER)


@APP.on_event("startup")
def _on_startup() -> None:
    global _IDLE_SWEEPER_THREAD
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
