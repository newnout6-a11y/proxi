"""Local client/chat labels, independent of upstream conversation affinity."""
import collections
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
_CACHE = collections.OrderedDict()
_CACHE_MAX = 1024
_USER_ID_LIMIT = 4096
_LOCK = threading.Lock()
_CLIENTS = {"claude-code": "Claude Code", "claude-desktop": "Claude Desktop", "opencode": "OpenCode"}
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_READ_LIMIT = 256 * 1024
# Lookups scan local client folders on the request path. A found title is
# re-read soon enough to show renames; a miss is retried less often.
_TITLE_TTL = 15
_MISS_TTL = 30
SERVICE_KINDS = frozenset({"auto-mode-check", "session-title", "connection-probe"})
CLIENT_TEXT_KINDS = SERVICE_KINDS | {"client-text"}
# x-claude-code-request-class values whose system prompt is the client's own
# output contract (summaries, titles, classifiers), never the chat the relay
# policy is written for. The header needs CLAUDE_CODE_GATEWAY_HINT_HEADERS=1.
OWN_PROMPT_CLASSES = frozenset({"auxiliary", "compaction"})
# OpenCode runs its title, compaction and summary jobs as hidden agents of these names. The 1.x plugin
# scripts/integrations/proxi-session-labels.js sends the agent of every request as X-Proxi-Agent.
OPENCODE_SERVICE_AGENTS = frozenset({"title", "compaction", "summary"})
# The opening of the system prompt of the 1.x compaction agent (agent/prompt/compaction.txt: the first line since
# 2026-04-22, and the one before it, which Claude Code's own summarizer has used as well).
COMPACTION_SYSTEM_PROMPTS = (
    "You are an anchored context summarization assistant for coding sessions.",
    "You are a helpful AI assistant tasked with summarizing conversations.",
)
# The opening of the prompt each of those agents runs on, which is its whole system text (the prompt layer
# matches on the start, never the full text: the rest is reworded from release to release).
#   1.x agent/prompt/title.txt (unchanged since 2025-12, and still the 2.0 and 2.1 title agent), 2.2 title agent in
#   core/src/plugin/agent.ts, 1.x compaction.txt (the second line before 2026-04-22), summary.txt and the 2.x summary agent.
# OpenCode 2.x compaction has no prompt of its own: it resends the chat's own system and tools and ends on a
# user message, so prompt_purpose does not tell it apart and it goes where the chat goes (which keeps the prompt
# cache). client_task does tell it apart, for the journal and for the effort of the dialog (see COMPACTION).
OPENCODE_SERVICE_PROMPTS = (
    "You are a title generator. You output ONLY a thread title. Nothing else.",
    "You name conversations so the user can find them later in a list of past sessions.",
    *COMPACTION_SYSTEM_PROMPTS,
    "Summarize what was done in this conversation. Write like a pull request description.",
)
# client_task of a request that summarizes the whole conversation so that the client can replace it by the summary.
COMPACTION = "compaction"
# OpenCode 2.x (every tag from v2.0.0 to v2.2.5, packages/core/src/session/compaction.ts buildPrompt) ends
# the chat's own request with one more user message: the first compaction asks for a summary, a later one for an
# update of the checkpoint. The start of the message is the sign, never the text, which is reworded between releases
# (and a quoted sentence further down a message is no instruction).
# Claude Code does the same (docs: its summary request repeats the system prompt, the tools and the history, and ends
# with the summarization instruction, code.claude.com/docs/en/prompt-caching). Its headers name the task when the
# gateway hint headers are on, which is off by default behind a custom base URL, so the start of its instruction is
# read as well: "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools." in 2.1.316 (a community capture,
# clawdipedia.cc/prompts/context-and-memory/compact; unofficial, the product is closed), and the opening it has had
# since 2025 without that line, "Your task is to create a detailed summary of the conversation so far".
COMPACTION_INSTRUCTIONS = (
    "You MUST summarize the conversation above into a structured summary that will be given to another agent to resume the work.",
    "Update the existing checkpoint in the conversation above into one consolidated summary.",
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.",
    "Your task is to create a detailed summary of the conversation so far",
)
# The reminder OpenCode 2.x adds as a further user message when the reply skipped the summary template.
COMPACTION_NUDGE = "The previous response did not fill in the required summary template."
# OpenCode 1.x summarizes on its hidden agent without tools, so these are not the chat's own request; its system
# prompt (COMPACTION_SYSTEM_PROMPTS) and agent name give it away as well, and the instruction opens its user message.
# By tag, from a sweep of every v1.* tag of github.com/anomalyco/opencode (the message is the default prompt of
# packages/opencode/src/session/compaction.ts, from v1.17 of packages/core as well):
#   v1.0.144 to v1.14.18  "Provide a detailed prompt for continuing our conversation above."
#   v1.14.19 to v1.14.20  "When constructing the summary, try to stick to this template:"
#   v1.14.21 to v1.18.16  "Create a new anchored summary from the conversation history" (the first), or
#                         "Update the anchored summary below using the conversation history above." (a later one)
#   v1.18.17 to v1.19.5   "Here is the conversation so far:" and the whole conversation in <conversation> tags
# v1.0.0 to v1.0.143 word the instruction differently and are not covered.
LEGACY_COMPACTION_INSTRUCTIONS = (
    "Provide a detailed prompt for continuing our conversation above.",
    "When constructing the summary, try to stick to this template:",
    "Create a new anchored summary from the conversation history",
    "Update the anchored summary below using the conversation history above.",
    "Here is the conversation so far:\n\n<conversation>",
)
# How many of the user messages that end a conversation are read for the instruction: it is the last one, or the one before
# a reminder.
_TRAILING_USER_MESSAGES = 3
# Request headers a recorded request keeps for a preview: client identity and affinity, never a credential.
_RECORDED_HEADERS = frozenset({"user-agent", "originator", "x-session-affinity", "x-session-id"})
_RECORDED_PREFIXES = ("x-proxi-", "x-claude-code-", "x-opencode-")
SDK_TITLE_PREFIX = "You are naming a coding session so the user can pick it out of a long list of sessions."
# Claude Code before 2.1.234 asked for the same title in these words.
LEGACY_TITLE_PREFIX = "Generate a concise, sentence-case title"
_TITLE_PREFIXES = (SDK_TITLE_PREFIX, LEGACY_TITLE_PREFIX,
                   "You write short session titles. Reply with only the tagged fields the prompt asks for.")
