"""Items 5 and 6 and 9 and 10 of the bug summary (C5, C13, F4): a reply the output stopped is told as cut, and a call it cut is
neither reported as whole nor repaired into text the client has not seen.

- C5: a tool call whose arguments are not JSON at the end was reported as complete (Anthropic input {}, Chat and Responses
  a broken string) and the turn as a tool call.
- C13: the fallback rebuilt such arguments from the raw upstream text, which differs from the streamed text wherever a
  number or a boolean was written as a string, and the adapters sent the whole rebuilt text as one more delta.
- F4: a reasoning model that spends a small output cap on reasoning ended with nothing, was asked again with the same cap and
  ended 502 upstream_empty_turn after two paid attempts, where OpenAI and Anthropic answer 200 with length or max_tokens."""
import asyncio
import copy
import json

import pytest

import adapters
import direct
from prompt_support import client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_native_context_reuse import FIRST, request, run
from test_native_history import TOOLS, close, harness
from test_review_fixes_pr19 import text

CAP = 64
CUT_ARGS = '{"path": "a.py", "limit": "3", "note": "hel'
STREAMED = '{"path": "a.py", "limit": 3, "note": "hel'  # the number was written as a string and is sent as a number
OPEN_CALL = [{"event": "content_block_start", "type": "tool_call", "toolCallId": "c1", "toolName": "read_file"}]
END_OF_STREAM = {"event": "end_of_stream"}
REASONING_ONLY = [{"event": "content_block_start", "type": "redacted_thinking", "data": "opaque"},
                  {"event": "content_block_end"}]


def usage(output, credits=0.1):
    return {"event": "metadata", "usage": {"inputTokens": 4, "outputTokens": output, "creditsUsed": credits}}


def call_delta(piece):
    return {"event": "content_block_delta", "type": "tool_call", "partialInputs": piece}


def cut_call(output=CAP, pieces=(CUT_ARGS[:20], CUT_ARGS[20:])):
    return [*OPEN_CALL, *(call_delta(piece) for piece in pieces), usage(output), END_OF_STREAM]


def whole_call(output=CAP):
    return [*OPEN_CALL, call_delta('{"path": "a.py", "limit": "3"}'),
            {"event": "content_block_end", "type": "tool_call", "toolCallId": "c1"}, usage(output), END_OF_STREAM]


def drive(tmp_path, monkeypatch, scripts, cap=CAP, dialect="chat"):
    pool, runner, sent = harness(tmp_path, monkeypatch, scripts)

    async def scenario():
        try:
            parsed = request(copy.deepcopy(FIRST), dialect=dialect, tools=TOOLS)
            parsed.max_tokens = cap
            events = await run(runner, parsed)
            return events, pool._conversations["header:client-a"]
        finally:
            await close(pool)

    events, session = asyncio.run(scenario())
    return events, session, sent


def calls_of(events):
    return [event for event in events if event["type"] == "tool_call"]


# --- the stream of one turn: direct level -----------------------------------------------------------------------------

def test_a_cut_call_is_cut_and_its_text_is_what_the_client_has_seen(tmp_path, monkeypatch):
    events, session, sent = drive(tmp_path, monkeypatch, [cut_call()])
    calls = calls_of(events)
    texts = [event["arguments"] for event in calls]
    # every update continues the one before: whatever the client builds from the deltas is a prefix of the last text
    assert all(after.startswith(before) for before, after in zip(texts, texts[1:]))
    assert texts[-1] == STREAMED  # not rebuilt from the raw text, which holds the number as a string
    assert calls[-1]["done"] is False and not any(event["done"] for event in calls)
    assert events[-1] == {"type": "end", "reason": "length"}
    assert len(sent) == 1


def test_the_rebuilt_text_of_the_old_fallback_is_not_what_the_stream_carried():
    # what _final_call_event used to send for this call: the raw text, not an extension of the streamed one
    assert direct.coerce_arguments(CUT_ARGS, {"limit": {"type": "number"}}) == CUT_ARGS != STREAMED


def test_a_cut_call_is_no_branch_to_continue_and_no_warm_body(tmp_path, monkeypatch):
    events, session, sent = drive(tmp_path, monkeypatch, [cut_call()])
    assert session._native_history.branches == [] and session._warm_body is None
    assert session._turn_outcome == "truncated" and session._turn_final is False


