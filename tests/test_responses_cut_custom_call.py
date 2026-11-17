"""A custom tool call that a cancel or a timeout cuts off is an incomplete item, not an upstream error.

Codex's apply_patch (Responses' custom tool) is announced to the model as one string field, and the relay hands its
input on only once the model's envelope {"input": "..."} is whole. A turn that ended before that (the client left, or
the turn timed out) made the adapter raise 502 upstream_error while it closed the items: the journal recorded http.error
for a turn the client or the clock had ended, and the console printed [err] (ultracode audit of the relay, G2.1). A turn
that upstream itself completed with a cut input stays a 502: an empty patch must never pass for a whole one."""
import json

import pytest

import adapters
import apierr
import journal
from test_adapter_protocols import Writer, parsed
from test_client_contracts import _body
from test_structured_output import post, sse_events

CUT = '{"input":"*** Begin Patch\n*** Upd'
PATCH = "*** Begin Patch\n*** Add File: x.py\n+x=1\n*** End Patch"


def with_cut_call(stream, arguments=CUT):
    writer = Writer() if stream else None
    adapter = adapters.ResponsesAdapter(parsed(), writer)
    if stream:
        adapter.begin()
    adapter.feed({"type": "text", "delta": "Let me patch."})
    adapter.feed({"type": "tool_call", "id": "c1", "name": "apply_patch", "tool_type": "custom",
                  "arguments": arguments})
    return writer, adapter


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["cancelled", "timeout"])
def test_a_cut_custom_call_closes_as_an_incomplete_item(reason, stream):
    writer, adapter = with_cut_call(stream)
    final = adapter.end(reason)
    call = final["output"][-1]
    assert call["type"] == "custom_tool_call" and call["call_id"] == "c1"
    assert call["status"] == "incomplete" and call["input"] == ""
    assert final["output"][0]["type"] == "message" and final["output"][0]["content"][0]["text"] == "Let me patch."
    if stream:
        kinds = [kind for kind, _ in writer.events]
        assert not any(kind == "response.custom_tool_call_input.delta" for kind in kinds)
        done = next(p for kind, p in writer.events if kind == "response.custom_tool_call_input.done")
        assert done["input"] == ""
        assert [p["item"] for kind, p in writer.events if kind == "response.output_item.done"] == final["output"]
        assert kinds[-2:] == ["response.incomplete" if reason == "timeout" else "response.completed", "done"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["cancelled", "timeout"])
def test_a_whole_custom_call_is_untouched_by_the_reason_the_turn_ended_for(reason, stream):
    _, adapter = with_cut_call(stream, json.dumps({"input": PATCH}))
    call = adapter.end(reason)["output"][-1]
    assert call["input"] == PATCH
    assert call["status"] == ("incomplete" if reason == "timeout" else "completed")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["tool_calls", "stop"])
def test_a_cut_input_of_a_turn_upstream_completed_is_still_an_upstream_error(reason, stream):
    _, adapter = with_cut_call(stream)
    with pytest.raises(apierr.ApiError) as error:
        adapter.end(reason)
    assert error.value.status == 502 and error.value.code == "upstream_error"


def patch_request(**extra):
    return dict(_body("responses", tools=[{"type": "custom", "name": "apply_patch", "description": "Patch"}]), **extra)


def http_errors(since):
    return [event for event in journal.JOURNAL.query(since=since, limit=20000) if event["kind"] == "http.error"]


# A turn ends "cancelled" while its client still reads only when the relay is stopping, which is a 503 for the client
# (test_shutdown_cut_reply.py); when the client left, the handler cancels the turn and nobody reads the end. The adapter's
# handling of a cancelled turn is tested above; over HTTP the clock's cut is what a client still connected can see.
@pytest.mark.parametrize("reason", ["timeout"])
def test_a_streamed_turn_cut_in_a_patch_is_no_error_in_the_journal(relay, reason):
    relay.fake.script = [{"type": "tool_call", "id": "c1", "name": "apply_patch", "tool_type": "custom",
                          "arguments": CUT}, {"type": "end", "reason": reason}]
    seq = journal.JOURNAL.last_seq
    status, _, raw = post(relay, "responses", patch_request(stream=True))
    names = [name for name, _ in sse_events(raw)]
    assert status == 200 and "response.failed" not in names and "error" not in names
    assert names[-1] == ("response.incomplete" if reason == "timeout" else "response.completed")
    assert http_errors(seq) == []


@pytest.mark.parametrize("reason", ["timeout"])
def test_a_whole_answer_to_a_turn_cut_in_a_patch_is_no_error_either(relay, reason):
    relay.fake.script = [{"type": "tool_call", "id": "c1", "name": "apply_patch", "tool_type": "custom",
                          "arguments": CUT}, {"type": "end", "reason": reason}]
    seq = journal.JOURNAL.last_seq
    status, _, raw = post(relay, "responses", patch_request())
    answer = json.loads(raw)
    assert status == 200 and answer["output"][-1]["status"] == "incomplete" and answer["output"][-1]["input"] == ""
    assert http_errors(seq) == []
