"""A reminder the client adds to one request only does not send the next request cold.

OpenCode's goal plugin (packages/core/src/plugin/goal.ts) adds "<system-reminder>A goal is active for this session...
</system-reminder>" as a user message to every request without keeping it: at the end, or just before a trailing user
message. In the next request the reminder is gone and the model's reply stands where it was, so the kept signed reply
never matched and every step went cold: 89 requests in a row on 2026-11-06 (9b839275847f, ffb84e3c975b, 0aff4f28a7eb),
cache reads stuck at the 11.5k tokens of the system prompt, up to 122k tokens written and 4 to 18 credits a step."""
import asyncio
import copy
import json

import journal
import native_history
from test_conversation_state import stored
from test_native_context_reuse import FIRST, run
from test_native_history import append_result, close, harness, parsed, strip_cache, tool_reply
from test_review_fixes_pr19 import END, text

GOAL_TEXT = native_history.reminder("A goal is active for this session. Keep working toward it.\n\n"
                                    "<untrusted_objective>\nFix the parser.\n</untrusted_objective>")
GOAL = {"role": "user", "content": GOAL_TEXT}


def user(value):
    return {"role": "user", "content": [{"type": "text", "text": value}]}


def call(call_id):
    return {"type": "tool_call", "toolCallId": call_id, "toolName": "read_file", "toolInput": {"path": call_id}}


def result(call_id):
    return {"role": "tool", "content": {"toolCallId": call_id, "toolResult": "ok"}}


def reply(call_id):
    return [{"type": "thinking", "thinking": "", "signature": "opaque-" + call_id}, call(call_id)]


def echo(call_id):
    return native_history.canonical_assistant([call(call_id)])


TASK, REMINDER, LATER = user("Fix the parser."), user(GOAL_TEXT), user("Now the tests.")
UPSTREAM = [{"role": "user", "content": [{"type": "text", "text": "what upstream got", "cache": True}]}]


def kept(history, client, call_id="c1"):
    history.record("scope", client, copy.deepcopy(UPSTREAM), reply(call_id), echo(call_id))
    return history.branches[-1]


def test_a_reminder_left_out_after_the_tool_results_is_kept_upstream_and_the_reply_resumes():
    history = native_history.History()
    before = [TASK, echo("c0"), result("c0")]
    snapshot = kept(history, before + [REMINDER])
    client = before + [echo("c1"), result("c1"), REMINDER]
    found, messages = history.resume("scope", client)
    assert found is snapshot
    assert messages == UPSTREAM + [{"role": "assistant", "content": reply("c1")}, result("c1"), REMINDER]
    assert native_history.dropped(snapshot, client, messages) == 1


def test_a_reminder_left_out_before_a_new_user_message_is_kept_upstream_and_the_reply_resumes():
    history = native_history.History()
    answer = native_history.canonical_assistant([{"type": "text", "text": "Parser fixed."}])
    snapshot = kept(history, [TASK, answer, REMINDER, LATER])
    client = [TASK, answer, LATER, echo("c1"), result("c1"), REMINDER]
    found, messages = history.resume("scope", client)
    assert found is snapshot and messages[-2:] == [result("c1"), REMINDER]
    assert native_history.dropped(snapshot, client, messages) == 1


def test_a_reminder_the_client_keeps_resumes_as_before():
    history = native_history.History()
    snapshot = kept(history, [TASK, REMINDER])
    client = [TASK, REMINDER, echo("c1"), result("c1")]
    found, messages = history.resume("scope", client)
    assert found is snapshot and native_history.dropped(snapshot, client, messages) == 0


def test_only_a_reminder_may_be_left_out_and_nothing_before_the_closing_user_messages():
    history = native_history.History()
    answer = native_history.canonical_assistant([{"type": "text", "text": "Parser fixed."}])
    kept(history, [TASK, answer, LATER])
    assert history.resume("scope", [TASK, answer, echo("c1"), result("c1")]) == (None, None)  # the user's own words
    history = native_history.History()
    kept(history, [TASK, echo("c0"), result("c0"), REMINDER])
    edited = [user("Fix the lexer."), echo("c0"), result("c0")]
    assert history.resume("scope", edited + [echo("c1"), result("c1")]) == (None, None)  # an earlier message changed
    moved = [TASK, echo("c0"), result("c0"), echo("c1"), REMINDER, result("c1")]
    assert history.resume("scope", moved) == (None, None)  # a reminder between the call and its result
    other = [TASK, echo("c0"), result("c0"), user(native_history.reminder("Another note.")), echo("c1"), result("c1")]
    assert history.resume("scope", other) == (None, None)  # a new message before the reply


def test_what_counts_as_a_reminder():
    assert native_history.transient(REMINDER)
    assert native_history.transient(user("  " + GOAL_TEXT + "\n"))
    assert not native_history.transient(user("Keep working. " + GOAL_TEXT))
    assert not native_history.transient({"role": "user", "content": [{"type": "text", "text": GOAL_TEXT},
                                                                     {"type": "image", "imageData": "x"}]})
    assert not native_history.transient({"role": "assistant", "content": [{"type": "text", "text": GOAL_TEXT}]})
    assert not native_history.transient({"role": "user", "content": []})


def test_three_steps_of_a_conversation_with_a_goal_resume_and_extend_the_cached_prefix(tmp_path, monkeypatch):
    """The shape OpenCode sends: the reminder before the first message, then after the tool results of every step."""
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply("c1", "opaque-one", "a"),
                                                         tool_reply("c2", "opaque-two", "b"), [text("done"), *END]])
    seq = journal.JOURNAL.last_seq

    async def scenario():
        try:
            history = copy.deepcopy(FIRST)
            p = parsed([copy.deepcopy(GOAL)] + copy.deepcopy(history))
            history = append_result(history, p, await run(runner, p), "chat")
            p = parsed(copy.deepcopy(history) + [copy.deepcopy(GOAL)])
            history = append_result(history, p, await run(runner, p), "chat")
            await run(runner, parsed(copy.deepcopy(history) + [copy.deepcopy(GOAL)]))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 3
    assert not stored(seq, "native_history.reset", "native_history.cold")
    assert [e["data"].get("reminders_kept") for e in stored(seq, "native_history.resume")] == [1, 1]
    assert "opaque-one" in json.dumps(sent[1]) and "opaque-one" in json.dumps(sent[2]) and "opaque-two" in json.dumps(sent[2])
    for before, after in ((sent[0], sent[1]), (sent[1], sent[2])):
        # Every request extends the one before it, the reminder it no longer sends included, and the breakpoint stays at
        # the end of the request before: the whole of it is read from the cache.
        assert strip_cache(after["messages"][:len(before["messages"])]) == strip_cache(before["messages"])
        assert after["messages"][len(before["messages"]) - 1]["content"][-1].get("cache")
    assert "<system-reminder>" in sent[1]["messages"][-1]["content"][-1]["text"]
