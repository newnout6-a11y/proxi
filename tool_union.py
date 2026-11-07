"""The tools a client has declared before, so that the list the model is given stops changing under a client that fills it late.

The tool list is part of the cached prefix and of the key of a signed branch: one tool more or less rewrites the whole
cache and drops the signed history. Codex starts its MCP servers after the first request (the optional-server grace
period is one second, mcp_optional_startup_grace_ms), so request 1 carries 44 to 46 tools and request 2 carries 50 to 60:
27 of 27 models in the Codex test run of 2026-11-06, every default-config run. A rewrite of a 334k-token conversation
costs 31 credits, a warm request 1.5, and `codex exec resume` starts a new process, and so a new flap, on every turn (two
rewrites per turn, measured). The Desktop engine does the same after thread/start and thread/resume.

ToolUnion remembers, per client and project, the tools the client has declared and gives a request the union of those and
the ones it declares itself. The client's own declaration always wins for a tool it names (its newest definition is the one
the model sees); a tool it has not named in this request is carried from its earlier requests. A request that arrives
before a slow server has started therefore declares what the next one will, and nothing changes upstream when the server
comes up. The union is bounded in time (a tool the client has not declared for `ttl` seconds is dropped, so a server the
user removes goes within hours), in size and in how many clients are kept.

A call the model makes to a carried tool goes to the client like any other. Codex answers it with an error the model reads
and recovers from ("unsupported call": it routes a call with the tool list of the request that produced it, so this holds
even when the server has come up since) and declares the tool from the next request on. A call to a name the client never
declared stays what it was: not passed on, and journaled (tool.call_dropped).
"""
import collections
import hashlib
import json
import os
import re
import threading
import time

import journal

TTL = 6 * 3600
MAX_CLIENTS = 64
MAX_TOOLS = 400
# A tool definition of this size (JSON characters) is not carried: a union is a way to keep a prefix, not to grow one.
MAX_TOOL_CHARS = 64 * 1024
# Claude Code names its folder in a bullet of its environment block (" - Primary working directory: C:\..."), Codex in <cwd>.
_FOLDER = (re.compile(r"<cwd>(.*?)</cwd>", re.S),
           re.compile(r"^[ \t]*(?:[-*][ \t]+)?(?:Primary working|Working) directory:[ \t]*(.+?)[ \t]*$", re.M))
_SEARCHED = 40000
# Where the union is kept between runs (in conversation_state_dir), and how often a table whose only change is the time a
# tool was last declared is written again.
FILE_NAME = "tool-union.json"
SAVE_EVERY = 60
# The families whose sub-agents differ by kind (Claude Code's Explore agent has fewer tools than a general-purpose one):
# their system text tells the kinds apart. Its first line, Claude Code's attribution, is left out: its version carries three
# characters drawn from the session's first message (cc_version=2.1.310.2e4, .6cd, .29f... in one test run), so with it no
# two sessions would share a table, and every session would push another client's table out of the bounded union.
_KINDS_BY_SYSTEM = ("claude-code", "claude-desktop")
_KIND_CHARS = 400
_ATTRIBUTION = "x-anthropic-billing-header:"


def _opening(system):
    """The first characters of a system text that say which kind of agent it is, without the attribution line."""
    text = (system or "").lstrip()
    if text.startswith(_ATTRIBUTION):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return text.lstrip()[:_KIND_CHARS]


def project_of(parsed):
    """The working folder a request names (Codex: <cwd> of its environment context), lower-cased; "" when it names none."""
    text = "%s\n%s" % ((getattr(parsed, "system", "") or "")[:_SEARCHED], (getattr(parsed, "prompt", "") or "")[:_SEARCHED])
    for pattern in _FOLDER:
        found = pattern.search(text)
        if found and found.group(1).strip():
            return found.group(1).strip().replace("/", "\\").rstrip("\\").lower()
    return ""


