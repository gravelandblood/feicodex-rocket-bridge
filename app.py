#!/usr/bin/env python3
from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

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
DEFAULT_TURN_TIMEOUT_SEC = int(os.environ.get("BRIDGE_TURN_TIMEOUT_SEC", "180"))

_state_path = Path(STATE_PATH).expanduser()
if not _state_path.is_absolute():
    _state_path = APP_DIR / _state_path
STORE = BridgeStateStore(str(_state_path.resolve()))


class TurnRequest(BaseModel):
    text: str = Field(min_length=1, description="User input text")
    image_paths: list[str] = Field(default_factory=list, description="Optional local image paths")
    cwd: str = Field(default="")
    model: str = Field(default="")
    sandbox: str = Field(default="")
    approval_policy: str = Field(default="")
    personality: str = Field(default="")
    timeout_sec: int = Field(default=DEFAULT_TURN_TIMEOUT_SEC, ge=5, le=1800)
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
            )
            self._runtimes[clean_chat_id] = runtime
            return runtime

    def runtimes_count(self) -> int:
        with self._lock:
            return len(self._runtimes)

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
    }
    if patch:
        data.update(patch)
    return STORE.upsert_chat(runtime.chat_id, data)


def _read_rate_limits(runtime: ChatRuntime) -> Dict[str, Any]:
    cached = runtime.client.get_account_rate_limits()
    if cached:
        return cached
    try:
        read = runtime.client.account_rate_limits_read()
        if isinstance(read.get("rateLimits"), dict):
            return dict(read.get("rateLimits") or {})
    except Exception:
        return {}
    return {}


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
    return {
        "ok": True,
        "service": "feicodex-rocket-bridge",
        "api_prefix": API_PREFIX,
        "active_runtime_chats": RUNTIMES.runtimes_count(),
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
    active_turn_id = str(runtime.active_turn_id or persisted.get("active_turn_id") or "")

    if thread_id and runtime.is_client_running():
        thread_status = runtime.client.get_thread_status(thread_id)
        token_usage = runtime.client.get_thread_token_usage(thread_id)
        rate_limits = _read_rate_limits(runtime)
        if not active_turn_id:
            active_turn_id = runtime.client.get_active_turn_id(thread_id)
    if not rate_limits:
        rate_limits = _read_rate_limits(runtime)
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
            "cwd": str(runtime.cwd or persisted.get("cwd") or DEFAULT_CWD),
            "model": str(runtime.model or persisted.get("model") or DEFAULT_MODEL),
            "sandbox": str(runtime.sandbox or persisted.get("sandbox") or DEFAULT_SANDBOX),
            "approval_policy": str(runtime.approval_policy or persisted.get("approval_policy") or DEFAULT_APPROVAL),
            "personality": str(runtime.personality or persisted.get("personality") or DEFAULT_PERSONALITY),
            "state": persisted,
        },
    }


@ROUTER.post("/chat/{chat_id}/thread/reset", dependencies=[Depends(require_api_token)])
def chat_thread_reset(chat_id: str, body: ResetThreadRequest) -> Dict[str, Any]:
    runtime = RUNTIMES.get(chat_id)
    with runtime.lock:
        _resolve_chat_config(runtime, body)
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
        _resolve_chat_config(runtime, body)
        try:
            thread_id = _ensure_thread(runtime, reset_thread=bool(body.reset_thread))
        except AppServerError as exc:
            state = _persist_runtime(runtime, {"last_error": str(exc), "last_turn_status": "failed"})
            raise HTTPException(
                status_code=502,
                detail={"ok": False, "error": str(exc), "thread_id": runtime.thread_id, "state": state},
            ) from exc
        active_now = str(runtime.active_turn_id or runtime.client.get_active_turn_id(thread_id))
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


@APP.on_event("shutdown")
def _on_shutdown() -> None:
    RUNTIMES.stop_all()
