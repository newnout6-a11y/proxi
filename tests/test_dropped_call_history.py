"""A call the client never received is no part of the branch kept for the reply (item 11, a5's ultracode audit, C4).

The relay passes on a call only when the client can run it. A call of a tool the client did not declare, and under a named
choice a call of any other tool than the named one, is dropped (`tool.call_dropped`). The model made it all the same, so the
reply kept as the branch had a call that the client never echoes, and the request that carried its tool result could not
continue the branch: `native_history.reset`, no signatures, the prefix written again (a5: the client got only `read_file`,
the stored branch held `list_dir` and `read_file`, and the next turn reset to cold). A reply whose only call was dropped
was told as an empty turn and kept as a usable one.
"""
import asyncio
import copy
import io
import json
import urllib.error

import pytest

import direct
import forced_choice
import journal
import native_history
from test_conversation_state import stored
from test_dropped_tool_calls import call
from test_native_context_reuse import FIRST, request, run
from test_native_history import TOOLS, append_result, close, harness
from test_review_fixes_pr19 import END, THINKING, response, signature, text

LIST_DIR = {"type": "function", "function": {"name": "list_dir", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}}}}}
NAMED = {"type": "function", "function": {"name": "read_file"}}
KEY = "header:client-a"


def reply(*calls, sig="opaque-one"):
    """Signed thinking, then the calls (each from `call`), as Retool streams it."""
    return [*THINKING, signature(sig[:3]), signature(sig[3:]), {"event": "content_block_end", "type": "thinking"},
            *[event for one in calls for event in one], *END]


@pytest.fixture
def native_choice(monkeypatch):
    """Claude's forced choice is asked for in words and checked by the relay, with a second try (test_forced_choice). The
    tests that use this are about the orchestrator's check of a route that takes the choice itself and ignores it."""
    monkeypatch.setattr(forced_choice, "requirement", lambda *args, **kwargs: None)


def asked(messages, dialect, tools, choice=None):
    parsed = request(messages, dialect=dialect, tools=tools)
    if choice is not None:
        parsed.tool_choice = choice
    return parsed


def assistant_of(body):
    return [message for message in body["messages"] if message["role"] == "assistant"][-1]["content"]


def blocks_of(snapshot_blocks, kind):
    return [block for block in snapshot_blocks if block["type"] == kind]


# --- the branch ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
@pytest.mark.parametrize("choice", [None, NAMED])
def test_the_next_step_continues_the_branch_when_the_other_call_of_the_reply_was_dropped(tmp_path, monkeypatch, dialect, choice):
    # `None`: a call of a tool that was never declared; a named choice: a call of a declared tool that was not named
    tools = TOOLS + [LIST_DIR] if choice else TOOLS
    # under a named choice every step has to call the tool again, so the second answer is a call too
    second = reply(call("c3", "read_file", {"path": "b"}), sig="opaque-two") if choice else [text("done"), *END]
    pool, runner, sent = harness(tmp_path, monkeypatch, [
        reply(call("c1", "list_dir", {"path": "."}), call("c2", "read_file", {"path": "a"})), second])
    seq = journal.JOURNAL.last_seq

    async def scenario():
        messages = copy.deepcopy(FIRST)
        try:
            first = asked(messages, dialect, tools, choice)
            events = await run(runner, first)
            assert {event["name"] for event in events if event["type"] == "tool_call"} == {"read_file"}
            recorded = list(pool._conversations[KEY]._native_history.branches)
            messages = append_result(messages, first, events, dialect)
            final = await run(runner, asked(messages, dialect, tools, choice))
            assert not any(event["type"] == "error" for event in final)
            assert any(event.get("delta") == "done" or event["type"] == "tool_call" for event in final)
            return recorded
        finally:
            await close(pool)

    branches = asyncio.run(scenario())
    assert len(sent) == 2
    # the signed thinking of the first reply went back with the call the client has, and nothing else of the reply
    held = assistant_of(sent[1])
    assert [block["type"] for block in held] == ["thinking", "tool_call"]
    assert held[0]["signature"] == "opaque-one" and held[1]["toolCallId"] == "c2"
    # Under a named choice the relay asks Claude for the call in a user turn of its own, at the end (forced_choice).
    result = sent[1]["messages"][-2] if choice else sent[1]["messages"][-1]
    assert result["role"] == "tool" and result["content"]["toolCallId"] == "c2"
    assert len(stored(seq, "native_history.resume")) == 1
    assert not stored(seq, "native_history.reset") and not stored(seq, "native_history.cold")
    pruned = stored(seq, "native_history.pruned")
    assert len(pruned) == 1 and "list_dir" in pruned[0]["message"] and pruned[0]["data"]["kept"] is True
    assert [block["toolName"] for block in blocks_of(branches[-1].assistant, "tool_call")] == ["read_file"]


