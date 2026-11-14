"""The values a tool parameter is restricted to reach the model, and a conversation already running keeps its declaration.

The relay translates client tools to Retool's param shape, whose enum holds strings. It turned {"type": "string",
"enum": [...]} into a plain string, so the model guessed the value and the client's MCP server refused the call
(wrap_up of Claude Desktop: "Invalid option: expected one of "commit"|"create_pr"|..."). A conversation whose signed
history was made with that declaration keeps it: the tools are part of the signed and of the cached prefix."""
import asyncio
import copy
import json

import pytest

import direct
import native_history
from test_native_context_reuse import FIRST, request, run
from test_native_history import append_result, close, harness, strip_cache, tool_reply
from test_review_fixes_pr19 import END, text

WRAP_UP_ACTIONS = ["commit", "create_pr", "merge", "fix_ci", "address_comments", "resolve_conflicts", "verify",
                   "archive", "new_session"]
WRAP_UP = {"type": "function", "function": {"name": "wrap_up", "description": "Finish the session", "parameters": {
    "type": "object", "required": ["actions"], "properties": {"actions": {
        "type": "array", "description": "What to do", "items": {
            "type": "object", "required": ["action"], "properties": {
                "action": {"type": "string", "enum": WRAP_UP_ACTIONS, "description": "The step"},
                "message": {"type": "string"}}}}}}}}
VALID_TYPES = {"literal", "string", "number", "boolean", "null", "array", "object", "enum", "anyOf"}


def convert(schema, required=False):
    return direct.tools_to_retool([{"type": "function", "function": {
        "name": "t", "parameters": {"type": "object", "properties": {"p": schema},
                                    "required": ["p"] if required else []}}}])[0]["params"]["p"]


def test_a_string_enum_reaches_the_model_as_a_retool_enum():
    params = direct.tools_to_retool([WRAP_UP])[0]["params"]
    action = params["actions"]["items"]["properties"]["action"]
    assert action == {"type": "enum", "enum": WRAP_UP_ACTIONS, "description": "The step", "required": True}
    assert params["actions"]["items"]["properties"]["message"] == {"type": "string", "required": False}
    assert params["actions"]["required"] is True and params["actions"]["description"] == "What to do"


def test_const_and_an_anyof_of_literals_become_an_enum():
    assert convert({"type": "string", "const": "fixed"}) == {"type": "enum", "enum": ["fixed"], "required": False}
    assert convert({"anyOf": [{"const": "a"}, {"const": "b"}, {"type": "null"}]}) == {
        "type": "enum", "enum": ["a", "b"], "required": False}
    assert convert({"oneOf": [{"type": "string", "enum": ["x", "y"]}, {"const": "z"}]})["enum"] == ["x", "y", "z"]
    # nullable: Retool's enum holds strings, and a field that may be null may also be left out
    assert convert({"type": ["string", "null"], "enum": ["a", None]}, required=True) == {
        "type": "enum", "enum": ["a"], "required": True}
    # a union that is not of literals stays what it was
    assert convert({"anyOf": [{"type": "string"}, {"type": "null"}]}) == {"type": "string", "required": False}


def test_values_retool_cannot_hold_as_strings_are_named_in_the_description():
    assert convert({"type": "integer", "enum": [1, 2, 3], "description": "Level"}) == {
        "type": "number", "description": "Level Allowed values: 1, 2, 3.", "required": False}
    assert convert({"enum": [True, False]}) == {
        "type": "boolean", "description": "Allowed values: true, false.", "required": False}
    assert convert({"enum": ["a", 1]}) == {"type": "string", "description": 'Allowed values: "a", 1.', "required": False}


def test_every_param_is_of_a_type_retool_accepts_and_an_enum_holds_strings():
    def walk(node):
        assert node["type"] in VALID_TYPES and isinstance(node["required"], bool)
        if node["type"] == "enum":
            assert node["enum"] and all(isinstance(value, str) for value in node["enum"])
        for child in (node.get("properties") or {}).values():
            walk(child)
        if "items" in node:
            walk(node["items"])

    for schema in ({"enum": []}, {"const": None}, {"enum": [None]}, {"type": "string", "enum": "not a list"},
                   {"anyOf": []}, {"anyOf": [{"const": 1}, {"const": "a"}]}, {"type": "array", "items": {"enum": ["a"]}},
                   {"oneOf": "junk"}, {"anyOf": [{"type": "object", "properties": {"q": {"const": "v"}}}]}):
        walk(convert(schema))
    for tool in direct.tools_to_retool([WRAP_UP]):
        for node in tool["params"].values():
            walk(node)


