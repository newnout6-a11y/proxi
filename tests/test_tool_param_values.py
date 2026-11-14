"""The schema of a client tool reaches the model as the client wrote it: allowed values, optional fields and their types.

The relay translates client tools to Retool's param shape. Three things went wrong there, and Claude Desktop, Claude
Code and the DeepSeek harness paid for each with failed tool calls:
- a string enum came out as a plain string: the model guessed the value (wrap_up: "Invalid option: expected one of
  "commit"|"create_pr"|...");
- every tool was declared strict, and in strict mode Retool makes every parameter required: the model filled the
  optional ones with placeholders (show_pane path "" and tab_id "", update_plan items [], tavily_search a country and an
  end date);
- an optional field written as "X or null" (Pydantic, many MCP servers) became a plain string: the model sent "" for
  the booleans and lists of tavily_search."""
import asyncio
import copy
import json

import pytest

import direct
from test_native_context_reuse import FIRST, request, run
from test_native_history import close, harness
from test_review_fixes_pr19 import END, text

WRAP_UP_ACTIONS = ["commit", "create_pr", "merge", "fix_ci", "address_comments", "resolve_conflicts", "verify",
                   "archive", "new_session"]
WRAP_UP = {"type": "function", "function": {"name": "wrap_up", "description": "Finish the session", "parameters": {
    "type": "object", "required": ["actions"], "properties": {"actions": {
        "type": "array", "description": "What to do", "items": {
            "type": "object", "required": ["label"], "properties": {
                "action": {"type": "string", "enum": WRAP_UP_ACTIONS, "description": "The step"},
                "label": {"type": "string"}}}}}}}}
# The shape Pydantic gives the optional fields of tavily_search
TAVILY = {"type": "function", "function": {"name": "tavily_search", "parameters": {
    "type": "object", "required": ["query"], "properties": {
        "query": {"type": "string"},
        "time_range": {"anyOf": [{"type": "string", "enum": ["day", "week", "month", "year"]}, {"type": "null"}],
                       "default": None, "description": "How far back"},
        "include_images": {"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": None},
        "include_domains": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
        "max_results": {"type": ["null", "integer"]}}}}}
VALID_TYPES = {"literal", "string", "number", "boolean", "null", "array", "object", "enum", "anyOf"}


def convert(schema, required=False):
    return direct.tools_to_retool([{"type": "function", "function": {
        "name": "t", "parameters": {"type": "object", "properties": {"p": schema},
                                    "required": ["p"] if required else []}}}])[0]["params"]["p"]


def test_a_string_enum_reaches_the_model_as_a_retool_enum():
    params = direct.tools_to_retool([WRAP_UP])[0]["params"]
    item = params["actions"]["items"]["properties"]
    assert item["action"] == {"type": "enum", "enum": WRAP_UP_ACTIONS, "description": "The step", "required": False}
    assert item["label"] == {"type": "string", "required": True}
    assert params["actions"]["required"] is True and params["actions"]["description"] == "What to do"


def test_const_and_an_anyof_of_literals_become_an_enum():
    assert convert({"type": "string", "const": "fixed"}) == {"type": "enum", "enum": ["fixed"], "required": False}
    assert convert({"anyOf": [{"const": "a"}, {"const": "b"}, {"type": "null"}]}) == {
        "type": "enum", "enum": ["a", "b"], "required": False}
    assert convert({"oneOf": [{"type": "string", "enum": ["x", "y"]}, {"const": "z"}]})["enum"] == ["x", "y", "z"]
    # nullable: Retool's enum holds strings, and a field that may be null may also be left out
    assert convert({"type": ["string", "null"], "enum": ["a", None]}, required=True) == {
        "type": "enum", "enum": ["a"], "required": True}


def test_values_retool_cannot_hold_as_strings_are_named_in_the_description():
    assert convert({"type": "integer", "enum": [1, 2, 3], "description": "Level"}) == {
        "type": "number", "description": "Level Allowed values: 1, 2, 3.", "required": False}
    assert convert({"enum": [True, False]}) == {
        "type": "boolean", "description": "Allowed values: true, false.", "required": False}
    assert convert({"enum": ["a", 1]}) == {"type": "string", "description": 'Allowed values: "a", 1.', "required": False}


