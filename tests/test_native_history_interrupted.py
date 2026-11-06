"""A reply the relay could not keep does not send the conversation cold.

When a stream breaks (a few seconds without internet on 2026-11-06: "Retool connection failed: ConnectionResetError"),
OpenCode keeps the part of the reply it got as an assistant message of its own, answers its tool call with the error,
and adds "The previous response was interrupted. Continue from where you left off." The relay had kept the reply
before it, but any assistant message after that reply's tool results failed the resume check, so the next request
went cold and the whole cached conversation was written again: 281k tokens (14 credits) for one agent, 617k (31) for
another. Such turns now follow the kept reply unsigned, as a cold replay would send them, and the cached prefix with
the signed history is read instead of rewritten."""
import asyncio
import copy
import json

import journal
import native_history
from test_conversation_state import stored
from test_native_context_reuse import FIRST
from test_native_history import append_result, close, harness, parsed, tool_reply
from test_review_fixes_pr19 import END, text
from test_native_context_reuse import run

INTERRUPTED = [
    {"role": "assistant", "content": "Writing the test now.",
     "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "b"}'}}]},
    {"role": "tool", "tool_call_id": "c2", "content": "Retool connection failed: ConnectionResetError"},
    {"role": "user", "content": "The previous response was interrupted. Continue from where you left off."}]


def test_the_next_turn_resumes_after_an_interrupted_reply(tmp_path, monkeypatch):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), [text("done"), *END]])
    seq = journal.JOURNAL.last_seq

    async def scenario():
        try:
            p = parsed(copy.deepcopy(FIRST))
            messages = append_result(copy.deepcopy(FIRST), p, await run(runner, p), "chat") + copy.deepcopy(INTERRUPTED)
            await run(runner, parsed(messages))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2
    assert "opaque-one" in json.dumps(sent[1])  # the signed reply went back: no cold rewrite
    roles = [m["role"] for m in sent[1]["messages"]]
    assert roles[-5:] == ["assistant", "tool", "assistant", "tool", "user"]
    unsigned = sent[1]["messages"][-3]["content"]
    assert [b["type"] for b in unsigned] == ["text", "tool_call"] and unsigned[1]["toolCallId"] == "c2"
    assert not stored(seq, "native_history.reset", "native_history.cold")


def test_a_turn_that_leaves_a_call_unanswered_still_goes_cold():
    history = native_history.History()
    echo = {"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "c1", "toolName": "t", "toolInput": {}}]}
    history.record("scope", [{"role": "user", "content": [{"type": "text", "text": "go"}]}], [], echo["content"], echo)
    base = [{"role": "user", "content": [{"type": "text", "text": "go"}]}, echo,
            {"role": "tool", "content": {"toolCallId": "c1", "toolResult": "ok"}}]
    open_call = {"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "c2", "toolName": "t",
                                                   "toolInput": {}}]}
    user = {"role": "user", "content": [{"type": "text", "text": "continue"}]}
    assert history.resume("scope", base + [open_call, user]) == (None, None)  # c2 never answered
    assert history.resume("scope", base + [open_call]) == (None, None)  # ends on the assistant
    reused = {"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "c1", "toolName": "t",
                                                "toolInput": {}}]}
    assert history.resume("scope", base + [reused, {"role": "tool", "content": {"toolCallId": "c1",
                                                                                "toolResult": "x"}}]) == (None, None)
    answered = [open_call, {"role": "tool", "content": {"toolCallId": "c2", "toolResult": "err"}}, user]
    assert history.resume("scope", base + answered)[0] is not None
