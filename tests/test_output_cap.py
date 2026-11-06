"""The output cap of a request is kept by the relay on a route that ignores it: Retool's Claude route took maxCompletionTokens
and went on (a cap of 300 returned 5,466 tokens and end_turn).

The reply ends where the cap is reached, as the provider ends one: stop_reason max_tokens, finish_reason length, status
incomplete. A tool call is delivered whole or not at all. What upstream really produced is still reported: its usage and its
cost."""
import json
from types import SimpleNamespace

import pytest

import adapters
import apierr
import generation
import validate
from test_adapter_protocols import Writer, parsed as plain
from test_client_contracts import CFG, _body

# 10 tokens: 35 characters of ASCII at 3.5 per token
CAP = 10
PAID = {"type": "credits", "value": 0.7, "usage": {"input_tokens": 50, "output_tokens": 5466}}


def parsed(cap=CAP, **extra):
    return SimpleNamespace(**vars(plain()), output_cap=cap, **extra)


def call(call_id="c1", name="read", arguments='{"path":"a"}', done=True):
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": arguments, "done": done}


def run(dialect, events, *, stream=False, reason="stop", cap=CAP, **fields):
    writer = Writer() if stream else None
    adapter = adapters.for_dialect(dialect, parsed(cap, **fields), writer)
    adapter.begin()
    for event in events:
        adapter.feed(event)
    return writer, adapter.end(reason)


def text(value):
    return {"type": "text", "delta": value}


def streamed_text(writer):
    parts = []
    for name, payload in writer.events:
        if name == "content_block_delta" and payload["delta"]["type"] == "text_delta":
            parts.append(payload["delta"]["text"])
        elif name == "data" and isinstance(payload, dict):
            parts += [(c.get("delta") or {}).get("content") for c in payload.get("choices") or []
                      if (c.get("delta") or {}).get("content")]
        elif name == "response.output_text.delta":
            parts.append(payload["delta"])
    return "".join(parts)


def test_text_is_cut_where_the_cap_is_reached_and_the_cost_of_what_upstream_wrote_is_kept():
    _, final = run("anthropic", [text("x" * 30), text("x" * 30), text("x" * 40), PAID])
    assert final["content"] == [{"type": "text", "text": "x" * 35}]
    assert final["stop_reason"] == "max_tokens"
    # usage and cost are what upstream really produced: the client sees 5,466 tokens against a cap of 10
    assert final["usage"]["output_tokens"] == 5466 and final["usage"]["credits_used"] == 0.7


def test_the_stream_stops_at_the_cap_and_ends_as_max_tokens():
    writer, final = run("anthropic", [text("x" * 30), text("x" * 30), text("x" * 40), PAID], stream=True)
    assert streamed_text(writer) == "x" * 35
    names = [name for name, _ in writer.events]
    delta = next(payload for name, payload in writer.events if name == "message_delta")
    assert delta["delta"]["stop_reason"] == "max_tokens" and delta["usage"]["output_tokens"] == 5466
    assert names.index("content_block_stop") < names.index("message_delta") < names.index("message_stop")
    assert final["stop_reason"] == "max_tokens"


def test_chat_ends_with_finish_reason_length():
    _, final = run("chat", [text("x" * 80), PAID])
    assert final["choices"][0]["message"]["content"] == "x" * 35
    assert final["choices"][0]["finish_reason"] == "length"
    writer, _ = run("chat", [text("x" * 80), PAID], stream=True)
    chunks = [payload for name, payload in writer.events if name == "data" and isinstance(payload, dict)]
    assert [c["choices"][0]["finish_reason"] for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]] == ["length"]
    assert streamed_text(writer) == "x" * 35


def test_responses_ends_incomplete_for_the_cap():
    writer, final = run("responses", [text("x" * 80), PAID], stream=True)
    assert final["status"] == "incomplete" and final["incomplete_details"] == {"reason": "max_output_tokens"}
    assert final["output_text"] == "x" * 35
    names = [name for name, _ in writer.events]
    assert "response.incomplete" in names and "response.completed" not in names
    assert streamed_text(writer) == "x" * 35
    # a reply that fits is complete as before
    _, whole = run("responses", [text("short")])
    assert whole["status"] == "completed" and "incomplete_details" not in whole


