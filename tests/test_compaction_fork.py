"""A compaction that forks from the last request continues the prefix that request cached (item 21, a4 F11).

Claude Desktop's summary request is the history of its last request, without the reply to it, plus one user message: the
client files a reply into the next turn it builds, and the compaction is built before that. History.resume needs the echo
of that reply and never accepted this shape, so a compaction on a warm cache started the history again and wrote all of it:
485k tokens, 51.9 credits for one /compact (a4, real session 3410bb25); emulated at 130k tokens 6.61 credits and 9.2 s to the
first token against 2.57 and 2.5 s when the prefix is read. History.resume_fork answers that shape for a compaction only,
from the snapshot's own input, and replays no reply of it."""
import copy
import json

import pytest

import native_history
import validate
from prompt_support import client_request, prompt_relay, strip_cache  # noqa: F401 - prompt_relay is a fixture
from test_compaction_cache import (CLAUDE_CODE, INSTRUCTION, Chat, Idle, OPENCODE, SUMMARY, count, tail_after,
                                   thinking, with_instruction)
from test_client_contracts import CFG
from test_native_context_reuse import FIRST as TASK
from test_native_history import tool_reply

SCOPE = "scope"
CALL = {"type": "tool_call", "toolCallId": "c1", "toolName": "read", "toolInput": {"path": "a"}}
ECHO = {"role": "assistant", "content": [CALL]}


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def result(call_id="c1"):
    return {"role": "tool", "content": {"toolCallId": call_id, "toolResult": "file contents"}}


def history(client, upstream=5):
    """A History as the relay holds it after serving `client`: the upstream messages are opaque, counted markers."""
    held = native_history.History()
    held.record(SCOPE, client, [{"role": "marker", "n": number} for number in range(upstream)], [CALL], ECHO)
    return held


MAIN = [user("task"), {"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "c0", "toolName": "read",
                                                            "toolInput": {}}]}, result("c0")]
FORK = MAIN + [user("summarize the conversation")]


def test_the_fork_shape_is_the_last_requests_history_plus_user_messages():
    held = history(MAIN, upstream=7)
    assert held.resume(SCOPE, FORK) == (None, None)  # it never was a continuation: there is no echo of a reply in it
    snapshot, messages = held.resume_fork(SCOPE, FORK)
    assert snapshot is held.branches[-1]
    assert messages[:7] == snapshot.messages and messages[7:] == [user("summarize the conversation")]
    assert not any(message.get("role") == "assistant" for message in messages[7:])  # no reply, no signature
    # several user messages after the shared history (an instruction and a nudge) fork as well
    _, two = held.resume_fork(SCOPE, MAIN + [user("one"), user("two")])
    assert two[7:] == [user("one"), user("two")]


def test_the_sequential_shape_is_still_answered_by_resume_and_not_by_the_fork():
    held = history(MAIN)
    sequential = MAIN + [ECHO, result("c1"), user("summarize")]
    assert held.resume(SCOPE, sequential)[0] is held.branches[-1]
    assert held.resume_fork(SCOPE, sequential) == (None, None)  # an assistant message follows the shared history


def test_the_fork_needs_the_history_to_be_the_same_the_scope_to_be_the_same_and_something_to_add():
    held = history(MAIN)
    assert held.resume_fork(SCOPE, FORK)[0] is not None
    changed = copy.deepcopy(FORK)
    changed[0] = user("another task")
    assert held.resume_fork(SCOPE, changed) == (None, None)  # a message of the shared history differs
    assert held.resume_fork(SCOPE, MAIN[:-1] + [user("x")]) == (None, None)  # the history is shorter than the snapshot's
    with_result = MAIN + [result("c9"), user("summarize")]
    assert held.resume_fork(SCOPE, with_result) == (None, None)  # a tool result between: not a fork, the call has no echo
    assert held.resume_fork("another", FORK) == (None, None)  # another effort, model, head, tools, account
    assert held.resume_fork(SCOPE, MAIN) == (None, None)  # the same history again, nothing added
    assert history(MAIN).resume_fork(SCOPE, []) == (None, None) and native_history.History().resume_fork(SCOPE, FORK) == (None, None)


