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
import re
import threading
import time

TTL = 6 * 3600
MAX_CLIENTS = 64
MAX_TOOLS = 400
# A tool definition of this size (JSON characters) is not carried: a union is a way to keep a prefix, not to grow one.
MAX_TOOL_CHARS = 64 * 1024
_FOLDER = (re.compile(r"<cwd>(.*?)</cwd>", re.S),
           re.compile(r"^[ \t]*(?:Primary working|Working) directory:[ \t]*(.+?)[ \t]*$", re.M))
_SEARCHED = 40000


def project_of(parsed):
    """The working folder a request names (Codex: <cwd> of its environment context), lower-cased; "" when it names none."""
    text = "%s\n%s" % ((getattr(parsed, "system", "") or "")[:_SEARCHED], (getattr(parsed, "prompt", "") or "")[:_SEARCHED])
    for pattern in _FOLDER:
        found = pattern.search(text)
        if found and found.group(1).strip():
            return found.group(1).strip().replace("/", "\\").rstrip("\\").lower()
    return ""


def client_key(family, headers, parsed):
    """(family, originator, model, which kind of agent, project, profile) for a request, or None when it has no tools of its own.

    The originator keeps Codex's CLI and its Desktop apart (their servers differ: 60 tools against 97), the model keeps the
    lists apart that the client builds per model (multi-agent v1 or v2, image tools), root and sub-agents apart (a sub-agent
    has its own, smaller list), and a prompts profile and its revision apart (a profile may hide tools)."""
    if not getattr(parsed, "tools", None):
        return None
    lowered = {str(name).lower(): value for name, value in (headers or {}).items()}
    sub = bool(lowered.get("x-openai-subagent") or lowered.get("x-codex-parent-thread-id")
               or lowered.get("x-claude-code-agent-id"))
    plan = getattr(parsed, "prompt_plan", None)
    return (family, str(lowered.get("originator") or "").strip().lower(), getattr(parsed, "model", "") or "",
            "sub" if sub else "root", project_of(parsed), getattr(plan, "profile", None) or "",
            getattr(plan, "rev", None) or 0)


class ToolUnion:
    """key -> the tools declared under it, newest use last."""

    def __init__(self, ttl=TTL, max_clients=MAX_CLIENTS, max_tools=MAX_TOOLS, clock=time.time):
        self._ttl, self._max_clients, self._max_tools, self._clock = ttl, max_clients, max_tools, clock
        self._lock = threading.Lock()
        self._clients = collections.OrderedDict()
        self._announced = {}

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
            if table is None:
                table = self._clients[key] = collections.OrderedDict()
            self._clients.move_to_end(key)
            while len(self._clients) > self._max_clients:
                evicted, _ = self._clients.popitem(last=False)
                self._announced.pop(evicted, None)
            for name in [name for name, entry in table.items() if name not in declared and now - entry[1] > self._ttl]:
                del table[name]
            for name, tool in declared.items():
                if len(repr(tool)) <= MAX_TOOL_CHARS:
                    table[name] = [tool, now]
                    table.move_to_end(name)
            while len(table) > self._max_tools:
                table.popitem(last=False)
            carried = [name for name in table if name not in declared]
            merged = list(tools) + [table[name][0] for name in carried]
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
