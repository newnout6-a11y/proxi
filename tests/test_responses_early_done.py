"""A call of a Responses reply is closed (response.output_item.done) as soon as upstream ends it, not when the reply ends.

codex-rs starts a tool the moment the done event of its item arrives (stream_events_utils.rs: handle_output_item_done
dispatches at once) and files the item into its history in the order the done events arrive. The adapter held every done
event to the end of the reply, so the first call of a batch waited for the last to be written: 0.45 to 2.8 s for five to
eight short calls, 11 to 21 s for script-writing calls (Opus, 5 calls: 20.9 s; a4 F13, 2026-11-06). The Anthropic adapter
already stops each tool_use block when upstream ends the call (d8f8763); this is the same for Responses."""
import http.client
import io
import json
import time

import pytest

import adapters
from prompt_support import KEY, PATHS, client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_adapter_protocols import Writer, parsed


def adapter_and_writer(tools=()):
    writer = Writer()
    adapter = adapters.ResponsesAdapter(parsed(tools=list(tools)), writer)
    adapter.begin()
    return adapter, writer


def call(adapter, call_id, arguments, *, done, name="read", tool_type=None):
    adapter.feed({"type": "tool_call", "id": call_id, "name": name, "arguments": arguments, "done": done,
                  **({"tool_type": tool_type} if tool_type else {})})


def names(writer):
    return [kind for kind, _ in writer.events]


def done_indexes(writer):
    return [p["output_index"] for kind, p in writer.events if kind == "response.output_item.done"]


def position(writer, kind, **fields):
    return next(i for i, (name, payload) in enumerate(writer.events)
                if name == kind and all(payload.get(key) == value for key, value in fields.items()))


def test_the_first_call_is_closed_before_the_next_one_is_written():
    adapter, writer = adapter_and_writer()
    call(adapter, "a", "", done=False)
    call(adapter, "a", '{"path":"a"}', done=False)
    assert done_indexes(writer) == []  # still being written
    call(adapter, "a", '{"path":"a"}', done=True)
    assert done_indexes(writer) == [0]  # closed at once
    assert "response.function_call_arguments.done" in names(writer)
    call(adapter, "b", '{"path":"b"}', done=False)
    call(adapter, "b", '{"path":"b"}', done=True)
    assert done_indexes(writer) == [0, 1]
    assert position(writer, "response.output_item.done", output_index=0) < position(
        writer, "response.output_item.added", output_index=1)
    final = adapter.end("tool_calls")
    assert done_indexes(writer) == [0, 1] and [item["status"] for item in final["output"]] == ["completed"] * 2
    assert names(writer)[-2:] == ["response.completed", "done"]


def test_the_items_before_a_call_are_closed_first_so_that_the_history_keeps_the_order_of_the_output():
    adapter, writer = adapter_and_writer()
    adapter.feed({"type": "thinking", "delta": "I will look"})
    adapter.feed({"type": "text", "delta": "Checking two files"})
    call(adapter, "a", '{"path":"a"}', done=True)
    # reasoning, message, then the call: codex-rs files items in the order of their done events
    assert done_indexes(writer) == [0, 1, 2]
    items = [p["item"] for kind, p in writer.events if kind == "response.output_item.done"]
    assert [item["type"] for item in items] == ["reasoning", "message", "function_call"]
    assert items[0]["summary"][0]["text"] == "I will look" and items[1]["content"][0]["text"] == "Checking two files"
    call(adapter, "b", '{"path":"b"}', done=True)
    final = adapter.end("tool_calls")
    assert done_indexes(writer) == [0, 1, 2, 3]
    assert [p["item"] for kind, p in writer.events if kind == "response.output_item.done"] == final["output"]


def test_nothing_is_said_about_an_item_after_its_done_event_and_the_numbers_run_on():
    adapter, writer = adapter_and_writer()
    adapter.feed({"type": "text", "delta": "one"})
    call(adapter, "a", '{"path":"a"}', done=True)
    adapter.feed({"type": "text", "delta": "two"})
    adapter.feed({"type": "thinking", "delta": "late thought"})
    call(adapter, "b", '{"path":"b"}', done=True)
    adapter.end("tool_calls")
    closed = {}
    for kind, payload in writer.events:
        index = payload.get("output_index") if payload else None
        if index is None:
            continue
        assert index not in closed, "%s for item %s after its done event" % (kind, index)
        if kind == "response.output_item.done":
            closed[index] = True
    sequences = [p["sequence_number"] for _, p in writer.events if p and "sequence_number" in p]
    assert sequences == list(range(1, len(sequences) + 1))
    added = [p["output_index"] for kind, p in writer.events if kind == "response.output_item.added"]
    assert added == sorted(added) and len(added) == len(set(added))