def test_without_a_dropped_call_nothing_changes_for_the_branch(tmp_path, monkeypatch):
    pool, runner, sent = harness(tmp_path, monkeypatch, [reply(call("c2", "read_file", {"path": "a"})), [text("done"), *END]])
    seq = journal.JOURNAL.last_seq

    async def scenario():
        messages = copy.deepcopy(FIRST)
        try:
            first = asked(messages, "chat", TOOLS)
            events = await run(runner, first)
            await run(runner, asked(append_result(messages, first, events, "chat"), "chat", TOOLS))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert assistant_of(sent[1])[0]["signature"] == "opaque-one" and not stored(seq, "native_history.pruned")
    assert len(stored(seq, "native_history.resume")) == 1


def test_on_another_route_the_branch_is_recorded_as_before(tmp_path, monkeypatch):
    # OpenAI wants a reasoning item followed by the item it was made with, so a call cannot be left out of a branch there
    monkeypatch.setattr(direct, "_anthropic_route", lambda model: False)
    pool, runner, sent = harness(tmp_path, monkeypatch, [
        reply(call("c1", "list_dir", {"path": "."}), call("c2", "read_file", {"path": "a"})), [text("done"), *END]])
    seq = journal.JOURNAL.last_seq

    async def scenario():
        messages = copy.deepcopy(FIRST)
        try:
            first = asked(messages, "chat", TOOLS)
            events = await run(runner, first)
            recorded = list(pool._conversations[KEY]._native_history.branches)
            await run(runner, asked(append_result(messages, first, events, "chat"), "chat", TOOLS))
            return recorded
        finally:
            await close(pool)

    branches = asyncio.run(scenario())
    assert [block["toolName"] for block in blocks_of(branches[-1].assistant, "tool_call")] == ["list_dir", "read_file"]
    assert not stored(seq, "native_history.pruned")


# --- a reply whose only call was dropped --------------------------------------------------------------------------

def test_a_reply_that_only_calls_a_tool_the_client_cannot_receive_is_told_as_such_and_records_nothing(tmp_path, monkeypatch, native_choice):
    tools = TOOLS + [LIST_DIR]
    pool, runner, sent = harness(tmp_path, monkeypatch, [reply(call("c1", "list_dir", {"path": "."}))])
    seq = journal.JOURNAL.last_seq

    async def scenario():
        try:
            events = await run(runner, asked(copy.deepcopy(FIRST), "chat", tools, NAMED))
            session = pool._conversations[KEY]
            return events, session._native_history.branches, session._turn_outcome
        finally:
            await close(pool)

    events, branches, outcome = asyncio.run(scenario())
    error = events[-1]["error"]
    assert (error.status, error.code) == (502, "upstream_tool_choice_ignored")  # was upstream_empty_turn: it is no empty turn
    assert "required client tool choice" in error.message and "list_dir" in error.message
    assert error.upstream_attempts == 1 and error.diagnostic_id
    assert len(sent) == 1  # a reply that holds a call is not asked for again
    assert branches == []  # the branch of that reply would have held a call that nobody can answer
    assert outcome != "content"  # and it is no answer to keep warm
    dropped = stored(seq, "tool.call_dropped")
    assert [(event["data"]["name"], event["data"]["call_id"]) for event in dropped] == [("list_dir", "c1")]
    assert [event["data"]["kept"] for event in stored(seq, "native_history.pruned")] == [False]