def test_a_reply_that_fits_is_not_touched():
    for dialect in ("anthropic", "chat", "responses"):
        _, final = run(dialect, [text("x" * 35)])
        wire = json.dumps(final)
        assert "max_tokens" not in wire and '"length"' not in wire and "incomplete" not in wire


def test_what_is_not_ascii_costs_two_characters_a_token():
    _, final = run("anthropic", [text("ж" * 50)])
    assert final["content"][0]["text"] == "ж" * 20 and final["stop_reason"] == "max_tokens"
    assert adapters.output_tokens(35, 0) == 10 and adapters.output_tokens(36, 0) == 11
    assert adapters.output_tokens(0, 20) == 10 and adapters.output_tokens(2, 19) == 11


def test_thinking_is_not_counted_against_the_cap():
    events = [{"type": "thinking", "delta": "t" * 2000}, text("hello")]
    _, final = run("anthropic", events)
    assert [block["type"] for block in final["content"]] == ["thinking", "text"]
    assert final["content"][1]["text"] == "hello" and final["stop_reason"] == "end_turn"


# --- tool calls ----------------------------------------------------------------------------------------------------------

def test_a_call_that_fits_is_delivered_whole_when_upstream_ends_it():
    writer = Writer()
    adapter = adapters.for_dialect("anthropic", parsed(), writer)
    adapter.begin()
    adapter.feed(call(arguments="", done=False))
    adapter.feed(call(arguments='{"pa', done=False))
    assert not any(name == "content_block_start" for name, _ in writer.events)  # nothing of a call before it is whole
    adapter.feed(call())
    final = adapter.end("tool_calls")
    starts = [payload["content_block"] for name, payload in writer.events if name == "content_block_start"]
    assert starts == [{"type": "tool_use", "id": "c1", "name": "read", "input": {}}]
    deltas = [payload["delta"]["partial_json"] for name, payload in writer.events if name == "content_block_delta"]
    assert deltas == ['{"path":"a"}']
    assert final["stop_reason"] == "tool_use" and final["content"][0]["input"] == {"path": "a"}


def test_a_call_that_does_not_fit_never_reaches_the_client_even_in_part():
    writer = Writer()
    adapter = adapters.for_dialect("anthropic", parsed(), writer)
    adapter.begin()
    adapter.feed(text("ok"))
    adapter.feed(call("big", arguments='{"content":"', done=False))
    adapter.feed(call("big", arguments='{"content":"' + "z" * 200, done=False))
    adapter.feed(call("big", arguments='{"content":"' + "z" * 200 + '"}'))
    adapter.feed(text("after the call"))
    adapter.feed(PAID)
    final = adapter.end("tool_calls")
    wire = json.dumps(writer.events)
    assert "tool_use" not in wire and "input_json_delta" not in wire and "zzz" not in wire
    assert "after the call" not in wire
    assert final["content"] == [{"type": "text", "text": "ok"}]
    assert final["stop_reason"] == "max_tokens" and final["usage"]["output_tokens"] == 5466


@pytest.mark.parametrize("dialect", ["chat", "responses"])
def test_the_other_dialects_never_get_half_a_call_either(dialect):
    writer, final = run(dialect, [call("big", arguments='{"content":"' + "z" * 200, done=False),
                                  call("big", arguments='{"content":"' + "z" * 200 + '"}'), PAID],
                        stream=True, reason="tool_calls")
    wire = json.dumps([writer.events, final])
    assert "zzz" not in wire and "function_call" not in wire and "tool_calls" not in wire.replace('"finish_reason"', "")
    if dialect == "chat":
        assert final["choices"][0]["finish_reason"] == "length" and "tool_calls" not in final["choices"][0]["message"]
    else:
        assert final["status"] == "incomplete" and all(item["type"] == "message" for item in final["output"])


