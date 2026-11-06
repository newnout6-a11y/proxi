"""A tool-call delta that names its call reaches that call, not the newest open one.

_drain glued every delta onto the newest open call, while ReplyCapture (the signed history) honoured the toolCallId a delta
carries: with two calls open and their deltas tagged, the client got '{"file_path":"1"}{"file_path":"2"}' for one call and
'{}' for the other, and the history said something else (ultracode audit of the relay, G1.1). Retool's deltas carry no id
today (none of the 20 257 deltas of the traces saved on 2026-11-06 does), so this only matters once it starts to tag them;
a delta without one still goes to the newest open call."""
import asyncio
import json

import pytest

import direct


class Session:
    _cancelled = False
    _native_client_messages = []  # a client with a native history: the reply is captured for it as well


def start(call_id, name="Read"):
    return {"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": name}


def delta(piece, call_id=None):
    event = {"event": "content_block_delta", "type": "tool_call", "partialInputs": piece}
    if call_id:
        event["toolCallId"] = call_id
    return event


def end(call_id=None):
    event = {"event": "content_block_end", "type": "tool_call"}
    if call_id:
        event["toolCallId"] = call_id
    return event


def drain(lines, params=None):
    async def go():
        queue = asyncio.Queue()
        for line in [*lines, {"event": "end_of_stream"}]:
            queue.put_nowait(json.dumps(line))
        flags = {}
        events = [event async for event in direct._drain({}, Session(), queue, [], [], params, flags)]
        return events, flags
    return asyncio.run(go())


def finals(events):
    return {event["id"]: event["arguments"] for event in events if event["type"] == "tool_call" and not event.get("partial")}


def history(flags):
    return {block["toolCallId"]: block["toolInput"] for block in flags["assistant_blocks"] if block["type"] == "tool_call"}


def test_deltas_that_name_their_call_reach_it_whole():
    events, flags = drain([start("a"), start("b"), delta('{"file_path":"1"}', "a"), delta('{"file_path":"2"}', "b"),
                           end("b"), end("a")])
    assert finals(events) == {"a": '{"file_path":"1"}', "b": '{"file_path":"2"}'}
    assert history(flags) == {"a": {"file_path": "1"}, "b": {"file_path": "2"}}


def test_deltas_that_name_their_call_reach_it_in_pieces():
    events, flags = drain([start("a"), start("b"), delta('{"file_path":', "a"), delta('{"file_path":', "b"),
                           delta('"1"}', "a"), delta('"2"}', "b"), end("b"), end("a")])
    assert finals(events) == {"a": '{"file_path":"1"}', "b": '{"file_path":"2"}'}
    assert history(flags) == {"a": {"file_path": "1"}, "b": {"file_path": "2"}}


def test_the_partial_events_of_each_call_carry_only_its_own_arguments():
    events, _ = drain([start("a"), start("b"), delta('{"file_path":', "a"), delta('{"file_path":', "b"),
                       delta('"1"}', "a"), delta('"2"}', "b"), end("b"), end("a")])
    for call_id, expected in (("a", '{"file_path":"1"}'), ("b", '{"file_path":"2"}')):
        seen = [e["arguments"] for e in events if e["type"] == "tool_call" and e["id"] == call_id]
        assert seen and all(expected.startswith(text) for text in seen) and seen[-1] == expected


def test_a_delta_without_an_id_still_goes_to_the_newest_open_call():
    events, flags = drain([start("a"), delta('{"file_path":"1"}'), end("a"), start("b"), delta('{"file_path":"2"}'),
                           end("b")])
    assert finals(events) == {"a": '{"file_path":"1"}', "b": '{"file_path":"2"}'}
    assert history(flags) == {"a": {"file_path": "1"}, "b": {"file_path": "2"}}
    nested, _ = drain([start("a"), start("b"), delta('{"file_path":"x"}'), end(), end()])
    assert finals(nested) == {"a": "{}", "b": '{"file_path":"x"}'}


def test_a_tagged_and_an_untagged_delta_may_follow_each_other():
    events, _ = drain([start("a"), start("b"), delta('{"file_path":', "a"), delta('"2"}'), delta('"1"}', "a"),
                       end("b"), end("a")])
    # the untagged piece belongs to the newest open call, b; the tagged ones to a
    assert finals(events) == {"a": '{"file_path":"1"}', "b": '"2"}'}


@pytest.mark.parametrize("unknown", ["zzz", "a"])
def test_a_delta_for_an_unknown_or_a_finished_call_is_not_glued_onto_another(unknown):
    events, flags = drain([start("a"), delta('{"file_path":"1"}', "a"), end("a"), start("b"),
                           delta('{"file_path":"2"}', "b"), delta("GARBAGE", unknown), end("b")])
    assert finals(events) == {"a": '{"file_path":"1"}', "b": '{"file_path":"2"}'}
    assert flags["assistant_blocks"] is None  # the history does not record a reply it cannot place a delta of


def test_a_tagged_delta_is_coerced_by_the_params_of_its_own_call():
    params = {"offset": {"type": "number"}}
    events, _ = drain([start("a", "Read"), start("b", "Other"), delta('{"offset": "7"}', "a"),
                       delta('{"offset": "7"}', "b"), end("b"), end("a")], params={"Read": params})
    assert finals(events) == {"a": '{"offset": 7}', "b": '{"offset": "7"}'}