def test_the_newest_snapshot_that_fits_is_the_one_forked_from():
    held = native_history.History()
    held.record(SCOPE, MAIN[:1], [{"role": "marker", "n": 0}], [CALL], ECHO)
    held.record(SCOPE, MAIN, [{"role": "marker", "n": number} for number in range(4)], [CALL], ECHO)
    snapshot, messages = held.resume_fork(SCOPE, FORK)
    assert snapshot is held.branches[-1] and len(messages) == 5
    other = native_history.History()
    other.record(SCOPE, MAIN, [{"role": "marker", "n": 0}], [CALL], ECHO)
    other.record(SCOPE, [user("an unrelated dialog")], [{"role": "marker", "n": 1}], [CALL], ECHO)
    assert other.resume_fork(SCOPE, FORK)[0] is other.branches[0]  # an older branch of the same dialog still fits


def test_a_desktop_body_is_parsed_into_exactly_this_shape():
    """The canonical history of a Messages request that ends in tool results, and of the compaction that merged its
    instruction into the same user turn (what the real capture 4f7c9d59 of Desktop looked like in the relay's form)."""
    tools = [{"name": "Read", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    call = {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "a"}}
    last = [{"role": "user", "content": "task"},
            {"role": "assistant", "content": [{"type": "text", "text": "reading"}, call]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "data"}]}]
    compaction = copy.deepcopy(last)
    compaction[-1]["content"].append({"type": "text", "text": "Your task is to create a detailed summary"})

    def canonical(messages):
        return validate.parse_anthropic({"model": "claude-opus-5-5", "max_tokens": 64, "messages": messages, "tools": tools},
                                        CFG).native_messages

    held = history(canonical(last))
    snapshot, messages = held.resume_fork(SCOPE, canonical(compaction))
    assert snapshot is not None and messages[-1]["role"] == "user"
    assert messages[-1]["content"][0]["text"].startswith("Your task is to create")
    assert held.resume(SCOPE, canonical(compaction)) == (None, None)


# --- what _dialog_effort says about it -------------------------------------------------------------------------------

def fork_messages(idle):
    """The compaction of Desktop's shape for an Idle dialog: the history of its first request and the instruction."""
    return json.loads(json.dumps(TASK)) + [{"role": "user", "content": INSTRUCTION}]


def test_the_dialog_effort_is_kept_for_a_fork_as_for_a_turn_of_the_dialog(tmp_path, monkeypatch):
    idle = Idle(tmp_path, monkeypatch)
    forked = fork_messages(idle)
    assert idle.ask("high", messages=forked) == ("max", None)  # taken: the fork continues on max
    assert idle.ask("max", messages=forked) == (None, None)  # the request already asks for it


def test_a_lower_dialog_effort_is_never_taken_for_a_fork_either(tmp_path, monkeypatch):
    idle = Idle(tmp_path, monkeypatch, effort="low")
    assert idle.ask("high", messages=fork_messages(idle)) == (None, {"reason": "lower"})
    assert idle.ask("low", messages=fork_messages(idle)) == (None, None)


def test_a_history_that_is_neither_a_turn_nor_a_fork_is_still_a_history_that_restarts(tmp_path, monkeypatch):
    idle = Idle(tmp_path, monkeypatch)
    edited = fork_messages(idle)
    edited[0] = {"role": "user", "content": "an edited task"}
    assert idle.ask("high", messages=edited) == (None, {"reason": "history"})


# --- through the relay -----------------------------------------------------------------------------------------------

