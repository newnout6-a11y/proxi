"""The legacy Chat functions are refused clearly, and a history written in them is not dropped.

`functions` and `function_call` are the deprecated form of `tools` and `tool_choice`: the model is offered the functions,
and its call comes back as message.function_call. The relay accepted them and dropped them: the model got no tools, the
client got a plain reply, and the request was paid for (ultracode audit of the relay, C11; offline reproduction:
tools [], tool_choice auto, no x-proxi-ignored-parameters). Like every other control the route cannot honour, it is now a
422 that says what to send instead. A history that holds the legacy shapes (an assistant message with function_call, a
"function" message) is read, not refused: the call no longer vanishes from it."""
import json

import pytest

import apierr
import native_history
import validate
from test_client_contracts import CFG, _body

LS = {"name": "ls", "description": "list a directory", "parameters": {"type": "object", "properties": {}}}


def chat(**extra):
    return validate.parse_chat(_body("chat", **extra), CFG)


@pytest.mark.parametrize("extra,param", [
    ({"functions": [LS]}, "functions"),
    ({"functions": [LS], "function_call": "auto"}, "functions"),
    ({"functions": [LS], "function_call": {"name": "ls"}}, "functions"),
    ({"function_call": {"name": "ls"}}, "function_call"),
    ({"function_call": "required"}, "function_call"),
    ({"functions": "PRIVATE"}, "functions")])
def test_the_legacy_function_calling_of_chat_is_refused_with_what_to_send_instead(extra, param):
    with pytest.raises(apierr.ApiError) as error:
        chat(**extra)
    assert error.value.status == 422 and error.value.param == param
    assert "'tools'" in error.value.message and "'tool_choice'" in error.value.message
    assert "PRIVATE" not in error.value.message


@pytest.mark.parametrize("extra", [{}, {"functions": []}, {"functions": None}, {"function_call": None}])
def test_a_request_that_offers_no_function_is_as_it_was(extra):
    parsed = chat(**extra)
    assert parsed.tools == [] and parsed.tool_choice == "auto" and parsed.declared_ignored == []


@pytest.mark.parametrize("choice", ["auto", "none"])
def test_a_function_call_choice_with_no_function_to_call_is_named_as_ignored_and_routine(choice):
    parsed = chat(function_call=choice)
    assert parsed.declared_ignored == ["function_call"] and parsed.routine_ignored == ["function_call"]


def test_the_other_dialects_do_not_read_the_legacy_names():
    tools = [{"type": "function", "name": "read", "parameters": {"type": "object"}}]
    assert validate.parse_responses(_body("responses", tools=tools, functions=[LS]), CFG).declared_ignored == []
    assert validate.parse_anthropic(_body("anthropic", functions=[LS]), CFG).declared_ignored == []


def test_the_route_answers_a_legacy_request_with_a_422_and_does_not_start_a_turn(relay):
    status, body = relay.json_any("/v1/chat/completions", _body("chat", functions=[LS], function_call="auto"))
    assert status == 422 and body["error"]["param"] == "functions" and "'tools'" in body["error"]["message"]
    assert relay.fake.seen == []


LEGACY_HISTORY = [
    {"role": "user", "content": "q"},
    {"role": "assistant", "content": None, "function_call": {"name": "ls", "arguments": '{"path": "."}'}},
    {"role": "function", "name": "ls", "content": "a b"}]
MODERN_HISTORY = [
    {"role": "user", "content": "q"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "ls", "type": "function", "function": {"name": "ls", "arguments": '{"path": "."}'}}]},
    {"role": "tool", "tool_call_id": "ls", "content": "a b"}]
TOOLS = [{"type": "function", "function": LS}]


def test_the_call_in_a_legacy_history_is_on_the_text_bridge_and_in_the_native_view():
    legacy = chat(messages=LEGACY_HISTORY, tools=TOOLS)
    assert legacy.message_chunks[1] == 'Assistant: \n[tool_call ls id=ls: {"path": "."}]'
    assert legacy.message_chunks[2] == "[tool result ls id=ls]: a b"
    assert legacy.native_messages == chat(messages=MODERN_HISTORY, tools=TOOLS).native_messages
    assert legacy.native_messages[1]["content"] == [
        {"type": "tool_call", "toolCallId": "ls", "toolName": "ls", "toolInput": {"path": "."}}]


def test_a_legacy_call_beside_calls_of_the_modern_shape_loses_none_of_them():
    messages = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": "x", "function_call": {"name": "ls", "arguments": "{}"},
                 "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "cat", "arguments": "{}"}}]}]
    chunks = chat(messages=messages, tools=TOOLS).message_chunks
    assert "[tool_call cat id=c2: {}]" in json.dumps(chunks) and "[tool_call ls id=ls: {}]" in json.dumps(chunks)


def test_a_function_message_for_a_call_that_has_no_id_is_matched_by_its_name_only_when_it_is_a_function_message():
    with_name = [{"role": "user", "content": "q"}, {"role": "tool", "name": "ls", "content": "a b"}]
    assert native_history.client_messages(with_name, None) is None  # a tool message still needs its call id
