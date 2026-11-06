"""In the Messages stream no block is stopped before its arguments are whole, and none gets a delta after its stop.

Text (or thinking) that arrives inside a tool call that is still being written closed that call's block at once; the rest
of its arguments then had nowhere to go: commit d8f8763 stopped sending a delta for a stopped block (it breaks the
protocol, which has no re-open), so the client got a tool_use whose input ended in the middle, '{"p":' (ultracode audit of
the relay, C22; offline reproduction). Retool does not interleave its blocks (none of the 1 062 upstream streams with calls
that were saved on 2026-11-06 has text or thinking inside an open call), so this is about what the relay does if it ever
does. What waits now is the interleaved text, not the arguments: an event that would open a text or thinking block while a
tool call is open is held until the open calls have stopped, and goes out as the next block, in the order it came. The
message the stream describes is the message the non-streaming reply holds."""
import json
import random

import pytest

import adapters
from test_adapter_protocols import Writer, parsed


def call(call_id, arguments, done=False, name="reader"):
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": arguments, "done": done}


def text(value):
    return {"type": "text", "delta": value}


def run(events, stream=True, reason="tool_calls"):
    writer = Writer() if stream else None
    adapter = adapters.AnthropicAdapter(parsed(), writer)
    if stream:
        adapter.begin()
    for event in events:
        adapter.feed(event)
    final = adapter.end(reason) if stream else adapter.payload(reason)
    return writer, final


def accumulate(writer):
    """What a strict client builds from the stream: a block is whole at its stop, a delta after the stop is lost."""
    blocks, open_, late = {}, set(), []
    for name, payload in writer.events:
        if name == "content_block_start":
            blocks[payload["index"]] = dict(payload["content_block"], json="", text="", thinking="", signature="")
            open_.add(payload["index"])
        elif name == "content_block_delta":
            if payload["index"] not in open_:
                late.append(payload)
                continue
            delta, block = payload["delta"], blocks[payload["index"]]
            block["json"] += delta.get("partial_json", "")
            block["text"] += delta.get("text", "")
            block["thinking"] += delta.get("thinking", "")
            block["signature"] += delta.get("signature", "")
        elif name == "content_block_stop":
            open_.discard(payload["index"])
    return blocks, late, open_


def shape(blocks):
    out = []
    for index in sorted(blocks):
        block = blocks[index]
        if block["type"] == "tool_use":
            out.append(("tool_use", block["id"], json.loads(block["json"] or "{}")))
        elif block["type"] == "text":
            out.append(("text", block["text"]))
        else:
            out.append(("thinking", block["thinking"], block["signature"]))
    return out


def payload_shape(final):
    out = []
    for block in final["content"]:
        if block["type"] == "tool_use":
            out.append(("tool_use", block["id"], block["input"]))
        elif block["type"] == "text":
            out.append(("text", block["text"]))
        else:
            out.append(("thinking", block["thinking"], block["signature"]))
    return out


INTERLEAVED = [call("t1", ""), call("t1", '{"p":'), text("hi"), call("t1", '{"p":1}'), call("t1", '{"p":1}', done=True)]


def test_the_arguments_of_a_call_are_whole_when_text_arrives_inside_it():
    writer, final = run(INTERLEAVED)
    blocks, late, open_ = accumulate(writer)
    assert late == [] and open_ == set()
    assert shape(blocks) == [("tool_use", "t1", {"p": 1}), ("text", "hi")]
    assert payload_shape(final) == shape(blocks)


def test_the_text_waits_for_the_call_and_comes_after_it():
    writer, _ = run(INTERLEAVED)
    order = [(name, payload["index"]) for name, payload in writer.events if name.startswith("content_block")]
    assert order == [("content_block_start", 0), ("content_block_delta", 0), ("content_block_delta", 0),
                     ("content_block_stop", 0), ("content_block_start", 1), ("content_block_delta", 1),
                     ("content_block_stop", 1)]


def test_thinking_and_its_signature_inside_a_call_wait_for_it_as_well():
    events = [call("t1", '{"p":'), {"type": "thinking", "delta": "plan"}, {"type": "thinking_signature", "signature": "sig"},
              call("t1", '{"p":1}'), call("t1", '{"p":1}', done=True), text("done")]
    writer, final = run(events)
    blocks, late, _ = accumulate(writer)
    assert late == []
    assert shape(blocks) == [("tool_use", "t1", {"p": 1}), ("thinking", "plan", "sig"), ("text", "done")]
    assert payload_shape(final) == shape(blocks)