def test_a_tool_without_restricted_values_is_declared_byte_for_byte_as_before():
    # Nothing changes for such tools, not even the order of the keys: their cached prefix stays as it was
    tools = [{"type": "function", "function": {"name": "shell", "description": "Run", "parameters": {
        "type": "object", "required": ["command"], "properties": {
            "command": {"type": "string", "description": "What to run"}, "timeout": {"type": "integer"},
            "env": {"type": "object", "properties": {"k": {"type": ["string", "null"]}}},
            "files": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}}}},
            "free": {}, "ref": {"$ref": "#/x"}}}}}]
    assert json.dumps(direct.tools_to_retool(tools)) == json.dumps(direct.tools_to_retool(tools, legacy=True))


def test_the_legacy_declaration_is_the_one_before_the_change():
    action = direct.tools_to_retool([WRAP_UP], legacy=True)[0]["params"]["actions"]["items"]["properties"]["action"]
    assert action == {"type": "string", "description": "The step", "required": True}


class Session:
    def __init__(self, *tools_sent):
        self._native_history = native_history.History()
        for tools in tools_sent:
            scope = direct.native_scope("k", {"model": "m"}, "head", tools, "auto")
            self._native_history.branches.append(native_history.Snapshot(scope, [], [], [], None))


def test_a_conversation_keeps_the_declaration_its_signed_history_was_made_with():
    current, legacy = direct.tools_to_retool([WRAP_UP]), direct.tools_to_retool([WRAP_UP], legacy=True)
    assert current != legacy
    assert direct.tools_for_session([WRAP_UP], Session(legacy)) == legacy       # made before the change
    assert direct.tools_for_session([WRAP_UP], Session(current)) == current     # made after it
    assert direct.tools_for_session([WRAP_UP], Session()) == current            # a new conversation
    assert direct.tools_for_session([WRAP_UP], None) == current
    other = direct.tools_to_retool([{"type": "function", "function": {"name": "other", "parameters": {}}}])
    assert direct.tools_for_session([WRAP_UP], Session(other)) == current       # the tools changed anyway
    assert direct.tools_for_session([WRAP_UP], Session(current, legacy)) == legacy  # the newest branch decides


ENUM_TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "number"},
                                     "mode": {"type": "string", "enum": ["head", "tail"]}}}}}]


def turn(messages, dialect="chat", key="header:client-a"):
    return request(messages, dialect=dialect, tools=copy.deepcopy(ENUM_TOOLS), key=key)


@pytest.mark.parametrize("dialect", ["chat", "anthropic"])
def test_a_new_conversation_sends_the_enum_upstream(tmp_path, monkeypatch, dialect):
    pool, runner, sent = harness(tmp_path, monkeypatch, [[text("ok"), *END]])

    async def scenario():
        try:
            await run(runner, turn(FIRST, dialect))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert sent[0]["tools"][0]["params"]["mode"] == {"type": "enum", "enum": ["head", "tail"], "required": False}


def test_a_conversation_made_before_the_change_keeps_its_declaration_and_its_signed_history(tmp_path, monkeypatch):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), [text("done"), *END], [text("new"), *END]])
    current = direct._param_schema

    async def scenario():
        try:
            monkeypatch.setattr(direct, "_param_schema", direct._param_schema_v1)   # the relay before the change
            first = turn(FIRST)
            events = await run(runner, first)
            monkeypatch.setattr(direct, "_param_schema", current)                  # restarted on the new code
            await run(runner, turn(append_result(copy.deepcopy(FIRST), first, events, "chat")))
            await run(runner, turn(FIRST, key="header:client-b"))                   # a conversation started afterwards
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert sent[0]["tools"][0]["params"]["mode"] == {"type": "string", "required": False}
    assert sent[1]["tools"] == sent[0]["tools"]                                     # the same declaration...
    assert strip_cache(sent[1]["messages"][:len(sent[0]["messages"])]) == strip_cache(sent[0]["messages"])
    assert sent[1]["messages"][len(sent[0]["messages"])]["content"][0]["signature"] == "opaque-one"  # ...and history
    assert sent[2]["tools"][0]["params"]["mode"]["type"] == "enum"