def test_the_message_of_the_error_names_every_call_that_was_dropped_once(tmp_path, monkeypatch, native_choice):
    tools = TOOLS + [LIST_DIR]
    pool, runner, _ = harness(tmp_path, monkeypatch, [reply(
        call("c1", "list_dir", {"path": "."}), call("c2", "list_dir", {"path": "x"}), call("c3", "grep", {"q": "y"}))])

    async def scenario():
        try:
            return await run(runner, asked(copy.deepcopy(FIRST), "chat", tools, NAMED))
        finally:
            await close(pool)

    message = asyncio.run(scenario())[-1]["error"].message
    assert message.count("list_dir") == 1 and "grep" in message


def test_a_reply_with_text_and_a_dropped_call_under_a_named_choice_keeps_its_text_and_the_old_error(tmp_path, monkeypatch, native_choice):
    tools = TOOLS + [LIST_DIR]
    pool, runner, _ = harness(tmp_path, monkeypatch, [
        [*THINKING, signature("opa"), signature("que"), {"event": "content_block_end", "type": "thinking"}, text("Let me look."),
         *call("c1", "list_dir", {"path": "."}), *END]])

    async def scenario():
        try:
            events = await run(runner, asked(copy.deepcopy(FIRST), "chat", tools, NAMED))
            return events, pool._conversations[KEY]._native_history.branches
        finally:
            await close(pool)

    events, branches = asyncio.run(scenario())
    assert events[-1]["error"].code == "upstream_tool_choice_ignored" and "list_dir" in events[-1]["error"].message
    assert [block["type"] for block in branches[-1].assistant] == ["thinking", "text"]  # what the client was told, no call


def test_an_empty_reply_under_a_named_choice_is_still_an_empty_turn(tmp_path, monkeypatch, native_choice):
    pool, runner, _ = harness(tmp_path, monkeypatch, [[*END], [*END], [*END]])

    async def scenario():
        try:
            return await run(runner, asked(copy.deepcopy(FIRST), "chat", TOOLS, NAMED))
        finally:
            await close(pool)

    error = asyncio.run(scenario())[-1]["error"]
    assert error.code == "upstream_empty_turn" and "it called only" not in error.message


# --- the pieces --------------------------------------------------------------------------------------------------------

def blocks(*kinds):
    made = []
    for number, kind in enumerate(kinds):
        if kind == "thinking":
            made.append({"type": "thinking", "thinking": "", "signature": "S"})
        elif kind == "text":
            made.append({"type": "text", "text": "hello"})
        else:
            made.append({"type": "tool_call", "toolCallId": "c%d" % number, "toolName": kind, "toolInput": {"n": "1"}})
    return made


def coerce(raw, params):
    return raw


def test_deliverable_leaves_the_reply_alone_when_every_call_went_through():
    reply_ = (blocks("thinking", "read_file", "read_file"), {"role": "assistant", "content": []})
    assert native_history.deliverable(reply_, {"read_file"}, {}, coerce) is reply_
    assert native_history.deliverable((blocks("thinking", "text"), {}), set(), {}, coerce)[0] == blocks("thinking", "text")


def test_deliverable_drops_the_calls_of_other_tools_and_keeps_the_rest_in_order():
    original = blocks("thinking", "list_dir", "text", "read_file", "grep")
    kept, shown = native_history.deliverable((original, None), {"read_file"}, {}, coerce)
    assert [block["type"] if block["type"] != "tool_call" else block["toolName"] for block in kept] == [
        "thinking", "text", "read_file"]
    assert kept[0] is original[0]  # the signed block is the very block that came
    assert shown == {"role": "assistant", "content": [{"type": "text", "text": "hello"}, original[3]]}
    assert [block["toolName"] for block in original if block["type"] == "tool_call"] == ["list_dir", "read_file", "grep"]


def test_deliverable_has_nothing_to_record_when_only_thinking_is_left():
    assert native_history.deliverable((blocks("thinking", "list_dir"), None), {"read_file"}, {}, coerce) is None
    assert native_history.deliverable((blocks("list_dir"), None), set(), {}, coerce) is None


