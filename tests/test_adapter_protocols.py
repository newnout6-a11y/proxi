"""Client-visible protocol contracts for reasoning and tool streams."""
import json
from types import SimpleNamespace

import pytest

import adapters
import apierr


class Writer:
    def __init__(self):
        self.events = []

    def data(self, payload):
        self.events.append(("data", payload))

    def event(self, name, payload):
        self.events.append((name, payload))

    def done(self):
        self.events.append(("done", None))


def parsed(show=True, tools=None):
    return SimpleNamespace(model="claude-opus-5-5", system="", show_thinking=show,
                           wants_usage=False, tools=tools or [])


@pytest.mark.parametrize("dialect", ["chat", "responses", "anthropic"])
def test_disabled_reasoning_is_absent_from_stream_and_final_payload(dialect):
    writer = Writer()
    adapter = adapters.for_dialect(dialect, parsed(show=False), writer)
    adapter.begin()
    adapter.feed({"type": "thinking", "delta": "private-provider-summary"})
    adapter.feed({"type": "thinking_signature", "signature": "opaque-provider-signature"})
    adapter.feed({"type": "text", "delta": "answer"})
    final = adapter.end("stop")
    wire = json.dumps([writer.events, final])
    assert "private-provider-summary" not in wire
    assert "opaque-provider-signature" not in wire


def _responses_stream(events):
    writer = Writer()
    adapter = adapters.ResponsesAdapter(parsed(), writer)
    adapter.begin()
    for event in events:
        adapter.feed(event)
    return writer, adapter.end("tool_calls" if adapter.tool_calls else "stop")


def test_responses_reasoning_and_message_items_have_complete_distinct_lifecycles():
    writer, final = _responses_stream([
        {"type": "thinking", "delta": "provider-summary"},
        {"type": "text", "delta": "answer"},
    ])
    opened = [payload for kind, payload in writer.events if kind == "response.output_item.added"]
    closed = [payload for kind, payload in writer.events if kind == "response.output_item.done"]
    assert [item["item"]["type"] for item in opened] == ["reasoning", "message"]
    assert [item["output_index"] for item in opened] == [0, 1]
    assert [item["item"] for item in closed] == final["output"]
    for kind, payload in writer.events:
        if payload and "item_id" in payload:
            assert payload["item_id"] == opened[payload["output_index"]]["item"]["id"]
    kinds = [kind for kind, _ in writer.events]
    assert kinds.index("response.reasoning_summary_part.added") < kinds.index("response.reasoning_summary_text.delta")
    assert kinds.index("response.reasoning_summary_text.done") < kinds.index("response.reasoning_summary_part.done")
    assert kinds.index("response.output_text.done") < kinds.index("response.content_part.done")
    assert kinds[-2:] == ["response.completed", "done"]
    sequences = [p["sequence_number"] for _, p in writer.events if p and "sequence_number" in p]
    assert sequences == list(range(1, len(sequences) + 1))


def test_responses_tool_only_turn_has_no_phantom_assistant_message():
    writer, final = _responses_stream([
        {"type": "tool_call", "id": "call_1", "name": "read_file", "arguments": '{"path":"app.py"}'},
    ])
    assert [item["type"] for item in final["output"]] == ["function_call"]
    assert not any(kind.startswith("response.output_text") or kind.startswith("response.content_part")
                   for kind, _ in writer.events)
    opened = [p for kind, p in writer.events if kind == "response.output_item.added"]
    assert opened[0]["output_index"] == 0


def test_responses_payload_and_stream_share_observed_output_order():
    writer, final = _responses_stream([
        {"type": "text", "delta": "Checking a file"},
        {"type": "thinking", "delta": "provider-summary"},
        {"type": "tool_call", "id": "call_1", "name": "read_file", "arguments": "{}"},
    ])
    assert [item["type"] for item in final["output"]] == ["message", "reasoning", "function_call"]
    assert [p["item"]["id"] for kind, p in writer.events if kind == "response.output_item.added"] == [
        item["id"] for item in final["output"]]


def test_responses_timeout_closes_items_without_emitting_completed():
    writer = Writer()
    adapter = adapters.ResponsesAdapter(parsed(), writer)
    adapter.begin()
    adapter.feed({"type": "text", "delta": "partial"})
    final = adapter.end("timeout")
    assert final["status"] == "incomplete"
    assert final["output"][0]["status"] == "incomplete"
    assert "response.incomplete" in [kind for kind, _ in writer.events]
    assert "response.completed" not in [kind for kind, _ in writer.events]


def test_chat_interleaved_tool_updates_keep_the_original_call_index():
    writer = Writer()
    adapter = adapters.ChatAdapter(parsed(), writer)
    adapter.feed({"type": "tool_call", "id": "a", "name": "read", "arguments": '{"path":'})
    adapter.feed({"type": "tool_call", "id": "b", "name": "read", "arguments": "{}"})
    adapter.feed({"type": "tool_call", "id": "a", "name": "read", "arguments": '{"path":"x"}'})
    chunks = [p["choices"][0]["delta"]["tool_calls"][0] for kind, p in writer.events if kind == "data"]
    assert [chunk["index"] for chunk in chunks] == [0, 1, 0]
    assert chunks[-1]["function"]["arguments"] == '"x"}'


def test_chat_keeps_text_before_a_native_tool_call():
    adapter = adapters.ChatAdapter(parsed())
    adapter.feed({"type": "text", "delta": "Reading the file"})
    adapter.feed({"type": "tool_call", "id": "a", "name": "read", "arguments": "{}"})
    assert adapter.payload("tool_calls")["choices"][0]["message"]["content"] == "Reading the file"