def test_text_after_a_call_is_a_message_of_its_own_and_the_texts_stay_apart():
    adapter, writer = adapter_and_writer()
    adapter.feed({"type": "text", "delta": "before "})
    adapter.feed({"type": "text", "delta": "the call"})
    call(adapter, "a", '{"path":"a"}', done=True)
    adapter.feed({"type": "text", "delta": "after"})
    final = adapter.end("tool_calls")
    kinds = [item["type"] for item in final["output"]]
    assert kinds == ["message", "function_call", "message"]
    assert final["output"][0]["content"][0]["text"] == "before the call"
    assert final["output"][2]["content"][0]["text"] == "after"
    assert final["output_text"] == "before the callafter"
    assert len({item["id"] for item in final["output"]}) == 3
    deltas = [(p["output_index"], p["delta"]) for kind, p in writer.events if kind == "response.output_text.delta"]
    assert deltas == [(0, "before "), (0, "the call"), (2, "after")]


def test_thinking_after_a_call_is_a_reasoning_item_of_its_own_and_the_placeholder_is_not_counted():
    parsed_with_label = parsed()
    parsed_with_label.thinking_placeholder = "Thinking...\n"
    writer = Writer()
    adapter = adapters.ResponsesAdapter(parsed_with_label, writer)
    adapter.begin()
    adapter.feed({"type": "thinking_start"})  # the label is shown but is not part of the thinking
    adapter.feed({"type": "thinking", "delta": "first thought"})
    call(adapter, "a", "{}", done=True)
    adapter.feed({"type": "thinking", "delta": "second thought"})
    final = adapter.end("tool_calls")
    summaries = [item["summary"][0]["text"] for item in final["output"] if item["type"] == "reasoning"]
    assert summaries == ["first thought", "second thought"]


def test_a_call_that_is_not_ended_holds_back_the_calls_after_it():
    adapter, writer = adapter_and_writer()
    call(adapter, "a", '{"path": "a', done=False)
    call(adapter, "b", '{"path":"b"}', done=False)
    call(adapter, "b", '{"path":"b"}', done=True)
    # b is whole but a is not: closing b would close a with half its arguments, and codex-rs would run it
    assert done_indexes(writer) == []
    call(adapter, "a", '{"path": "a"}', done=True)
    assert done_indexes(writer) == [0, 1]
    items = [p["item"] for kind, p in writer.events if kind == "response.output_item.done"]
    assert [json.loads(item["arguments"]) for item in items] == [{"path": "a"}, {"path": "b"}]


def test_after_a_timeout_a_finished_call_stays_completed_and_only_what_was_cut_is_incomplete():
    adapter, writer = adapter_and_writer()
    adapter.feed({"type": "text", "delta": "Looking"})
    call(adapter, "a", '{"path":"a"}', done=True)
    call(adapter, "b", '{"path": "b', done=False)
    final = adapter.end("timeout")
    assert [item["status"] for item in final["output"]] == ["completed", "completed", "incomplete"]
    assert [p["item"]["status"] for kind, p in writer.events if kind == "response.output_item.done"] == [
        "completed", "completed", "incomplete"]
    assert names(writer)[-2:] == ["response.incomplete", "done"]


def test_a_custom_call_is_closed_with_its_whole_input_when_upstream_ends_it():
    adapter, writer = adapter_and_writer()
    patch = "*** Begin Patch\n*** Add File: x.py\n+x = 1\n*** End Patch"
    whole = json.dumps({"input": patch})
    for end in range(1, len(whole), 9):
        call(adapter, "p1", whole[:end], done=False, name="apply_patch", tool_type="custom")
    assert done_indexes(writer) == []
    call(adapter, "p1", whole, done=True, name="apply_patch", tool_type="custom")
    assert done_indexes(writer) == [0]
    deltas = [p["delta"] for kind, p in writer.events if kind == "response.custom_tool_call_input.delta"]
    done = next(p["input"] for kind, p in writer.events if kind == "response.custom_tool_call_input.done")
    assert "".join(deltas) == done == patch
    call(adapter, "p2", json.dumps({"input": "*** Begin Patch\n*** End Patch"}), done=True, name="apply_patch",
         tool_type="custom")
    assert done_indexes(writer) == [0, 1]
    adapter.end("tool_calls")
    assert done_indexes(writer) == [0, 1]


def test_a_call_that_is_never_marked_done_is_closed_at_the_end_as_before():
    adapter, writer = adapter_and_writer()
    adapter.feed({"type": "tool_call", "id": "a", "name": "read", "arguments": '{"path":"a"}'})
    assert done_indexes(writer) == []
    final = adapter.end("tool_calls")
    assert done_indexes(writer) == [0] and final["output"][0]["status"] == "completed"


