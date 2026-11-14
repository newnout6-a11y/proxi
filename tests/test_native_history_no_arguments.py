"""A reply that calls a tool without arguments is kept for native continuation, and a reset says why.

Retool streams such a call as one content_block_delta whose partialInputs is "" (live trace, 2026-11-06: CronList,
ListMcpResourcesTool). The stream gives the client {} for it, but the reply capture parsed the empty text as JSON,
failed, and dropped the whole reply: native_history.unavailable. Claude Desktop's own tools (get_settings, get_layout,
list_terminal_tabs, preview_list, ...) take no arguments, so every such reply reset the next turn to a cold history
and rewrote the cached tail of the conversation. The journal only said "prefix_or_settings_mismatch" for a reset; it
now names the check the newest kept reply failed."""
import asyncio
import copy
import json

import adapters
import journal
import native_history
from test_conversation_state import stored
from test_native_context_reuse import FIRST, request, run
from test_native_history import append_result, close, harness, parsed, tool_reply
from test_review_fixes_pr19 import END, THINKING, signature, text

import pytest

TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {
             "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
         {"type": "function", "function": {"name": "list_tabs", "parameters": {"type": "object", "properties": {}}}}]


def no_argument_call(call_id):
    """The exact shape Retool streams for a call without arguments."""
    return [{"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": "list_tabs"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": ""},
            {"event": "content_block_end"}]


def reply(sig="opaque-sig"):
    return [*THINKING, signature(sig), {"event": "content_block_end"}, *no_argument_call("t1"),
            {"event": "content_block_start", "type": "tool_call", "toolCallId": "r1", "toolName": "read_file"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": ""},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"path": "a"}'},
            {"event": "content_block_end"}, *END]


def answered(messages, p, events, dialect):
    """The client's next request: its echo of the reply and a result for every call."""
    cls = {"chat": adapters.ChatAdapter, "anthropic": adapters.AnthropicAdapter,
           "responses": adapters.ResponsesAdapter}[dialect]
    adapter = cls(p, None)
    for event in events:
        adapter.feed(event)
    payload = adapter.payload("tool_calls")
    if dialect == "chat":
        assistant = payload["choices"][0]["message"]
        return messages + [assistant] + [{"role": "tool", "tool_call_id": call["id"], "content": "result"}
                                         for call in assistant["tool_calls"]]
    if dialect == "anthropic":
        calls = [b["id"] for b in payload["content"] if b["type"] == "tool_use"]
        return messages + [{"role": "assistant", "content": payload["content"]},
                           {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                                         "content": "result"} for call_id in calls]}]
    calls = [b["call_id"] for b in payload["output"] if b["type"] == "function_call"]
    return messages + payload["output"] + [{"type": "function_call_output", "call_id": call_id, "output": "result"}
                                           for call_id in calls]


def test_the_capture_keeps_a_call_without_arguments_as_an_empty_object():
    capture = native_history.ReplyCapture()
    for event in reply()[:-len(END)]:
        capture.feed(event)
    blocks = capture.finish()
    assert [b["type"] for b in blocks] == ["thinking", "tool_call", "tool_call"]
    assert [b["toolInput"] for b in blocks[1:]] == [{}, {"path": "a"}]


def test_arguments_that_are_not_json_still_cannot_be_kept():
    capture = native_history.ReplyCapture()
    for event in [{"event": "content_block_start", "type": "tool_call", "toolCallId": "a", "toolName": "list_tabs"},
                  {"event": "content_block_delta", "type": "tool_call", "partialInputs": " "}]:
        capture.feed(event)
    assert capture.finish() is None


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_the_next_turn_continues_a_reply_with_a_call_without_arguments(tmp_path, monkeypatch, dialect):
    pool, runner, sent = harness(tmp_path, monkeypatch, [reply(), [text("done"), *END]])

    async def scenario():
        try:
            p = request(copy.deepcopy(FIRST), dialect=dialect, tools=TOOLS)
            events = await run(runner, p)
            assert not any(e["type"] == "error" for e in events)
            calls = [e for e in events if e["type"] == "tool_call"]
            assert {e["name"]: e["arguments"] for e in calls if e["name"] == "list_tabs"} == {"list_tabs": "{}"}
            following = request(answered(copy.deepcopy(FIRST), p, events, dialect), dialect=dialect, tools=TOOLS)
            await run(runner, following)
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2
    # The signed thinking of turn 1 goes back upstream: the turn continued natively instead of going cold.
    assistant = [m for m in sent[1]["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[0]["content"][0] == {"type": "thinking", "thinking": "", "signature": "opaque-sig"}
    assert [b["toolInput"] for b in assistant[0]["content"] if b["type"] == "tool_call"] == [{}, {"path": "a"}]
    assert json.dumps(sent[1]).count("opaque-sig") == 1


@pytest.mark.parametrize("change,why", [
    ("prefix", "message:0:user"), ("arguments", "echo:arguments"), ("call_id", "echo:call_ids"),
    ("result_id", "tool_results"), ("missing_result", "tool_results"), ("head", "scope:head"),
    ("effort", "scope:provider(reasoningEffort)"), ("tools", "scope:tools")])
def test_a_reset_names_the_check_the_kept_reply_failed(tmp_path, monkeypatch, change, why):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), [text("new answer"), *END]])
    seq = journal.JOURNAL.last_seq

    async def scenario():
        try:
            p = parsed(FIRST)
            messages = append_result(copy.deepcopy(FIRST), p, await run(runner, p), "chat")
            if change == "prefix":
                messages[0]["content"] = "edited task"
            elif change == "arguments":
                messages[-2]["tool_calls"][0]["function"]["arguments"] = '{"path":"different","limit":3}'
            elif change == "call_id":
                messages[-2]["tool_calls"][0]["id"] = "other"
            elif change == "result_id":
                messages[-1]["tool_call_id"] = "other"
            elif change == "missing_result":
                messages[-1] = {"role": "user", "content": "continue"}
            p = parsed(messages, system="NEW_SYSTEM" if change == "head" else "CLIENT_SYSTEM")
            if change == "effort":
                p.effort = "low"
            if change == "tools":
                p.tools[0]["function"]["description"] = "changed tool"
            await run(runner, p)
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert [e["data"]["why"] for e in stored(seq, "native_history.reset")] == [why]
