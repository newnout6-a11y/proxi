"""A number past the range of a double is never written as Infinity: that is no JSON.

A tool call whose input holds 1e400 is valid JSON, and Python reads it as inf and writes it back as Infinity. The kept
reply held the inf, so the next request upstream carried "Infinity" and the endpoint refused the whole body, and the
non-streaming Anthropic reply carried it to the client in tool_use.input (ultracode audit of the relay, G3.2; offline
reproduction: upstream body 2 is invalid JSON). The number is null from the moment the relay reads it, which is what a
JavaScript client makes of it as well (it reads Infinity and writes null when it echoes it), so that client's echo is
still the reply the relay kept. A client history that holds an inf of its own (a Python client echoing 1e400) goes upstream
as null too."""
import asyncio
import copy
import json

import pytest

import adapters
import direct
import native_history
import validate
from test_native_context_reuse import FIRST, PARSE_CFG, run
from test_native_history import TOOLS, close, harness
from test_review_fixes_pr19 import END, THINKING, signature, text

READ_FILE = [{"name": "read_file", "description": "", "input_schema": TOOLS[0]["function"]["parameters"]}]


def strict(raw):
    def refuse(constant):
        raise ValueError("not JSON: " + constant)
    return json.loads(raw, parse_constant=refuse)


@pytest.mark.parametrize("raw,expected", [
    pytest.param('{"x": 1e400}', {"x": None}, id="1e400"), pytest.param('{"x": -1e400}', {"x": None}, id="-1e400"),
    pytest.param('{"x": 1E+999, "y": [2e999]}', {"x": None, "y": [None]}, id="nested"),
    pytest.param('{"x": NaN}', {"x": None}, id="NaN"), pytest.param('{"x": Infinity}', {"x": None}, id="Infinity"),
    pytest.param('{"x": -Infinity}', {"x": None}, id="-Infinity"),
    pytest.param('{"a": 1.5, "b": 2, "c": [0.1, true, null, "s"], "d": 1e5, "e": -0.0}',
                 {"a": 1.5, "b": 2, "c": [0.1, True, None, "s"], "d": 1e5, "e": -0.0}, id="ordinary values"),
    pytest.param('{"big": ' + "9" * 400 + '}', {"big": int("9" * 400)}, id="a 400-digit integer")])
def test_a_number_the_double_cannot_hold_is_null_and_every_other_value_is_as_json_reads_it(raw, expected):
    assert native_history.loads_finite(raw) == expected


def call_events(arguments):
    return [{"event": "content_block_start", "type": "tool_call", "toolCallId": "c1", "toolName": "read_file"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": arguments},
            {"event": "content_block_end", "type": "tool_call", "toolCallId": "c1"}]


def test_the_kept_reply_and_its_echo_hold_null_for_it():
    capture = native_history.ReplyCapture()
    for event in call_events('{"path": "a", "limit": 1e400}'):
        capture.feed(event)
    blocks = capture.finish()
    assert blocks[0]["toolInput"] == {"path": "a", "limit": None}
    echo = native_history.echo(blocks, {}, direct.coerce_arguments)
    assert echo["content"][0]["toolInput"] == {"path": "a", "limit": None}
    json.dumps([blocks, echo], allow_nan=False)


def test_the_anthropic_reply_is_json_and_the_stream_carries_the_number_as_the_model_wrote_it():
    from test_adapter_protocols import Writer, parsed
    arguments = '{"path": "a", "limit": 1e400}'
    adapter = adapters.AnthropicAdapter(parsed(), None)
    adapter.feed({"type": "tool_call", "id": "toolu_1", "name": "read_file", "arguments": arguments})
    payload = adapter.payload("tool_calls")
    assert payload["content"][0]["input"] == {"path": "a", "limit": None}
    strict(json.dumps(payload, ensure_ascii=False))
    writer = Writer()
    streaming = adapters.AnthropicAdapter(parsed(), writer)
    streaming.begin()
    streaming.feed({"type": "tool_call", "id": "toolu_1", "name": "read_file", "arguments": arguments})
    streaming.end("tool_calls")
    assert "".join(p["delta"]["partial_json"] for kind, p in writer.events
                   if kind == "content_block_delta") == arguments
    strict("".join(p["delta"]["partial_json"] for kind, p in writer.events if kind == "content_block_delta"))


def test_the_wire_writes_null_for_a_number_that_is_not_one_and_everything_else_as_it_did():
    body = {"messages": [{"a": [float("inf"), float("-inf"), float("nan"), 1.5, 2, "é"]}], "k": {"x": float("inf")}}
    assert strict(direct._wire_json(body)) == {"messages": [{"a": [None, None, None, 1.5, 2, "é"]}], "k": {"x": None}}
    plain = {"messages": [{"role": "user", "content": [{"type": "text", "text": "né 日本"}]}], "n": [1, 2.5, True]}
    assert direct._wire_json(plain) == json.dumps(plain, ensure_ascii=True).encode("utf-8")


def second_request(input_text):
    """The client's next request as JSON text: its echo of the call holds `input_text` as the input."""
    blocks = [{"type": "thinking", "thinking": "", "signature": "sig-inf"},
              {"type": "tool_use", "id": "c1", "name": "read_file", "input": "__INPUT__"}]
    messages = copy.deepcopy(FIRST) + [
        {"role": "assistant", "content": blocks},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}]}]
    document = json.dumps({"model": "claude-opus-5-5", "system": "CLIENT_SYSTEM", "max_tokens": 64,
                           "tools": READ_FILE, "messages": messages})
    parsed = validate.parse_anthropic(validate.load_json(document.replace('"__INPUT__"', input_text).encode()),
                                      PARSE_CFG)
    parsed.conversation_key = "header:client-a"
    return parsed


