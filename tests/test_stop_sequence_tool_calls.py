"""A stop sequence that follows a tool call must not turn a Chat reply into a plain stop.

The reply to a Chat request that names `stop` ended with finish_reason "stop" whatever came before the sequence, so a
reply with a call to run (Bash, written before the text that met the sequence) carried tool_calls beside "stop": a client
that decides by finish_reason took the turn for final and never ran the call (ultracode audit of the relay, C23). A reply
that ends at the sequence with a call before it is still a turn that calls a tool. The Messages API says stop_reason
"stop_sequence" for it, as the provider does, and keeps the tool_use block in the content (test_stop_sequences pins it)."""
import pytest

import adapters
from test_adapter_protocols import Writer
from test_stop_sequences import parsed, streamed_text

CALL = {"type": "tool_call", "id": "call_1", "name": "Bash", "arguments": '{"command":"ls"}'}


def text(value):
    return {"type": "text", "delta": value}


def go(dialect, events, *, stream=False, reason="tool_calls", stops=("</done>",)):
    writer = Writer() if stream else None
    adapter = adapters.for_dialect(dialect, parsed(list(stops)), writer)
    adapter.begin()
    for event in events:
        adapter.feed(event)
    return writer, adapter.end(reason)


def finish_reasons(writer):
    return [choice["finish_reason"] for kind, payload in writer.events if kind == "data" and isinstance(payload, dict)
            for choice in payload.get("choices") or [] if choice.get("finish_reason")]


@pytest.mark.parametrize("upstream", ["tool_calls", "stop"])
@pytest.mark.parametrize("stream", [False, True])
def test_chat_keeps_tool_calls_as_the_finish_reason_when_the_sequence_follows_a_call(stream, upstream):
    writer, final = go("chat", [text("Running it."), CALL, text("ok </done> trailing")], stream=stream, reason=upstream)
    choice = final["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert [call["function"]["name"] for call in choice["message"]["tool_calls"]] == ["Bash"]
    assert choice["message"]["content"] == "Running it.ok "
    if stream:
        assert finish_reasons(writer) == ["tool_calls"]
        assert "trailing" not in "".join(streamed_text(writer)) and "</done>" not in "".join(streamed_text(writer))


@pytest.mark.parametrize("stream", [False, True])
def test_chat_without_a_call_before_the_sequence_still_stops(stream):
    writer, final = go("chat", [text("done </done> more"), CALL], stream=stream)
    choice = final["choices"][0]
    # the call came after the sequence: it is dropped with everything else the model wrote past it
    assert choice["finish_reason"] == "stop" and "tool_calls" not in choice["message"]
    if stream:
        assert finish_reasons(writer) == ["stop"]


def test_chat_with_a_call_and_no_sequence_is_untouched():
    _, final = go("chat", [text("x"), CALL, text("no sequence here")])
    assert final["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.parametrize("stream", [False, True])
def test_the_messages_reply_keeps_stop_sequence_and_its_tool_use_block(stream):
    writer, final = go("anthropic", [CALL, text("a </done> b")], stream=stream)
    assert final["stop_reason"] == "stop_sequence" and final["stop_sequence"] == "</done>"
    assert [block["type"] for block in final["content"]] == ["tool_use", "text"]
    assert final["content"][1]["text"] == "a "
    if stream:
        delta = next(payload for name, payload in writer.events if name == "message_delta")["delta"]
        assert delta == {"stop_reason": "stop_sequence", "stop_sequence": "</done>"}


def test_a_cancelled_reply_stays_a_plain_stop_in_chat():
    _, final = go("chat", [CALL, text("a </done>")], reason="cancelled")
    assert final["choices"][0]["finish_reason"] == "stop"
