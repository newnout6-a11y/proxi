"""A turn the user takes back does not send the conversation cold.

Every client has an undo for the conversation: OpenCode's /undo and Revert, Claude Code's /rewind (double Esc), Codex's
double Esc, Claude Desktop's edit and retry. The request after it is the conversation up to the end of an earlier turn and
a new message. The final answer of that turn was replaced by the turn after it, so the request found nothing to continue
and was sent cold: every message written to the cache again, though the provider still held the prefix it had cached."""
import asyncio
import copy

import pytest

import journal
import native_history
from test_conversation_state import stored
from test_native_context_reuse import FIRST, run
from test_native_history import append_result, close, harness, parsed, strip_cache, tool_reply
from test_review_fixes_pr19 import END, text


def call(call_id):
    return {"type": "tool_call", "toolCallId": call_id, "toolName": "read_file", "toolInput": {"path": call_id}}


def user(value):
    return {"role": "user", "content": [{"type": "text", "text": value}]}


def result(call_id):
    return {"role": "tool", "content": {"toolCallId": call_id, "toolResult": "ok"}}


def answer(value):
    return native_history.canonical_assistant([{"type": "text", "text": value}])


def tool_step(history, client, call_id, parent):
    blocks = [{"type": "thinking", "thinking": "", "signature": "sig-" + call_id}, call(call_id)]
    history.record("scope", client, [{"role": "user", "content": []}], blocks, native_history.canonical_assistant([call(call_id)]),
                   parent)
    return history.branches[-1]


def final_step(history, client, value, parent):
    history.record("scope", client, [{"role": "user", "content": []}], [{"type": "text", "text": value}], answer(value), parent)
    return history.branches[-1]


def test_a_final_answer_stays_a_point_to_go_back_to_and_a_tool_step_is_still_replaced():
    history = native_history.History()
    one = tool_step(history, [user("task")], "c1", None)
    done = final_step(history, [user("task"), native_history.canonical_assistant([call("c1")]), result("c1")], "done", one)
    assert history.branches == [done]  # the tool step was replaced by the step after it
    base = [user("task"), native_history.canonical_assistant([call("c1")]), result("c1"), answer("done")]
    two = tool_step(history, base + [user("next")], "c2", done)
    assert history.branches == [done, two] and history.points == [done]
    # The user takes "next" back and asks something else: the request continues the final answer before it.
    found, messages = history.resume("scope", base + [user("other")])
    assert found is done and messages[-2:] == [{"role": "assistant", "content": done.assistant}, user("other")]
    # The same request again (a retry) continues it too.
    assert history.resume("scope", base + [user("next")])[0] in (done, two) and history.resume(
        "scope", base + [user("next")])[0] is done
    # Going on from the newest branch is unchanged.
    assert history.resume("scope", base + [user("next"), native_history.canonical_assistant([call("c2")]),
                                           result("c2")])[0] is two


def test_points_are_bounded_and_give_way_to_other_branches():
    history = native_history.History()
    client, parent = [user("t0")], None
    for turn in range(6):
        parent = final_step(history, copy.deepcopy(client), "a%d" % turn, parent)
        client = client + [answer("a%d" % turn), user("t%d" % (turn + 1))]
    assert len(history.points) == native_history.REWIND_POINTS
    assert len(history.branches) == native_history.REWIND_POINTS + 1
    assert all(any(p is s for s in history.branches) for p in history.points)
    for index in range(20):  # parallel branches on the same key (a fork, a subagent)
        history.record("scope-%d" % index, [user("x%d" % index)], [], [{"type": "text", "text": "y"}], answer("y"))
    assert len(history.branches) == native_history.LIVE_BRANCHES and history.points == []


def test_a_restored_history_without_points_records_as_before():
    history = native_history.History()
    del history.points  # a History another version made, or one whose branches were replaced from a stored state
    one = tool_step(history, [user("task")], "c1", None)
    tool_step(history, [user("task"), native_history.canonical_assistant([call("c1")]), result("c1")], "c2", one)
    assert len(history.branches) == 1 and history.points == []


def append_answer(messages, value, dialect):
    if dialect == "chat":
        return messages + [{"role": "assistant", "content": value}]
    if dialect == "anthropic":
        return messages + [{"role": "assistant", "content": [{"type": "text", "text": value}]}]
    return messages + [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": value}]}]


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
@pytest.mark.parametrize("retry", [False, True], ids=["undo", "retry"])
def test_the_request_after_an_undo_continues_the_turn_before_it_and_reads_its_cached_prefix(tmp_path, monkeypatch, dialect,
                                                                                            retry):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply("c1", "opaque-one", "a"), [text("done"), *END],
                                                         tool_reply("c2", "opaque-two", "b"), tool_reply("c3", "opaque-tri", "c")])
    seq = journal.JOURNAL.last_seq
    second = "Now check b." if retry else "Now check c instead."

    async def scenario():
        try:
            history = copy.deepcopy(FIRST)
            p = parsed(history, dialect)
            history = append_result(history, p, await run(runner, p), dialect)
            await run(runner, parsed(history, dialect))
            history = append_answer(history, "done", dialect)
            await run(runner, parsed(history + [{"role": "user", "content": "Now check b."}], dialect))
            # The user takes "Now check b." back (or asks for it again) before the tool runs.
            await run(runner, parsed(history + [{"role": "user", "content": second}], dialect))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 4
    assert not stored(seq, "native_history.reset", "native_history.cold")
    assert [e["data"].get("rewound") for e in stored(seq, "native_history.resume")] == [None, None, True]
    # The request after the undo is the request before it up to its last message: the provider reads that prefix from the
    # cache, the breakpoints where they were, and the first answer's signature goes back with it.
    assert sent[3]["messages"][:-1] == sent[2]["messages"][:-1]
    assert "opaque-one" in str(sent[3]["messages"])
    if retry:
        assert strip_cache(sent[3]["messages"]) == strip_cache(sent[2]["messages"])
