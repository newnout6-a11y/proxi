"""Claude Code's tool search, which a gateway that does not run it has to turn down.

Since 2.1.304 Claude Code defers its MCP and some of its own tools behind ToolSearch when ANTHROPIC_BASE_URL is a gateway:
the first request lists a handful of tools, ToolSearch and a placeholder with defer_loading, and every tool the model loads
afterwards is added to `tools` (and a tool_reference to the result). The relay puts `tools` first in the prompt it sends
upstream, so each load rewrites the whole cached conversation: four loads were 41% of all tokens written in the Claude
Code dialog of 2026-11-06 (14.6, 29, 31 and 33.8 credits, the last one at 336k tokens). Anthropic's own API does not
pay this (a deferred tool is not part of the cached prefix there); a translating layer cannot copy it, and nothing in
/v1/models, in a response header or in anthropic-beta tells the client not to defer: the decision is the client's, made from
its own settings (ENABLE_TOOL_SEARCH) and the model name.

What the client documents (code.claude.com/docs/en/mcp, "Tool search through an LLM gateway") is the other way round: from
2.1.305 on, a 400 or 422 to a request that carries tool search makes it send the request again without defer_loading and
tool_reference, with every tool loaded up front, and keep it that way for the conversation. It was checked against the
installed 2.1.310 on a fake gateway: the first request (14 tools, ToolSearch, a deferred placeholder) is answered 400, the
client repeats it once, is answered 400 again, and then sends the request with all 26 tools and no deferral; a resumed
conversation whose history holds tool_reference blocks gets them stripped. Status 422, a message that does not name tool
search, and x-should-retry: false all take the same way. Nothing is stored between processes: each conversation pays two
rejected requests, which cost nothing upstream.
"""
import collections
import re
import threading
import time

# 2.1.304 sent tool search to every gateway and failed every request it refused; 2.1.305 fixed that and added the retry.
MIN_VERSION = (2, 1, 305)
_VERSION = re.compile(r"(?:claude-cli|claude-code|claude)/(\d+)\.(\d+)\.(\d+)")
_EXPLANATION = (" This gateway does not run tool search: it passes every tool to the model up front. Claude Code resends "
                "the request with all of its tools loaded.")
# One conversation is refused twice before the client falls back; more than this in a window is a client that does not.
LOOP_LIMIT = 6
LOOP_WINDOW = 600


def message(where):
    """The text of the 400 for a request that carries tool search at `where` (what deferral() returned)."""
    if where == "reference":
        return "messages: tool_reference blocks are not permitted." + _EXPLANATION
    return "tools.%s.defer_loading: Extra inputs are not permitted." % where + _EXPLANATION


def client_version(user_agent):
    """(major, minor, patch) of a Claude Code user agent ("claude-cli/2.1.310 (external, sdk-cli)"), None when it names none."""
    found = _VERSION.search(str(user_agent or "").lower())
    return tuple(int(part) for part in found.groups()) if found else None


def deferral(data):
    """Where a request carries tool search: "N" of the first tool with defer_loading, "reference" for a tool_reference in
    the history, None when it carries none."""
    tools = data.get("tools")
    for index, tool in enumerate(tools if isinstance(tools, list) else []):
        if isinstance(tool, dict) and tool.get("defer_loading"):
            return str(index)
    messages = data.get("messages")
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            inner = block.get("content") if isinstance(block, dict) and block.get("type") == "tool_result" else None
            if isinstance(inner, list) and any(isinstance(item, dict) and item.get("type") == "tool_reference" for item in inner):
                return "reference"
    return None


class LoopGuard:
    """Lets a conversation be refused LOOP_LIMIT times in LOOP_WINDOW seconds: the client that keeps sending tool search
    after the retry is served as it asks (today's behaviour) instead of being refused for ever."""

    def __init__(self, limit=LOOP_LIMIT, window=LOOP_WINDOW, clock=time.time, max_keys=1024):
        self._limit, self._window, self._clock, self._max = limit, window, clock, max_keys
        self._lock = threading.Lock()
        self._seen = collections.OrderedDict()

    def allow(self, key):
        now = self._clock()
        with self._lock:
            stamps = self._seen.pop(key, None) or collections.deque()
            while stamps and now - stamps[0] > self._window:
                stamps.popleft()
            allowed = len(stamps) < self._limit
            if allowed:
                stamps.append(now)
            self._seen[key] = stamps
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return allowed

    def forgive(self, key):
        """The conversation sent a request without tool search: the refusals before it worked, so they no longer count.

        Claude Code asks for tool search again after every compaction (two refusals each), and the count of all refusals
        of a conversation tripped the guard at the third compaction within ten minutes: the deferred list was served, the
        tools went 29 -> 15 and the history was written again (23,247 tokens, 9bf6cbebe345, Claude Code test run of
        2026-11-07). Only refusals in a row, with no request in between that complied, make a loop."""
        with self._lock:
            self._seen.pop(key, None)
