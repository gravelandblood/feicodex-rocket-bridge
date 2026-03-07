#!/usr/bin/env python3
"""Feishu long-connection receiver for app-server bridge.

This process handles Feishu events and forwards actions to local control API.
Interaction model:
- Natural language text messages -> direct Codex turn.
- Menu events -> interactive cards.
- Card actions -> multi-step state-machine actions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import lark_oapi as lark
import requests
from dotenv import load_dotenv
from lark_oapi.api.application.v6 import P2ApplicationBotMenuV6
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env", override=False)

LOG = logging.getLogger("feicodex_rocket_long_conn")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

FEISHU_API = "https://open.feishu.cn/open-apis"
APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
SINGLE_CHAT_ONLY = str(os.getenv("BRIDGE_SINGLE_CHAT_ONLY", "true")).strip().lower() in {"1", "true", "yes", "on"}

CONTROL_BASE = str(os.getenv("BRIDGE_CONTROL_BASE", "http://127.0.0.1:18788")).strip().rstrip("/")
API_PREFIX = str(os.getenv("BRIDGE_API_PREFIX", "/appbridge/api")).strip()
API_TOKEN = str(os.getenv("BRIDGE_API_TOKEN", "")).strip()
TURN_TIMEOUT_SEC = max(5, int(os.getenv("BRIDGE_TURN_TIMEOUT_SEC", "21600")))
PROGRESS_PING_INTERVAL_SEC = max(30, int(os.getenv("BRIDGE_PROGRESS_PING_INTERVAL_SEC", "180")))
UPLOAD_ROOT = Path(os.getenv("BRIDGE_UPLOAD_ROOT", str(APP_DIR / "data" / "uploads"))).expanduser()
if not UPLOAD_ROOT.is_absolute():
    UPLOAD_ROOT = APP_DIR / UPLOAD_ROOT
UPLOAD_ROOT = UPLOAD_ROOT.resolve()
PROJECT_ROOT = Path(os.getenv("BRIDGE_PROJECT_ROOT", str(APP_DIR.parent / "projects"))).expanduser()
if not PROJECT_ROOT.is_absolute():
    PROJECT_ROOT = APP_DIR / PROJECT_ROOT
PROJECT_ROOT = PROJECT_ROOT.resolve()
PROJECTS_STORE_PATH = Path(os.getenv("BRIDGE_PROJECTS_STORE_PATH", str(APP_DIR / "data" / "projects.json"))).expanduser()
if not PROJECTS_STORE_PATH.is_absolute():
    PROJECTS_STORE_PATH = APP_DIR / PROJECTS_STORE_PATH
PROJECTS_STORE_PATH = PROJECTS_STORE_PATH.resolve()
USER_CHAT_MAP_PATH = Path(os.getenv("BRIDGE_USER_CHAT_MAP_PATH", str(APP_DIR / "data" / "user_chat_map.json"))).expanduser()
if not USER_CHAT_MAP_PATH.is_absolute():
    USER_CHAT_MAP_PATH = APP_DIR / USER_CHAT_MAP_PATH
USER_CHAT_MAP_PATH = USER_CHAT_MAP_PATH.resolve()
CARD_AUTO_DELETE_ON_ACTION = str(os.getenv("BRIDGE_CARD_AUTO_DELETE_ON_ACTION", "true")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
OUTPUT_FILE_AUTO_SEND = str(os.getenv("BRIDGE_OUTPUT_FILE_AUTO_SEND", "true")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FEISHU_FILE_UPLOAD_MAX_SIZE_MB = 30
OUTPUT_FILE_MAX_COUNT = int(os.getenv("BRIDGE_OUTPUT_FILE_MAX_COUNT", "0"))
OUTPUT_FILE_MAX_SIZE_MB = min(
    FEISHU_FILE_UPLOAD_MAX_SIZE_MB,
    max(1, int(os.getenv("BRIDGE_OUTPUT_FILE_MAX_SIZE_MB", str(FEISHU_FILE_UPLOAD_MAX_SIZE_MB)))),
)
OUTPUT_FILE_MAX_AGE_SEC = max(60, int(os.getenv("BRIDGE_OUTPUT_FILE_MAX_AGE_SEC", "3600")))
OUTPUT_FILE_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".txt"}

DEFAULT_MENU_ACTIONS = {
    "menu_project_manage": "open_project_manage",
    "menu_session_manage": "open_session_manage",
}

DEFAULT_PROJECTS = {
    APP_DIR.name: str(APP_DIR),
    "workspace": str(APP_DIR.parent),
}

SUPPORTED_ATTACHMENT_TYPES = {"image", "file", "media", "audio", "video", "sticker"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
SUPPORTED_EFFORTS = ["medium", "high", "xhigh"]


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _split_text(text: str, max_len: int = 1500) -> List[str]:
    raw = str(text or "")
    if len(raw) <= max_len:
        return [raw]
    out: List[str] = []
    start = 0
    while start < len(raw):
        out.append(raw[start : start + max_len])
        start += max_len
    return out


def _split_text_by_boundary(text: str, max_len: int = 1500) -> List[str]:
    raw = str(text or "")
    if len(raw) <= max_len:
        return [raw]
    out: List[str] = []
    remaining = raw
    while len(remaining) > max_len:
        window = remaining[:max_len]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind("。"), window.rfind("！"), window.rfind("？"))
        if cut < max_len // 3:
            cut = max(window.rfind(" "), window.rfind("，"), window.rfind(","), window.rfind("；"), window.rfind(";"))
        if cut < max_len // 3:
            cut = max_len
        else:
            cut += 1
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return [item for item in out if item]


def _trim(text: str, max_len: int = 1200) -> str:
    txt = str(text or "")
    if len(txt) <= max_len:
        return txt
    return txt[: max_len - 16] + "\n...<truncated>"


def _parse_content_json(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    txt = str(raw or "")
    if not txt:
        return {}
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}


def _parse_json_map(raw: str, default_map: Dict[str, str]) -> Dict[str, str]:
    txt = str(raw or "").strip()
    if not txt:
        return dict(default_map)
    try:
        obj = json.loads(txt)
    except Exception:
        LOG.warning("invalid json map, fallback to default: %s", txt[:200])
        return dict(default_map)
    if not isinstance(obj, dict):
        return dict(default_map)
    out = dict(default_map)
    for k, v in obj.items():
        kk = str(k or "").strip()
        vv = str(v or "").strip()
        if kk and vv:
            out[kk] = vv
    return out


def _sanitize_filename(raw: str, fallback: str) -> str:
    name = str(raw or "").strip() or fallback
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    safe = safe.strip("._")
    if not safe:
        safe = fallback
    return safe[:120]


def _normalize_display_line(line: str, in_code_block: bool = False) -> str:
    txt = str(line or "").rstrip()
    if not txt.strip():
        return ""
    if in_code_block:
        return "    " + txt.strip("\n")
    stripped = txt.strip()
    heading = re.match(r"^#{1,6}\s*(.+)$", stripped)
    if heading:
        stripped = heading.group(1)
    quote = re.match(r"^>\s*(.+)$", stripped)
    if quote:
        stripped = f"引用：{quote.group(1)}"
    bullet = re.match(r"^[-*+]\s+(.+)$", stripped)
    if bullet:
        stripped = f"• {bullet.group(1)}"
    numbered = re.match(r"^(\d+)\.\s+(.+)$", stripped)
    if numbered:
        stripped = f"{numbered.group(1)}. {numbered.group(2)}"
    stripped = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1", stripped)
    stripped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", stripped)
    stripped = stripped.replace("**", "").replace("__", "").replace("`", "")
    return stripped.strip()


def _text_to_post_chunks(text: str, title: str = "Codex 回复", max_chars: int = 4500, max_rows: int = 35) -> List[Dict[str, Any]]:
    rows: List[List[Dict[str, str]]] = []
    in_code_block = False
    for raw_line in str(text or "").splitlines():
        marker = raw_line.strip()
        if marker.startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                rows.append([{"tag": "text", "text": "代码："}])
            continue
        normalized = _normalize_display_line(raw_line, in_code_block=in_code_block)
        if not normalized:
            continue
        for part in _split_text_by_boundary(normalized, 450):
            rows.append([{"tag": "text", "text": part}])
    if not rows:
        rows = [[{"tag": "text", "text": "(empty)"}]]

    chunks: List[Dict[str, Any]] = []
    current_rows: List[List[Dict[str, str]]] = []
    current_chars = 0
    for row in rows:
        row_chars = sum(len(str(cell.get("text") or "")) for cell in row)
        if current_rows and (len(current_rows) >= max_rows or current_chars + row_chars > max_chars):
            chunks.append({"title": title, "rows": current_rows})
            current_rows = []
            current_chars = 0
        current_rows.append(row)
        current_chars += row_chars
    if current_rows:
        chunks.append({"title": title, "rows": current_rows})

    if len(chunks) > 1:
        total = len(chunks)
        for idx, item in enumerate(chunks, start=1):
            item["title"] = f"{title} ({idx}/{total})"
    return chunks


def _text_to_card_chunks(text: str, max_len: int = 8000) -> List[str]:
    normalized = str(text or "").strip()
    chunks = [part for part in _split_text_by_boundary(normalized, max_len=max_len) if part.strip()]
    if not chunks:
        chunks = ["(empty)"]
    return chunks


def _guess_suffix(msg_type: str) -> str:
    t = str(msg_type or "").strip().lower()
    if t == "image":
        return ".png"
    if t == "audio":
        return ".mp3"
    if t == "video":
        return ".mp4"
    if t == "sticker":
        return ".webp"
    return ".bin"


def _is_image_path(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def _parse_model_candidates(text: str) -> List[str]:
    out: List[str] = []

    def _add(value: str) -> None:
        val = str(value or "").strip().strip("`")
        if not val:
            return
        if val not in out:
            out.append(val)

    section_visibility = "list"
    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        lower = line.lower()
        if "visibility=hide" in lower or line.startswith("隐藏"):
            section_visibility = "hide"
            continue
        if "visibility=list" in lower or line.startswith("可见"):
            section_visibility = "list"
            continue

        m = re.match(
            r"^\s*(?:\d+\.\s*|-\s*)`?([A-Za-z0-9._:-]+)`?(?:\s*\(`?(list|hide)`?\))?\s*$",
            line,
            re.IGNORECASE,
        )
        if not m:
            continue
        visibility = str(m.group(2) or section_visibility).strip().lower()
        if visibility == "hide":
            continue
        _add(m.group(1))

    for line in str(text or "").splitlines():
        if "effective" in line and "=" in line:
            _add(line.split("=", 1)[1])

    return out


def _extract_status_value(text: str, key: str) -> str:
    pattern = re.compile(rf"(?mi)^\s*{re.escape(str(key or '').strip())}\s*=\s*(.+?)\s*$")
    m = pattern.search(str(text or ""))
    if not m:
        return ""
    return str(m.group(1) or "").strip().strip("`")


def _format_limit_line(label: str, node: Dict[str, Any]) -> str:
    if not isinstance(node, dict):
        return f"{label}: unavailable"
    try:
        used_percent = float(node.get("usedPercent"))
    except Exception:
        used_percent = -1.0
    left = max(0, min(100, int(round(100.0 - used_percent)))) if used_percent >= 0 else -1
    resets_at_raw = node.get("resetsAt")
    try:
        resets_at = int(resets_at_raw)
    except Exception:
        resets_at = 0
    if resets_at > 0:
        dt = datetime.fromtimestamp(resets_at)
        reset_text = f"{dt:%H:%M} on {dt.day} {dt:%b}"
    else:
        reset_text = "unknown"
    if left >= 0:
        return f"{label}: {left}% left (resets {reset_text})"
    return f"{label}: unavailable (resets {reset_text})"


def _format_rate_limit_lines(rate_limits: Dict[str, Any]) -> List[str]:
    if not isinstance(rate_limits, dict) or not rate_limits:
        return []
    primary = rate_limits.get("primary") if isinstance(rate_limits.get("primary"), dict) else {}
    secondary = rate_limits.get("secondary") if isinstance(rate_limits.get("secondary"), dict) else {}
    lines: List[str] = []
    if primary:
        lines.append(_format_limit_line("5h limit", primary))
    if secondary:
        lines.append(_format_limit_line("Weekly limit", secondary))
    return lines


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = ""
        self._token_expire_at = 0.0
        self._token_lock = threading.Lock()

    def _tenant_access_token(self) -> str:
        with self._token_lock:
            now = time.time()
            if self._token and now < self._token_expire_at:
                return self._token

            resp = requests.post(
                f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Feishu token error: {data}")
            self._token = str(data.get("tenant_access_token") or "")
            if not self._token:
                raise RuntimeError("Feishu token response missing tenant_access_token")
            self._token_expire_at = now + int(data.get("expire", 7200)) - 60
            return self._token

    def send_text_by_receive_id(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> None:
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{FEISHU_API}/im/v1/messages?receive_id_type={receive_id_type}"
        for chunk in _split_text(str(text or ""), 1500):
            payload = {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": chunk}, ensure_ascii=False),
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            if resp.status_code >= 300:
                LOG.error("send_text failed status=%s body=%s", resp.status_code, resp.text)
                continue
            try:
                data = resp.json()
            except Exception:
                data = {}
            if data.get("code") != 0:
                LOG.error("send_text feishu err: %s", data)

    def send_text(self, chat_id: str, text: str) -> None:
        self.send_text_by_receive_id(chat_id, text, receive_id_type="chat_id")

    def send_post_by_receive_id(self, receive_id: str, text: str, title: str = "Codex 回复", receive_id_type: str = "chat_id") -> None:
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{FEISHU_API}/im/v1/messages?receive_id_type={receive_id_type}"
        for chunk in _text_to_post_chunks(text=text, title=title):
            payload = {
                "receive_id": receive_id,
                "msg_type": "post",
                "content": json.dumps(
                    {
                        "zh_cn": {
                            "title": str(chunk.get("title") or title),
                            "content": chunk.get("rows") or [[{"tag": "text", "text": "(empty)"}]],
                        }
                    },
                    ensure_ascii=False,
                ),
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            if resp.status_code >= 300:
                raise RuntimeError(f"send_post failed status={resp.status_code} body={resp.text}")
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"send_post feishu err: {data}")

    def send_post(self, chat_id: str, text: str, title: str = "Codex 回复") -> None:
        self.send_post_by_receive_id(chat_id, text=text, title=title, receive_id_type="chat_id")

    def send_markdown_card(self, chat_id: str, text: str, title: str = "Codex 回复") -> None:
        for idx, chunk in enumerate(_text_to_card_chunks(text=text), start=1):
            content = chunk
            if idx > 1:
                content = f"（续 {idx}）\n\n{content}"
            self.send_card(
                chat_id,
                {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "body": {
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": content,
                            }
                        ]
                    },
                },
            )

    def smart_send_by_receive_id(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str = "chat_id",
        title: str = "Codex 回复",
        prefer_rich: bool = True,
    ) -> None:
        raw = str(text or "").strip()
        if not raw:
            return
        looks_structured = ("\n" in raw) or bool(re.search(r"(^|\n)\s*(#{1,6}\s|[-*+]\s|\d+\.\s|```|>)", raw))
        use_post = prefer_rich and (len(raw) > 280 or looks_structured)
        if not use_post:
            self.send_text_by_receive_id(receive_id, raw, receive_id_type=receive_id_type)
            return
        try:
            if receive_id_type == "chat_id":
                self.send_markdown_card(receive_id, raw, title=title)
                return
            self.send_post_by_receive_id(receive_id, raw, title=title, receive_id_type=receive_id_type)
        except Exception as card_exc:
            LOG.warning("smart_send card fallback receive_id=%s err=%s", receive_id, card_exc)
            try:
                self.send_post_by_receive_id(receive_id, raw, title=title, receive_id_type=receive_id_type)
            except Exception as post_exc:
                LOG.warning("smart_send post fallback to text receive_id=%s err=%s", receive_id, post_exc)
                self.send_text_by_receive_id(receive_id, raw, receive_id_type=receive_id_type)

    def smart_send(self, chat_id: str, text: str, title: str = "Codex 回复", prefer_rich: bool = True) -> None:
        self.smart_send_by_receive_id(
            chat_id,
            text=text,
            receive_id_type="chat_id",
            title=title,
            prefer_rich=prefer_rich,
        )

    def reply_text(self, message_id: str, text: str) -> str:
        mid = str(message_id or "").strip()
        if not mid:
            return ""
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{FEISHU_API}/im/v1/messages/{mid}/reply"
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": str(text or "")}, ensure_ascii=False),
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        if resp.status_code >= 300:
            raise RuntimeError(f"reply_text failed status={resp.status_code} body={resp.text}")
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"reply_text feishu err: {data}")
        out = data.get("data") if isinstance(data.get("data"), dict) else {}
        return str(out.get("message_id") or "").strip()

    def update_text(self, message_id: str, text: str) -> None:
        mid = str(message_id or "").strip()
        if not mid:
            return
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{FEISHU_API}/im/v1/messages/{mid}"
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": str(text or "")}, ensure_ascii=False),
        }
        resp = requests.put(url, headers=headers, json=payload, timeout=20)
        if resp.status_code >= 300:
            raise RuntimeError(f"update_text failed status={resp.status_code} body={resp.text}")
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"update_text feishu err: {data}")

    def delete_message(self, message_id: str) -> None:
        mid = str(message_id or "").strip()
        if not mid:
            return
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{FEISHU_API}/im/v1/messages/{mid}"
        resp = requests.delete(url, headers=headers, timeout=20)
        if resp.status_code >= 300:
            raise RuntimeError(f"delete_message failed status={resp.status_code} body={resp.text}")
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"delete_message feishu err: {data}")

    def send_card(self, chat_id: str, card: Dict[str, Any]) -> None:
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{FEISHU_API}/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        if resp.status_code >= 300:
            raise RuntimeError(f"send_card failed status={resp.status_code} body={resp.text}")
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"send_card feishu err: {data}")

    def upload_file(self, file_path: str, file_name: str = "") -> str:
        path = Path(str(file_path or "")).expanduser().resolve()
        if not path.exists() or (not path.is_file()):
            raise RuntimeError(f"file not found: {path}")
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        with path.open("rb") as f:
            files = {"file": (str(file_name or path.name), f)}
            data = {"file_type": "stream", "file_name": str(file_name or path.name)}
            resp = requests.post(f"{FEISHU_API}/im/v1/files", headers=headers, data=data, files=files, timeout=60)
        if resp.status_code >= 300:
            raise RuntimeError(f"upload_file failed status={resp.status_code} body={resp.text}")
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"upload_file feishu err: {payload}")
        file_key = str((payload.get("data") or {}).get("file_key") or "").strip()
        if not file_key:
            raise RuntimeError(f"upload_file missing file_key: {payload}")
        return file_key

    def send_file_by_receive_id(self, receive_id: str, file_key: str, receive_id_type: str = "chat_id") -> str:
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{FEISHU_API}/im/v1/messages?receive_id_type={receive_id_type}"
        payload = {
            "receive_id": str(receive_id or ""),
            "msg_type": "file",
            "content": json.dumps({"file_key": str(file_key or "")}, ensure_ascii=False),
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        if resp.status_code >= 300:
            raise RuntimeError(f"send_file failed status={resp.status_code} body={resp.text}")
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"send_file feishu err: {data}")
        return str((data.get("data") or {}).get("message_id") or "")

    def send_file(self, chat_id: str, file_path: str, file_name: str = "") -> str:
        file_key = self.upload_file(file_path=file_path, file_name=file_name)
        return self.send_file_by_receive_id(chat_id, file_key, receive_id_type="chat_id")

    def create_reply_status(self, user_open_id: str, biz_id: str, text: str = "🤖 正在回复") -> bool:
        open_id = str(user_open_id or "").strip()
        biz = str(biz_id or "").strip()
        if not open_id or not biz:
            return False
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{FEISHU_API}/im/v2/app_feed_card?user_id_type=open_id"
        payload = {
            "user_ids": [open_id],
            "app_feed_card": {
                "biz_id": biz,
                "status_label": {
                    "text": str(text or "🤖 正在回复"),
                    "type": "primary",
                },
            },
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
        except Exception as exc:
            LOG.warning("create_reply_status request failed open_id=%s biz_id=%s err=%s", open_id, biz, exc)
            return False
        if resp.status_code >= 300:
            LOG.warning("create_reply_status failed status=%s body=%s", resp.status_code, resp.text)
            return False
        try:
            data = resp.json()
        except Exception:
            LOG.warning("create_reply_status invalid json body=%s", resp.text[:400])
            return False
        if data.get("code") != 0:
            LOG.warning("create_reply_status feishu err: %s", data)
            return False
        failed = ((data.get("data") or {}).get("failed_cards") or []) if isinstance(data.get("data"), dict) else []
        if failed:
            LOG.warning("create_reply_status has failed_cards: %s", failed)
            return False
        return True

    def delete_reply_status(self, user_open_id: str, biz_id: str) -> None:
        open_id = str(user_open_id or "").strip()
        biz = str(biz_id or "").strip()
        if not open_id or not biz:
            return
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{FEISHU_API}/im/v2/app_feed_card/batch?user_id_type=open_id"
        payload = {"feed_cards": [{"biz_id": biz, "user_id": open_id}]}
        try:
            resp = requests.delete(url, headers=headers, json=payload, timeout=20)
        except Exception as exc:
            LOG.warning("delete_reply_status request failed open_id=%s biz_id=%s err=%s", open_id, biz, exc)
            return
        if resp.status_code >= 300:
            LOG.warning("delete_reply_status failed status=%s body=%s", resp.status_code, resp.text)
            return
        try:
            data = resp.json()
        except Exception:
            LOG.warning("delete_reply_status invalid json body=%s", resp.text[:400])
            return
        if data.get("code") != 0:
            LOG.warning("delete_reply_status feishu err: %s", data)
            return
        failed = ((data.get("data") or {}).get("failed_cards") or []) if isinstance(data.get("data"), dict) else []
        if failed:
            LOG.warning("delete_reply_status has failed_cards: %s", failed)

    def download_image(self, image_key: str, save_path: Path) -> None:
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{FEISHU_API}/im/v1/images/{image_key}"
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(resp.content)

    def download_message_resource(self, message_id: str, file_key: str, resource_type: str, save_path: Path) -> None:
        token = self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{FEISHU_API}/im/v1/messages/{message_id}/resources/{file_key}"
        resp = requests.get(url, headers=headers, params={"type": resource_type}, timeout=60)
        resp.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(resp.content)


class ControlAPI:
    def __init__(self, base: str, api_prefix: str, api_token: str):
        self.base = base.rstrip("/")
        self.api_prefix = api_prefix
        self.api_token = api_token

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
        if not self.api_token:
            raise RuntimeError("BRIDGE_API_TOKEN is empty")
        url = f"{self.base}{self.api_prefix}{path}"
        headers = {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}
        resp = requests.request(method=method, url=url, headers=headers, json=(body or {}), timeout=timeout)
        text = resp.text or ""
        try:
            data = resp.json()
        except Exception:
            data = {"raw": text}
        if resp.status_code >= 300:
            err = data.get("detail") if isinstance(data, dict) else data
            raise RuntimeError(str(err or f"http {resp.status_code}"))
        if not isinstance(data, dict):
            raise RuntimeError(f"invalid api response: {data}")
        return data

    def status(self, chat_id: str) -> Dict[str, Any]:
        return self._call("GET", f"/chat/{chat_id}/status", None, timeout=20)

    def auth_profiles(self) -> Dict[str, Any]:
        return self._call("GET", "/auth/profiles", None, timeout=30)

    def update_config(
        self,
        chat_id: str,
        cwd: str = "",
        model: str = "",
        sandbox: str = "",
        approval_policy: str = "",
        personality: str = "",
    ) -> Dict[str, Any]:
        body = {
            "cwd": str(cwd or ""),
            "model": str(model or ""),
            "sandbox": str(sandbox or ""),
            "approval_policy": str(approval_policy or ""),
            "personality": str(personality or ""),
        }
        return self._call("POST", f"/chat/{chat_id}/config", body, timeout=20)

    def update_auth_profile(self, chat_id: str, profile: str = "") -> Dict[str, Any]:
        return self._call("POST", f"/chat/{chat_id}/auth-profile", {"profile": str(profile or "")}, timeout=30)

    def reset(self, chat_id: str, cwd: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if cwd:
            body["cwd"] = cwd
        return self._call("POST", f"/chat/{chat_id}/thread/reset", body, timeout=30)

    def interrupt(self, chat_id: str) -> Dict[str, Any]:
        return self._call("POST", f"/chat/{chat_id}/interrupt", {}, timeout=20)

    def turn(self, chat_id: str, text: str, timeout_sec: int, image_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        return self._call(
            "POST",
            f"/chat/{chat_id}/turn",
            {
                "text": text,
                "timeout_sec": int(timeout_sec),
                "image_paths": [str(p) for p in (image_paths or []) if str(p).strip()],
            },
            timeout=max(30, int(timeout_sec) + 20),
        )

    def steer(self, chat_id: str, text: str, image_paths: Optional[List[str]] = None, expected_turn_id: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "text": text,
            "image_paths": [str(p) for p in (image_paths or []) if str(p).strip()],
        }
        if expected_turn_id:
            body["expected_turn_id"] = str(expected_turn_id)
        return self._call("POST", f"/chat/{chat_id}/turn/steer", body, timeout=30)


class AppServerBotBridge:
    def __init__(
        self,
        feishu: FeishuClient,
        control: ControlAPI,
        upload_root: Path,
        menu_actions: Dict[str, str],
        projects: Dict[str, str],
        project_root: Path,
        projects_store_path: Path,
        user_chat_map_path: Path,
    ):
        self.feishu = feishu
        self.control = control
        self.upload_root = upload_root
        self.menu_actions = dict(menu_actions)
        self.project_root = Path(project_root).expanduser().resolve()
        self.projects_store_path = Path(projects_store_path).expanduser().resolve()
        self.user_chat_map_path = Path(user_chat_map_path).expanduser().resolve()
        self.projects = {str(k): str(v) for k, v in projects.items() if str(k).strip() and str(v).strip()}

        self._event_lock = threading.Lock()
        self._seen_event_ids: List[str] = []

        self._projects_lock = threading.Lock()

        self._chat_locks_guard = threading.Lock()
        self._chat_locks: Dict[str, threading.Lock] = {}

        self._pending_files_lock = threading.Lock()
        self._pending_files: Dict[str, List[str]] = {}

        self._queued_inputs_lock = threading.Lock()
        self._queued_inputs: Dict[str, List[Dict[str, Any]]] = {}

        self._user_chat_lock = threading.Lock()
        self._user_chat_map: Dict[str, str] = {}
        self._await_project_name_lock = threading.Lock()
        self._await_project_name: Dict[str, bool] = {}

        self._load_persisted_projects()
        self._load_persisted_user_chat_map()

    def handle_event_async(self, payload: Dict[str, Any]) -> None:
        threading.Thread(target=self._handle_event_safe, args=(payload,), daemon=True).start()

    def handle_menu_event_async(self, payload: Dict[str, Any]) -> None:
        threading.Thread(target=self._handle_menu_event_safe, args=(payload,), daemon=True).start()

    def handle_card_action_async(self, payload: P2CardActionTrigger) -> None:
        threading.Thread(target=self._handle_card_action_safe, args=(payload,), daemon=True).start()

    def _handle_event_safe(self, payload: Dict[str, Any]) -> None:
        try:
            self._handle_event(payload)
        except Exception as exc:
            LOG.exception("Failed handling message event: %s", exc)

    def _handle_menu_event_safe(self, payload: Dict[str, Any]) -> None:
        try:
            self._handle_menu_event(payload)
        except Exception as exc:
            LOG.exception("Failed handling menu event: %s", exc)

    def _handle_card_action_safe(self, payload: P2CardActionTrigger) -> None:
        try:
            self._handle_card_action(payload)
        except Exception as exc:
            LOG.exception("Failed handling card action: %s", exc)

    def _dedupe_event(self, event_id: str) -> bool:
        eid = str(event_id or "").strip()
        if not eid:
            return False
        with self._event_lock:
            if eid in self._seen_event_ids:
                return True
            self._seen_event_ids.append(eid)
            if len(self._seen_event_ids) > 2000:
                self._seen_event_ids = self._seen_event_ids[-1000:]
            return False

    def _chat_lock(self, chat_id: str) -> threading.Lock:
        clean_chat = str(chat_id)
        with self._chat_locks_guard:
            lock = self._chat_locks.get(clean_chat)
            if lock:
                return lock
            lock = threading.Lock()
            self._chat_locks[clean_chat] = lock
            return lock

    def _load_persisted_projects(self) -> None:
        path = self.projects_store_path
        if not path.exists():
            return
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("load projects store failed path=%s err=%s", path, exc)
            return
        if not isinstance(obj, dict):
            return
        loaded: Dict[str, str] = {}
        for k, v in obj.items():
            name = str(k or "").strip()
            p = str(v or "").strip()
            if not name or not p:
                continue
            loaded[name] = str(Path(p).expanduser().resolve())
        if not loaded:
            return
        with self._projects_lock:
            self.projects.update(loaded)

    def _persist_projects(self) -> None:
        path = self.projects_store_path
        payload: Dict[str, str] = {}
        with self._projects_lock:
            for k, v in sorted(self.projects.items()):
                name = str(k or "").strip()
                p = str(v or "").strip()
                if name and p:
                    payload[name] = p
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _load_persisted_user_chat_map(self) -> None:
        path = self.user_chat_map_path
        if not path.exists():
            return
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("load user chat map failed path=%s err=%s", path, exc)
            return
        if not isinstance(obj, dict):
            return
        loaded: Dict[str, str] = {}
        for k, v in obj.items():
            key = str(k or "").strip()
            chat = str(v or "").strip()
            if not key or not chat:
                continue
            loaded[key] = chat
        if not loaded:
            return
        with self._user_chat_lock:
            self._user_chat_map.update(loaded)

    def _persist_user_chat_map(self) -> None:
        path = self.user_chat_map_path
        with self._user_chat_lock:
            payload = {str(k): str(v) for k, v in self._user_chat_map.items() if str(k).strip() and str(v).strip()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _set_await_project_name(self, chat_id: str, enabled: bool) -> None:
        cid = str(chat_id or "")
        with self._await_project_name_lock:
            if enabled:
                self._await_project_name[cid] = True
            else:
                self._await_project_name.pop(cid, None)

    def _consume_await_project_name(self, chat_id: str) -> bool:
        cid = str(chat_id or "")
        with self._await_project_name_lock:
            if self._await_project_name.get(cid):
                self._await_project_name.pop(cid, None)
                return True
        return False

    def _is_await_project_name(self, chat_id: str) -> bool:
        cid = str(chat_id or "")
        with self._await_project_name_lock:
            return bool(self._await_project_name.get(cid))

    def _create_project_from_name(self, chat_id: str, raw_name: str) -> str:
        name = _sanitize_filename(raw_name, "")
        if not name:
            return "项目名无效，请使用字母/数字/.-_ 组合。"
        root = self.project_root
        root.mkdir(parents=True, exist_ok=True)
        proj_path = (root / name).resolve()
        created = False
        if not proj_path.exists():
            proj_path.mkdir(parents=True, exist_ok=True)
            created = True

        with self._projects_lock:
            self.projects[name] = str(proj_path)
        self._persist_projects()

        try:
            self.control.reset(chat_id=chat_id, cwd=str(proj_path))
        except Exception as exc:
            return f"项目已{'创建' if created else '注册'}，但切换失败: {exc}\nname={name}\npath={proj_path}"
        return f"项目已{'创建' if created else '注册'}并切换成功。\nname={name}\npath={proj_path}"

    def _bind_user_chat(self, sender_id: Dict[str, Any], chat_id: str) -> None:
        open_id = str(sender_id.get("open_id") or "").strip()
        user_id = str(sender_id.get("user_id") or "").strip()
        union_id = str(sender_id.get("union_id") or "").strip()
        changed = False
        with self._user_chat_lock:
            if open_id:
                if self._user_chat_map.get(open_id) != chat_id:
                    changed = True
                self._user_chat_map[open_id] = chat_id
                if self._user_chat_map.get(f"open:{open_id}") != chat_id:
                    changed = True
                self._user_chat_map[f"open:{open_id}"] = chat_id
            if user_id:
                if self._user_chat_map.get(f"user:{user_id}") != chat_id:
                    changed = True
                self._user_chat_map[f"user:{user_id}"] = chat_id
            if union_id:
                if self._user_chat_map.get(f"union:{union_id}") != chat_id:
                    changed = True
                self._user_chat_map[f"union:{union_id}"] = chat_id
        if changed:
            self._persist_user_chat_map()

    def _resolve_chat_by_user(self, open_id: str = "", user_id: str = "", union_id: str = "") -> str:
        with self._user_chat_lock:
            candidates: List[str] = []
            if open_id:
                candidates += [open_id, f"open:{open_id}"]
            if user_id:
                candidates.append(f"user:{user_id}")
            if union_id:
                candidates.append(f"union:{union_id}")
            for key in candidates:
                val = str(self._user_chat_map.get(key) or "")
                if val:
                    return val
            if SINGLE_CHAT_ONLY:
                for v in self._user_chat_map.values():
                    txt = str(v or "")
                    if txt:
                        return txt
        return ""

    def _append_pending_file(self, chat_id: str, file_path: str) -> int:
        with self._pending_files_lock:
            items = self._pending_files.setdefault(str(chat_id), [])
            items.append(str(file_path))
            return len(items)

    def _list_pending_files(self, chat_id: str) -> List[str]:
        with self._pending_files_lock:
            return list(self._pending_files.get(str(chat_id), []))

    def _consume_pending_files(self, chat_id: str) -> List[str]:
        with self._pending_files_lock:
            return list(self._pending_files.pop(str(chat_id), []))

    def _enqueue_input(self, chat_id: str, text: str, image_paths: Optional[List[str]] = None) -> int:
        payload = {
            "text": str(text or ""),
            "image_paths": [str(p) for p in (image_paths or []) if str(p).strip()],
        }
        with self._queued_inputs_lock:
            items = self._queued_inputs.setdefault(str(chat_id), [])
            items.append(payload)
            return len(items)

    def _pop_next_queued_input(self, chat_id: str) -> Optional[Dict[str, Any]]:
        with self._queued_inputs_lock:
            items = self._queued_inputs.get(str(chat_id)) or []
            if not items:
                return None
            nxt = items.pop(0)
            if not items:
                self._queued_inputs.pop(str(chat_id), None)
            return nxt

    def _drain_queued_inputs(self, chat_id: str) -> None:
        while True:
            nxt = self._pop_next_queued_input(chat_id)
            if not nxt:
                return
            text = str(nxt.get("text") or "").strip()
            image_paths = [str(p) for p in (nxt.get("image_paths") or []) if str(p).strip()]
            if not text:
                continue
            try:
                answer = self._run_turn(chat_id=chat_id, text=text, image_paths=image_paths)
                self.feishu.smart_send(chat_id, answer)
            except Exception as exc:
                self.feishu.send_text(chat_id, f"排队任务处理失败:\n{exc}")

    def _format_pending_files_text(self, chat_id: str) -> str:
        items = self._list_pending_files(chat_id)
        if not items:
            return "当前没有暂存附件。"
        lines = [f"当前暂存附件 {len(items)} 个:"]
        for idx, path in enumerate(items, start=1):
            lines.append(f"{idx}. {path}")
        lines.append("发送任意文本后，这些路径会自动带入下一轮对话。")
        return "\n".join(lines)

    def _build_prompt_with_files(self, user_text: str, files: List[str]) -> Tuple[str, List[str]]:
        if not files:
            return user_text, []

        valid_files = [str(p) for p in files if str(p).strip()]
        image_paths = [p for p in valid_files if _is_image_path(p) and Path(p).exists()]
        lines = [
            "User attached files. Local paths:",
            *[f"- {p}" for p in valid_files],
            "Use these files as context for this request.",
            "If a file is non-text/binary, explain how to inspect it first.",
            "",
            "User request:",
            user_text,
        ]
        return "\n".join(lines).strip(), image_paths

    def _extract_output_files(self, answer_text: str, exclude_paths: Optional[List[str]] = None) -> List[Path]:
        if not OUTPUT_FILE_AUTO_SEND:
            return []
        raw = str(answer_text or "")
        if not raw:
            return []
        excludes = {str(Path(p).resolve()) for p in (exclude_paths or []) if str(p).strip()}
        now = time.time()
        paths: List[Path] = []
        seen: set[str] = set()
        for token in re.findall(r"/[A-Za-z0-9._/\- ]+", raw):
            candidate = token.strip().rstrip("`'\".,;:!?)]")
            if not candidate:
                continue
            p = Path(candidate).expanduser()
            if not p.is_absolute():
                continue
            try:
                resolved = p.resolve()
            except Exception:
                continue
            if str(resolved) in seen or str(resolved) in excludes:
                continue
            if not resolved.exists() or (not resolved.is_file()):
                continue
            if resolved.suffix.lower() not in OUTPUT_FILE_SUFFIXES:
                continue
            try:
                st = resolved.stat()
            except Exception:
                continue
            if st.st_size > OUTPUT_FILE_MAX_SIZE_MB * 1024 * 1024:
                continue
            if (now - float(st.st_mtime)) > float(OUTPUT_FILE_MAX_AGE_SEC):
                continue
            seen.add(str(resolved))
            paths.append(resolved)
            if OUTPUT_FILE_MAX_COUNT > 0 and len(paths) >= OUTPUT_FILE_MAX_COUNT:
                break
        return paths

    def _send_output_files(self, chat_id: str, files: List[Path]) -> None:
        if not files:
            return
        sent: List[str] = []
        for p in files:
            try:
                self.feishu.send_file(chat_id=chat_id, file_path=str(p), file_name=p.name)
                sent.append(p.name)
            except Exception as exc:
                LOG.warning("send output file failed chat_id=%s path=%s err=%s", chat_id, p, exc)
        if sent:
            self.feishu.send_text(chat_id, "已回传结果文件: " + ", ".join(sent))

    def _extract_resource_key(self, msg_type: str, content: Dict[str, Any]) -> str:
        if str(msg_type) == "image":
            return str(content.get("image_key") or "").strip()
        for key in ["file_key", "media_key", "audio_key", "video_key", "sticker_key", "image_key", "file_token"]:
            val = str(content.get(key) or "").strip()
            if val:
                return val
        return ""

    def _extract_post_text_and_resources(self, content: Dict[str, Any]) -> Tuple[str, List[Tuple[str, Dict[str, Any]]]]:
        post = content.get("post") if isinstance(content.get("post"), dict) else {}
        lines: List[str] = []
        resources: List[Tuple[str, Dict[str, Any]]] = []
        for lang_payload in post.values():
            if not isinstance(lang_payload, dict):
                continue
            rows = lang_payload.get("content")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, list):
                    continue
                segs: List[str] = []
                for node in row:
                    if not isinstance(node, dict):
                        continue
                    tag = str(node.get("tag") or "").strip().lower()
                    if tag == "text":
                        txt = str(node.get("text") or "").strip()
                        if txt:
                            segs.append(txt)
                        continue
                    if tag == "a":
                        txt = str(node.get("text") or node.get("href") or "").strip()
                        if txt:
                            segs.append(txt)
                        continue
                    if tag == "at":
                        txt = str(node.get("user_name") or node.get("name") or "").strip()
                        if txt:
                            segs.append(f"@{txt}")
                        continue
                    if tag == "img":
                        image_key = str(node.get("image_key") or "").strip()
                        if image_key:
                            resources.append(("image", {"image_key": image_key}))
                        continue
                    if tag in {"file", "media", "audio", "video"}:
                        obj: Dict[str, Any] = {}
                        for key in ("file_key", "media_key", "audio_key", "video_key", "file_token", "file_name", "name", "title"):
                            val = node.get(key)
                            if val not in (None, ""):
                                obj[key] = val
                        if obj:
                            resources.append((tag, obj))
                        continue
                line = "".join(segs).strip()
                if line:
                    lines.append(line)
        text = "\n".join(lines).strip()
        uniq: List[Tuple[str, Dict[str, Any]]] = []
        seen: set[str] = set()
        for t, obj in resources:
            key = self._extract_resource_key(t, obj)
            if not key:
                continue
            sig = f"{t}:{key}"
            if sig in seen:
                continue
            seen.add(sig)
            uniq.append((t, obj))
        return text, uniq

    def _download_attachment(self, chat_id: str, msg_type: str, message_id: str, content: Dict[str, Any]) -> str:
        file_key = self._extract_resource_key(msg_type, content)
        if not file_key:
            raise RuntimeError(f"missing resource key for message type={msg_type}")

        raw_name = str(content.get("file_name") or content.get("name") or content.get("title") or "").strip()
        base_name = _sanitize_filename(raw_name, f"{msg_type}_{file_key[:10]}")
        suffix = Path(base_name).suffix.lower()
        if not suffix:
            base_name += _guess_suffix(msg_type)

        chat_dir = self.upload_root / _sanitize_filename(chat_id, "chat")
        chat_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        save_path = chat_dir / f"{ts}_{base_name}"

        if msg_type == "image":
            if message_id:
                try:
                    self.feishu.download_message_resource(
                        message_id=message_id,
                        file_key=file_key,
                        resource_type="image",
                        save_path=save_path,
                    )
                    return str(save_path)
                except Exception as exc:
                    LOG.warning(
                        "download image via message resource failed, fallback to images api: message_id=%s key=%s err=%s",
                        message_id,
                        file_key,
                        exc,
                    )
            self.feishu.download_image(file_key, save_path)
            return str(save_path)

        if not message_id:
            raise RuntimeError("missing message_id for non-image resource")

        resource_types = [msg_type, "file", "media", "audio", "video", "image"]
        tried: List[str] = []
        last_exc: Exception = RuntimeError("download failed")
        for t in resource_types:
            if t in tried:
                continue
            tried.append(t)
            try:
                self.feishu.download_message_resource(message_id=message_id, file_key=file_key, resource_type=t, save_path=save_path)
                return str(save_path)
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"download failed for key={file_key} tried={tried}: {last_exc}")

    def _current_project_name(self, cwd: str) -> str:
        target = str(cwd or "").strip()
        for name, path in self.projects.items():
            if Path(path).resolve() == Path(target).resolve():
                return name
        return ""

    def _status_data(self, chat_id: str) -> Dict[str, Any]:
        data = self.control.status(chat_id).get("data")
        if isinstance(data, dict):
            return data
        return {}

    def _status_text(self, chat_id: str) -> str:
        data = self._status_data(chat_id)
        thread_id = str(data.get("thread_id") or "<none>")
        turn_id = str(data.get("active_turn_id") or "<none>")
        tstatus = data.get("thread_status") if isinstance(data.get("thread_status"), dict) else {}
        token_usage_wrap = data.get("token_usage") if isinstance(data.get("token_usage"), dict) else {}
        token_usage = token_usage_wrap.get("tokenUsage") if isinstance(token_usage_wrap.get("tokenUsage"), dict) else {}
        rate_limits = data.get("rate_limits") if isinstance(data.get("rate_limits"), dict) else {}
        total_usage = token_usage.get("total") if isinstance(token_usage.get("total"), dict) else {}
        last_usage = token_usage.get("last") if isinstance(token_usage.get("last"), dict) else {}
        model_ctx = token_usage.get("modelContextWindow")
        pending_count = len(self._list_pending_files(chat_id))
        cwd = str(data.get("cwd") or "")
        proj = self._current_project_name(cwd)
        auto_switch_enabled = bool(data.get("auto_auth_switch_enabled"))
        auto_switch_threshold = int(data.get("auto_auth_switch_threshold_pct") or 0)
        last_auto_switch = data.get("last_auto_auth_switch") if isinstance(data.get("last_auto_auth_switch"), dict) else {}
        usage_lines: List[str] = []
        if total_usage:
            usage_lines.append(
                "token_total="
                + json.dumps(
                    {
                        "total": total_usage.get("totalTokens"),
                        "input": total_usage.get("inputTokens"),
                        "cached_input": total_usage.get("cachedInputTokens"),
                        "output": total_usage.get("outputTokens"),
                        "reasoning_output": total_usage.get("reasoningOutputTokens"),
                    },
                    ensure_ascii=False,
                )
            )
        if last_usage:
            usage_lines.append(
                "token_last="
                + json.dumps(
                    {
                        "total": last_usage.get("totalTokens"),
                        "input": last_usage.get("inputTokens"),
                        "cached_input": last_usage.get("cachedInputTokens"),
                        "output": last_usage.get("outputTokens"),
                        "reasoning_output": last_usage.get("reasoningOutputTokens"),
                    },
                    ensure_ascii=False,
                )
            )
        if model_ctx is not None:
            usage_lines.append(f"model_context_window={model_ctx}")
        usage_lines.extend(_format_rate_limit_lines(rate_limits))
        last_from = str(last_auto_switch.get("from") or "").strip() or "default"
        last_to = str(last_auto_switch.get("to") or "").strip() or "default"
        last_reason = str(last_auto_switch.get("reason") or "").strip()
        last_at = int(last_auto_switch.get("at") or 0)
        auto_switch_lines = [f"auto_auth_switch={'on' if auto_switch_enabled else 'off'} threshold={auto_switch_threshold}%"]
        if last_at > 0:
            last_when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_at))
            line = f"last_auto_switch={last_from}->{last_to} at={last_when}"
            if last_reason:
                line += f" reason={last_reason}"
            auto_switch_lines.append(line)
        return (
            "Status\n"
            f"chat_id={chat_id}\n"
            f"project={proj or '<custom>'}\n"
            f"cwd={cwd}\n"
            f"thread_id={thread_id}\n"
            f"active_turn_id={turn_id}\n"
            f"thread_status={json.dumps(tstatus, ensure_ascii=False)}\n"
            f"pending_files={pending_count}\n"
            f"model={data.get('model') or ''}\n"
            f"auth_profile={data.get('auth_profile') or 'default'}\n"
            f"auth_identity={data.get('auth_identity') or ''}\n"
            + "\n".join(auto_switch_lines)
            + ("\n" + "\n".join(usage_lines) if usage_lines else "")
        )

    def _progress_ping_text(self, chat_id: str, started_at: float) -> str:
        data = self._status_data(chat_id)
        elapsed = max(0, int(time.time() - float(started_at or time.time())))
        mins, secs = divmod(elapsed, 60)
        active_turn = str(data.get("active_turn_id") or "<none>")
        thread_status = data.get("thread_status") if isinstance(data.get("thread_status"), dict) else {}
        progress = data.get("turn_progress") if isinstance(data.get("turn_progress"), dict) else {}
        preview = _trim(str(progress.get("preview") or "").strip().replace("\n", " "), 320)
        lines = [
            f"⏳ 任务仍在执行（{mins}m{secs:02d}s）",
            f"turn={active_turn}",
            f"thread_status={json.dumps(thread_status, ensure_ascii=False)}",
        ]
        if preview:
            lines.append(f"current: {preview}")
        return "\n".join(lines)

    def _progress_ping_loop(
        self,
        chat_id: str,
        started_at: float,
        stop_event: threading.Event,
        reply_tip_mid: str,
    ) -> None:
        while not stop_event.wait(PROGRESS_PING_INTERVAL_SEC):
            try:
                text = self._progress_ping_text(chat_id=chat_id, started_at=started_at)
                if reply_tip_mid:
                    self.feishu.update_text(reply_tip_mid, text)
                else:
                    self.feishu.send_text(chat_id, text)
            except Exception as exc:
                LOG.warning("progress ping failed chat_id=%s err=%s", chat_id, exc)

    def _run_turn(self, chat_id: str, text: str, image_paths: Optional[List[str]] = None) -> str:
        resp = self.control.turn(chat_id=chat_id, text=text, timeout_sec=TURN_TIMEOUT_SEC, image_paths=image_paths or [])
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        answer = str(data.get("assistant_text") or "").strip()
        switch_info = data.get("auto_auth_switch") if isinstance(data.get("auto_auth_switch"), dict) else {}
        if switch_info:
            from_profile = str(switch_info.get("from") or "").strip() or "default"
            to_profile = str(switch_info.get("to") or "").strip() or "default"
            identity = str(switch_info.get("identity") or "").strip()
            note = f"已自动切换账号：`{from_profile}` -> `{to_profile}`"
            if identity:
                note += f" ({identity})"
            answer = (note + "\n\n" + answer).strip()
        return answer or "(assistant returned empty text)"

    def _run_session_command(self, chat_id: str, cmd_text: str) -> str:
        lock = self._chat_lock(chat_id)
        if not lock.acquire(blocking=False):
            queued = self._enqueue_input(chat_id=chat_id, text=cmd_text, image_paths=[])
            return f"当前任务执行中，命令已加入队列（第{queued}条）。"
        try:
            try:
                answer = self._run_turn(chat_id=chat_id, text=cmd_text, image_paths=[])
                self._drain_queued_inputs(chat_id)
                return answer
            except Exception as first_exc:
                first_err = str(first_exc)
                recoverable = (
                    "disconnected while waiting for turn completion" in first_err
                    or "Timeout waiting for turn completion" in first_err
                    or "app-server timeout for method=" in first_err
                )
                if not recoverable:
                    return f"会话命令失败: {first_err}"

                recovery_steps: List[str] = []
                try:
                    self.control.interrupt(chat_id=chat_id)
                    recovery_steps.append("interrupt=ok")
                except Exception as exc:
                    recovery_steps.append(f"interrupt=err({exc})")

                try:
                    self.control.reset(chat_id=chat_id)
                    recovery_steps.append("reset=ok")
                except Exception as exc:
                    recovery_steps.append(f"reset=err({exc})")
                    return f"会话命令失败: {first_err}\n自动恢复失败: {'; '.join(recovery_steps)}"

                try:
                    answer = self._run_turn(chat_id=chat_id, text=cmd_text, image_paths=[])
                    self._drain_queued_inputs(chat_id)
                    return answer
                except Exception as second_exc:
                    return (
                        f"会话命令失败: {first_err}\n"
                        f"已尝试自动恢复({'; '.join(recovery_steps)})后仍失败: {second_exc}"
                    )
        finally:
            lock.release()

    def _card_header(self, title: str) -> Dict[str, Any]:
        return {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        }

    def _action_button(self, label: str, value: Dict[str, Any], btn_type: str = "default") -> Dict[str, Any]:
        return {
            "tag": "button",
            "type": btn_type,
            "text": {"tag": "plain_text", "content": label},
            "value": value,
        }

    def _build_project_manage_card(self, chat_id: str) -> Dict[str, Any]:
        status = self._status_data(chat_id)
        cwd = str(status.get("cwd") or "")
        project_name = self._current_project_name(cwd)
        lines = [
            f"当前项目: `{project_name or '<custom>'}`",
            f"cwd: `{cwd}`",
            f"thread: `{status.get('thread_id') or '<none>'}`",
        ]
        if self._is_await_project_name(chat_id):
            lines.append("新建项目流程中：请直接发送项目名，或发送 `/cancel` 取消。")

        rows: List[Dict[str, Any]] = [
            {"tag": "markdown", "content": "\n".join(lines)},
        ]

        proj_buttons: List[Dict[str, Any]] = []
        with self._projects_lock:
            items = sorted(self.projects.items())
        for name, path in items:
            label = name if len(name) <= 18 else (name[:15] + "...")
            btype = "primary" if name == project_name else "default"
            proj_buttons.append(self._action_button(label, {"op": "project_switch", "project": name}, btn_type=btype))

        if proj_buttons:
            chunk_size = 3
            for idx in range(0, len(proj_buttons), chunk_size):
                rows.append({"tag": "action", "actions": proj_buttons[idx : idx + chunk_size]})

        rows.append(
            {
                "tag": "action",
                "actions": [
                    self._action_button("新建项目", {"op": "project_create_begin"}, btn_type="primary"),
                    self._action_button("刷新状态", {"op": "open_project_manage"}),
                    self._action_button("会话管理", {"op": "open_session_manage"}),
                ],
            }
        )

        return {
            "config": {"wide_screen_mode": True},
            "header": self._card_header("项目管理"),
            "elements": rows,
        }

    def _build_session_manage_card(self, chat_id: str) -> Dict[str, Any]:
        status = self._status_data(chat_id)
        tstatus = status.get("thread_status") if isinstance(status.get("thread_status"), dict) else {}
        auth_profile = str(status.get("auth_profile") or "").strip() or "default"
        auth_identity = str(status.get("auth_identity") or "").strip()
        auto_switch_enabled = bool(status.get("auto_auth_switch_enabled"))
        auto_switch_threshold = int(status.get("auto_auth_switch_threshold_pct") or 0)
        last_auto_switch = status.get("last_auto_auth_switch") if isinstance(status.get("last_auto_auth_switch"), dict) else {}
        lines = [
            f"thread: `{status.get('thread_id') or '<none>'}`",
            f"active_turn: `{status.get('active_turn_id') or '<none>'}`",
            f"thread_status: `{json.dumps(tstatus, ensure_ascii=False)}`",
            f"model: `{status.get('model') or ''}`",
            f"account: `{auth_profile}`" + (f" ({auth_identity})" if auth_identity else ""),
            f"auto_switch: `{'on' if auto_switch_enabled else 'off'} / {auto_switch_threshold}%`",
            f"pending_files: `{len(self._list_pending_files(chat_id))}`",
        ]
        last_from = str(last_auto_switch.get("from") or "").strip() or "default"
        last_to = str(last_auto_switch.get("to") or "").strip() or "default"
        last_reason = str(last_auto_switch.get("reason") or "").strip()
        last_at = int(last_auto_switch.get("at") or 0)
        if last_at > 0:
            last_when = time.strftime("%m-%d %H:%M", time.localtime(last_at))
            lines.append(f"last_switch: `{last_from} -> {last_to} @ {last_when}`")
            if last_reason:
                lines.append(f"reason: `{_trim(last_reason, 180)}`")
        return {
            "config": {"wide_screen_mode": True},
            "header": self._card_header("会话管理（Codex 内部命令）"),
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
                {
                    "tag": "action",
                    "actions": [
                        self._action_button("状态 /status", {"op": "session_cmd", "cmd": "/status"}),
                        self._action_button("审批 /approvals", {"op": "session_cmd", "cmd": "/approvals"}),
                        self._action_button("权限 /permissions", {"op": "session_cmd", "cmd": "/permissions"}),
                    ],
                },
                {
                    "tag": "action",
                    "actions": [
                        self._action_button("切换模型", {"op": "session_model_start"}, btn_type="primary"),
                        self._action_button("切换账号", {"op": "session_auth_start"}),
                        self._action_button("中断", {"op": "session_interrupt"}),
                    ],
                },
                {"tag": "action", "actions": [self._action_button("项目管理", {"op": "open_project_manage"})]},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "注：文本输入会直通 Codex；斜杠命令建议通过本卡片触发。",
                        }
                    ],
                },
            ],
        }

    def _build_auth_select_card(self, current_profile: str, profiles: List[Dict[str, Any]]) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = [
            {
                "tag": "markdown",
                "content": "选择要切换的账号。\n\n放置规范：`data/auth_profiles/<profile>.auth.json`，可选配套 `data/auth_profiles/<profile>.config.toml`。",
            }
        ]
        buttons: List[Dict[str, Any]] = []
        for item in profiles:
            profile = str(item.get("profile") or "").strip()
            label = profile or "default"
            identity = str(item.get("email") or "").strip()
            valid = bool(item.get("valid"))
            if identity:
                rows.append({"tag": "markdown", "content": f"- `{label}` : `{identity}`"})
            else:
                rows.append({"tag": "markdown", "content": f"- `{label}`"})
            btn_type = "primary" if (profile or "default") == (current_profile or "default") else "default"
            if valid:
                buttons.append(self._action_button(label, {"op": "session_auth_apply", "profile": profile}, btn_type=btn_type))
        if buttons:
            for idx in range(0, len(buttons), 3):
                rows.append({"tag": "action", "actions": buttons[idx : idx + 3]})
        rows.append({"tag": "action", "actions": [self._action_button("返回会话管理", {"op": "open_session_manage"})]})
        return {
            "config": {"wide_screen_mode": True},
            "header": self._card_header("切换账号"),
            "elements": rows,
        }

    def _build_model_select_card(self, models: List[str]) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = [
            {
                "tag": "markdown",
                "content": "步骤 1/3：选择模型\n\n请选择要切换的模型。",
            }
        ]
        buttons = [
            self._action_button(m, {"op": "session_model_pick", "model": m}, btn_type="primary")
            for m in models[:12]
        ]
        for idx in range(0, len(buttons), 3):
            rows.append({"tag": "action", "actions": buttons[idx : idx + 3]})
        rows.append({"tag": "action", "actions": [self._action_button("返回会话管理", {"op": "open_session_manage"})]})
        return {
            "config": {"wide_screen_mode": True},
            "header": self._card_header("切换模型 / Step1"),
            "elements": rows,
        }

    def _build_effort_select_card(self, model: str) -> Dict[str, Any]:
        actions = [
            self._action_button(
                f"{effort}",
                {"op": "session_model_apply", "model": model, "effort": effort},
                btn_type="primary" if effort == "high" else "default",
            )
            for effort in SUPPORTED_EFFORTS
        ]
        return {
            "config": {"wide_screen_mode": True},
            "header": self._card_header("切换模型 / Step2"),
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"步骤 2/3：选择推理强度\n\n已选模型：`{model}`",
                },
                {"tag": "action", "actions": actions},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "下一步将执行：/model use <model> + /effort <level>",
                        }
                    ],
                },
            ],
        }

    def _handle_text(self, chat_id: str, text: str, sender_open_id: str = "", message_id: str = "") -> None:
        raw = str(text or "").strip()
        if not raw:
            return
        if self._consume_await_project_name(chat_id):
            if raw.lower() in {"/cancel", "cancel", "取消"}:
                self.feishu.send_text(chat_id, "已取消新建项目。")
                return
            result = self._create_project_from_name(chat_id=chat_id, raw_name=raw)
            self.feishu.send_text(chat_id, result)
            self.feishu.send_card(chat_id, self._build_project_manage_card(chat_id))
            return

        pending_files = self._consume_pending_files(chat_id)
        prompt, image_paths = self._build_prompt_with_files(raw, pending_files)
        typing_biz_id = ""
        has_app_feed_status = False
        reply_tip_mid = ""
        reply_tip_consumed = False
        progress_stop = threading.Event()
        progress_thread: Optional[threading.Thread] = None
        sender_open_id = str(sender_open_id or "").strip()
        if sender_open_id:
            msg = str(message_id or "").strip()
            if msg:
                typing_biz_id = f"typing:{msg}"
            else:
                typing_biz_id = f"typing:{chat_id}:{int(time.time() * 1000)}"
            has_app_feed_status = self.feishu.create_reply_status(sender_open_id, typing_biz_id, "🤖 正在回复")
        if (not has_app_feed_status) and str(message_id or "").strip():
            try:
                reply_tip_mid = self.feishu.reply_text(str(message_id), "🤖 正在回复...")
            except Exception as exc:
                LOG.warning("reply tip create failed message_id=%s err=%s", message_id or "<none>", exc)

        try:
            lock = self._chat_lock(chat_id)
            if not lock.acquire(blocking=False):
                LOG.info("chat busy, steering message chat_id=%s message_id=%s", chat_id, message_id or "<none>")
                try:
                    self.control.steer(chat_id=chat_id, text=prompt, image_paths=image_paths)
                    if reply_tip_mid:
                        try:
                            self.feishu.delete_message(reply_tip_mid)
                        except Exception as exc:
                            LOG.warning("reply tip delete failed message_id=%s err=%s", reply_tip_mid, exc)
                except Exception as steer_exc:
                    queued = self._enqueue_input(chat_id=chat_id, text=prompt, image_paths=image_paths)
                    if reply_tip_mid:
                        try:
                            self.feishu.update_text(reply_tip_mid, f"已加入队列（第{queued}条）")
                            reply_tip_consumed = True
                        except Exception as exc:
                            LOG.warning("reply tip update failed message_id=%s err=%s", reply_tip_mid, exc)
                            self.feishu.send_text(chat_id, f"steer 失败，已加入队列（第{queued}条）: {steer_exc}")
                    else:
                        self.feishu.send_text(chat_id, f"steer 失败，已加入队列（第{queued}条）: {steer_exc}")
                return
            try:
                start = time.time()
                LOG.info("turn start chat_id=%s message_id=%s", chat_id, message_id or "<none>")
                progress_thread = threading.Thread(
                    target=self._progress_ping_loop,
                    args=(chat_id, start, progress_stop, reply_tip_mid),
                    daemon=True,
                )
                progress_thread.start()
                if pending_files:
                    self.feishu.send_text(chat_id, f"检测到 {len(pending_files)} 个附件，已自动带入本轮对话。")
                answer = self._run_turn(chat_id=chat_id, text=prompt, image_paths=image_paths)
                output_files = self._extract_output_files(answer_text=answer, exclude_paths=pending_files)
                progress_stop.set()
                if progress_thread:
                    progress_thread.join(timeout=1.0)
                if reply_tip_mid:
                    try:
                        self.feishu.delete_message(reply_tip_mid)
                        reply_tip_consumed = True
                        self.feishu.smart_send(chat_id, answer)
                    except Exception as exc:
                        LOG.warning("reply tip update final failed message_id=%s err=%s", reply_tip_mid, exc)
                        self.feishu.smart_send(chat_id, answer)
                else:
                    self.feishu.smart_send(chat_id, answer)
                self._send_output_files(chat_id, output_files)
                self._drain_queued_inputs(chat_id)
                LOG.info("turn done chat_id=%s message_id=%s elapsed=%.3fs", chat_id, message_id or "<none>", time.time() - start)
            except Exception as exc:
                LOG.exception("turn failed chat_id=%s message_id=%s", chat_id, message_id or "<none>")
                progress_stop.set()
                if progress_thread:
                    progress_thread.join(timeout=1.0)
                if reply_tip_mid:
                    try:
                        self.feishu.update_text(reply_tip_mid, f"处理失败:\n{exc}")
                        reply_tip_consumed = True
                    except Exception:
                        self.feishu.send_text(chat_id, f"处理失败:\n{exc}")
                else:
                    self.feishu.send_text(chat_id, f"处理失败:\n{exc}")
            finally:
                progress_stop.set()
                if progress_thread and progress_thread.is_alive():
                    progress_thread.join(timeout=0.5)
                lock.release()
        finally:
            progress_stop.set()
            if progress_thread and progress_thread.is_alive():
                progress_thread.join(timeout=0.5)
            if typing_biz_id and sender_open_id and has_app_feed_status:
                self.feishu.delete_reply_status(sender_open_id, typing_biz_id)
            if reply_tip_mid and (not reply_tip_consumed):
                try:
                    self.feishu.delete_message(reply_tip_mid)
                except Exception as exc:
                    LOG.warning("reply tip cleanup failed message_id=%s err=%s", reply_tip_mid, exc)

    def _handle_event(self, payload: Dict[str, Any]) -> None:
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        if str(header.get("event_type") or "") != "im.message.receive_v1":
            return

        event_id = str(header.get("event_id") or "")
        if self._dedupe_event(event_id):
            return

        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        if str(sender.get("sender_type") or "") == "app":
            return

        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        chat_id = str(message.get("chat_id") or "")
        if not chat_id:
            return

        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        self._bind_user_chat(sender_id=sender_id, chat_id=chat_id)

        chat_type = str(message.get("chat_type") or "").strip().lower()
        if SINGLE_CHAT_ONLY and chat_type and chat_type != "p2p":
            self.feishu.send_text(chat_id, "This bot is configured for 1:1 chat only.")
            return

        msg_type = str(message.get("message_type") or "").strip().lower()
        content = _parse_content_json(message.get("content"))

        if msg_type == "text":
            text = str(content.get("text") or "").strip()
            sender_open_id = str(sender_id.get("open_id") or "").strip()
            message_id = str(message.get("message_id") or "").strip()
            self._handle_text(chat_id, text, sender_open_id=sender_open_id, message_id=message_id)
            return

        if msg_type in SUPPORTED_ATTACHMENT_TYPES:
            message_id = str(message.get("message_id") or "")
            try:
                saved = self._download_attachment(chat_id=chat_id, msg_type=msg_type, message_id=message_id, content=content)
                staged = self._append_pending_file(chat_id, saved)
                self.feishu.send_text(
                    chat_id,
                    f"已暂存附件 ({msg_type})\npath={saved}\n当前待处理附件: {staged}\n发送一条文本后将自动带入。",
                )
            except Exception as exc:
                self.feishu.send_text(chat_id, f"附件下载失败: {exc}")
            return

        if msg_type == "post":
            message_id = str(message.get("message_id") or "")
            sender_open_id = str(sender_id.get("open_id") or "").strip()
            post_text, post_resources = self._extract_post_text_and_resources(content)

            if post_resources:
                for rtype, rcontent in post_resources:
                    try:
                        saved = self._download_attachment(
                            chat_id=chat_id,
                            msg_type=rtype,
                            message_id=message_id,
                            content=rcontent,
                        )
                        self._append_pending_file(chat_id, saved)
                    except Exception as exc:
                        LOG.warning(
                            "post resource download failed chat_id=%s message_id=%s type=%s err=%s",
                            chat_id,
                            message_id,
                            rtype,
                            exc,
                        )

            if post_text:
                self._handle_text(chat_id, post_text, sender_open_id=sender_open_id, message_id=message_id)
                return

            staged = len(self._list_pending_files(chat_id))
            if staged > 0:
                self.feishu.send_text(chat_id, f"已暂存 {staged} 个 post 附件。请再发一条文本指令继续处理。")
                return

            self.feishu.send_text(chat_id, "post 消息未解析到可处理内容，请补发文本或文件。")
            return

        self.feishu.send_text(chat_id, f"Unsupported message type: {msg_type}")

    def _handle_menu_event(self, payload: Dict[str, Any]) -> None:
        event_key = str(payload.get("event_key") or "").strip()
        open_id = str(payload.get("open_id") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        union_id = str(payload.get("union_id") or "").strip()

        if not event_key:
            return
        action = str(self.menu_actions.get(event_key) or "").strip()
        if not action:
            if open_id:
                self.feishu.send_text_by_receive_id(open_id, f"未配置菜单动作: {event_key}", receive_id_type="open_id")
            return

        chat_id = self._resolve_chat_by_user(open_id=open_id, user_id=user_id, union_id=union_id)
        if not chat_id:
            if open_id:
                self.feishu.send_text_by_receive_id(
                    open_id,
                    "菜单已点击，但尚未绑定会话。请先给机器人发送一条消息。",
                    receive_id_type="open_id",
                )
            return

        LOG.info("menu event mapped: key=%s action=%s chat_id=%s", event_key, action, chat_id)
        self._run_card_action(chat_id=chat_id, op=action, value={})

    def _handle_card_action(self, payload: P2CardActionTrigger) -> None:
        event = payload.event
        if not event:
            return

        raw_value: Any = event.action.value if event.action else None
        value: Dict[str, Any] = {}
        if isinstance(raw_value, dict):
            value = raw_value
        elif isinstance(raw_value, str):
            try:
                parsed = json.loads(raw_value)
                if isinstance(parsed, dict):
                    value = parsed
            except Exception:
                value = {}
        op = str(value.get("op") or "").strip()
        if not op:
            LOG.warning("card action dropped: missing op raw_type=%s raw_value=%s", type(raw_value).__name__, str(raw_value)[:300])
            return

        operator = event.operator
        open_id = str(getattr(operator, "open_id", "") or "")
        user_id = str(getattr(operator, "user_id", "") or "")
        union_id = str(getattr(operator, "union_id", "") or "")

        ctx = event.context
        chat_id = str(getattr(ctx, "open_chat_id", "") or "")
        source_message_id = str(getattr(ctx, "open_message_id", "") or "")
        if not chat_id:
            chat_id = self._resolve_chat_by_user(open_id=open_id, user_id=user_id, union_id=union_id)
        if not chat_id:
            LOG.warning("card action dropped: no chat mapping op=%s", op)
            return

        self._bind_user_chat({"open_id": open_id, "user_id": user_id, "union_id": union_id}, chat_id)
        self._run_card_action(chat_id=chat_id, op=op, value=value)
        if CARD_AUTO_DELETE_ON_ACTION and source_message_id:
            try:
                self.feishu.delete_message(source_message_id)
            except Exception as exc:
                LOG.warning("delete card message failed message_id=%s op=%s err=%s", source_message_id, op, exc)

    def _run_card_action(self, chat_id: str, op: str, value: Dict[str, Any]) -> None:
        LOG.info("card action: op=%s chat_id=%s value=%s", op, chat_id, value)

        if op == "open_project_manage":
            self.feishu.send_card(chat_id, self._build_project_manage_card(chat_id))
            return

        if op == "open_session_manage":
            self.feishu.send_card(chat_id, self._build_session_manage_card(chat_id))
            return

        if op == "project_switch":
            project = str(value.get("project") or "").strip()
            cwd = str(self.projects.get(project) or "").strip()
            if not cwd:
                self.feishu.send_text(chat_id, f"未知项目: {project}")
                return
            try:
                self._set_await_project_name(chat_id, False)
                self.control.reset(chat_id=chat_id, cwd=cwd)
                self.feishu.send_text(chat_id, f"已切换项目: {project}\ncwd={cwd}")
            except Exception as exc:
                self.feishu.send_text(chat_id, f"切换项目失败: {exc}")
            return

        if op == "project_create_begin":
            self._set_await_project_name(chat_id, True)
            self.feishu.send_text(
                chat_id,
                f"请输入新项目名（将创建到 `{self.project_root}`）。\n示例：`my_new_project`\n发送 `/cancel` 可取消。",
            )
            return

        if op == "session_interrupt":
            try:
                result = self.control.interrupt(chat_id)
                self.feishu.send_text(chat_id, _trim(f"中断结果:\n{json.dumps(result, ensure_ascii=False)}", 900))
            except Exception as exc:
                self.feishu.send_text(chat_id, f"中断失败: {exc}")
            return

        if op == "session_cmd":
            cmd = str(value.get("cmd") or "").strip()
            if not cmd:
                self.feishu.send_text(chat_id, "空命令，已忽略")
                return
            if cmd == "/status":
                answer = self._status_text(chat_id)
            else:
                answer = self._run_session_command(chat_id=chat_id, cmd_text=cmd)
            self.feishu.smart_send(chat_id, _trim(answer, 3000), title=f"Codex {cmd}")
            return

        if op == "session_model_start":
            answer = self._run_session_command(chat_id=chat_id, cmd_text="/model list")
            models = _parse_model_candidates(answer)
            if not models:
                self.feishu.send_text(chat_id, f"未解析到可用模型，原始返回：\n{_trim(answer, 2000)}")
                return
            self.feishu.send_card(chat_id, self._build_model_select_card(models))
            return

        if op == "session_auth_start":
            resp = self.control.auth_profiles()
            data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            profiles = data.get("profiles") if isinstance(data.get("profiles"), list) else []
            current_profile = str(self._status_data(chat_id).get("auth_profile") or "").strip()
            self.feishu.send_card(chat_id, self._build_auth_select_card(current_profile=current_profile, profiles=profiles))
            return

        if op == "session_auth_apply":
            profile = str(value.get("profile") or "").strip()
            try:
                result = self.control.update_auth_profile(chat_id=chat_id, profile=profile)
                data = result.get("data") if isinstance(result.get("data"), dict) else {}
                self.control.reset(chat_id=chat_id)
                label = str(data.get("auth_profile") or "").strip() or "default"
                identity = str(data.get("auth_identity") or "").strip()
                suffix = f" ({identity})" if identity else ""
                self.feishu.send_text(chat_id, f"已切换账号: {label}{suffix}")
            except Exception as exc:
                self.feishu.send_text(chat_id, f"切换账号失败: {exc}")
            return

        if op == "session_model_pick":
            model = str(value.get("model") or "").strip()
            if not model:
                self.feishu.send_text(chat_id, "未选择模型")
                return
            self.feishu.send_card(chat_id, self._build_effort_select_card(model))
            return

        if op == "session_model_apply":
            model = str(value.get("model") or "").strip()
            effort = str(value.get("effort") or "").strip().lower()
            if not model or effort not in SUPPORTED_EFFORTS:
                self.feishu.send_text(chat_id, "模型或推理强度参数无效")
                return

            result_model = self._run_session_command(chat_id=chat_id, cmd_text=f"/model use {model}")
            result_effort = self._run_session_command(chat_id=chat_id, cmd_text=f"/effort {effort}")
            verify = self._run_session_command(chat_id=chat_id, cmd_text="/status")
            synced_model = _extract_status_value(verify, "effective_model") or _extract_status_value(verify, "model") or model
            sync_note = ""
            try:
                self.control.update_config(chat_id=chat_id, model=synced_model)
            except Exception as exc:
                sync_note = f"\n\nconfig_sync_failed: {exc}"
            self.feishu.smart_send(
                chat_id,
                text=_trim(
                    "模型切换完成。\n\n"
                    f"/model use {model}\n{result_model}\n\n"
                    f"/effort {effort}\n{result_effort}\n\n"
                    f"verify:\n{verify}{sync_note}",
                    3500,
                ),
                title="模型切换结果",
            )
            return

        self.feishu.send_text(chat_id, f"未支持的卡片动作: {op}")


def _message_event_to_payload(data: P2ImMessageReceiveV1) -> Dict[str, Any]:
    sender_type = ""
    sender_open_id = ""
    sender_user_id = ""
    sender_union_id = ""
    chat_id = ""
    chat_type = ""
    message_type = ""
    message_id = ""
    content = ""
    event_id = ""

    if data.header:
        event_id = _safe_str(data.header.event_id)

    if data.event:
        if data.event.sender:
            sender_type = _safe_str(data.event.sender.sender_type)
            if data.event.sender.sender_id:
                sender_open_id = _safe_str(data.event.sender.sender_id.open_id)
                sender_user_id = _safe_str(data.event.sender.sender_id.user_id)
                sender_union_id = _safe_str(data.event.sender.sender_id.union_id)
        if data.event.message:
            chat_id = _safe_str(data.event.message.chat_id)
            chat_type = _safe_str(data.event.message.chat_type)
            message_type = _safe_str(data.event.message.message_type)
            message_id = _safe_str(data.event.message.message_id)
            content = _safe_str(data.event.message.content)

    return {
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": event_id,
        },
        "event": {
            "sender": {
                "sender_type": sender_type or "user",
                "sender_id": {
                    "open_id": sender_open_id,
                    "user_id": sender_user_id,
                    "union_id": sender_union_id,
                },
            },
            "message": {
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": message_type,
                "content": content,
                "message_id": message_id,
            },
        },
    }


def _menu_event_to_payload(data: P2ApplicationBotMenuV6) -> Dict[str, Any]:
    event_key = ""
    open_id = ""
    user_id = ""
    union_id = ""
    event_id = ""
    if data.header:
        event_id = _safe_str(data.header.event_id)
    if data.event:
        event_key = _safe_str(data.event.event_key)
        if data.event.operator and data.event.operator.operator_id:
            open_id = _safe_str(data.event.operator.operator_id.open_id)
            user_id = _safe_str(data.event.operator.operator_id.user_id)
            union_id = _safe_str(data.event.operator.operator_id.union_id)
    return {
        "event_key": event_key,
        "open_id": open_id,
        "user_id": user_id,
        "union_id": union_id,
        "event_id": event_id,
    }


def _log_level_from_env() -> lark.LogLevel:
    name = os.getenv("LOG_LEVEL", "INFO").upper()
    if name == "DEBUG":
        return lark.LogLevel.DEBUG
    if name == "WARNING":
        return lark.LogLevel.WARNING
    if name == "ERROR":
        return lark.LogLevel.ERROR
    if name == "CRITICAL":
        return lark.LogLevel.CRITICAL
    return lark.LogLevel.INFO


def _card_callback_ack(toast_text: str = "处理中...") -> P2CardActionTriggerResponse:
    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()
    toast.type = "info"
    toast.content = toast_text
    resp.toast = toast
    return resp


def main() -> None:
    if not APP_ID or not APP_SECRET:
        raise RuntimeError("FEISHU_APP_ID or FEISHU_APP_SECRET is empty")
    if not API_TOKEN:
        raise RuntimeError("BRIDGE_API_TOKEN is empty")
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

    menu_actions = _parse_json_map(os.getenv("BRIDGE_MENU_ACTIONS_JSON", ""), DEFAULT_MENU_ACTIONS)
    projects = _parse_json_map(os.getenv("BRIDGE_PROJECTS_JSON", ""), DEFAULT_PROJECTS)

    feishu = FeishuClient(APP_ID, APP_SECRET)
    control = ControlAPI(base=CONTROL_BASE, api_prefix=API_PREFIX, api_token=API_TOKEN)
    bridge = AppServerBotBridge(
        feishu=feishu,
        control=control,
        upload_root=UPLOAD_ROOT,
        menu_actions=menu_actions,
        projects=projects,
        project_root=PROJECT_ROOT,
        projects_store_path=PROJECTS_STORE_PATH,
        user_chat_map_path=USER_CHAT_MAP_PATH,
    )
    level = _log_level_from_env()

    LOG.info("menu actions: %s", json.dumps(menu_actions, ensure_ascii=False))
    LOG.info("projects: %s", json.dumps(projects, ensure_ascii=False))
    LOG.info("attachment upload root: %s", str(UPLOAD_ROOT))
    LOG.info("user chat map path: %s", str(USER_CHAT_MAP_PATH))
    LOG.info(
        "output auto send limits: max_count=%s max_size_mb=%s",
        "unlimited" if OUTPUT_FILE_MAX_COUNT <= 0 else OUTPUT_FILE_MAX_COUNT,
        OUTPUT_FILE_MAX_SIZE_MB,
    )

    def _on_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
        bridge.handle_event_async(_message_event_to_payload(data))

    def _on_bot_menu_v6(data: P2ApplicationBotMenuV6) -> None:
        bridge.handle_menu_event_async(_menu_event_to_payload(data))

    def _on_card_action_trigger(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        bridge.handle_card_action_async(data)
        return _card_callback_ack("已收到操作，处理中...")

    event_handler = (
        lark.EventDispatcherHandler.builder("", "", level)
        .register_p2_im_message_receive_v1(_on_message_receive_v1)
        .register_p2_application_bot_menu_v6(_on_bot_menu_v6)
        .register_p2_card_action_trigger(_on_card_action_trigger)
        .build()
    )

    LOG.info("Starting Feishu long connection receiver for app-server bridge...")
    client = lark.ws.Client(APP_ID, APP_SECRET, log_level=level, event_handler=event_handler)
    client.start()


if __name__ == "__main__":
    main()