def client_key(family, headers, parsed):
    """(family, originator, model, which kind of agent, project, profile, revision, agent role) for a request, or None
    when it has no tools of its own.

    The originator keeps Codex's CLI and its Desktop apart (their servers differ: 60 tools against 97), the model keeps the
    lists apart that the client builds per model (multi-agent v1 or v2, image tools), root and sub-agents apart (a sub-agent
    has its own, smaller list), and a prompts profile and its revision apart (a profile may hide tools). For Claude Code the
    opening of the system text keeps the kinds of agent apart too: an Explore sub-agent must not be offered the tools of a
    general-purpose one."""
    if not getattr(parsed, "tools", None):
        return None
    lowered = {str(name).lower(): value for name, value in (headers or {}).items()}
    sub = bool(lowered.get("x-openai-subagent") or lowered.get("x-codex-parent-thread-id")
               or lowered.get("x-claude-code-agent-id"))
    plan = getattr(parsed, "prompt_plan", None)
    role = ""
    if family in _KINDS_BY_SYSTEM:
        opening = _opening(getattr(parsed, "system", ""))
        role = hashlib.sha256(opening.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return (family, str(lowered.get("originator") or "").strip().lower(), getattr(parsed, "model", "") or "",
            "sub" if sub else "root", project_of(parsed), getattr(plan, "profile", None) or "",
            getattr(plan, "rev", None) or 0, role)


class ToolUnion:
    """key -> the tools declared under it, newest use last; kept in `path` between runs when one is given.

    In memory only, a relay restart emptied the union, and the first request of a Codex process after it declared fewer
    tools than the conversation's stored branch: tools 91 -> 100, a reset and 32,599 tokens written (the owner's Codex
    Desktop at 12:18:31 on 2026-11-07, 4e298b6eb041; the Codex test run measured the same at every first turn of a key)."""

    def __init__(self, ttl=TTL, max_clients=MAX_CLIENTS, max_tools=MAX_TOOLS, clock=time.time, path=None):
        self._ttl, self._max_clients, self._max_tools, self._clock = ttl, max_clients, max_tools, clock
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._clients = collections.OrderedDict()
        self._announced = {}
        self._path = path
        self._dirty = False
        self._saved_at = 0.0
        self._warned = False
        self._load()

    def apply(self, key, tools):
        """(the tools to give the model, the names that were carried from earlier requests)."""
        now = self._clock()
        declared = {}
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                declared[function["name"]] = tool
        with self._lock:
            table = self._clients.get(key)
            changed = table is None
            if table is None:
                table = self._clients[key] = collections.OrderedDict()
            self._clients.move_to_end(key)
            while len(self._clients) > self._max_clients:
                evicted, _ = self._clients.popitem(last=False)
                self._announced.pop(evicted, None)
            for name in [name for name, entry in table.items() if name not in declared and now - entry[1] > self._ttl]:
                del table[name]
                changed = True
            for name, tool in declared.items():
                if len(repr(tool)) <= MAX_TOOL_CHARS:
                    changed = changed or name not in table or table[name][0] != tool
                    table[name] = [tool, now]
                    table.move_to_end(name)
            while len(table) > self._max_tools:
                table.popitem(last=False)
            carried = [name for name in table if name not in declared]
            merged = list(tools) + [table[name][0] for name in carried]
            self._dirty = True
            due = changed or now - self._saved_at >= SAVE_EVERY
        if due:
            self._save(now)
        return merged, carried

    def announce(self, key, carried):
        """True when `carried` is not what was last announced for this key: the journal says it when it changes, not every time."""
        signature = tuple(sorted(carried))
        with self._lock:
            if self._announced.get(key) == signature:
                return False
            self._announced[key] = signature
            return True

    def __len__(self):
        with self._lock:
            return sum(len(table) for table in self._clients.values())

    def _load(self):
        """The tables of an earlier run, without the tools not declared for `ttl` seconds; nothing when there is no file."""
        if not self._path:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                document = json.load(handle)
            now = self._clock()
            for key, entries in document["clients"]:
                table = collections.OrderedDict()
                for name, tool, seen in entries:
                    if isinstance(name, str) and isinstance(tool, dict) and now - float(seen) <= self._ttl:
                        table[name] = [tool, float(seen)]
                if table:
                    self._clients[tuple(key)] = table
            while len(self._clients) > self._max_clients:
                self._clients.popitem(last=False)
        except (OSError, ValueError, KeyError, TypeError):
            self._clients.clear()  # nothing to go on: the next request of each client starts its table

    def _save(self, now):
        if not self._path:
            return
        with self._save_lock:
            with self._lock:
                if not self._dirty:
                    return
                self._dirty = False
                self._saved_at = now
                document = {"version": 1, "clients": [[list(key), [[name, entry[0], entry[1]] for name, entry in table.items()]]
                                                      for key, table in self._clients.items()]}
            try:
                temporary = "%s.%d.tmp" % (self._path, os.getpid())
                with open(temporary, "w", encoding="utf-8") as handle:
                    json.dump(document, handle, ensure_ascii=False)
                os.replace(temporary, self._path)
            except (OSError, TypeError, ValueError) as exc:
                if not self._warned:
                    self._warned = True
                    journal.emit("tools.union_save_failed", "Объединение инструментов клиентов не сохранено на диск: %s"
                                 % type(exc).__name__, "warn")
