#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _preview_text(value: Any, max_len: int = 120) -> str:
    raw = " ".join(str(value or "").split()).strip()
    if len(raw) <= max_len:
        return raw
    return raw[: max_len - 3].rstrip() + "..."


def _session_display_title(user_text: str, assistant_text: str, error_text: str) -> str:
    user_preview = _preview_text(user_text, max_len=48)
    if user_preview:
        return user_preview
    error_preview = _preview_text(error_text, max_len=48)
    if error_preview:
        return error_preview
    assistant_preview = _preview_text(assistant_text, max_len=48)
    if assistant_preview:
        return assistant_preview
    return "未命名会话"


class BridgeHistoryStore:
    def __init__(self, db_path: str, max_turns: int = 2000, legacy_json_path: Optional[str] = None):
        self.db_path = Path(db_path)
        self.max_turns = max(100, int(max_turns or 2000))
        self.legacy_json_path = Path(legacy_json_path) if str(legacy_json_path or "").strip() else None
        self._lock = threading.Lock()
        self._init_db()
        self._migrate_legacy_json()

    def append_turn(self, item: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._normalize_turn(dict(item or {}))
        payload.setdefault("id", f"turn_{int(time.time() * 1000)}")
        payload["updated_at"] = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO turns (
                    id, project, chat_id, thread_id, turn_id, cwd, model, auth_profile, status,
                    started_at, ended_at, duration_sec, user_text, assistant_text, error_text,
                    events_json, events_count, token_usage_json, rate_limits_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._turn_values(payload),
            )
            self._trim_excess_rows(conn)
            conn.commit()
        return dict(payload)

    def list_turns(self, limit: int = 200) -> List[Dict[str, Any]]:
        cap = max(1, int(limit or 200))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM turns
                ORDER BY COALESCE(NULLIF(ended_at, 0), updated_at, started_at) DESC, id DESC
                LIMIT ?
                """,
                (cap,),
            ).fetchall()
        out = [self._row_to_turn(row, include_events=True) for row in rows]
        out.reverse()
        return out

    def project_summaries(self, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        safe_offset, safe_limit = self._page_args(offset, limit)
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM turns GROUP BY project)").fetchone()[0]
            rows = conn.execute(
                """
                SELECT
                    project,
                    MIN(CASE
                        WHEN started_at > 0 THEN started_at
                        WHEN ended_at > 0 THEN ended_at
                        ELSE updated_at
                    END) AS started_at,
                    MAX(CASE
                        WHEN ended_at > 0 THEN ended_at
                        WHEN updated_at > 0 THEN updated_at
                        ELSE started_at
                    END) AS updated_at,
                    COUNT(*) AS turn_count,
                    COUNT(DISTINCT chat_id) AS session_count
                FROM turns
                GROUP BY project
                ORDER BY started_at ASC, project ASC
                LIMIT ? OFFSET ?
                """,
                (safe_limit, safe_offset),
            ).fetchall()
        items = [
            {
                "name": str(row["project"] or "未命名项目"),
                "started_at": _safe_int(row["started_at"]),
                "updated_at": _safe_int(row["updated_at"]),
                "turn_count": _safe_int(row["turn_count"]),
                "session_count": _safe_int(row["session_count"]),
            }
            for row in rows
        ]
        return {"items": items, "pagination": self._pagination(safe_offset, safe_limit, total)}

    def session_summaries(self, project: str = "", offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        safe_offset, safe_limit = self._page_args(offset, limit)
        target_project = str(project or "").strip()
        with self._lock, self._connect() as conn:
            total = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM turns
                    WHERE (? = '' OR project = ?)
                    GROUP BY project, chat_id
                )
                """,
                (target_project, target_project),
            ).fetchone()[0]
            rows = conn.execute(
                """
                SELECT
                    project,
                    chat_id,
                    MIN(CASE
                        WHEN started_at > 0 THEN started_at
                        WHEN ended_at > 0 THEN ended_at
                        ELSE updated_at
                    END) AS started_at,
                    MAX(CASE
                        WHEN ended_at > 0 THEN ended_at
                        WHEN updated_at > 0 THEN updated_at
                        ELSE started_at
                    END) AS updated_at,
                    COUNT(*) AS turn_count
                FROM turns
                WHERE (? = '' OR project = ?)
                GROUP BY project, chat_id
                ORDER BY started_at ASC, chat_id ASC
                LIMIT ? OFFSET ?
                """,
                (target_project, target_project, safe_limit, safe_offset),
            ).fetchall()
            items: List[Dict[str, Any]] = []
            for row in rows:
                item = {
                    "project": str(row["project"] or "未命名项目"),
                    "chat_id": str(row["chat_id"] or ""),
                    "cwd": "",
                    "model": "",
                    "auth_profile": "default",
                    "started_at": _safe_int(row["started_at"]),
                    "updated_at": _safe_int(row["updated_at"]),
                    "turn_count": _safe_int(row["turn_count"]),
                }
                latest = conn.execute(
                    """
                    SELECT turn_id, status, started_at, ended_at, updated_at, cwd, model, auth_profile, user_text, assistant_text, error_text
                    FROM turns
                    WHERE project = ? AND chat_id = ?
                    ORDER BY COALESCE(NULLIF(ended_at, 0), updated_at, started_at) DESC, id DESC
                    LIMIT 1
                    """,
                    (item["project"], item["chat_id"]),
                ).fetchone()
                latest_user_text = str((latest["user_text"] if latest else "") or "")
                latest_assistant_text = str((latest["assistant_text"] if latest else "") or "")
                latest_error_text = str((latest["error_text"] if latest else "") or "")
                item.update(
                    {
                        "cwd": str((latest["cwd"] if latest else "") or ""),
                        "model": str((latest["model"] if latest else "") or ""),
                        "auth_profile": str((latest["auth_profile"] if latest else "") or "") or "default",
                        "latest_turn_id": str((latest["turn_id"] if latest else "") or ""),
                        "latest_status": str((latest["status"] if latest else "") or ""),
                        "latest_started_at": _safe_int(latest["started_at"] if latest else 0),
                        "latest_ended_at": _safe_int(latest["ended_at"] if latest else 0),
                        "latest_updated_at": _safe_int(latest["updated_at"] if latest else 0),
                        "latest_user_text": latest_user_text,
                        "latest_user_preview": _preview_text(latest_user_text, max_len=72),
                        "latest_assistant_preview": _preview_text(latest_assistant_text, max_len=120),
                        "latest_error_preview": _preview_text(latest_error_text, max_len=120),
                        "display_title": _session_display_title(
                            latest_user_text,
                            latest_assistant_text,
                            latest_error_text,
                        ),
                        "display_preview": _preview_text(latest_assistant_text or latest_error_text, max_len=120),
                    }
                )
                items.append(item)
        return {"items": items, "pagination": self._pagination(safe_offset, safe_limit, total)}

    def turn_items(
        self,
        project: str = "",
        chat_id: str = "",
        offset: int = 0,
        limit: int = 50,
        include_events: bool = False,
    ) -> Dict[str, Any]:
        safe_offset, safe_limit = self._page_args(offset, limit)
        target_project = str(project or "").strip()
        target_chat = str(chat_id or "").strip()
        with self._lock, self._connect() as conn:
            total = conn.execute(
                """
                SELECT COUNT(*) FROM turns
                WHERE (? = '' OR project = ?) AND (? = '' OR chat_id = ?)
                """,
                (target_project, target_project, target_chat, target_chat),
            ).fetchone()[0]
            rows = conn.execute(
                """
                SELECT * FROM turns
                WHERE (? = '' OR project = ?) AND (? = '' OR chat_id = ?)
                ORDER BY started_at ASC, ended_at ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (target_project, target_project, target_chat, target_chat, safe_limit, safe_offset),
            ).fetchall()
        items: List[Dict[str, Any]] = []
        for row in rows:
            item = self._row_to_turn(row, include_events=include_events)
            items.append(
                {
                    "id": item["id"],
                    "project": item["project"],
                    "chat_id": item["chat_id"],
                    "turn_id": item["turn_id"],
                    "status": item["status"],
                    "started_at": item["started_at"],
                    "ended_at": item["ended_at"],
                    "duration_sec": item["duration_sec"],
                    "user_text": item["user_text"],
                    "assistant_text": item["assistant_text"],
                    "error_text": item["error_text"],
                    "events_count": item["events_count"],
                    **({"events": item["events"]} if include_events else {}),
                }
        )
        return {"items": items, "pagination": self._pagination(safe_offset, safe_limit, total)}

    def turn_detail(self, turn_id: str, include_events: bool = True) -> Optional[Dict[str, Any]]:
        target_turn = str(turn_id or "").strip()
        if not target_turn:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM turns WHERE turn_id = ? OR id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
                (target_turn, target_turn),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_turn(row, include_events=include_events)

    def load(self) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM turns
                ORDER BY started_at ASC, ended_at ASC, id ASC
                """
            ).fetchall()
        turns = [self._row_to_turn(row, include_events=True) for row in rows]
        updated_at = max([_safe_int(item.get("updated_at")) for item in turns] or [0])
        return {"turns": turns, "updated_at": updated_at}

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    model TEXT NOT NULL,
                    auth_profile TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    ended_at INTEGER NOT NULL,
                    duration_sec INTEGER NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    error_text TEXT NOT NULL,
                    events_json TEXT NOT NULL,
                    events_count INTEGER NOT NULL,
                    token_usage_json TEXT NOT NULL,
                    rate_limits_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turns_project_started ON turns(project, started_at, id);
                CREATE INDEX IF NOT EXISTS idx_turns_chat_started ON turns(chat_id, started_at, id);
                CREATE INDEX IF NOT EXISTS idx_turns_updated ON turns(updated_at, id);
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def _migrate_legacy_json(self) -> None:
        if not self.legacy_json_path or not self.legacy_json_path.exists():
            return
        migration_key = f"legacy_json_migrated:{self.legacy_json_path}"
        with self._lock, self._connect() as conn:
            current = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            migrated = conn.execute("SELECT value FROM meta WHERE key = ?", (migration_key,)).fetchone()
            if current > 0 or migrated:
                return
            try:
                raw = self.legacy_json_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                items = data.get("turns") if isinstance(data.get("turns"), list) else []
            except Exception:
                items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                payload = self._normalize_turn(item)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO turns (
                        id, project, chat_id, thread_id, turn_id, cwd, model, auth_profile, status,
                        started_at, ended_at, duration_sec, user_text, assistant_text, error_text,
                        events_json, events_count, token_usage_json, rate_limits_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._turn_values(payload),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (migration_key, str(int(time.time()))),
            )
            self._trim_excess_rows(conn)
            conn.commit()

    def _normalize_turn(self, item: Dict[str, Any]) -> Dict[str, Any]:
        events = item.get("events") if isinstance(item.get("events"), list) else []
        start_ts = _safe_int(item.get("started_at"))
        end_ts = _safe_int(item.get("ended_at"), _safe_int(item.get("updated_at"), int(time.time())))
        return {
            "id": str(item.get("id") or f"turn_{int(time.time() * 1000)}"),
            "project": str(item.get("project") or "未命名项目"),
            "chat_id": str(item.get("chat_id") or ""),
            "thread_id": str(item.get("thread_id") or ""),
            "turn_id": str(item.get("turn_id") or ""),
            "cwd": str(item.get("cwd") or ""),
            "model": str(item.get("model") or ""),
            "auth_profile": str(item.get("auth_profile") or "") or "default",
            "status": str(item.get("status") or ""),
            "started_at": start_ts,
            "ended_at": end_ts,
            "duration_sec": _safe_int(item.get("duration_sec"), max(0, end_ts - start_ts) if start_ts > 0 else 0),
            "user_text": str(item.get("user_text") or ""),
            "assistant_text": str(item.get("assistant_text") or ""),
            "error_text": str(item.get("error_text") or ""),
            "events": [evt for evt in events if isinstance(evt, dict)],
            "events_count": len(events),
            "token_usage": dict(item.get("token_usage") or {}),
            "rate_limits": dict(item.get("rate_limits") or {}),
            "updated_at": _safe_int(item.get("updated_at"), end_ts or int(time.time())),
        }

    def _turn_values(self, item: Dict[str, Any]) -> tuple[Any, ...]:
        return (
            str(item.get("id") or ""),
            str(item.get("project") or "未命名项目"),
            str(item.get("chat_id") or ""),
            str(item.get("thread_id") or ""),
            str(item.get("turn_id") or ""),
            str(item.get("cwd") or ""),
            str(item.get("model") or ""),
            str(item.get("auth_profile") or "") or "default",
            str(item.get("status") or ""),
            _safe_int(item.get("started_at")),
            _safe_int(item.get("ended_at")),
            _safe_int(item.get("duration_sec")),
            str(item.get("user_text") or ""),
            str(item.get("assistant_text") or ""),
            str(item.get("error_text") or ""),
            json.dumps(item.get("events") or [], ensure_ascii=False),
            _safe_int(item.get("events_count"), len(item.get("events") or [])),
            json.dumps(item.get("token_usage") or {}, ensure_ascii=False),
            json.dumps(item.get("rate_limits") or {}, ensure_ascii=False),
            _safe_int(item.get("updated_at"), int(time.time())),
        )

    def _row_to_turn(self, row: sqlite3.Row, include_events: bool = True) -> Dict[str, Any]:
        data = {
            "id": str(row["id"] or ""),
            "project": str(row["project"] or "未命名项目"),
            "chat_id": str(row["chat_id"] or ""),
            "thread_id": str(row["thread_id"] or ""),
            "turn_id": str(row["turn_id"] or ""),
            "cwd": str(row["cwd"] or ""),
            "model": str(row["model"] or ""),
            "auth_profile": str(row["auth_profile"] or "") or "default",
            "status": str(row["status"] or ""),
            "started_at": _safe_int(row["started_at"]),
            "ended_at": _safe_int(row["ended_at"]),
            "duration_sec": _safe_int(row["duration_sec"]),
            "user_text": str(row["user_text"] or ""),
            "assistant_text": str(row["assistant_text"] or ""),
            "error_text": str(row["error_text"] or ""),
            "events_count": _safe_int(row["events_count"]),
            "token_usage": self._json_loads(str(row["token_usage_json"] or "{}"), {}),
            "rate_limits": self._json_loads(str(row["rate_limits_json"] or "{}"), {}),
            "updated_at": _safe_int(row["updated_at"]),
        }
        data["events"] = self._json_loads(str(row["events_json"] or "[]"), []) if include_events else []
        return data

    def _trim_excess_rows(self, conn: sqlite3.Connection) -> None:
        total = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        overflow = int(total or 0) - self.max_turns
        if overflow <= 0:
            return
        conn.execute(
            """
            DELETE FROM turns
            WHERE id IN (
                SELECT id FROM turns
                ORDER BY started_at ASC, ended_at ASC, id ASC
                LIMIT ?
            )
            """,
            (overflow,),
        )

    def _page_args(self, offset: int, limit: int) -> tuple[int, int]:
        safe_offset = max(0, int(offset or 0))
        safe_limit = max(1, min(200, int(limit or 50)))
        return safe_offset, safe_limit

    def _pagination(self, offset: int, limit: int, total: int) -> Dict[str, Any]:
        return {
            "offset": offset,
            "limit": limit,
            "total": int(total or 0),
            "has_more": offset + limit < int(total or 0),
        }

    def _json_loads(self, raw: str, default: Any) -> Any:
        try:
            return json.loads(raw)
        except Exception:
            return default