_ATTRIBUTION = "x-anthropic-billing-header:"
# One-line identity blocks that Claude Code and the Agent SDK put before their real prompt.
_IDENTITY = re.compile(r"You are (?:Claude Code|a Claude agent)\b[^\n]*")


def _after_preamble(system):
    """System text after the leading attribution and identity lines of Claude clients."""
    rest = system.lstrip()
    for _ in range(4):
        line, _sep, tail = rest.partition("\n")
        line = line.strip()
        if not (line.startswith(_ATTRIBUTION) or _IDENTITY.fullmatch(line)):
            break
        rest = tail.lstrip()
    return rest


def is_sdk_title_system(system):
    # The title prompt opens its own block. A later line that only quotes the
    # wording is ordinary instruction text, not a title request.
    return _after_preamble(system).startswith(_TITLE_PREFIXES)


def keeps_client_prompt(purpose, identity=None):
    """True when relay prompt policy must leave the client's system prompt alone."""
    info = current() if identity is None else identity
    return purpose in SERVICE_KINDS or info.get("request_class") in OWN_PROMPT_CLASSES


def request_kind(parsed, identity=None):
    """Describe known client calls; prompt length alone says nothing about purpose."""
    if getattr(parsed, "tools", None):
        return "client-tools"
    system = (getattr(parsed, "system", "") or "").lstrip()
    if re.search(r"(?:^|\n)You are a security monitor for autonomous AI coding agents(?:\.|\n|$)", system):
        return "auto-mode-check"
    # The schema is the wording-independent signal: a tool-less request for exactly
    # {title: string} is a title whatever the client's prompt says today.
    if is_sdk_title_system(system) or (getattr(parsed, "structured_output", None) or {}).get("title"):
        return "session-title"
    info = current() if identity is None else identity
    if (info.get("client") == "claude-desktop" and not system
            and getattr(parsed, "max_tokens", None) == 1
            and (getattr(parsed, "prompt", "") or "").strip() == "User: ."):
        return "connection-probe"
    return "client-text" if info.get("client") in _CLIENTS else "generation"


