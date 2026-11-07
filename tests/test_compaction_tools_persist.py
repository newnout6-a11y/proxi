"""The tools of each Codex chat are kept on disk, so a compaction right after a relay restart still reads the chat's cache.

Kept in memory only, a relay restart forgot them, and a Codex thread whose first request after the restart was its compaction
(`codex exec resume` of a thread already over its limit, Codex Desktop reopening one) was served without tools: its whole history
written again although the provider's cache of the chat was still alive (v8's report, 2026-11-07)."""
import json

import compaction_tools
from compaction_tools import ChatTools
from prompt_support import prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_codex_compaction import SESSION, codex_chat, compact
from test_compaction_cache import count

TOOLS = [{"type": "function", "name": "shell", "parameters": {"type": "object"}},
         {"type": "function", "name": "apply_patch", "parameters": {"type": "object"}}]


def test_the_tools_are_read_back_by_the_next_run(tmp_path):
    path = str(tmp_path / compaction_tools.FILE_NAME)
    ChatTools(path=path).remember("header:a", "gpt-6.1-sol", TOOLS)
    assert ChatTools(path=path).recall("header:a", "gpt-6.1-sol") == TOOLS
    assert ChatTools(path=path).recall("header:a", "claude-opus-5-5") is None  # another model is another prefix


def test_one_copy_of_a_list_is_kept_for_all_the_chats_that_declared_it(tmp_path):
    path = tmp_path / compaction_tools.FILE_NAME
    tools = ChatTools(path=str(path))
    for key in ("header:a", "header:b", "header:c"):
        tools.remember(key, "gpt-6.1-sol", TOOLS)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert len(document["lists"]) == 1 and len(document["chats"]) == 3


def test_a_chat_not_seen_for_the_time_to_live_is_not_read_back(tmp_path):
    path, now = str(tmp_path / compaction_tools.FILE_NAME), [1000.0]
    ChatTools(path=path, ttl=100, clock=lambda: now[0]).remember("header:a", "m", TOOLS)
    now[0] += 101
    assert ChatTools(path=path, ttl=100, clock=lambda: now[0]).recall("header:a", "m") is None


def test_a_file_that_cannot_be_read_starts_empty_and_without_a_path_nothing_is_written(tmp_path):
    path = tmp_path / compaction_tools.FILE_NAME
    path.write_text("{not json", encoding="utf-8")
    assert ChatTools(path=str(path)).recall("header:a", "m") is None
    ChatTools().remember("header:a", "m", TOOLS)
    assert [p.name for p in tmp_path.iterdir()] == [compaction_tools.FILE_NAME]


def test_a_compaction_right_after_a_restart_declares_the_chats_tools_and_continues_its_history(prompt_relay, tmp_path):
    state = str(tmp_path / "state")
    relay = prompt_relay(conversation_state_dir=state)
    chat = codex_chat(relay)
    _, second = chat.work(2)
    assert relay.runtime.pool._store.flush(10)
    again = prompt_relay(conversation_state_dir=state)  # the relay started again; the compaction is the chat's first request
    chat.relay = again
    mark = again.mark()
    _, (summary,) = compact(chat)
    assert summary["tools"] == second["tools"] and summary["openAICacheKey"] == second["openAICacheKey"]
    assert len(again.events(mark, "codex.compaction_tools_restored")) == 1
    assert count(again, mark, "codex.compaction_tools_unknown") == 0
    assert count(again, mark, "native_history.resume") == 1 and count(again, mark, "native_history.reset") == 0
    assert SESSION in json.dumps(json.loads((tmp_path / "state" / compaction_tools.FILE_NAME).read_text(encoding="utf-8")))