def test_a_cut_call_is_journaled_as_cut_not_as_a_tool_call(tmp_path, monkeypatch):
    import journal
    mark = journal.JOURNAL.last_seq
    drive(tmp_path, monkeypatch, [cut_call()])
    kinds = [event["kind"] for event in journal.JOURNAL.query(since=mark, limit=2000, order="asc")]
    assert "tool.call_incomplete" in kinds and "tool.call" not in kinds and "upstream.truncated" in kinds
    ended = [event for event in journal.JOURNAL.query(since=mark, limit=2000) if event["kind"] == "turn.end"]
    assert ended[-1]["data"]["reason"] == "length" and ended[-1]["level"] == "warn"


def test_a_whole_call_that_ends_on_the_cap_is_a_call_not_a_cut(tmp_path, monkeypatch):
    events, session, sent = drive(tmp_path, monkeypatch, [whole_call()])
    calls = calls_of(events)
    assert calls[-1]["done"] is True and json.loads(calls[-1]["arguments"]) == {"path": "a.py", "limit": 3}
    assert events[-1] == {"type": "end", "reason": "tool_calls"}


def test_a_call_cut_by_the_stream_without_the_cap_is_cut_too(tmp_path, monkeypatch):
    # fewer output tokens than the cap: not the cap, but the arguments are still no JSON at the end
    events, session, sent = drive(tmp_path, monkeypatch, [cut_call(output=10)], cap=1000)
    assert calls_of(events)[-1]["done"] is False and events[-1] == {"type": "end", "reason": "length"}


def test_a_reasoning_model_that_spends_the_cap_on_reasoning_is_cut_after_one_attempt(tmp_path, monkeypatch):
    capped = [*REASONING_ONLY, usage(CAP), END_OF_STREAM]
    events, session, sent = drive(tmp_path, monkeypatch, [capped, capped])
    assert len(sent) == 1  # asking again with the same cap pays for the same nothing
    assert not any(event["type"] == "error" for event in events)
    assert events[-1] == {"type": "end", "reason": "length"}


