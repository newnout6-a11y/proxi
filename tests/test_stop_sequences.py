"""Stop sequences are emulated: the reply ends at the first one, as Anthropic and OpenAI end it.

Retool has none, so the relay refused them with 422. The safety classifier of Claude Desktop sends stop_sequences on a
non-streaming Messages request, and every check it made failed: preview_start was blocked as "temporarily unavailable"."""
import json
from types import SimpleNamespace

import pytest

import adapters
import apierr
import generation
import validate
from test_adapter_protocols import Writer, parsed as plain
from test_client_contracts import CFG, _body


def parsed(stops, **extra):
    return SimpleNamespace(**vars(plain()), stop_sequences=stops, **extra)


def run(dialect, stops, deltas, *, stream=False, reason="stop", extra=(), **fields):
    writer = Writer() if stream else None
    adapter = adapters.for_dialect(dialect, parsed(stops, **fields), writer)
    adapter.begin()
    for delta in deltas:
        adapter.feed({"type": "text", "delta": delta})
    for event in extra:
        adapter.feed(event)
    return writer, adapter.end(reason)


def streamed_text(writer):
    parts = []
    for name, payload in writer.events:
        if name == "content_block_delta" and payload["delta"]["type"] == "text_delta":
            parts.append(payload["delta"]["text"])
        elif name == "data" and isinstance(payload, dict):
            for choice in payload.get("choices") or []:
                if (choice.get("delta") or {}).get("content"):
                    parts.append(choice["delta"]["content"])
    return parts


def test_the_classifier_reply_ends_at_its_stop_sequence():
    _, final = run("anthropic", ["</block>"], ["<block>no", "</block>\n<reason>it only reads files</reason>"])
    assert final["content"] == [{"type": "text", "text": "<block>no"}]
    assert (final["stop_reason"], final["stop_sequence"]) == ("stop_sequence", "</block>")


def test_a_stop_sequence_split_across_deltas_never_reaches_the_stream():
    writer, final = run("anthropic", ["</block>"], ["abc</bl", "ock>tail"], stream=True)
    assert streamed_text(writer) == ["abc"]
    delta = next(payload for name, payload in writer.events if name == "message_delta")["delta"]
    assert delta == {"stop_reason": "stop_sequence", "stop_sequence": "</block>"}
    assert final["content"] == [{"type": "text", "text": "abc"}]


def test_held_text_that_does_not_become_a_stop_sequence_is_released_in_order():
    writer, final = run("anthropic", ["</block>"], ["abc</bl", "ah", " done"], stream=True)
    parts = streamed_text(writer)
    assert parts[0] == "abc" and "".join(parts) == "abc</blah done"
    assert (final["stop_reason"], final["stop_sequence"]) == ("end_turn", None)
    # a held end the reply finishes on is released at the end
    _, final = run("anthropic", ["</block>"], ["abc</bl"])
    assert final["content"][0]["text"] == "abc</bl" and final["stop_sequence"] is None


def test_the_stop_sequence_complete_first_wins():
    _, final = run("anthropic", ["xyz", "y"], ["xyz"])
    assert (final["content"][0]["text"], final["stop_sequence"]) == ("x", "y")
    _, final = run("anthropic", ["abcd", "bc"], ["ab", "cd"])
    assert (final["content"][0]["text"], final["stop_sequence"]) == ("a", "bc")


def test_events_after_the_stop_are_dropped_but_their_cost_counts():
    extra = [{"type": "tool_call", "id": "a", "name": "read", "arguments": "{}"},
             {"type": "credits", "value": 0.5, "usage": {"input_tokens": 10, "output_tokens": 20}}]
    _, final = run("anthropic", ["STOP"], ["ok STOP more"], reason="tool_calls", extra=extra)
    assert final["content"] == [{"type": "text", "text": "ok "}]
    assert final["stop_reason"] == "stop_sequence" and final["usage"]["credits_used"] == 0.5