def _is_opencode_service_job(parsed, identity=None):
    """True for OpenCode's own title, compaction and summary jobs: tool-less, run by one of its hidden agents."""
    if getattr(parsed, "tools", None):
        return False
    info = current() if identity is None else identity
    agent = str(info.get("agent") or "").strip().lower()
    if info.get("client") == "opencode" and agent in OPENCODE_SERVICE_AGENTS:
        return True
    return (getattr(parsed, "system", "") or "").lstrip().startswith(OPENCODE_SERVICE_PROMPTS)


def prompt_purpose(parsed, identity=None):
    """The purpose prompt rules match on: request_purpose, except that OpenCode's own title, compaction and
    summary jobs are "client-service".

    request_kind and parsed.request_purpose stay as they are. The journal, the console, the retry of an empty reply
    and execution read them, so a legacy request must keep its old value (plan 4.3 item 14); the prompt layer alone
    protects client-service jobs from prompt edits. A request that sends tools never counts: OpenCode 2.x
    compaction resends the chat's own tools and system, so it follows the chat's profile and shares its cache.
    """
    kind = getattr(parsed, "request_purpose", None) or request_kind(parsed, identity)
    if kind in ("client-text", "generation") and _is_opencode_service_job(parsed, identity):
        return "client-service"
    return kind


def _first_text(content):
    """The text a message opens with: a plain string, or the first text block of a list of blocks."""
    if isinstance(content, str):
        return content
    for block in content or ():
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    return ""


def _trailing_user_texts(parsed):
    """What the user messages that end the conversation open with, the last one first.

    The canonical client messages are read when the parse made them: a tool result is a message of its own there,
    whatever the dialect, so a Messages turn that carries results and then the instruction still ends on a user
    message that opens with the instruction. Without them the rendered chunks stand in ("User: <text>")."""
    texts = []
    native = getattr(parsed, "native_messages", None)
    if native:
        for message in reversed(native):
            if message.get("role") != "user" or len(texts) == _TRAILING_USER_MESSAGES:
                break
            texts.append(_first_text(message.get("content")))
        return texts
    for chunk in reversed(getattr(parsed, "message_chunks", None) or ()):
        text = chunk if isinstance(chunk, str) else _first_text(chunk)
        if not text.startswith("User: ") or len(texts) == _TRAILING_USER_MESSAGES:
            break
        texts.append(text[len("User: "):])
    return texts


def client_task(parsed, identity=None):
    """COMPACTION for a request that asks the model to summarize the whole conversation, "" for any other.

    A label of its own: request_kind and parsed.request_purpose stay as they are (the journal, the retries of an
    empty reply and the execution read them, and the prompt layer's purpose follows them). The headers are read first,
    Claude Code's request class and compaction header, the agent name the OpenCode 1.x plugin sends and an explicit
    X-Proxi-Task, then what the conversation ends on: the instruction of OpenCode 2.x (see COMPACTION_INSTRUCTIONS) or
    the prompt of its 1.x agent, which a request without tools only can be.
    """
    info = current() if identity is None else identity
    agent = str(info.get("agent") or "").strip().lower()
    if (info.get("request_class") == COMPACTION or info.get("compaction_trigger") or info.get("task_hint") == COMPACTION
            or (info.get("client") == "opencode" and agent == COMPACTION)):
        return COMPACTION
    texts = [text.lstrip() for text in _trailing_user_texts(parsed)]
    if texts and texts[0].startswith(COMPACTION_INSTRUCTIONS):
        return COMPACTION
    if len(texts) > 1 and texts[0].startswith(COMPACTION_NUDGE) and texts[1].startswith(COMPACTION_INSTRUCTIONS):
        return COMPACTION
    if not getattr(parsed, "tools", None):
        if texts and texts[0].startswith(LEGACY_COMPACTION_INSTRUCTIONS):
            return COMPACTION
        if (getattr(parsed, "system", "") or "").lstrip().startswith(COMPACTION_SYSTEM_PROMPTS):
            return COMPACTION
    return ""


def recorded_headers(headers):
    """The headers of a request that a recorded preview keeps, lower-cased: client identity and affinity, no credential."""
    kept = {}
    for name, value in (headers or {}).items():
        lowered = str(name).lower()
        if lowered in _RECORDED_HEADERS or lowered.startswith(_RECORDED_PREFIXES):
            kept[lowered] = str(value)
    return kept