def fork_of(relay, dialect, effort="max", asked="high", headers=None, model="claude-sonnet-5-5", rounds=1):
    """(chat, body of the last request, body of the compaction) with the compaction in Desktop's shape: the history of the
    last request, not its reply, and the instruction after it (in the same user turn for Messages, after it otherwise)."""
    chat = Chat(relay, dialect=dialect, effort=effort, headers=headers, model=model)
    chat.work(rounds)
    before = copy.deepcopy(chat.messages)  # the history of the next request, the reply to which the client files later
    last = chat.ask(tool_reply("c9", "opaque-9"))
    summary = chat.ask(SUMMARY, effort=asked, messages=with_instruction(dialect, before, INSTRUCTION))
    return chat, last, summary


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_a_compaction_that_forks_from_the_last_request_reads_its_prefix_byte_for_byte(prompt_relay, dialect):
    relay = prompt_relay()
    chat = Chat(relay, dialect=dialect, effort="max")
    chat.work(2)
    before = copy.deepcopy(chat.messages)
    last = chat.ask(tool_reply("c9", "opaque-9"))
    mark = relay.mark()
    summary = chat.ask(SUMMARY, effort="high", messages=with_instruction(dialect, before, INSTRUCTION))
    # the provider sees the dialog's effort and its own prefix; the reply to the last request is not in the body
    assert summary["reasoningEffort"] == "max"
    added = tail_after(last, summary)
    assert [message["role"] for message in added] == ["user"]
    assert thinking(summary) == thinking(last) and len(thinking(last)) >= 2  # the signatures of the turns before, as sent
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "native_history.resume") == 1
    resume = relay.events(mark, "native_history.resume")[0]["data"]
    assert resume["fork"] is True and resume["blocks"] == 0 and resume["signatures"] == 0
    kept = relay.events(mark, "cache.effort_kept")
    assert len(kept) == 1 and kept[0]["data"]["dialog_effort"] == "max" and kept[0]["data"]["requested_effort"] == "high"
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert (begin["client_task"], begin["effort_resolution"]) == ("compaction", "cache_continuity")
    assert count(relay, mark, "cache.compaction_restart") == 0


def test_the_fork_is_a_side_request_it_records_nothing_and_the_chat_goes_on(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay, effort="max")
    chat.work(2)
    before = copy.deepcopy(chat.messages)
    chat.ask(tool_reply("c9", "opaque-9"))
    session = relay.runtime.pool._conversations["header:ses_c3"]
    branches = list(session._native_history.branches)
    chat.ask(SUMMARY, effort="high", messages=with_instruction("chat", before, INSTRUCTION))
    assert session._native_history.branches == branches  # the summary is no branch of the dialog
    mark = relay.mark()
    chat.ask(tool_reply("c10", "opaque-10"))  # the chat goes on from its own branch, after a compaction its client dropped
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "native_history.resume") == 1


def test_the_same_shape_that_is_no_compaction_is_a_history_that_restarts_as_before(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay, effort="max")
    chat.work(2)
    before = copy.deepcopy(chat.messages)
    chat.ask(tool_reply("c9", "opaque-9"))
    mark = relay.mark()
    ordinary = chat.ask(SUMMARY, effort="max", messages=with_instruction("chat", before, "Now tell me a joke."))
    assert count(relay, mark, "native_history.resume") == 0 and count(relay, mark, "native_history.reset") == 1
    assert thinking(ordinary) == []  # no signature goes with a rebuilt prefix


def test_a_fork_of_a_dialog_on_a_lower_effort_is_not_taken_for_a_client_that_asks_for_more(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay, effort="low")
    chat.work(2)
    before = copy.deepcopy(chat.messages)
    chat.ask(tool_reply("c9", "opaque-9"))
    mark = relay.mark()
    summary = chat.ask(SUMMARY, effort="high", messages=with_instruction("chat", before, INSTRUCTION))
    assert summary["reasoningEffort"] == "high"
    assert [event["data"]["reason"] for event in relay.events(mark, "cache.compaction_restart")] == ["lower"]
    assert count(relay, mark, "cache.effort_kept") == 0 and count(relay, mark, "native_history.resume") == 0


def test_with_the_setting_off_the_client_effort_decides_and_the_fork_does_not_match(prompt_relay):
    relay = prompt_relay(compaction_keep_effort=False)
    chat = Chat(relay, effort="max")
    chat.work(2)
    before = copy.deepcopy(chat.messages)
    chat.ask(tool_reply("c9", "opaque-9"))
    mark = relay.mark()
    summary = chat.ask(SUMMARY, effort="high", messages=with_instruction("chat", before, INSTRUCTION))
    assert summary["reasoningEffort"] == "high"  # another effort is another scope: nothing continues, as it always was
    assert count(relay, mark, "native_history.resume") == 0 and count(relay, mark, "native_history.reset") == 1


def test_claude_code_in_the_messages_dialect_forks_as_desktop_does(prompt_relay):
    relay = prompt_relay()
    chat, last, summary = fork_of(relay, "anthropic", headers=CLAUDE_CODE, rounds=2)
    assert [message["role"] for message in tail_after(last, summary)] == ["user"]
    assert summary["reasoningEffort"] == "max"