def test_anthropic_block_order_and_payload_match_the_stream():
    writer = Writer()
    adapter = adapters.AnthropicAdapter(parsed(), writer)
    adapter.begin()
    for event in [
        {"type": "thinking", "delta": "provider-summary"},
        {"type": "thinking_signature", "signature": "opaque-signature"},
        {"type": "text", "delta": "Checking"},
        {"type": "tool_call", "id": "a", "name": "read", "arguments": '{"path":"x"}'},
    ]:
        adapter.feed(event)
    final = adapter.end("tool_calls")
    starts = [p for kind, p in writer.events if kind == "content_block_start"]
    assert [p["content_block"]["type"] for p in starts] == ["thinking", "text", "tool_use"]
    assert [block["type"] for block in final["content"]] == ["thinking", "text", "tool_use"]
    assert final["content"][0]["signature"] == "opaque-signature"
    assert final["content"][0]["thinking"] == "provider-summary"
    signature = next(i for i, (kind, p) in enumerate(writer.events)
                     if kind == "content_block_delta" and p["delta"]["type"] == "signature_delta")
    stop = next(i for i, (kind, p) in enumerate(writer.events)
                if kind == "content_block_stop" and p["index"] == 0)
    assert signature < stop
    assert sorted(p["index"] for kind, p in writer.events if kind == "content_block_stop") == [0, 1, 2]


def test_anthropic_does_not_invent_a_thinking_signature_or_empty_text():
    adapter = adapters.AnthropicAdapter(parsed())
    adapter.feed({"type": "thinking", "delta": "provider-summary"})
    adapter.feed({"type": "tool_call", "id": "a", "name": "read", "arguments": "{}"})
    final = adapter.payload("tool_calls")
    assert [block["type"] for block in final["content"]] == ["thinking", "tool_use"]
    assert final["content"][0]["signature"] == ""


def test_anthropic_interleaved_tool_arguments_target_the_right_block():
    writer = Writer()
    adapter = adapters.AnthropicAdapter(parsed(), writer)
    adapter.feed({"type": "tool_call", "id": "a", "name": "read", "arguments": '{"path":'})
    adapter.feed({"type": "tool_call", "id": "b", "name": "read", "arguments": '{"path":"b"}'})
    adapter.feed({"type": "tool_call", "id": "a", "name": "read", "arguments": '{"path":"a"}'})
    final = adapter.end("tool_calls")
    assert [p["index"] for kind, p in writer.events if kind == "content_block_delta"] == [0, 1, 0]
    assert [block["input"] for block in final["content"]] == [{"path": "a"}, {"path": "b"}]
    assert [p["index"] for kind, p in writer.events if kind == "content_block_stop"] == [0, 1]


@pytest.mark.parametrize("explicit", [True, False])
def test_responses_custom_tool_call_preserves_raw_patch_input(explicit):
    tools = [{"type": "function", "function": {"name": "apply_patch", "x-client-type": "custom"}}]
    writer = Writer()
    adapter = adapters.ResponsesAdapter(parsed(tools=[] if explicit else tools), writer)
    adapter.begin()
    patch = "*** Begin Patch\n*** Add File: x.py\n+x=1\n*** End Patch"
    event = {"type": "tool_call", "id": "patch-1", "name": "apply_patch",
             "arguments": json.dumps({"input": patch})}
    if explicit:
        event["tool_type"] = "custom"
    adapter.feed(event)
    final = adapter.end("tool_calls")
    assert len(final["output"]) == 1
    call = final["output"][0]
    assert call["type"] == "custom_tool_call"
    assert call["input"] == patch
    assert "arguments" not in call
    deltas = [p["delta"] for kind, p in writer.events if kind == "response.custom_tool_call_input.delta"]
    assert "".join(deltas) == patch
    done = next(p for kind, p in writer.events if kind == "response.custom_tool_call_input.done")
    assert done["input"] == patch


def test_responses_custom_tool_buffers_partial_json_envelopes():
    writer = Writer()
    adapter = adapters.ResponsesAdapter(parsed(), writer)
    first = {"type": "tool_call", "id": "p", "name": "apply_patch", "tool_type": "custom",
             "arguments": '{"input":"*** Begin'}
    adapter.feed(first)
    assert not any(kind == "response.custom_tool_call_input.delta" for kind, _ in writer.events)
    adapter.feed({**first, "arguments": json.dumps({"input": "*** Begin Patch\n*** End Patch"})})
    final = adapter.end("tool_calls")
    assert final["output"][0]["input"] == "*** Begin Patch\n*** End Patch"


def test_responses_custom_tool_keeps_raw_non_json_grammar_input():
    adapter = adapters.ResponsesAdapter(parsed())
    adapter.feed({"type": "tool_call", "id": "p", "name": "custom_edit", "tool_type": "custom",
                  "arguments": "{ edit arbitrary grammar }"})
    final = adapter.end("tool_calls")
    assert final["output"][0]["input"] == "{ edit arbitrary grammar }"


def test_responses_does_not_replace_truncated_custom_input_with_an_empty_patch():
    adapter = adapters.ResponsesAdapter(parsed())
    adapter.feed({"type": "tool_call", "id": "p", "name": "apply_patch", "tool_type": "custom",
                  "arguments": '{"input":"truncated'})
    with pytest.raises(apierr.ApiError) as error:
        adapter.end("tool_calls")
    assert error.value.status == 502
