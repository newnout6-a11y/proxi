"""A JavaScript client echoes a big integer as the double it can hold, and that is still the reply the relay kept.

Claude Code and the AI SDK keep a tool call's input as a parsed object, and JavaScript has no integers past 2**53: the
id 9007199254740993 the model wrote comes back as 9007199254740992. The relay's check compared the exact integers, took
the echo for another reply and sent the whole conversation again, uncached (ultracode audit of the relay, G3.1; offline
reproduction: resume False, mismatch echo:arguments, with a control client that echoed the exact number). Numbers are
compared as doubles now, the way a JSON client can carry them; the numbers that differ as doubles still differ."""
import asyncio
import copy
import json

import pytest

import direct
import native_history
from test_native_context_reuse import FIRST, request, run
from test_native_history import close, harness
from test_review_fixes_pr19 import END, THINKING, signature, text

GET_MSG = {"type": "function", "function": {"name": "get_msg", "parameters": {
    "type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}}


@pytest.mark.parametrize("ours,theirs,same", [
    pytest.param(9007199254740993, 9007199254740992, True, id="2**53+1"),
    pytest.param(1234567890123456789, 1234567890123456768, True, id="19 digits"),
    pytest.param(-9007199254740993, -9007199254740992, True, id="negative"),
    pytest.param(10 ** 22 + 1, 10 ** 22, True, id="1e22+1"),
    pytest.param(1, 1.0, True, id="1 and 1.0"), pytest.param(0, 0.0, True, id="0 and 0.0"),
    pytest.param(0.1, 0.1, True, id="0.1"), pytest.param(2 ** 53, float(2 ** 53), True, id="2**53 as a float"),
    pytest.param(9007199254740993, 9007199254740994, False, id="two doubles apart"),
    pytest.param(9007199254740993, 9007199254740996, False, id="three doubles apart"),
    pytest.param(1, 2, False, id="1 and 2"), pytest.param(0.1, 0.2, False, id="0.1 and 0.2"),
    pytest.param(1.5, 1.5000000000000002, False, id="one ulp"),
    pytest.param(10 ** 400, 10 ** 401, True, id="both infinity to JavaScript"),
    pytest.param(True, 1, False, id="true and 1"), pytest.param(False, 0, False, id="false and 0"),
    pytest.param(1, "1", False, id="1 and '1'"), pytest.param(None, 0, False, id="null and 0")])
def test_numbers_are_compared_as_the_doubles_a_json_client_holds(ours, theirs, same):
    assert native_history.same_json(ours, theirs) is same
    assert native_history.same_json(theirs, ours) is same
    assert native_history.same_json({"n": [ours]}, {"n": [theirs]}) is same


def kept_reply(raw):
    capture = native_history.ReplyCapture()
    capture.feed({"event": "content_block_start", "type": "tool_call", "toolCallId": "toolu_1", "toolName": "get_msg"})
    capture.feed({"event": "content_block_delta", "type": "tool_call", "partialInputs": raw})
    return capture.finish()


@pytest.mark.parametrize("raw,js", [('{"id": "9007199254740993"}', 9007199254740992),
                                    ('{"id": 1234567890123456789}', 1234567890123456768)])
def test_a_javascript_echo_resumes_the_branch_and_so_does_the_exact_one(raw, js):
    params = {"id": {"type": "number", "required": True}}
    blocks = kept_reply(raw)
    expected = native_history.echo(blocks, {"get_msg": params}, direct.coerce_arguments)
    first = native_history.client_messages([{"role": "user", "content": "hi"}], None)
    history = native_history.History()
    history.record("S", first, [{"role": "user"}], blocks, expected)
    exact = expected["content"][0]["toolInput"]["id"]
    assert exact != js  # the premise: the two clients hold different integers

    def client(value):
        return native_history.client_messages([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "get_msg",
                                               "input": {"id": value}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}], None)

    assert history.resume("S", client(js))[0] is not None
    assert history.resume("S", client(exact))[0] is not None
    assert history.mismatch("S", client(js)) == "tool_results"
    assert history.resume("S", client(js + 2048))[0] is None  # a number that is another double is another number


def test_the_next_request_goes_on_from_the_signed_reply_when_the_client_rounds_the_id(tmp_path, monkeypatch):
    reply = [*THINKING, signature("sig-big"), {"event": "content_block_end"},
             {"event": "content_block_start", "type": "tool_call", "toolCallId": "toolu_1", "toolName": "get_msg"},
             {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"id": 9007199254740993}'},
             {"event": "content_block_end"}, *END]
    pool, runner, sent = harness(tmp_path, monkeypatch, [reply, [text("done"), *END]])

    async def scenario():
        try:
            messages = copy.deepcopy(FIRST)
            events = await run(runner, request(messages, dialect="anthropic", tools=[GET_MSG]))
            assert [e["arguments"] for e in events if e["type"] == "tool_call" and e.get("done")] == [
                '{"id": 9007199254740993}']
            messages = messages + [
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "sig-big"},
                                                  {"type": "tool_use", "id": "toolu_1", "name": "get_msg",
                                                   "input": {"id": 9007199254740992}}]},  # what JSON.parse made of it
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}]
            await run(runner, request(messages, dialect="anthropic", tools=[GET_MSG]))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2
    assert "sig-big" in json.dumps(sent[1])  # resumed natively with the signed reply
    replayed = next(m for m in sent[1]["messages"] if m["role"] == "assistant")
    assert next(b for b in replayed["content"] if b["type"] == "tool_call")["toolInput"] == {"id": 9007199254740993}
