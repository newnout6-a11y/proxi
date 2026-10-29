"""Tool-call arguments stream to clients while the model generates them."""
import asyncio
import json
import random

import pytest

import adapters
import direct

PARAMS = {"offset": {"type": "number"}, "limit": {"type": "number"}, "flag": {"type": "boolean"},
          "name": {"type": "string"}, "file_path": {"type": "string"}, "new_string": {"type": "string"}}

CASES = [
    '{"offset": "1", "limit": "30", "flag": "true", "name": "7"}',
    '{"file_path":"C:\\\\a\\\\b.py","new_string":"line1\\nline \\"two\\" {x: [1,2]}\\n","limit":"2.5"}',
    '{"nested": {"limit": "3", "list": ["a", {"b": "}"}]}, "offset": "x1", "flag": "False"}',
    '{}',
    ' {"flag":true,"offset":5}',
    '{"name": "\\u043f\\u0440\\u0438\\u0432\\u0435\\u0442", "limit": "-4"}',
]


def chunks(text, rng):
    out, i = [], 0
    while i < len(text):
        step = rng.randint(1, 7)
        out.append(text[i:i + step])
        i += step
    return out


@pytest.mark.parametrize("raw", CASES)
def test_stream_matches_batch_coercion_for_any_chunking(raw):
    expected = json.loads(direct.coerce_arguments(raw, PARAMS))
    rng = random.Random(raw)
    for _ in range(40):
        stream = direct.ArgStream(PARAMS)
        emitted = ""
        for piece in chunks(raw, rng):
            emitted += stream.feed(piece)
        emitted += stream.finish()
        assert json.loads(emitted) == expected


def test_long_string_bodies_are_emitted_before_the_call_ends():
    stream = direct.ArgStream(PARAMS)
    first = stream.feed('{"file_path":"x.py","new_string":"def f():\\n    return 1')
    assert first.endswith("return 1")  # an unfinished string body is already visible
    held = stream.feed('","limit":"1')
    assert held == '","limit":'  # only the number value waits for its closing quote
    assert stream.feed('2"}') == "12}"


def test_non_object_and_truncated_input_pass_through():
    stream = direct.ArgStream(PARAMS)
    assert stream.feed("not json") == "not json"
    stream = direct.ArgStream(PARAMS)
    assert stream.feed('{"limit":"12') == '{"limit":'
    assert stream.finish() == '"12'


class _S:
    _cancelled = False


def _drain_events(lines, params=None):
    async def collect():
        queue = asyncio.Queue()
        for line in lines:
            queue.put_nowait(json.dumps(line))
        queue.put_nowait(None)
        return [event async for event in direct._drain({}, _S(), queue, [], [], params)]
    return asyncio.run(collect())


TOOL_LINES = [
    {"event": "content_block_start", "type": "tool_call", "toolName": "Edit", "toolCallId": "c1"},
    {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"file_path":"a.py",'},
    {"event": "content_block_delta", "type": "tool_call", "partialInputs": '"new_string":"hello '},
    {"event": "content_block_delta", "type": "tool_call", "partialInputs": 'world","limit":"3"}'},
    {"event": "content_block_end"},
]


def test_drain_announces_call_then_streams_growing_arguments():
    events = _drain_events(TOOL_LINES, {"Edit": PARAMS})
    assert all(e["type"] == "tool_call" for e in events)
    assert events[0] == {"type": "tool_call", "id": "c1", "name": "Edit", "arguments": "", "partial": True}
    partial = [e["arguments"] for e in events if e.get("partial")]
    assert any("hello" in a and not a.endswith("}") for a in partial)
    for before, after in zip(partial, partial[1:]):
        assert after.startswith(before)
    final = events[-1]
    assert "partial" not in final and final["arguments"].startswith(partial[-1])
    assert json.loads(final["arguments"]) == {"file_path": "a.py", "new_string": "hello world", "limit": 3}


def test_drain_interleaved_calls_keep_separate_arguments():
    lines = [
        {"event": "content_block_start", "type": "tool_call", "toolName": "Read", "toolCallId": "a"},
        {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"file_path":"1"}'},
        {"event": "content_block_end"},
        {"event": "content_block_start", "type": "tool_call", "toolName": "Read", "toolCallId": "b"},
        {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"file_path":"2"}'},
        {"event": "content_block_end"},
    ]
    finals = [e for e in _drain_events(lines) if not e.get("partial")]
    assert [(e["id"], json.loads(e["arguments"])) for e in finals] == [("a", {"file_path": "1"}),
                                                                       ("b", {"file_path": "2"})]


class Writer:
    def __init__(self):
        self.events = []

    def event(self, name, payload):
        self.events.append((name, payload))

    def data(self, payload):
        self.events.append(("data", payload))

    def done(self):
        self.events.append(("done", None))


def _parsed():
    return type("P", (), {"show_thinking": True, "model": "claude-opus-5-5", "system": "", "prompt": "x",
                          "tools": [{"type": "function", "function": {"name": "Edit", "parameters": {}}}],
                          "wants_usage": False})()


def _feed(adapter):
    """Mirror the orchestrator: strip the partial marker and feed every event."""
    for event in _drain_events(TOOL_LINES, {"Edit": PARAMS}):
        adapter.feed({k: v for k, v in event.items() if k != "partial"} | {"tool_type": "function"})


EXPECTED = {"file_path": "a.py", "new_string": "hello world", "limit": 3}


def test_anthropic_streams_input_json_deltas_incrementally():
    writer = Writer()
    adapter = adapters.AnthropicAdapter(_parsed(), writer)
    _feed(adapter)
    starts = [p for n, p in writer.events if n == "content_block_start"]
    assert len(starts) == 1 and starts[0]["content_block"]["type"] == "tool_use"
    deltas = [p["delta"]["partial_json"] for n, p in writer.events
              if n == "content_block_delta" and p["delta"]["type"] == "input_json_delta"]
    assert len(deltas) >= 3
    assert json.loads("".join(deltas)) == EXPECTED
    adapter.end("tool_calls")
    assert adapter.payload("tool_calls")["content"][0]["input"] == EXPECTED


def test_chat_streams_tool_call_argument_chunks():
    writer = Writer()
    adapter = adapters.ChatAdapter(_parsed(), writer)
    _feed(adapter)
    items = [p["choices"][0]["delta"]["tool_calls"][0] for n, p in writer.events
             if n == "data" and p["choices"] and "tool_calls" in p["choices"][0]["delta"]]
    assert items[0]["id"] == "c1" and items[0]["function"]["name"] == "Edit"
    assert sum(1 for item in items if item["function"]["arguments"]) >= 3
    assert json.loads("".join(item["function"]["arguments"] for item in items)) == EXPECTED
    call = adapter.payload("tool_calls")["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == EXPECTED


def test_responses_streams_function_call_argument_deltas():
    writer = Writer()
    adapter = adapters.ResponsesAdapter(_parsed(), writer)
    _feed(adapter)
    deltas = [p["delta"] for n, p in writer.events if n == "response.function_call_arguments.delta"]
    assert len(deltas) >= 3 and json.loads("".join(deltas)) == EXPECTED
    adapter.end("tool_calls")
    done = [p for n, p in writer.events if n == "response.function_call_arguments.done"]
    assert json.loads(done[0]["arguments"]) == EXPECTED
