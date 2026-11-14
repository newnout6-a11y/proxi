"""A reply the output cap cut is continued with its signed thinking, not sent cold.

The relay ends a reply at the cap the client set (OutputCapAdapter, a cap below 16,384 on Claude) while upstream writes it
whole, so the kept reply held more than the client got, its echo never matched, and the next request was a reset and a cold
replay (v4 protocol audit of 2026-11-07: 6.1k of a 27k prompt written again in all three dialects, 121bb5f5, a51006ce,
6bac31da)."""
import asyncio
import copy
import json

import journal
import native_history
from test_conversation_state import stored
from test_native_context_reuse import FIRST, run
from test_native_history import close, harness, parsed
from test_review_fixes_pr19 import END, THINKING, signature, text

FULL = "Progress report. " * 120
CALL = {"type": "tool_call", "toolCallId": "c1", "toolName": "read_file", "toolInput": {"path": "a"}}


def user(value):
    return {"role": "user", "content": [{"type": "text", "text": value}]}


def kept(blocks):
    history = native_history.History()
    client = [user("task")]
    history.record("scope", client, [user("upstream task")], blocks,
                   native_history.canonical_assistant([b for b in blocks if b["type"] in ("text", "tool_call")]))
    return history, client


THOUGHT = {"type": "thinking", "thinking": "", "signature": "sig-1"}


def test_a_cut_text_resumes_with_the_kept_thinking_and_the_text_the_client_got():
    history, client = kept([THOUGHT, {"type": "text", "text": FULL}])
    cut = FULL[:700]
    found, messages = history.resume("scope", client + [native_history.canonical_assistant([{"type": "text", "text": cut}]),
                                                        user("continue")])
    assert found is not None
    assert messages[-2] == {"role": "assistant", "content": [THOUGHT, {"type": "text", "text": cut}]}
    assert messages[-1] == user("continue")


def test_a_reply_whose_call_the_cap_dropped_resumes_with_the_text_only():
    history, client = kept([THOUGHT, {"type": "text", "text": FULL}, CALL])
    found, messages = history.resume("scope", client + [native_history.canonical_assistant([{"type": "text", "text": FULL}]),
                                                        user("continue")])
    assert found is not None and [b["type"] for b in messages[-2]["content"]] == ["thinking", "text"]


def test_an_echo_that_is_not_a_cut_of_the_kept_reply_is_not_resumed():
    history, client = kept([THOUGHT, {"type": "text", "text": FULL}])
    edited = native_history.canonical_assistant([{"type": "text", "text": "Another reply altogether."}])
    assert history.resume("scope", client + [edited, user("continue")]) == (None, None)
    empty = {"role": "assistant", "content": []}
    assert history.resume("scope", client + [empty, user("continue")]) == (None, None)


def test_a_follow_up_to_a_reply_cut_at_the_cap_continues_the_signed_history(tmp_path, monkeypatch):
    pool, runner, sent = harness(tmp_path, monkeypatch, [
        [*THINKING, signature("sig"), signature("-1"), {"event": "content_block_end", "type": "thinking"}, text(FULL), *END],
        [text("ok"), *END]])
    seq = journal.JOURNAL.last_seq

    async def scenario():
        try:
            await run(runner, parsed(copy.deepcopy(FIRST)))
            # The client was given the reply up to its cap: the first 700 characters.
            history = copy.deepcopy(FIRST) + [{"role": "assistant", "content": FULL[:700]}, {"role": "user", "content": "go on"}]
            await run(runner, parsed(history))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2
    assert not stored(seq, "native_history.reset", "native_history.cold")
    assert len(stored(seq, "native_history.resume")) == 1
    assistant = [m for m in sent[1]["messages"] if m["role"] == "assistant"][-1]["content"]
    assert [b["type"] for b in assistant] == ["thinking", "text"] and assistant[0]["signature"] == "sig-1"
    assert json.dumps(assistant[1]["text"]) == json.dumps(FULL[:700])