def test_text_inside_two_parallel_calls_waits_for_both():
    events = [call("a", '{"p":'), call("b", '{"q":'), text("hi"), call("a", '{"p":1}', done=True),
              text(" there"), call("b", '{"q":2}', done=True)]
    writer, final = run(events)
    blocks, late, open_ = accumulate(writer)
    assert late == [] and open_ == set()
    assert shape(blocks) == [("tool_use", "a", {"p": 1}), ("tool_use", "b", {"q": 2}), ("text", "hi there")]
    assert payload_shape(final) == shape(blocks)


def test_text_inside_a_call_that_is_never_marked_done_comes_at_the_end_after_the_call():
    writer, final = run([call("t1", '{"p":1}'), text("hi")])
    blocks, late, open_ = accumulate(writer)
    assert late == [] and open_ == set()
    assert shape(blocks) == [("tool_use", "t1", {"p": 1}), ("text", "hi")]
    assert payload_shape(final) == shape(blocks)


def test_a_reply_that_is_not_interleaved_streams_as_it_did():
    events = [text("Let me look."), call("a", '{"p":1}', done=True), text("Then"), call("b", '{"q":2}', done=True),
              text("Done.")]
    writer, final = run(events)
    order = [(name, payload["index"]) for name, payload in writer.events if name.startswith("content_block")]
    expected = []
    for index in range(5):  # text, call, text, call, text: each block starts, gets its delta and stops before the next
        expected += [("content_block_start", index), ("content_block_delta", index), ("content_block_stop", index)]
    assert order == expected
    blocks, late, _ = accumulate(writer)
    assert late == [] and payload_shape(final) == shape(blocks)
    assert [b[0] for b in payload_shape(final)] == ["text", "tool_use", "text", "tool_use", "text"]


def test_the_reply_that_is_not_streamed_was_complete_all_along():
    _, final = run(INTERLEAVED, stream=False)
    assert payload_shape(final) == [("tool_use", "t1", {"p": 1}), ("text", "hi")]


def random_reply(rng):
    """Events as a provider that interleaves would send them: calls, their pieces, text and thinking, in any order."""
    calls = {name: {"arguments": json.dumps({"k": rng.randint(0, 9), "s": "x" * rng.randint(0, 5)}), "sent": 0}
             for name in "abc"[:rng.randint(1, 3)]}
    events, open_ = [], []
    pending = [("start", name) for name in calls]
    rng.shuffle(pending)
    while pending or open_:
        move = rng.random()
        if pending and (not open_ or move < .3):
            name = pending.pop()[1]
            open_.append(name)
            events.append(call(name, ""))
        elif open_ and move < .7:
            name = rng.choice(open_)
            state = calls[name]
            state["sent"] = min(len(state["arguments"]), state["sent"] + rng.randint(1, 6))
            events.append(call(name, state["arguments"][:state["sent"]]))
            if state["sent"] == len(state["arguments"]) and rng.random() < .6:
                events.append(call(name, state["arguments"], done=True))
                open_.remove(name)
        elif move < .85:
            events.append(text(rng.choice(["a", "bb", " c "])))
        elif move < .95:
            events.append({"type": "thinking", "delta": rng.choice(["x", "yz"])})
        else:
            events.append({"type": "thinking_signature", "signature": "s"})
        if not pending and open_ and all(calls[name]["sent"] == len(calls[name]["arguments"]) for name in open_):
            for name in list(open_):
                if rng.random() < .5:
                    events.append(call(name, calls[name]["arguments"], done=True))
                open_.remove(name)
    return events


@pytest.mark.parametrize("seed", range(400))
def test_whatever_the_order_the_stream_is_the_message_and_no_delta_follows_a_stop(seed):
    events = random_reply(random.Random(seed))
    writer, final = run(events)
    blocks, late, open_ = accumulate(writer)
    assert late == [] and open_ == set(), events
    assert payload_shape(final) == shape(blocks), events
    _, plain = run(events, stream=False)
    assert [b for b in payload_shape(plain) if b[0] == "tool_use"] == [b for b in payload_shape(final) if b[0] == "tool_use"]