def clean(value, limit=240):
    if not isinstance(value, str):
        return ""
    value = "".join(" " if ch.isspace() else ch for ch in value[:4096]
                    if not unicodedata.category(ch).startswith("C") or ch.isspace())
    return " ".join(value.split())[:limit]


def _first(*values):
    """First non-empty string: a truthy non-string must not hide a later header."""
    return next((value for value in values if isinstance(value, str) and value.strip()), "")


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
    # Only titles the client itself assigned: a user's first prompt is private text.
    title, source = "", ""
    for path in projects.glob("*/%s.jsonl" % session_id):
        for row in _json_lines(path):
            if row.get("type") == "custom-title" and row.get("customTitle"):
                title, source = row["customTitle"], "claude-custom-title"
            elif not title and row.get("type") == "summary" and row.get("summary"):
                title, source = row["summary"], "claude-summary"
    title = clean(title)
    return {"chat_title": title, "title_source": source} if title else {}


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
            _CACHE.move_to_end(cache_key)
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
        _CACHE.pop(cache_key, None)
        while len(_CACHE) >= _CACHE_MAX:
            _CACHE.popitem(last=False)
        _CACHE[cache_key] = (time.monotonic() + (_TITLE_TTL if result else _MISS_TTL), result)
    return dict(result)


def describe(data, headers, cfg=None):
    headers = {str(key).lower(): value for key, value in headers.items()}
    metadata = data.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    user = metadata.get("user_id")
    identity = {}
    # Client-controlled and parsed on the request path: bound it before json.loads.
    if isinstance(user, str) and len(user) <= _USER_ID_LIMIT:
        try:
            identity = json.loads(user)
        except (ValueError, RecursionError):
            match = re.search(r"_session_(%s)$" % _UUID.pattern, user)
            if match:
                identity = {"session_id": match[1]}
    identity = identity if isinstance(identity, dict) else {}
    identity_session = _first(identity.get("session_id"))
    client = clean(headers.get("x-proxi-client"), 60).lower()
    ua = str(headers.get("user-agent", "")).lower()
    if not client:
        client = next((name for name in _CLIENTS if name in ua), "")
        if not client and re.search(r"\bclaude/\d", ua):
            client = "claude-desktop"
        if not client and ("claude-cli" in ua or identity_session):
            client = "claude-code"
    session_id = clean(_first(headers.get("x-proxi-session-id"), identity_session,
                              metadata.get("session_id"), headers.get("x-claude-code-session-id"),
                              headers.get("x-opencode-session-id"), headers.get("x-session-id"),
                              headers.get("x-opencode-session"), data.get("conversation_id"),
                              data.get("session_id")), 160)
    title = clean(unquote(str(headers.get("x-proxi-chat-title", "")))) or clean(metadata.get("chat_title"))
    # Claude Code names what a request is for; only its fixed vocabulary is kept.
    request_class = clean(headers.get("x-claude-code-request-class"), 64).lower()
    # What triggered a Claude Code compaction (auto, manual, reactive), and a task a plugin names itself.
    compaction = clean(headers.get("x-claude-code-compaction"), 64).lower()
    task = clean(headers.get("x-proxi-task"), 64).lower()
    info = {key: value for key, value in {"client": client, "client_session_id": session_id,
            "chat_title": title, "title_source": "client-header" if title else "",
            "request_class": request_class if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", request_class) else "",
            "compaction_trigger": compaction if re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", compaction) else "",
            "task_hint": task if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", task) else "",
            "agent": clean(_first(headers.get("x-proxi-agent"), headers.get("x-claude-code-agent-id"),
                                  metadata.get("agent_id"), identity.get("agent_id")), 100),
            "parent_session_id": clean(headers.get("x-proxi-parent-session-id") or
                                       headers.get("x-opencode-parent-session-id") or
                                       metadata.get("parent_session_id") or
                                       identity.get("parent_session_id"), 160)}.items() if value}
    if not title and session_id and client in _CLIENTS:
        home = Path((cfg or {}).get("client_metadata_home") or Path.home()).resolve()
        info.update(_local_title(client, session_id, home))
    return info