def test_deliverable_builds_the_echo_the_way_the_stream_gave_it_to_the_client():
    params = {"read_file": {"n": {"type": "number"}}}

    def numbers(raw, spec):
        return raw.replace('"1"', "1") if spec else raw

    kept, shown = native_history.deliverable((blocks("thinking", "list_dir", "read_file"), None), {"read_file"}, params, numbers)
    assert shown["content"][0]["toolInput"] == {"n": 1} and kept[-1]["toolInput"] == {"n": "1"}  # upstream's own block is kept


# --- the safety net: upstream may refuse a history that lost a call ---------------------------------------------------------

SIGNATURE_REFUSAL = "messages.1.content.0: Invalid `signature` in `thinking` block"


def refusing_second(tmp_path, monkeypatch, scripts, refusals, in_stream=()):
    """The harness of a conversation whose POSTs are answered from `scripts` in turn, except that POST number n for n in
    `refusals` (1-based) is refused with HTTP 400 and that message, or for n also in `in_stream` the way Retool passes a
    provider's refusal on: an error event with the status inside an HTTP 200 stream. Returns (pool, runner, bodies sent,
    refusals made)."""
    pool, runner, _ = harness(tmp_path, monkeypatch, scripts)
    bodies, refused = [], []
    answers = iter(scripts)

    def opener(req, **kwargs):
        bodies.append(json.loads(req.data))
        if len(bodies) in refusals:
            refused.append(len(bodies))
            if len(bodies) in in_stream:
                return response({"event": "error", "statusCode": 400, "isRetryable": False,
                                 "error": {"message": refusals[len(bodies)]}})
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                         io.BytesIO(json.dumps({"message": refusals[len(bodies)]}).encode()))
        return response(*next(answers))

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    return pool, runner, bodies, refused


def two_requests(pool, runner, dialect="chat"):
    """The events of the second request of a conversation: the client has the first answer and sends the tool result."""
    async def scenario():
        messages = copy.deepcopy(FIRST)
        try:
            first = asked(messages, dialect, TOOLS)
            events = await run(runner, first)
            return await run(runner, asked(append_result(messages, first, events, dialect), dialect, TOOLS))
        finally:
            await close(pool)

    return asyncio.run(scenario())


DROPPED = reply(call("c1", "list_dir", {"path": "."}), call("c2", "read_file", {"path": "a"}))


def test_a_refusal_that_blames_the_blocks_is_asked_again_from_the_clients_messages_and_pruning_stops(tmp_path, monkeypatch):
    pool, runner, bodies, refused = refusing_second(
        tmp_path, monkeypatch, [DROPPED, [text("done"), *END]], {2: SIGNATURE_REFUSAL})
    seq = journal.JOURNAL.last_seq
    events = two_requests(pool, runner)
    assert not any(event["type"] == "error" for event in events) and any(e.get("delta") == "done" for e in events)
    assert refused == [2] and len(bodies) == 3
    assert [block["type"] for block in assistant_of(bodies[1])] == ["thinking", "tool_call"]  # the history without the call
    # asked again from what the client sent: its own call, no signature
    assert [(block["type"], block["toolName"]) for block in assistant_of(bodies[2])] == [("tool_call", "read_file")]
    assert direct._PRUNING["refused"] is True
    warn = stored(seq, "native_history.prune_refused")
    assert len(warn) == 1 and warn[0]["level"] == "warn" and warn[0]["data"]["status"] == 400
    assert len(stored(seq, "native_history.cold")) == 1


def test_after_that_a_reply_with_a_dropped_call_is_recorded_as_before(tmp_path, monkeypatch):
    direct._PRUNING["refused"] = True
    pool, runner, sent = harness(tmp_path, monkeypatch, [DROPPED])
    seq = journal.JOURNAL.last_seq

    async def scenario():
        try:
            await run(runner, asked(copy.deepcopy(FIRST), "chat", TOOLS))
            return list(pool._conversations[KEY]._native_history.branches)
        finally:
            await close(pool)

    branches = asyncio.run(scenario())
    assert [block["toolName"] for block in blocks_of(branches[-1].assistant, "tool_call")] == ["list_dir", "read_file"]
    assert not stored(seq, "native_history.pruned")