def exchange(tmp_path, monkeypatch, echo):
    reply = [*THINKING, signature("sig-inf"), {"event": "content_block_end", "type": "thinking"},
             *call_events('{"path": "a", "limit": 1e400}'), *END]
    pool, runner, sent = harness(tmp_path, monkeypatch, [reply, [text("done"), *END]])
    raws, opener = [], direct.urllib.request.urlopen

    def capture(request, **kwargs):
        raws.append(request.data)
        return opener(request, **kwargs)

    monkeypatch.setattr(direct.urllib.request, "urlopen", capture)
    seen = {}

    async def scenario():
        try:
            first = validate.parse_anthropic({"model": "claude-opus-5-5", "system": "CLIENT_SYSTEM", "max_tokens": 64,
                                              "tools": READ_FILE, "messages": copy.deepcopy(FIRST)}, PARSE_CFG)
            first.conversation_key = "header:client-a"
            events = await run(runner, first)
            seen["streamed"] = [e["arguments"] for e in events if e["type"] == "tool_call" and e.get("done")]
            adapter = adapters.AnthropicAdapter(first, None)
            for event in events:
                adapter.feed(event)
            seen["payload"] = json.dumps(adapter.payload("tool_calls"), ensure_ascii=False)
            await run(runner, second_request(echo))
        finally:
            await close(pool)

    asyncio.run(scenario())
    return raws, seen


def test_a_javascript_client_that_echoes_null_goes_on_from_the_signed_reply(tmp_path, monkeypatch):
    raws, seen = exchange(tmp_path, monkeypatch, '{"path": "a", "limit": null}')
    assert seen["streamed"] == ['{"path": "a", "limit": 1e400}']
    tool_use = next(b for b in strict(seen["payload"])["content"] if b["type"] == "tool_use")
    assert tool_use["input"] == {"path": "a", "limit": None}
    assert len(raws) == 2 and all(strict(raw) for raw in raws)
    second = strict(raws[1])
    assert "sig-inf" in raws[1].decode()  # resumed natively
    replayed = next(m for m in second["messages"] if m["role"] == "assistant")
    assert next(b for b in replayed["content"] if b["type"] == "tool_call")["toolInput"] == {"path": "a", "limit": None}


def test_a_client_that_echoes_the_number_itself_sends_valid_json_upstream_as_well(tmp_path, monkeypatch):
    raws, _ = exchange(tmp_path, monkeypatch, '{"path": "a", "limit": 1e400}')
    assert len(raws) == 2 and all(strict(raw) for raw in raws)
    replayed = next(m for m in strict(raws[1])["messages"] if m["role"] == "assistant")
    assert next(b for b in replayed["content"] if b["type"] == "tool_call")["toolInput"] == {"path": "a", "limit": None}


def test_a_client_that_echoes_the_number_itself_goes_on_from_the_signed_reply_too(tmp_path, monkeypatch):
    # Codex echoes the arguments string as the model wrote it; an Anthropic client echoes the number. Both used to go cold.
    raws, _ = exchange(tmp_path, monkeypatch, '{"path": "a", "limit": 1e400}')
    assert len(raws) == 2 and "sig-inf" in raws[1].decode()


def test_an_echo_is_read_as_the_kept_reply_was():
    assert native_history.finite_values({"limit": float("inf"), "n": [float("nan"), 2], "big": 10 ** 400}) == {
        "limit": None, "n": [None, 2], "big": 10 ** 400}  # integers stay exact: JSON can carry them


def test_a_client_that_echoes_a_huge_integer_as_the_model_wrote_it_sends_its_next_turn(tmp_path, monkeypatch):
    # an integer past a double: kept exact on both sides; the length of the body as JavaScript counts it raised OverflowError
    reply = [*THINKING, signature("sig-big"), {"event": "content_block_end", "type": "thinking"},
             *call_events('{"path": "a", "limit": %s}' % ("9" * 400)), *END]
    pool, runner, sent = harness(tmp_path, monkeypatch, [reply, [text("done"), *END]])
    raws, opener = [], direct.urllib.request.urlopen

    def capture(request, **kwargs):
        raws.append(request.data)
        return opener(request, **kwargs)

    monkeypatch.setattr(direct.urllib.request, "urlopen", capture)

    async def scenario():
        try:
            first = validate.parse_anthropic({"model": "claude-opus-5-5", "system": "CLIENT_SYSTEM", "max_tokens": 64,
                                              "tools": READ_FILE, "messages": copy.deepcopy(FIRST)}, PARSE_CFG)
            first.conversation_key = "header:client-a"
            await run(runner, first)
            await run(runner, second_request('{"path": "a", "limit": %s}' % ("9" * 400)))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(raws) == 2 and "sig-big" in raws[1].decode()


def test_the_javascript_length_of_an_integer_past_a_double_does_not_fail():
    assert direct._js_delta({"big": 10 ** 400}) == len("null") - len(str(10 ** 400))
