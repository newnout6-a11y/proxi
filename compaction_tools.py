"""The tools each Codex chat declared in its last request, so that the chat's own compaction can declare them again.

Codex compacts a conversation for a custom provider by sending the chat's history and its summary prompt with NO tools
(codex-rs core/src/compact.rs: the Prompt of that request is the default one). The provider's cache is keyed by the prefix, and
tools stand first in it, so that request shared nothing with the chat: a tool-less request of the same history wrote all of it
again, 31,943 tokens and 1.65 credits for a chat whose last request had read 24,311 (Codex run of 2026-11-07, 363bb3d49ff4),
and Claude Opus billed 52,527 uncached input tokens, 4.80 credits (0697b894535c). With the chat's tools declared again, the same
body reads the chat's prefix (live replays: gpt-6.1-sol read 34,118 of 34,121 tokens for 0.13 credits, Opus read 60,446 for
0.50), and the relay's path for the compactions of clients that send their own tools applies to it: the chat's session and head,
its signed history continued (History.resume or resume_fork), nothing written for the instruction, keepwarm stopped after it.

The relay keeps the tools in memory only: after a restart, or for a conversation it never saw with tools, the compaction is served
as before. A request is the chat's own only for the same model: the tools of another model's list are another prefix.
"""
import collections
import copy
import threading
import time

TTL = 6 * 3600
MAX_CHATS = 256
# A tool list of this many JSON characters is not kept: a compaction that declared a giant list again would not read a prefix
# the chat could have written (the union of tool_union has the same bound for one tool, MAX_TOOL_CHARS).
MAX_CHARS = 1024 * 1024


class ChatTools:
    """conversation key -> (model, the tools of its last request that had some, when), least recently used out."""

    def __init__(self, max_chats=MAX_CHATS, ttl=TTL, clock=time.time):
        self._max, self._ttl, self._clock = max_chats, ttl, clock
        self._lock = threading.Lock()
        self._chats = collections.OrderedDict()

    def remember(self, key, model, tools):
        """Keep `tools` (the `tools` array of a request, as the client sent it) for the conversation `key` and `model`."""
        if not key or not isinstance(tools, list) or not tools or not isinstance(model, str):
            return
        now = self._clock()
        with self._lock:
            entry = self._chats.get(key)
            if entry is not None and entry[0] == model and entry[1] == tools:
                self._chats[key] = (model, entry[1], now)
            else:
                if len(repr(tools)) > MAX_CHARS:
                    self._chats.pop(key, None)
                    return
                self._chats[key] = (model, copy.deepcopy(tools), now)
            self._chats.move_to_end(key)
            while len(self._chats) > self._max:
                self._chats.popitem(last=False)

    def recall(self, key, model):
        """A copy of the tools the conversation declared last under `model`, or None when it has none, they are older than the
        time to live, or its last request with tools was for another model."""
        if not key:
            return None
        now = self._clock()
        with self._lock:
            entry = self._chats.get(key)
            if entry is None:
                return None
            if entry[0] != model or now - entry[2] > self._ttl:
                if now - entry[2] > self._ttl:
                    del self._chats[key]
                return None
            self._chats.move_to_end(key)
            return copy.deepcopy(entry[1])

    def __len__(self):
        with self._lock:
            return len(self._chats)