def test_an_empty_reply_below_the_cap_is_still_asked_again_and_then_an_error(tmp_path, monkeypatch):
    empty = [*REASONING_ONLY, usage(CAP // 2), END_OF_STREAM]
    events, session, sent = drive(tmp_path, monkeypatch, [empty, empty])
    assert len(sent) == 2 and events[-1]["type"] == "error" and events[-1]["error"].code == "upstream_empty_turn"


def test_text_that_ends_on_the_cap_is_delivered_and_told_cut(tmp_path, monkeypatch):
    events, session, sent = drive(tmp_path, monkeypatch, [[text("a long ans"), usage(CAP), END_OF_STREAM]])
    assert "".join(event["delta"] for event in events if event["type"] == "text") == "a long ans"
    assert events[-1] == {"type": "end", "reason": "length"} and session._native_history.branches == []


def test_a_route_that_ignores_the_cap_is_not_taken_for_cut(tmp_path, monkeypatch):
    # Claude's route wrote 750-980 tokens for caps of 16, 64 and 300: more than the cap is no sign of a cut
    events, session, sent = drive(tmp_path, monkeypatch, [[text("a whole answer"), usage(700), END_OF_STREAM]])
    assert events[-1] == {"type": "end", "reason": "stop"}


def test_a_reply_without_usage_is_not_taken_for_cut(tmp_path, monkeypatch):
    events, session, sent = drive(tmp_path, monkeypatch, [[text("an answer"), END_OF_STREAM]])
    assert events[-1] == {"type": "end", "reason": "stop"}


# --- the adapters -----------------------------------------------------------------------------------------------------

class Writer:
    def __init__(self):
        self.events = []

    def data(self, payload):
        self.events.append(("data", payload))

    def event(self, name, payload):
        self.events.append((name, payload))

    def done(self):
        self.events.append(("done", None))


def adapter_for(cls, tools=()):
    parsed = type("P", (), {"show_thinking": True, "model": "claude-opus-5-5", "system": "", "prompt": "x",
                            "tools": list(tools), "wants_usage": False})()
    writer = Writer()
    return cls(parsed, writer), writer


def feed_cut_call(adapter, pieces=('{"path": "a.py", ', '"note": "hel')):
    text_so_far = ""
    for piece in pieces:
        text_so_far += piece
        adapter.feed({"type": "tool_call", "id": "c1", "name": "read_file", "arguments": text_so_far, "done": False})


def test_anthropic_says_max_tokens_and_does_not_stop_the_cut_block_early():
    adapter, writer = adapter_for(adapters.AnthropicAdapter)
    adapter.begin()
    feed_cut_call(adapter)
    stops = [kind for kind, _ in writer.events if kind == "content_block_stop"]
    assert stops == []  # a block that is not whole is not closed as one
    final = adapter.end("length")
    assert [kind for kind, _ in writer.events][-4:] == ["content_block_stop", "message_delta", "message_stop", "done"]
    delta = next(payload for kind, payload in writer.events if kind == "message_delta")
    assert delta["delta"]["stop_reason"] == "max_tokens" and final["stop_reason"] == "max_tokens"
    assert "".join(p["delta"]["partial_json"] for kind, p in writer.events if kind == "content_block_delta") == (
        '{"path": "a.py", "note": "hel')
    assert final["content"][0]["input"] == {}  # a tool given {} refuses the call, one given half a file would write it


def test_chat_says_length_and_its_deltas_add_up_to_the_cut_text():
    adapter, writer = adapter_for(adapters.ChatAdapter)
    feed_cut_call(adapter)
    final = adapter.end("length")
    chunks = [p["choices"][0] for kind, p in writer.events if kind == "data" and p["choices"]]
    assert chunks[-1]["finish_reason"] == "length"
    deltas = [c["delta"]["tool_calls"][0]["function"]["arguments"] for c in chunks if "tool_calls" in c["delta"]]
    assert "".join(deltas) == '{"path": "a.py", "note": "hel'
    assert final["choices"][0]["finish_reason"] == "length"
    assert final["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"path": "a.py", "note": "hel'


@pytest.mark.parametrize("adapter_class", [adapters.ChatAdapter, adapters.AnthropicAdapter, adapters.ResponsesAdapter])
def test_a_text_that_does_not_continue_what_was_sent_is_not_sent_as_a_delta(adapter_class):
    adapter, writer = adapter_for(adapter_class)
    adapter.feed({"type": "tool_call", "id": "c1", "name": "read_file", "arguments": '{"limit": 3, "a": "x', "done": False})
    before = len(writer.events)
    adapter.feed({"type": "tool_call", "id": "c1", "name": "read_file", "arguments": '{"limit": "3", "a": "x"}',
                  "done": True})
    sent = []
    for kind, payload in writer.events[before:]:
        sent += [payload.get("delta")] if kind.endswith(".delta") or kind == "content_block_delta" else []
        sent += [c["delta"] for c in payload.get("choices", [])] if kind == "data" else []
    assert not any("limit\": \"3" in json.dumps(item) for item in sent)  # the whole new text was never added as a delta
    assert adapter.tool_calls[0]["arguments"] == '{"limit": 3, "a": "x'  # the call keeps what the client has


def test_without_a_stream_the_final_text_wins():
    adapter = adapters.ChatAdapter(type("P", (), {"show_thinking": True, "model": "m", "system": "", "prompt": "x",
                                                  "tools": [], "wants_usage": False})(), None)
    adapter.feed({"type": "tool_call", "id": "c1", "name": "t", "arguments": '{"limit": 3, "a": "x', "done": False})
    adapter.feed({"type": "tool_call", "id": "c1", "name": "t", "arguments": '{"limit": "3", "a": "x"}', "done": True})
    assert adapter.tool_calls[0]["arguments"] == '{"limit": "3", "a": "x"}'


def test_responses_says_incomplete_with_the_reason_and_never_completed():
    adapter, writer = adapter_for(adapters.ResponsesAdapter)
    adapter.begin()
    adapter.feed({"type": "text", "delta": "Looking"})
    feed_cut_call(adapter)
    final = adapter.end("length")
    names = [kind for kind, _ in writer.events]
    assert names[-2:] == ["response.incomplete", "done"] and "response.completed" not in names
    assert final["status"] == "incomplete" and final["incomplete_details"] == {"reason": "max_output_tokens"}
    assert [item["status"] for item in final["output"]] == ["completed", "incomplete"]
    assert "response.function_call_arguments.done" not in names  # the arguments of a cut call are not final
    done = [p["item"] for kind, p in writer.events if kind == "response.output_item.done"]
    assert done == final["output"]


def test_responses_does_not_fail_on_a_custom_call_the_cap_cut_inside_its_envelope():
    adapter, writer = adapter_for(adapters.ResponsesAdapter)
    adapter.begin()
    adapter.feed({"type": "tool_call", "id": "p", "name": "apply_patch", "tool_type": "custom",
                  "arguments": '{"input":"*** Begin Patch\\n*** Add Fi', "done": False})
    final = adapter.end("length")
    assert final["status"] == "incomplete" and final["output"][0]["status"] == "incomplete"
    deltas = [p["delta"] for kind, p in writer.events if kind == "response.custom_tool_call_input.delta"]
    done = next(p["input"] for kind, p in writer.events if kind == "response.custom_tool_call_input.done")
    assert "".join(deltas) == done == final["output"][0]["input"] == ""
    assert "response.incomplete" in [kind for kind, _ in writer.events]


def test_a_timeout_keeps_marking_every_item_incomplete():
    adapter, writer = adapter_for(adapters.ResponsesAdapter)
    adapter.begin()
    adapter.feed({"type": "text", "delta": "partial"})
    final = adapter.end("timeout")
    assert final["status"] == "incomplete" and "incomplete_details" not in final
    assert [item["status"] for item in final["output"]] == ["incomplete"]


# --- through the relay: the three dialects, with and without a stream --------------------------------------------------

def sse(raw):
    events, name = [], None
    for line in raw.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:") and line[5:].strip() != "[DONE]":
            events.append((name, json.loads(line[5:])))
            name = None
    return events


def ask(relay, dialect, script, stream, **extra):
    relay.scripts.extend(script)
    cap = {"chat": {"max_tokens": CAP}, "anthropic": {"max_tokens": CAP}, "responses": {"max_output_tokens": CAP}}
    body = client_request(dialect, [{"role": "user", "content": "go"}], **cap[dialect], **extra)
    body["stream"] = stream
    status, payload, headers = relay.post(dialect, body)
    return status, (sse(payload) if stream else payload)


@pytest.mark.parametrize("stream", [False, True])
def test_every_dialect_tells_a_cut_call_as_cut(prompt_relay, stream):
    relay = prompt_relay()
    status, anthropic = ask(relay, "anthropic", [[*cut_call()]], stream)
    status_chat, chat = ask(relay, "chat", [[*cut_call()]], stream)
    status_responses, responses = ask(relay, "responses", [[*cut_call()]], stream)
    assert status == status_chat == status_responses == 200
    if stream:
        assert [p for n, p in anthropic if n == "message_delta"][-1]["delta"]["stop_reason"] == "max_tokens"
        assert [p for _, p in chat if p.get("choices")][-1]["choices"][0]["finish_reason"] == "length"
        names = [n for n, _ in responses]
        assert names[-1] == "response.incomplete" and "response.completed" not in names
        assert responses[-1][1]["response"]["incomplete_details"] == {"reason": "max_output_tokens"}
        pieces = "".join(p["delta"] for n, p in responses if n == "response.function_call_arguments.delta")
        assert pieces == STREAMED
    else:
        assert anthropic["stop_reason"] == "max_tokens"
        assert chat["choices"][0]["finish_reason"] == "length"
        assert chat["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == STREAMED
        assert responses["status"] == "incomplete" and responses["output"][-1]["status"] == "incomplete"
    assert len(relay.posts) == 3  # nothing was asked again


@pytest.mark.parametrize("stream", [False, True])
def test_every_dialect_answers_an_empty_reply_at_the_cap_with_length_not_an_error(prompt_relay, stream):
    relay = prompt_relay()
    capped = [*REASONING_ONLY, usage(CAP), END_OF_STREAM]
    results = {dialect: ask(relay, dialect, [capped, capped], stream, tools=False)
               for dialect in ("anthropic", "chat", "responses")}
    assert [status for status, _ in results.values()] == [200, 200, 200]
    assert len(relay.posts) == 3  # one attempt each; it used to be two, then a 502 upstream_empty_turn
    anthropic, chat, responses = (payload for _, payload in results.values())
    if stream:
        assert [p for n, p in anthropic if n == "message_delta"][-1]["delta"]["stop_reason"] == "max_tokens"
        assert [p for _, p in chat if p.get("choices")][-1]["choices"][0]["finish_reason"] == "length"
        assert [n for n, _ in responses][-1] == "response.incomplete"
    else:
        assert anthropic["stop_reason"] == "max_tokens" and chat["choices"][0]["finish_reason"] == "length"
        assert responses["status"] == "incomplete"