def test_thinking_and_tool_calls_before_the_stop_pass_through():
    extra = [{"type": "text", "delta": "a STOP b"}]
    writer = Writer()
    adapter = adapters.for_dialect("anthropic", parsed(["STOP"]), writer)
    adapter.begin()
    adapter.feed({"type": "thinking", "delta": "plan"})
    adapter.feed({"type": "text", "delta": "x S"})
    adapter.feed({"type": "tool_call", "id": "t", "name": "read", "arguments": "{}"})  # ends the text: "S" is released
    for event in extra:
        adapter.feed(event)
    final = adapter.end("stop")
    assert [block["type"] for block in final["content"]] == ["thinking", "text", "tool_use", "text"]
    assert final["content"][1]["text"] == "x S" and final["content"][3]["text"] == "a "
    assert final["stop_reason"] == "stop_sequence"


def test_chat_ends_with_finish_reason_stop_and_streams_no_stop_text():
    _, final = run("chat", ["</block>"], ["<block>no</block> because"])
    assert final["choices"][0]["message"]["content"] == "<block>no"
    assert final["choices"][0]["finish_reason"] == "stop"
    writer, _ = run("chat", ["</block>"], ["<block>no</bl", "ock> because"], stream=True)
    assert "".join(streamed_text(writer)) == "<block>no"
    assert "</block>" not in json.dumps(writer.events)


def test_structured_output_is_checked_after_the_cut():
    spec = generation.structured_output({"response_format": {"type": "json_object"}}, "chat")
    _, final = run("chat", ["END"], ['{"a": 1}', "END and then prose"], structured_output=spec)
    assert json.loads(final["choices"][0]["message"]["content"]) == {"a": 1}


def test_a_cancelled_reply_stays_cancelled():
    _, final = run("anthropic", ["STOP"], ["a STOP"], reason="cancelled")
    assert final["stop_sequence"] == "STOP"


def test_the_stop_sequences_a_request_names():
    assert validate.parse_chat(_body("chat", stop="END"), CFG).stop_sequences == ["END"]
    assert validate.parse_chat(_body("chat", stop=["A", "", "A", "B"]), CFG).stop_sequences == ["A", "B"]
    assert validate.parse_chat(_body("chat", stop=""), CFG).stop_sequences == []
    assert validate.parse_chat(_body("chat"), CFG).stop_sequences == []
    assert validate.parse_anthropic(_body("anthropic", stop_sequences=["</block>"]), CFG).stop_sequences == ["</block>"]
    for bad in ({"stop": ["x"] * 17}, {"stop_sequences": ["x" * 1001]}, {"stop_sequences": "END"}, {"stop": [None]}):
        dialect = "anthropic" if "stop_sequences" in bad else "chat"
        with pytest.raises(apierr.ApiError) as error:
            getattr(validate, "parse_" + dialect)(_body(dialect, **bad), CFG)
        assert error.value.status == 422 and error.value.param in bad


def test_the_relay_answers_a_classifier_request_instead_of_refusing_it(relay):
    relay.fake.script = [{"type": "text", "delta": "<block>no</block>\n<reason>reads files</reason>"},
                         {"type": "end", "reason": "stop"}]
    body = _body("anthropic", stop_sequences=["</block>"], max_tokens=64, stream=False)
    with relay.request("/v1/messages", body) as response:
        answer = json.loads(response.read())
    assert response.status == 200
    assert answer["content"] == [{"type": "text", "text": "<block>no"}]
    assert (answer["stop_reason"], answer["stop_sequence"]) == ("stop_sequence", "</block>")


def test_a_streamed_chat_reply_ends_at_its_stop_sequence(relay):
    relay.fake.script = [{"type": "text", "delta": "one two"}, {"type": "text", "delta": " STOP three"},
                         {"type": "end", "reason": "stop"}]
    with relay.request("/v1/chat/completions", _body("chat", stop=["STOP"], stream=True)) as response:
        raw = response.read().decode("utf-8")
    assert response.status == 200 and "three" not in raw and "STOP" not in raw
    chunks = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: {")]
    text = "".join((choice.get("delta") or {}).get("content") or "" for chunk in chunks for choice in chunk["choices"])
    assert text == "one two "
    assert [choice["finish_reason"] for chunk in chunks for choice in chunk["choices"] if choice.get("finish_reason")] \
        == ["stop"]