def test_a_call_that_fits_and_the_text_that_overruns_after_it():
    _, final = run("anthropic", [call(), text("y" * 100)], reason="tool_calls")
    assert [block["type"] for block in final["content"]] == ["tool_use", "text"]
    assert final["content"][1]["text"] != "y" * 100 and len(final["content"][1]["text"]) < 35
    assert final["stop_reason"] == "max_tokens"


def test_the_calls_count_by_name_and_arguments():
    # {"path":"a"} + "read" is 16 characters: 5 tokens; two of them fit a cap of 10, a third does not
    _, final = run("anthropic", [call("a"), call("b"), call("c")], reason="tool_calls")
    assert [block["id"] for block in final["content"]] == ["a", "b"]
    assert final["stop_reason"] == "max_tokens"


def test_a_call_without_the_done_marker_counts_as_whole():
    event = {"type": "tool_call", "id": "t", "name": "read", "arguments": "{}"}
    _, final = run("anthropic", [event], reason="tool_calls")
    assert final["content"][0]["type"] == "tool_use" and final["stop_reason"] == "tool_use"


def streamed_arguments(writer):
    parts = []
    for name, payload in writer.events:
        if name == "content_block_delta" and payload["delta"]["type"] == "input_json_delta":
            parts.append(payload["delta"]["partial_json"])
        elif name == "data" and isinstance(payload, dict):
            for choice in payload.get("choices") or []:
                parts += [(item.get("function") or {}).get("arguments") or ""
                          for item in (choice.get("delta") or {}).get("tool_calls") or []]
        elif name == "response.function_call_arguments.delta":
            parts.append(payload["delta"])
    return "".join(parts)


@pytest.mark.parametrize("dialect", ["anthropic", "chat", "responses"])
def test_a_call_the_stream_ended_inside_goes_on_as_it_stands_not_swallowed_by_the_cap(dialect):
    # Upstream cut the call (its stream ended inside the arguments): that is no cap's doing, and the adapter below tells
    # the client the reply is cut. Held back until a call ends, the call would have vanished without a word.
    writer, final = run(dialect, [call(arguments='{"pa', done=False)], stream=True, reason="length")
    assert streamed_arguments(writer) == '{"pa'
    if dialect == "anthropic":
        assert final["stop_reason"] == "max_tokens"
    elif dialect == "chat":
        assert final["choices"][0]["finish_reason"] == "length"
    else:
        assert final["status"] == "incomplete"


def test_a_call_the_stream_ended_inside_is_held_to_the_cap_like_any_other():
    writer, final = run("anthropic", [text("ok"), call("big", arguments='{"content":"' + "z" * 200, done=False)],
                        stream=True, reason="length")
    wire = json.dumps(writer.events)
    assert "tool_use" not in wire and "zzz" not in wire
    assert final["stop_reason"] == "max_tokens" and final["content"] == [{"type": "text", "text": "ok"}]


def test_a_cancelled_reply_lets_an_open_call_go():
    writer, final = run("anthropic", [call(arguments='{"pa', done=False)], stream=True, reason="cancelled")
    assert "tool_use" not in json.dumps(writer.events)


# --- with the other adapters ---------------------------------------------------------------------------------------------

def test_a_stop_sequence_that_comes_first_still_ends_the_reply():
    _, final = run("anthropic", [text("abc STOP def" + " z" * 40)], cap=100, stop_sequences=["STOP"])
    assert (final["content"][0]["text"], final["stop_reason"], final["stop_sequence"]) == ("abc ", "stop_sequence", "STOP")


def test_the_cap_that_comes_first_hides_a_later_stop_sequence():
    _, final = run("anthropic", [text("abcdefghijklmnop STOP")], cap=2, stop_sequences=["STOP"])
    assert (final["content"][0]["text"], final["stop_reason"], final["stop_sequence"]) == ("abcdefg", "max_tokens", None)


