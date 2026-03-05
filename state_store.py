#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict


class BridgeStateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def get_chat(self, chat_id: str) -> Dict[str, Any]:
        with self._lock:
            state = self._load_unlocked()
            chats = state.get("chats") if isinstance(state.get("chats"), dict) else {}
            chat = chats.get(str(chat_id)) if isinstance(chats.get(str(chat_id)), dict) else {}
            return dict(chat)

    def upsert_chat(self, chat_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        clean_chat_id = str(chat_id)
        with self._lock:
            state = self._load_unlocked()
            chats = state.get("chats") if isinstance(state.get("chats"), dict) else {}
            cur = chats.get(clean_chat_id) if isinstance(chats.get(clean_chat_id), dict) else {}
            nxt = dict(cur)
            nxt.update(dict(patch or {}))
            nxt["updated_at"] = int(time.time())
            chats[clean_chat_id] = nxt
            state["chats"] = chats
            self._save_unlocked(state)
            return dict(nxt)

    def clear_chat_thread(self, chat_id: str) -> Dict[str, Any]:
        clean_chat_id = str(chat_id)
        with self._lock:
            state = self._load_unlocked()
            chats = state.get("chats") if isinstance(state.get("chats"), dict) else {}
            cur = chats.get(clean_chat_id) if isinstance(chats.get(clean_chat_id), dict) else {}
            nxt = dict(cur)
            nxt["thread_id"] = ""
            nxt["active_turn_id"] = ""
            nxt["updated_at"] = int(time.time())
            chats[clean_chat_id] = nxt
            state["chats"] = chats
            self._save_unlocked(state)
            return dict(nxt)

    def _load_unlocked(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"chats": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                chats = data.get("chats")
                if not isinstance(chats, dict):
                    data["chats"] = {}
                return data
        except Exception:
            pass
        return {"chats": {}}

    def _save_unlocked(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
