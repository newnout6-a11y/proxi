"""A client tool named like one of Retool's own tool choices can still be forced.

toolChoice is one string, and "none", "auto" and "required" mean what they say whoever a tool is called: forcing a tool
named "none" asked Retool for no tool at all, "auto" for nothing in particular and "required" for any tool, and the turn
then failed as a tool choice the model had ignored (ultracode audit of the relay, C20). Such a tool is forced the way a
lone tool is: it is the only tool of the request and the choice is "required". A tool with any other name is forced by
name as before, and the three words as plain choices keep their meaning."""
import asyncio
import copy

import pytest

import direct
import validate
from test_client_contracts import CFG, _body
from test_native_context_reuse import FIRST, request, run
from test_native_history import append_result, close, harness
from test_review_fixes_pr19 import END, THINKING, signature, text

WORDS = ["none", "auto", "required"]


def tool(name):
    return {"type": "function", "function": {"name": name, "description": "d", "parameters": {
        "type": "object", "properties": {"x": {"type": "string"}}}}}


def forced(name):
    return {"type": "function", "function": {"name": name}}


def body_for(names, choice):
    parsed = validate.parse_chat(_body("chat", tools=[tool(name) for name in names], tool_choice=choice), CFG)
    return direct.build_body({}, {"model": "m"}, "", "go", direct.tools_to_retool(parsed.tools), parsed.tool_choice, None)


@pytest.mark.parametrize("name", WORDS)
def test_a_forced_tool_named_like_a_choice_is_the_only_tool_and_required(name):
    body = body_for([name, "other"], forced(name))
    assert body["toolChoice"] == "required"
    assert [item["name"] for item in body["tools"]] == [name]
    assert body["rit"] == direct.make_rit(body["messages"], body["tools"])


@pytest.mark.parametrize("name", WORDS)
def test_the_requests_own_tool_list_is_not_cut_by_it(name):
    parsed = validate.parse_chat(_body("chat", tools=[tool(name), tool("other")], tool_choice=forced(name)), CFG)
    tools = direct.tools_to_retool(parsed.tools)
    direct.build_body({}, {"model": "m"}, "", "go", tools, parsed.tool_choice, None)
    assert [item["name"] for item in tools] == sorted([name, "other"])  # the request's own list is not cut


def test_a_tool_with_an_ordinary_name_is_still_forced_by_name_among_all_tools():
    body = body_for(["read_file", "other"], forced("read_file"))
    assert body["toolChoice"] == "read_file"
    assert [item["name"] for item in body["tools"]] == ["other", "read_file"]


@pytest.mark.parametrize("choice,expected", [("none", "none"), ("required", "required"), ("auto", None)])
def test_the_words_as_plain_choices_keep_their_meaning_with_every_tool(choice, expected):
    body = body_for(["none", "other"], choice)
    assert body.get("toolChoice") == expected
    assert [item["name"] for item in body["tools"]] == ["none", "other"]


def reply(name):
    return [*THINKING, signature("sig"), {"event": "content_block_end", "type": "thinking"},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": "c1", "toolName": name},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"x": "1"}'},
            {"event": "content_block_end", "type": "tool_call", "toolCallId": "c1"}, *END]


@pytest.mark.parametrize("name", WORDS)
def test_the_transport_sends_it_that_way_on_the_first_request_and_on_the_resumed_one(tmp_path, monkeypatch, name):
    pool, runner, sent = harness(tmp_path, monkeypatch, [reply(name), [text("done"), *END]])
    tools = [tool(name), tool("other")]

    def forced_request(messages):
        parsed = request(messages, tools=copy.deepcopy(tools))
        parsed.tool_choice = forced(name)
        return parsed

    async def scenario():
        try:
            first = forced_request(copy.deepcopy(FIRST))
            events = await run(runner, first)
            calls = [event for event in events if event["type"] == "tool_call" and event.get("done")]
            assert [call["name"] for call in calls] == [name]
            await run(runner, forced_request(append_result(copy.deepcopy(FIRST), first, events, "chat")))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2
    for body in sent:
        assert body["toolChoice"] == "required" and [item["name"] for item in body["tools"]] == [name]
        assert body["rit"] == direct.make_rit(body["messages"], body["tools"])
    assert sent[1]["messages"][-1]["role"] == "tool"  # the second request continued the signed history