def test_a_structured_reply_cut_by_the_cap_is_an_error_not_a_broken_document():
    spec = generation.structured_output({"response_format": {"type": "json_object"}}, "chat")
    with pytest.raises(apierr.ApiError) as error:
        run("chat", [text('{"a": "' + "v" * 100 + '"}')], structured_output=spec)
    assert error.value.status == 502 and "cut short (length)" in error.value.message


def test_a_cancelled_reply_stays_cancelled():
    _, final = run("anthropic", [text("x" * 80)], reason="cancelled")
    assert final["stop_reason"] == "end_turn"


# --- which requests are kept to a cap -------------------------------------------------------------------------------------

@pytest.mark.parametrize("model, cap, expected", [
    ("claude-opus-5-5", 300, 300), ("claude-sonnet-5-5", 1, 1), ("claude-haiku-4-5-20251001", 64, 64),
    ("claude-opus-5-5", 16383, 16383), ("claude-opus-5-5", 16384, None), ("claude-opus-5-5", 128000, None),
    ("gpt-6-sol", 300, None), ("zai-org/GLM-5.3", 300, None)])
def test_only_a_route_that_ignores_the_cap_and_a_cap_that_can_bind_are_kept(model, cap, expected):
    body = _body("anthropic", model=model, max_tokens=cap)
    assert validate.parse_anthropic(body, dict(CFG, strict_models=True)).output_cap == expected


def test_a_request_without_a_cap_has_none_to_keep():
    assert validate.parse_chat(_body("chat"), CFG).output_cap is None
    assert validate.parse_chat(_body("chat", max_tokens=200), CFG).output_cap == 200
    assert validate.parse_chat(_body("chat", max_completion_tokens=200), CFG).output_cap == 200
    assert validate.parse_responses(dict(_body("responses", model="claude-opus-5-5"), max_output_tokens=200), CFG).output_cap == 200


# --- over HTTP -----------------------------------------------------------------------------------------------------------

def script(*deltas):
    return [*({"type": "text", "delta": delta} for delta in deltas), PAID, {"type": "end", "reason": "stop"}]


def test_a_messages_request_gets_a_reply_cut_at_its_cap(relay):
    relay.fake.script = script("a" * 400, "b" * 400)
    body = _body("anthropic", max_tokens=300 // 15, stream=False)
    with relay.request("/v1/messages", body) as response:
        answer = json.loads(response.read())
    assert response.status == 200
    assert answer["content"] == [{"type": "text", "text": "a" * 70}] and answer["stop_reason"] == "max_tokens"
    assert answer["usage"]["output_tokens"] == 5466


def test_a_streamed_messages_reply_stops_at_its_cap(relay):
    relay.fake.script = script("a" * 40, "b" * 40, "c" * 40)
    with relay.request("/v1/messages", _body("anthropic", max_tokens=20, stream=True)) as response:
        raw = response.read().decode("utf-8")
    pieces = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: {")]
    sent = "".join(p["delta"]["text"] for p in pieces if p["type"] == "content_block_delta")
    assert sent == "a" * 40 + "b" * 30 and "c" not in sent  # 20 tokens are 70 characters of ASCII
    assert '"stop_reason": "max_tokens"' in raw


def test_a_chat_reply_cut_at_its_cap_says_length(relay):
    relay.fake.script = script("a" * 400)
    status, answer = relay.json("/v1/chat/completions", _body("chat", max_tokens=10))
    assert status == 200 and answer["choices"][0]["finish_reason"] == "length"
    assert answer["choices"][0]["message"]["content"] == "a" * 35


def test_a_gpt_route_that_keeps_the_cap_itself_is_not_cut_again(relay):
    relay.fake.script = script("a" * 400)
    status, answer = relay.json("/v1/chat/completions", _body("chat", model="gpt-6-sol", max_tokens=10))
    assert status == 200 and answer["choices"][0]["message"]["content"] == "a" * 400
