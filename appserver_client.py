#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


LOG = logging.getLogger("codex_appserver_client")


class AppServerError(RuntimeError):
    pass


class AppServerTimeout(AppServerError):
    pass


class AppServerDisconnected(AppServerError):
    pass


class AppServerProtocolError(AppServerError):
    pass


@dataclass
class TurnRunResult:
    thread_id: str
    turn_id: str
    turn_status: str
    text: str
    error: Optional[Dict[str, Any]]


class CodexAppServerClient:
    def __init__(
        self,
        codex_binary: str = "codex",
        listen_url: str = "stdio://",
        client_name: str = "feicodex-rocket-bridge",
        client_version: str = "0.1.0",
        request_timeout_sec: int = 30,
        env: Optional[Dict[str, str]] = None,
    ):
        self.codex_binary = codex_binary
        self.listen_url = listen_url
        self.client_name = client_name
        self.client_version = client_version
        self.request_timeout_sec = max(1, request_timeout_sec)
        self.env = dict(env or {})

        self.proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self._ready = False

        self._id_lock = threading.Lock()
        self._next_id = 1
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: Dict[str, queue.Queue] = {}
        self._notifications: queue.Queue = queue.Queue()

        self._thread_status_lock = threading.Lock()
        self._thread_status: Dict[str, Dict[str, Any]] = {}
        self._active_turn_by_thread: Dict[str, str] = {}
        self._token_usage_by_thread: Dict[str, Dict[str, Any]] = {}
        self._account_rate_limits: Dict[str, Any] = {}
        self._turn_started_at_by_thread: Dict[str, float] = {}
        self._turn_last_event_at_by_thread: Dict[str, float] = {}
        self._turn_preview_by_thread: Dict[str, str] = {}
        self._turn_events_by_thread: Dict[str, List[Dict[str, Any]]] = {}
        self._last_status_type_by_thread: Dict[str, str] = {}

    def start(self, experimental_api: bool = True) -> Dict[str, Any]:
        if self.proc and self.proc.poll() is None and self._ready:
            return {"userAgent": "already-running"}

        self._closed.clear()
        cmd = [self.codex_binary, "app-server", "--listen", self.listen_url]
        env = None
        if self.env:
            env = os.environ.copy()
            env.update(self.env)

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        if not self.proc.stdin or not self.proc.stdout or not self.proc.stderr:
            raise AppServerDisconnected("Failed to open app-server stdio streams.")

        self._stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        try:
            result = self._request(
                "initialize",
                {
                    "clientInfo": {"name": self.client_name, "version": self.client_version},
                    "capabilities": {"experimentalApi": bool(experimental_api)},
                },
                timeout_sec=10,
                ensure_started=False,
            )
        except Exception:
            self.stop()
            raise
        self._ready = True
        return result

    def stop(self) -> None:
        self._ready = False
        self._closed.set()
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._notify_disconnect()

    def ensure_started(self) -> None:
        if self.proc and self.proc.poll() is None and self._ready:
            return
        self.start()

    def is_running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None and self._ready)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout_sec: Optional[int] = None) -> Dict[str, Any]:
        return self._request(method, params=params, timeout_sec=timeout_sec, ensure_started=True)

    def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_sec: Optional[int] = None,
        ensure_started: bool = True,
    ) -> Dict[str, Any]:
        if ensure_started:
            self.ensure_started()
        req_id = self._new_request_id()
        wait_q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[req_id] = wait_q

        payload = {"id": req_id, "method": method, "params": params or {}}
        self._send(payload)

        timeout = float(timeout_sec if timeout_sec is not None else self.request_timeout_sec)
        try:
            resp = wait_q.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise AppServerTimeout(f"app-server timeout for method={method}") from exc

        if not isinstance(resp, dict):
            raise AppServerProtocolError(f"invalid response type for method={method}: {type(resp)}")
        if "error" in resp and resp["error"] is not None:
            raise AppServerProtocolError(f"app-server error for method={method}: {resp['error']}")
        if "result" not in resp:
            raise AppServerProtocolError(f"app-server response missing result for method={method}: {resp}")
        return dict(resp["result"] or {})

    def next_notification(self, timeout_sec: float = 0.5) -> Optional[Dict[str, Any]]:
        try:
            item = self._notifications.get(timeout=timeout_sec)
            if isinstance(item, dict):
                return item
        except queue.Empty:
            return None
        return None

    def drain_notifications(self, max_items: int = 200) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for _ in range(max_items):
            try:
                msg = self._notifications.get_nowait()
            except queue.Empty:
                break
            if isinstance(msg, dict):
                out.append(msg)
        return out

    def thread_start(
        self,
        cwd: str,
        model: str = "",
        sandbox: str = "danger-full-access",
        approval_policy: str = "never",
        personality: str = "pragmatic",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"cwd": str(Path(cwd).resolve()), "sandbox": sandbox}
        if model:
            payload["model"] = model
        if approval_policy:
            payload["approvalPolicy"] = approval_policy
        if personality:
            payload["personality"] = personality
        return self.request("thread/start", payload)

    def thread_resume(
        self,
        thread_id: str,
        cwd: str = "",
        model: str = "",
        sandbox: str = "",
        approval_policy: str = "",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"threadId": str(thread_id)}
        if cwd:
            payload["cwd"] = str(Path(cwd).resolve())
        if model:
            payload["model"] = model
        if sandbox:
            payload["sandbox"] = sandbox
        if approval_policy:
            payload["approvalPolicy"] = approval_policy
        return self.request("thread/resume", payload)

    def thread_read(self, thread_id: str, include_turns: bool = False) -> Dict[str, Any]:
        return self.request("thread/read", {"threadId": str(thread_id), "includeTurns": bool(include_turns)})

    def turn_start(
        self,
        thread_id: str,
        text: str,
        image_paths: Optional[List[str]] = None,
        model: str = "",
        cwd: str = "",
        effort: str = "",
    ) -> Dict[str, Any]:
        inputs: List[Dict[str, Any]] = [{"type": "text", "text": str(text)}]
        for path in image_paths or []:
            p = str(path).strip()
            if p:
                inputs.append({"type": "localImage", "path": p})
        payload: Dict[str, Any] = {"threadId": str(thread_id), "input": inputs}
        if model:
            payload["model"] = model
        if cwd:
            payload["cwd"] = str(Path(cwd).resolve())
        if effort:
            payload["effort"] = effort
        return self.request("turn/start", payload)

    def turn_steer(self, thread_id: str, expected_turn_id: str, text: str, image_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        inputs: List[Dict[str, Any]] = [{"type": "text", "text": str(text)}]
        for path in image_paths or []:
            p = str(path).strip()
            if p:
                inputs.append({"type": "localImage", "path": p})
        payload = {"threadId": str(thread_id), "expectedTurnId": str(expected_turn_id), "input": inputs}
        return self.request("turn/steer", payload)

    def turn_interrupt(self, thread_id: str, turn_id: str) -> Dict[str, Any]:
        return self.request("turn/interrupt", {"threadId": str(thread_id), "turnId": str(turn_id)})

    def account_rate_limits_read(self) -> Dict[str, Any]:
        return self.request("account/rateLimits/read", {})

    def wait_for_turn_completion(self, thread_id: str, turn_id: str, timeout_sec: int = 600) -> TurnRunResult:
        deadline = time.time() + max(1, int(timeout_sec))
        deltas: List[str] = []
        final_text = ""

        while time.time() < deadline:
            msg = self.next_notification(timeout_sec=1.0)
            if not msg:
                continue

            method = str(msg.get("method") or "")
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

            if method == "__bridge/disconnected__":
                raise AppServerDisconnected("app-server disconnected while waiting for turn completion")

            if str(params.get("threadId") or "") != str(thread_id):
                continue

            if method == "item/agentMessage/delta" and str(params.get("turnId") or "") == str(turn_id):
                delta = str(params.get("delta") or "")
                if delta:
                    deltas.append(delta)
                continue

            if method == "item/completed" and str(params.get("turnId") or "") == str(turn_id):
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if str(item.get("type") or "") == "agentMessage":
                    txt = str(item.get("text") or "").strip()
                    if txt:
                        final_text = txt
                continue

            if method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if str(turn.get("id") or "") != str(turn_id):
                    continue
                if not final_text:
                    final_text = self.extract_agent_text_from_turn(turn)
                if not final_text and deltas:
                    final_text = "".join(deltas).strip()
                return TurnRunResult(
                    thread_id=str(thread_id),
                    turn_id=str(turn_id),
                    turn_status=str(turn.get("status") or ""),
                    text=final_text,
                    error=turn.get("error") if isinstance(turn.get("error"), dict) else None,
                )

        raise AppServerTimeout(f"Timeout waiting for turn completion: thread={thread_id} turn={turn_id}")

    def get_thread_status(self, thread_id: str) -> Dict[str, Any]:
        with self._thread_status_lock:
            return dict(self._thread_status.get(str(thread_id), {}))

    def get_active_turn_id(self, thread_id: str) -> str:
        with self._thread_status_lock:
            return str(self._active_turn_by_thread.get(str(thread_id), ""))

    def get_thread_token_usage(self, thread_id: str) -> Dict[str, Any]:
        with self._thread_status_lock:
            return dict(self._token_usage_by_thread.get(str(thread_id), {}))

    def get_account_rate_limits(self) -> Dict[str, Any]:
        with self._thread_status_lock:
            return dict(self._account_rate_limits)

    def get_turn_progress(self, thread_id: str) -> Dict[str, Any]:
        tid = str(thread_id or "")
        with self._thread_status_lock:
            turn_id = str(self._active_turn_by_thread.get(tid) or "")
            started_at = float(self._turn_started_at_by_thread.get(tid) or 0.0)
            last_event_at = float(self._turn_last_event_at_by_thread.get(tid) or 0.0)
            preview = str(self._turn_preview_by_thread.get(tid) or "")
        now = time.time()
        elapsed = int(max(0.0, now - started_at)) if (turn_id and started_at > 0.0) else 0
        return {
            "turn_id": turn_id,
            "started_at": int(started_at) if started_at > 0.0 else 0,
            "elapsed_sec": elapsed,
            "last_event_at": int(last_event_at) if last_event_at > 0.0 else 0,
            "preview": preview,
        }

    def get_turn_events(self, thread_id: str, turn_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        tid = str(thread_id or "")
        target_turn = str(turn_id or "").strip()
        cap = max(1, int(limit or 20))
        with self._thread_status_lock:
            items = list(self._turn_events_by_thread.get(tid) or [])
        if target_turn:
            items = [item for item in items if str(item.get("turn_id") or "") in {"", target_turn}]
        if len(items) > cap:
            items = items[-cap:]
        return [dict(item) for item in items]

    @staticmethod
    def extract_agent_text_from_turn(turn: Dict[str, Any]) -> str:
        items = turn.get("items") if isinstance(turn.get("items"), list) else []
        chunks: List[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") != "agentMessage":
                continue
            txt = str(item.get("text") or "").strip()
            if txt:
                chunks.append(txt)
        return "\n".join(chunks).strip()

    def _stdout_loop(self) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    LOG.warning("app-server stdout non-json line: %s", raw[:400])
                    continue
                self._handle_message(msg)
        finally:
            self._notify_disconnect()

    def _stderr_loop(self) -> None:
        proc = self.proc
        if not proc or not proc.stderr:
            return
        for line in proc.stderr:
            text = (line or "").rstrip("\n")
            if text:
                LOG.info("app-server stderr: %s", text)

    def _handle_message(self, msg: Dict[str, Any]) -> None:
        if "id" in msg:
            req_id = str(msg.get("id"))
            with self._pending_lock:
                waiter = self._pending.pop(req_id, None)
            if waiter:
                waiter.put(msg)
                return

        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "thread/status/changed":
            thread_id = str(params.get("threadId") or "")
            status = params.get("status")
            if thread_id and isinstance(status, dict):
                with self._thread_status_lock:
                    self._thread_status[thread_id] = dict(status)
                self._append_turn_event(thread_id, self._event_from_status_change(thread_id, status))
        elif method == "thread/tokenUsage/updated":
            thread_id = str(params.get("threadId") or "")
            token_usage = params.get("tokenUsage")
            turn_id = str(params.get("turnId") or "")
            if thread_id and isinstance(token_usage, dict):
                payload: Dict[str, Any] = {"tokenUsage": dict(token_usage)}
                if turn_id:
                    payload["turnId"] = turn_id
                with self._thread_status_lock:
                    self._token_usage_by_thread[thread_id] = payload
        elif method == "account/rateLimits/updated":
            rate_limits = params.get("rateLimits")
            if isinstance(rate_limits, dict):
                with self._thread_status_lock:
                    self._account_rate_limits = dict(rate_limits)
        elif method == "turn/started":
            thread_id = str(params.get("threadId") or "")
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn_id = str(turn.get("id") or "")
            if thread_id and turn_id:
                with self._thread_status_lock:
                    self._active_turn_by_thread[thread_id] = turn_id
                    self._turn_started_at_by_thread[thread_id] = time.time()
                    self._turn_last_event_at_by_thread[thread_id] = time.time()
                    self._turn_preview_by_thread[thread_id] = ""
                    self._turn_events_by_thread[thread_id] = []
                self._append_turn_event(
                    thread_id,
                    {"ts": int(time.time()), "turn_id": turn_id, "kind": "status", "text": "已开始处理你的请求"},
                )
        elif method == "turn/completed":
            thread_id = str(params.get("threadId") or "")
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn_id = str(turn.get("id") or "")
            if thread_id and turn_id:
                with self._thread_status_lock:
                    self._turn_last_event_at_by_thread[thread_id] = time.time()
                    if self._active_turn_by_thread.get(thread_id) == turn_id:
                        self._active_turn_by_thread.pop(thread_id, None)
                        self._turn_started_at_by_thread.pop(thread_id, None)
                        # Keep preview for short-term status visibility after completion.
                self._append_turn_event(
                    thread_id,
                    {"ts": int(time.time()), "turn_id": turn_id, "kind": "status", "text": "已完成当前处理"},
                )
        elif method == "item/agentMessage/delta":
            thread_id = str(params.get("threadId") or "")
            turn_id = str(params.get("turnId") or "")
            delta = str(params.get("delta") or "")
            if thread_id and turn_id and delta:
                with self._thread_status_lock:
                    if self._active_turn_by_thread.get(thread_id) == turn_id:
                        cur = str(self._turn_preview_by_thread.get(thread_id) or "")
                        merged = (cur + delta).strip()
                        if len(merged) > 1200:
                            merged = merged[-1200:]
                        self._turn_preview_by_thread[thread_id] = merged
                        self._turn_last_event_at_by_thread[thread_id] = time.time()
        elif method == "item/completed":
            thread_id = str(params.get("threadId") or "")
            turn_id = str(params.get("turnId") or "")
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if thread_id and turn_id and str(item.get("type") or "") == "agentMessage":
                txt = str(item.get("text") or "").strip()
                if txt:
                    with self._thread_status_lock:
                        if self._active_turn_by_thread.get(thread_id) == turn_id:
                            self._turn_preview_by_thread[thread_id] = txt[-1200:]
                            self._turn_last_event_at_by_thread[thread_id] = time.time()
            if thread_id and turn_id:
                self._append_turn_event(thread_id, self._event_from_item_completed(turn_id, item))

        self._notifications.put(msg)

    def _append_turn_event(self, thread_id: str, event: Optional[Dict[str, Any]]) -> None:
        if not event:
            return
        tid = str(thread_id or "")
        if not tid:
            return
        payload = {
            "ts": int(event.get("ts") or time.time()),
            "turn_id": str(event.get("turn_id") or ""),
            "kind": str(event.get("kind") or "info"),
            "text": str(event.get("text") or "").strip(),
        }
        if not payload["text"]:
            return
        with self._thread_status_lock:
            items = list(self._turn_events_by_thread.get(tid) or [])
            if items and items[-1].get("text") == payload["text"] and items[-1].get("turn_id") == payload["turn_id"]:
                items[-1]["ts"] = payload["ts"]
            else:
                items.append(payload)
            if len(items) > 80:
                items = items[-80:]
            self._turn_events_by_thread[tid] = items
            self._turn_last_event_at_by_thread[tid] = time.time()

    def _event_from_status_change(self, thread_id: str, status: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        stype = str((status or {}).get("type") or "").strip().lower()
        if not stype:
            return None
        with self._thread_status_lock:
            prev = str(self._last_status_type_by_thread.get(thread_id) or "").strip().lower()
            self._last_status_type_by_thread[thread_id] = stype
            turn_id = str(self._active_turn_by_thread.get(thread_id) or "")
        if prev == stype:
            return None
        text_map = {
            "idle": "当前步骤已结束，正在等待下一步",
            "running": "正在继续处理",
            "busy": "正在处理",
            "systemerror": "处理中遇到了系统错误",
        }
        return {
            "ts": int(time.time()),
            "turn_id": turn_id,
            "kind": "status",
            "text": text_map.get(stype, f"状态已更新：{stype}"),
        }

    def _event_from_item_completed(self, turn_id: str, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type") or "").strip()
        if not item_type or item_type == "agentMessage":
            return None
        text = self._summarize_item(item_type, item)
        if not text:
            return None
        return {
            "ts": int(time.time()),
            "turn_id": str(turn_id or ""),
            "kind": "step",
            "text": text,
        }

    @staticmethod
    def _summarize_item(item_type: str, item: Dict[str, Any]) -> str:
        kind = str(item_type or "").strip()
        if kind == "userMessage":
            return ""
        label_map = {
            "toolCall": "已完成一个处理步骤",
            "toolResult": "已拿到一个处理结果",
            "commandExecution": "已执行一个命令",
            "applyPatch": "已完成一次文件修改",
            "fileChange": "已处理文件变更",
            "userMessage": "",
        }
        snippets: List[str] = []
        for key in ("title", "name", "label", "command", "cmd", "path", "summary", "text"):
            raw = item.get(key)
            if raw is None:
                continue
            txt = str(raw).strip()
            if txt:
                snippets.append(txt)
        if not snippets:
            for subkey in ("input", "output", "result", "metadata"):
                node = item.get(subkey)
                if isinstance(node, dict):
                    for key in ("command", "cmd", "path", "title", "name", "summary", "text"):
                        raw = node.get(key)
                        if raw is None:
                            continue
                        txt = str(raw).strip()
                        if txt:
                            snippets.append(txt)
                            break
                if snippets:
                    break
        desc = snippets[0] if snippets else ""
        desc = re.sub(r"\s+", " ", desc).strip()
        if len(desc) > 120:
            desc = desc[:117] + "..."
        head = label_map.get(kind, f"已完成步骤：{kind}")
        if desc:
            return f"{head}，{desc}"
        return head

    def _notify_disconnect(self) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        self._closed.set()
        sentinel = {
            "error": {"code": -32001, "message": "app-server disconnected"},
        }
        for waiter in pending:
            try:
                waiter.put_nowait(sentinel)
            except Exception:
                pass
        self._notifications.put({"method": "__bridge/disconnected__", "params": {}})

    def _new_request_id(self) -> str:
        with self._id_lock:
            cur = self._next_id
            self._next_id += 1
        return str(cur)

    def _send(self, payload: Dict[str, Any]) -> None:
        proc = self.proc
        if not proc or proc.poll() is not None or not proc.stdin:
            raise AppServerDisconnected("app-server is not running")
        raw = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            try:
                proc.stdin.write(raw + "\n")
                proc.stdin.flush()
            except Exception as exc:
                raise AppServerDisconnected(f"failed writing to app-server stdin: {exc}") from exc