def test_a_refusal_for_another_reason_is_not_taken_for_the_branch(tmp_path, monkeypatch):
    pool, runner, bodies, refused = refusing_second(
        tmp_path, monkeypatch, [DROPPED, [text("done"), *END]], {2: "prompt is too long: 240000 tokens > 200000 maximum"})
    events = two_requests(pool, runner)
    assert events[-1]["type"] == "error" and refused == [2] and len(bodies) == 2
    assert direct._PRUNING["refused"] is False


def test_a_refusal_that_comes_back_without_the_branch_is_not_taken_for_the_pruning(tmp_path, monkeypatch):
    pool, runner, bodies, refused = refusing_second(
        tmp_path, monkeypatch, [DROPPED, [text("done"), *END]], {2: SIGNATURE_REFUSAL, 3: SIGNATURE_REFUSAL})
    events = two_requests(pool, runner)
    assert events[-1]["type"] == "error" and refused == [2, 3] and len(bodies) == 3
    assert direct._PRUNING["refused"] is False  # the second try failed the same way: the shape was not the cause


def test_a_refusal_inside_the_stream_that_blames_the_blocks_is_asked_again_from_the_clients_messages_too(tmp_path, monkeypatch):
    """Retool passes a provider's refusal on as an error event inside an HTTP 200. The relay marks such an attempt as one whose
    price is unknown before it raises the error, and that mark of its own was taken for the start of an answer: the request was
    not asked again from the client's messages, the client got the 400, and replies went on being cut down that way."""
    pool, runner, bodies, refused = refusing_second(
        tmp_path, monkeypatch, [DROPPED, [text("done"), *END]], {2: SIGNATURE_REFUSAL}, in_stream={2})
    seq = journal.JOURNAL.last_seq
    events = two_requests(pool, runner)
    assert not any(event["type"] == "error" for event in events) and any(e.get("delta") == "done" for e in events)
    assert refused == [2] and len(bodies) == 3
    assert [(block["type"], block["toolName"]) for block in assistant_of(bodies[2])] == [("tool_call", "read_file")]
    assert direct._PRUNING["refused"] is True and len(stored(seq, "native_history.prune_refused")) == 1


def test_a_retry_refused_inside_the_stream_is_not_taken_for_the_pruning(tmp_path, monkeypatch):
    """The retry from the client's messages answered only by the relay's own mark of an unpriced attempt is no answer."""
    pool, runner, bodies, refused = refusing_second(
        tmp_path, monkeypatch, [DROPPED, [text("done"), *END]], {2: SIGNATURE_REFUSAL, 3: SIGNATURE_REFUSAL}, in_stream={3})
    seq = journal.JOURNAL.last_seq
    events = two_requests(pool, runner)
    assert events[-1]["type"] == "error" and refused == [2, 3] and len(bodies) == 3
    assert direct._PRUNING["refused"] is False and not stored(seq, "native_history.prune_refused")


def test_a_branch_that_lost_nothing_is_never_asked_again(tmp_path, monkeypatch):
    pool, runner, bodies, refused = refusing_second(
        tmp_path, monkeypatch, [reply(call("c2", "read_file", {"path": "a"})), [text("done"), *END]], {2: SIGNATURE_REFUSAL})
    events = two_requests(pool, runner)
    assert events[-1]["type"] == "error" and refused == [2] and len(bodies) == 2  # as it always was: no retry on a live branch


def test_the_branches_that_lost_a_call_are_forgotten_and_the_others_stay():
    history = native_history.History()
    scope, client = "s", [{"role": "user", "content": []}]
    history.record(scope, client, [], [], {})
    history.record(scope, client + [{"role": "user", "content": []}], [], [], {}, pruned=True)
    assert len(history.branches) == 2 and len(history.pruned) == 1
    revision = history.revision
    history.forget_pruned()
    assert len(history.branches) == 1 and len(history.pruned) == 0 and history.revision == revision + 1