def test_an_optional_field_written_as_x_or_null_keeps_the_type_of_x():
    params = direct.tools_to_retool([TAVILY])[0]["params"]
    assert params["time_range"] == {"type": "enum", "enum": ["day", "week", "month", "year"],
                                    "description": "How far back", "required": False}
    assert params["include_images"] == {"type": "boolean", "required": False}
    assert params["include_domains"] == {"type": "array", "items": {"type": "string", "required": False},
                                         "required": False}
    assert params["max_results"] == {"type": "number", "required": False}   # a type list prefers its non-null type
    assert params["query"] == {"type": "string", "required": True}


def test_another_union_of_types_becomes_a_retool_anyof():
    assert convert({"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                    "description": "One or many"}) == {
        "type": "anyOf", "description": "One or many", "required": False, "anyOf": [
            {"type": "string", "required": False},
            {"type": "array", "items": {"type": "string", "required": False}, "required": False}]}
    # a union inside a typed object is a constraint of that object, not its type
    assert convert({"type": "object", "properties": {"a": {"type": "string"}},
                    "anyOf": [{"required": ["a"]}, {"required": ["b"]}]})["type"] == "object"
    assert convert({"anyOf": [{"type": "string"}, {"type": "null"}]}) == {"type": "string", "required": False}


def test_every_param_is_of_a_type_retool_accepts_and_an_enum_holds_strings():
    def walk(node):
        assert node["type"] in VALID_TYPES and isinstance(node["required"], bool)
        if node["type"] == "enum":
            assert node["enum"] and all(isinstance(value, str) for value in node["enum"])
        for child in (node.get("properties") or {}).values():
            walk(child)
        for child in node.get("anyOf") or []:
            walk(child)
        if "items" in node:
            walk(node["items"])

    for schema in ({"enum": []}, {"const": None}, {"enum": [None]}, {"type": "string", "enum": "not a list"},
                   {"anyOf": []}, {"anyOf": [{"const": 1}, {"const": "a"}]}, {"type": "array", "items": {"enum": ["a"]}},
                   {"oneOf": "junk"}, {"anyOf": [{"type": "object", "properties": {"q": {"const": "v"}}}]},
                   {"anyOf": [{"type": "null"}]}, {"type": ["null"]}, {"anyOf": [{"$ref": "#/x"}, {"type": "null"}]},
                   {"anyOf": [{"anyOf": [{"type": "number"}, {"type": "null"}]}, {"type": "boolean"}]}):
        walk(convert(schema))
    for tool in direct.tools_to_retool([WRAP_UP, TAVILY]):
        for node in tool["params"].values():
            walk(node)


@pytest.mark.parametrize("given,expected", [(None, False), (False, False), (True, True)])
def test_a_tool_is_strict_only_when_the_client_asks(given, expected):
    function = {"name": "pick", "parameters": {"type": "object", "properties": {"a": {"type": "string"}}}}
    if given is not None:
        function["strict"] = given
    assert direct.tools_to_retool([{"type": "function", "function": function}])[0]["strict"] is expected


ENUM_TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "number"},
                                     "mode": {"type": "string", "enum": ["head", "tail"]}}}}}]


@pytest.mark.parametrize("dialect", ["chat", "anthropic"])
def test_upstream_gets_the_enum_and_a_tool_that_is_not_strict(tmp_path, monkeypatch, dialect):
    pool, runner, sent = harness(tmp_path, monkeypatch, [[text("ok"), *END]])

    async def scenario():
        try:
            await run(runner, request(copy.deepcopy(FIRST), dialect=dialect, tools=copy.deepcopy(ENUM_TOOLS)))
        finally:
            await close(pool)

    asyncio.run(scenario())
    tool = sent[0]["tools"][0]
    assert tool["params"]["mode"] == {"type": "enum", "enum": ["head", "tail"], "required": False}
    assert tool["strict"] is False
