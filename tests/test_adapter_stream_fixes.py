"""Wave-A stream fixes: _drain robustness, tool id fallback, end-of-stream finalization."""
import asyncio
import json
from types import SimpleNamespace

import direct
from adapters import error_payload
from apierr import upstream_error


def _drain_events(lines):
    async def collect():
        queue = asyncio.Queue()
        for line in lines:
            await queue.put(line)
        await queue.put(None)
        out = []
        async for event in direct._drain({"direct_debug": False},
                                         SimpleNamespace(_cancelled=False, thread_id="t"),
                                         queue, [], [], None, {}):
            out.append(event)
        return out

    return asyncio.run(collect())


def test_non_dict_json_line_is_skipped():
    lines = [json.dumps(123), json.dumps([1, 2]), json.dumps("x"),
             json.dumps({"event": "content_block_start", "type": "text"}),
             json.dumps({"event": "content_block_delta", "type": "text", "partialText": "ok"}),
             json.dumps({"event": "end_of_stream"})]
    events = _drain_events(lines)
    assert {"type": "text", "delta": "ok"} in events, "valid events must survive a stray scalar line"


def test_metadata_with_broken_usage_is_ignored():
    lines = [json.dumps({"event": "metadata", "usage": [1, 2]}),
             json.dumps({"event": "metadata", "usage": {"creditsUsed": "not-a-number"}}),
             json.dumps({"event": "metadata", "usage": {"creditsUsed": 1.25,
                                                        "inputTokens": 10, "outputTokens": 5}}),
             json.dumps({"event": "end_of_stream"})]
    events = _drain_events(lines)
    credits = [e for e in events if e["type"] == "credits"]
    assert len(credits) == 1, "broken metadata must not produce a credits event"
    assert credits[0]["value"] == 1.25


def test_tool_calls_without_ids_get_distinct_keys():
    lines = [json.dumps({"event": "content_block_start", "type": "tool_call", "toolName": "alpha"}),
             json.dumps({"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"x":'}),
             json.dumps({"event": "content_block_delta", "type": "tool_call", "partialInputs": '1}'}),
             json.dumps({"event": "content_block_end", "type": "tool_call"}),
             json.dumps({"event": "content_block_start", "type": "tool_call", "toolName": "beta"}),
             json.dumps({"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"y":2}'}),
             json.dumps({"event": "content_block_end", "type": "tool_call"}),
             json.dumps({"event": "end_of_stream"})]
    events = _drain_events(lines)
    finals = [e for e in events if e["type"] == "tool_call" and not e.get("partial")]
    assert len(finals) == 2, "id-less calls must not be merged into one"
    assert {f["name"] for f in finals} == {"alpha", "beta"}
    assert len({f["id"] for f in finals}) == 2, "fallback ids must be distinct"
    by_name = {f["name"]: f["arguments"] for f in finals}
    assert json.loads(by_name["alpha"]) == {"x": 1}
    assert json.loads(by_name["beta"]) == {"y": 2}


def test_text_block_end_does_not_finalize_tool_call():
    lines = [json.dumps({"event": "content_block_start", "type": "tool_call", "toolCallId": "tc1",
                         "toolName": "reader"}),
             json.dumps({"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"p":'}),
             json.dumps({"event": "content_block_end", "type": "text"}),
             json.dumps({"event": "content_block_delta", "type": "tool_call", "partialInputs": '1}'}),
             json.dumps({"event": "content_block_end", "type": "tool_call", "toolCallId": "tc1"}),
             json.dumps({"event": "end_of_stream"})]
    events = _drain_events(lines)
    finals = [e for e in events if e["type"] == "tool_call" and not e.get("partial")]
    assert len(finals) == 1, "a text-block end must not finalize the open tool call"
    assert json.loads(finals[0]["arguments"]) == {"p": 1}, "arguments from both deltas must survive"


def test_end_of_stream_finalizes_open_call():
    lines = [json.dumps({"event": "content_block_start", "type": "tool_call", "toolCallId": "tc1",
                         "toolName": "reader"}),
             json.dumps({"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"p":1}'}),
             json.dumps({"event": "end_of_stream"})]
    events = _drain_events(lines)
    finals = [e for e in events if e["type"] == "tool_call" and not e.get("partial")]
    assert len(finals) == 1, "an open call must be finalized when the stream ends"
    assert finals[0]["id"] == "tc1"
    assert json.loads(finals[0]["arguments"]) == {"p": 1}


def test_coerce_arguments_never_emits_infinity():
    result = direct.coerce_arguments('{"n": "1.0e999", "b": "true"}',
                                     {"n": {"type": "number"}, "b": {"type": "boolean"}})
    assert "Infinity" not in result, "non-finite numbers must stay raw strings"
    data = json.loads(result)
    assert data["n"] == "1.0e999"
    assert data["b"] is True


def test_error_payload_shapes_per_dialect():
    err = upstream_error("boom")
    assert error_payload("chat", err) == err.openai_envelope()
    assert error_payload("anthropic", err)["error"]["message"] == "boom"
    responses = error_payload("responses", err)
    assert responses["type"] == "error", "responses stream errors must carry an explicit type"
    assert responses["message"] == "boom"
