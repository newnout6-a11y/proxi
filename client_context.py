"""Local client/chat labels, independent of upstream conversation affinity."""
import contextvars
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import unquote

_CURRENT = contextvars.ContextVar("proxi_client", default=None)
_CACHE = {}
_LOCK = threading.Lock()
_CLIENTS = {"claude-code": "Claude Code", "claude-desktop": "Claude Desktop", "opencode": "OpenCode"}
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_READ_LIMIT = 256 * 1024


def clean(value, limit=240):
    if not isinstance(value, str):
        return ""
    value = "".join(" " if ch.isspace() else ch for ch in value[:4096]
                    if not unicodedata.category(ch).startswith("C") or ch.isspace())
    return " ".join(value.split())[:limit]


def current():
    return dict(_CURRENT.get() or {})


def bind(identity):
    return _CURRENT.set(dict(identity))


def reset(token):
    _CURRENT.reset(token)


def label(identity=None):
    info = current() if identity is None else identity
    client = _CLIENTS.get(info.get("client"), clean(info.get("client"), 60))
    title = clean(info.get("chat_title"))
    return " · ".join(part for part in (client, "«%s»" % title if title else "") if part)


def _json_lines(path):
    """Bounded head + tail: first prompt and latest rename, even in long chats."""
    try:
        handle = path.open("rb")
    except OSError:
        return
    with handle:
        head = handle.read(_READ_LIMIT)
        handle.seek(0, 2)
        size = handle.tell()
        tail = b""
        if size > _READ_LIMIT:
            handle.seek(max(_READ_LIMIT, size - _READ_LIMIT))
            tail = handle.read(_READ_LIMIT).split(b"\n", 1)[-1]
    for raw in (head + b"\n" + tail).splitlines():
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                yield value
        except (ValueError, UnicodeError):
            continue


def _claude_title(home, session_id):
    if not _UUID.fullmatch(session_id):
        return {}
    projects = home / ".claude" / "projects"
    first, title, source = "", "", ""
    for path in projects.glob("*/%s.jsonl" % session_id):
        for row in _json_lines(path):
            if row.get("type") == "custom-title" and row.get("customTitle"):
                title, source = row["customTitle"], "claude-custom-title"
            elif not title and row.get("type") == "summary" and row.get("summary"):
                title, source = row["summary"], "claude-summary"
            if not first and row.get("type") == "user" and not row.get("isMeta"):
                message = row.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str) and not content.startswith("<"):
                    first = content
    if not title:
        for row in _json_lines(home / ".claude" / "history.jsonl"):
            if row.get("sessionId") == session_id and row.get("display") and not first:
                first = row["display"]
    title = clean(title or first)
    return {"chat_title": title, "title_source": source or "first-user-message"} if title else {}


def _desktop_title(home, session_id):
    roots = [home / "AppData/Roaming/Claude",
             home / "AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude"]
    bytes_read = 0
    for root in roots:
        for kind in ("claude-code-sessions", "local-agent-mode-sessions"):
            for index, path in enumerate((root / kind).glob("*/*/local_*.json")):
                if index >= 2048:
                    break
                try:
                    if path.stat().st_size > _READ_LIMIT:
                        continue
                    bytes_read += path.stat().st_size
                    if bytes_read > 4 * 1024 * 1024:
                        return {}
                    row = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(row, dict):
                        continue
                    ids = [row.get("sessionId"), row.get("cliSessionId")]
                    ids += row.get("priorCliSessionIds") or []
                    if session_id in ids and clean(row.get("title")):
                        return {"chat_title": clean(row["title"]), "title_source": "claude-desktop-session",
                                "client": "claude-desktop"}
                except (OSError, ValueError, TypeError):
                    continue
    return {}


def _local_title(client, session_id, home):
    cache_key = (str(home), client, session_id)
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])
    result = {}
    try:
        if client == "opencode":
            db = home / ".local/share/opencode/opencode.db"
            # mode=ro avoids creating a DB or changing a running client's state.
            row, agent = None, None
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.05)) as conn:
                # Desktop 2.x and CLI 1.x coexist in the same DB with different tables.
                for table in ("session_v2", "session"):
                    try:
                        row = conn.execute("SELECT title, parent_id FROM %s WHERE id = ?" % table, (session_id,)).fetchone()
                        if row:
                            try:
                                agent = conn.execute("SELECT agent FROM %s WHERE id = ?" % table, (session_id,)).fetchone()[0]
                            except sqlite3.Error:
                                pass
                            break
                    except sqlite3.Error:
                        continue
            if row and clean(row[0]):
                result = {"chat_title": clean(row[0]), "title_source": "opencode-session"}
                if row[1]:
                    result["parent_session_id"] = clean(row[1], 160)
                if clean(agent):
                    result["agent"] = clean(agent, 100)
        elif client in ("claude-code", "claude-desktop"):
            result = _desktop_title(home, session_id) or _claude_title(home, session_id)
    except (OSError, ValueError, sqlite3.Error):
        pass  # Missing/locked metadata must never delay or reject a model turn.
    with _LOCK:
        if len(_CACHE) >= 1024:
            _CACHE.clear()
        _CACHE[cache_key] = (time.monotonic() + 2, result)
    return dict(result)


def describe(data, headers, cfg=None):
    headers = {str(key).lower(): value for key, value in headers.items()}
    metadata = data.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    user = metadata.get("user_id")
    identity = {}
    if isinstance(user, str):
        try:
            identity = json.loads(user)
        except ValueError:
            match = re.search(r"_session_(%s)$" % _UUID.pattern, user)
            if match:
                identity = {"session_id": match[1]}
    identity = identity if isinstance(identity, dict) else {}
    client = clean(headers.get("x-proxi-client"), 60).lower()
    ua = str(headers.get("user-agent", "")).lower()
    if not client:
        client = next((name for name in _CLIENTS if name in ua), "")
        if not client and ("claude-cli" in ua or identity.get("session_id")):
            client = "claude-code"
    session_id = clean(headers.get("x-proxi-session-id") or identity.get("session_id") or
                       metadata.get("session_id") or headers.get("x-claude-code-session-id") or
                       headers.get("x-opencode-session-id") or headers.get("x-session-id") or
                       headers.get("x-opencode-session") or data.get("conversation_id") or
                       data.get("session_id"), 160)
    title = clean(unquote(str(headers.get("x-proxi-chat-title", "")))) or clean(metadata.get("chat_title"))
    info = {key: value for key, value in {"client": client, "client_session_id": session_id,
            "chat_title": title, "title_source": "client-header" if title else "",
            "agent": clean(headers.get("x-proxi-agent") or headers.get("x-claude-code-agent-id") or
                           metadata.get("agent_id") or identity.get("agent_id"), 100),
            "parent_session_id": clean(headers.get("x-proxi-parent-session-id") or
                                       headers.get("x-opencode-parent-session-id"), 160)}.items() if value}
    if not title and session_id and client in _CLIENTS:
        home = Path((cfg or {}).get("client_metadata_home") or Path.home()).resolve()
        info.update(_local_title(client, session_id, home))
    return info