def test_a_reply_of_text_only_has_the_events_it_always_had():
    adapter, writer = adapter_and_writer()
    adapter.feed({"type": "thinking", "delta": "hm"})
    adapter.feed({"type": "text", "delta": "an answer"})
    final = adapter.end("stop")
    assert names(writer) == [
        "response.created", "response.output_item.added", "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta", "response.output_item.added", "response.content_part.added",
        "response.output_text.delta", "response.reasoning_summary_text.done", "response.reasoning_summary_part.done",
        "response.output_item.done", "response.output_text.done", "response.content_part.done",
        "response.output_item.done", "response.completed", "done"]
    assert [item["type"] for item in final["output"]] == ["reasoning", "message"]


def test_a_non_stream_reply_is_the_same_whether_calls_closed_early_or_not():
    adapter = adapters.ResponsesAdapter(parsed(), None)
    adapter.feed({"type": "text", "delta": "Looking"})
    call(adapter, "a", '{"path":"a"}', done=True)
    call(adapter, "b", '{"path":"b"}', done=True)
    final = adapter.end("tool_calls")
    assert [item["type"] for item in final["output"]] == ["message", "function_call", "function_call"]
    assert final["output"][0]["content"][0]["text"] == "Looking" and final["status"] == "completed"


# --- through the relay -------------------------------------------------------------------------------------------------

def upstream_call(call_id, path):
    return [{"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": "read_file"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps({"path": path})},
            {"event": "content_block_end", "type": "tool_call", "toolCallId": call_id}]


END = [{"event": "metadata", "usage": {"inputTokens": 4, "outputTokens": 30, "creditsUsed": 0.1}},
       {"event": "end_of_stream"}]


def stream_lines(relay_port, body):
    """[(seconds since the request, SSE event name, data)] of one streamed Responses request."""
    connection = http.client.HTTPConnection("127.0.0.1", relay_port, timeout=30)
    started = time.monotonic()
    connection.request("POST", PATHS["responses"], json.dumps(body).encode(),
                       {"content-type": "application/json", "authorization": "Bearer " + KEY})
    reply = connection.getresponse()
    events, name = [], None
    while True:
        line = reply.readline()
        if not line:
            break
        line = line.decode().strip()
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:") and line[5:].strip() != "[DONE]":
            events.append((time.monotonic() - started, name, json.loads(line[5:])))
    connection.close()
    return events


def responses_body(**extra):
    body = client_request("responses", [{"role": "user", "content": "read two files"}])
    return {**body, "stream": True, **extra}


def test_through_the_relay_each_call_is_closed_before_the_next_one_starts(prompt_relay):
    relay = prompt_relay()
    relay.scripts.append([*upstream_call("call_a", "a.py"), *upstream_call("call_b", "b.py"),
                          *upstream_call("call_c", "c.py"), *END])
    events = stream_lines(relay.port, responses_body())
    kinds = [(name, data.get("output_index"), (data.get("item") or {}).get("call_id")) for _, name, data in events]
    for first, second in ((0, 1), (1, 2)):
        done = next(i for i, (name, index, _) in enumerate(kinds) if name == "response.output_item.done" and index == first)
        added = next(i for i, (name, index, _) in enumerate(kinds) if name == "response.output_item.added" and index == second)
        assert done < added
    done_items = [data["item"] for _, name, data in events if name == "response.output_item.done"]
    assert [json.loads(item["arguments"]) for item in done_items] == [
        {"path": "a.py"}, {"path": "b.py"}, {"path": "c.py"}]
    assert events[-1][1] == "response.completed"
    assert [item["call_id"] for item in events[-1][2]["response"]["output"]] == ["call_a", "call_b", "call_c"]


def test_through_the_relay_a_slow_second_call_does_not_hold_back_the_first_one(prompt_relay, monkeypatch):
    """The upstream writes call A, waits, writes call B and ends: the client must have A's done event long before the end."""
    import direct

    class Slow(io.BytesIO):
        status = 200
        headers = {"request-id": "upstream-id"}

        def __init__(self, steps):
            super().__init__(b"")
            self.steps = steps

        def __iter__(self):
            for delay, event in self.steps:
                time.sleep(delay)
                yield json.dumps(event).encode() + b"\n"

    pause = 1.2
    steps = [(0, event) for event in upstream_call("call_a", "a.py")]
    steps += [(pause, event) for event in upstream_call("call_b", "b.py")[:1]]
    steps += [(0, event) for event in upstream_call("call_b", "b.py")[1:]] + [(0, event) for event in END]
    relay = prompt_relay()
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_k: Slow(steps))
    events = stream_lines(relay.port, responses_body())
    first_done = next(at for at, name, data in events
                      if name == "response.output_item.done" and data["item"]["call_id"] == "call_a")
    completed = next(at for at, name, _ in events if name == "response.completed")
    # without the fix both done events came with response.completed, `pause` seconds after call A was whole
    assert completed - first_done >= pause * 0.8, (first_done, completed)
