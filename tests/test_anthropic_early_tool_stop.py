"""In the Anthropic stream a tool_use block stops as soon as upstream ends its call, not at the end of the message.

Claude Code runs a tool when its block stops. The relay kept every tool block open until message_delta, so the first
call of a parallel batch waited for the last one to be written: with early stops a local test client started tool A
4.7 s before the message ended, with the relay's shape 1.3 s after it (Claude Code test run of 2026-11-06)."""
import asyncio
import copy
import json

import adapters
from test_adapter_protocols import Writer, parsed
from test_native_context_reuse import FIRST, request, run
from test_native_history import TOOLS, close, harness
from test_review_fixes_pr19 import END


def test_a_finished_call_stops_before_the_next_one_starts():
    writer = Writer()
    adapter = adapters.AnthropicAdapter(parsed(), writer)
    adapter.begin()
    for event in [
        {"type": "tool_call", "id": "a", "name": "read", "arguments": "", "done": False},
        {"type": "tool_call", "id": "a", "name": "read", "arguments": '{"path":"a"}', "done": False},
        {"type": "tool_call", "id": "a", "name": "read", "arguments": '{"path":"a"}', "done": True},
        {"type": "tool_call", "id": "b", "name": "read", "arguments": '{"path":"b"}', "done": False},
        {"type": "tool_call", "id": "b", "name": "read", "arguments": '{"path":"b"}', "done": True},
    ]:
        adapter.feed(event)
    final = adapter.end("tool_calls")
    order = [(kind, p.get("index")) for kind, p in writer.events
             if kind in ("content_block_start", "content_block_stop", "message_delta")]
    assert order == [("content_block_start", 0), ("content_block_stop", 0), ("content_block_start", 1),
                     ("content_block_stop", 1), ("message_delta", None)]
    deltas = "".join(p["delta"]["partial_json"] for kind, p in writer.events
                     if kind == "content_block_delta" and p["index"] == 0)
    assert deltas == '{"path":"a"}'
    assert [block["input"] for block in final["content"]] == [{"path": "a"}, {"path": "b"}]


def test_a_call_that_is_never_marked_done_still_stops_at_the_end():
    writer = Writer()
    adapter = adapters.AnthropicAdapter(parsed(), writer)
    adapter.begin()
    adapter.feed({"type": "tool_call", "id": "a", "name": "read", "arguments": '{"path":"a"}'})
    adapter.end("tool_calls")
    assert [p["index"] for kind, p in writer.events if kind == "content_block_stop"] == [0]


def test_through_the_relay_each_call_stops_when_upstream_ends_it(tmp_path, monkeypatch):
    def call(call_id, path):
        return [{"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": "read_file"},
                {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps({"path": path})},
                {"event": "content_block_end"}]

    pool, runner, sent = harness(tmp_path, monkeypatch, [[*call("a", "x"), *call("b", "y"), *END]])

    async def scenario():
        try:
            return await run(runner, request(copy.deepcopy(FIRST), dialect="anthropic", tools=TOOLS))
        finally:
            await close(pool)

    events = asyncio.run(scenario())
    calls = [e for e in events if e["type"] == "tool_call"]
    done = [(e["id"], e["arguments"]) for e in calls if e.get("done")]
    assert done == [("a", '{"path": "x"}'), ("b", '{"path": "y"}')]
    # each done event comes before the next call starts
    first_b = next(i for i, e in enumerate(calls) if e["id"] == "b")
    assert any(e.get("done") and e["id"] == "a" for e in calls[:first_b])
