"""A strict tool keeps the null its schema allows, so the model is not forced to invent a value.

OpenAI's strict mode lists every field as required and writes the optional ones as "X or null". Retool's strict mode makes
every parameter required as well, and the relay declared such a field as plain X: the model had to give a value, and gave
a placeholder (live, 2026-11-06, as an ordinary client of the relay: claude-haiku-4-5 sent max_results 10, order "desc" and
since "1970-01-01" for a search where the user wanted none of them, and gpt-5-mini refused to call the tool at all: "the
search tool requires values for max_results, order and since"). Declared as an anyOf with a null branch, which Retool
accepts, both answered null (ultracode audit of the relay, C7). A tool that is not strict is declared as before: its optional
field can simply be left out."""
import asyncio
import copy
import json

import pytest

import direct
from test_native_context_reuse import FIRST, request, run
from test_native_history import close, harness
from test_review_fixes_pr19 import END

NULL = {"type": "null", "required": False}


def tool(properties, required=None, strict=True, name="search"):
    function = {"name": name, "description": "Search", "parameters": {
        "type": "object", "required": required if required is not None else list(properties), "properties": properties}}
    if strict is not None:
        function["strict"] = strict
    return {"type": "function", "function": function}


def params(properties, **kwargs):
    return direct.tools_to_retool([tool(properties, **kwargs)])[0]["params"]


NULLABLE = {
    "query": {"type": "string"},
    "max_results": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    "since": {"type": ["string", "null"], "description": "Start date"},
    "order": {"type": ["string", "null"], "enum": ["asc", "desc", None]},
    "tags": {"oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}}


def test_a_strict_tool_keeps_the_null_of_each_field_that_allows_it():
    got = params(NULLABLE)
    assert got["query"] == {"type": "string", "required": True}
    assert got["max_results"] == {"type": "anyOf", "required": True, "anyOf": [
        {"type": "number", "required": False}, NULL]}
    assert got["since"] == {"type": "anyOf", "required": True, "description": "Start date", "anyOf": [
        {"type": "string", "required": False}, NULL]}
    assert got["order"] == {"type": "anyOf", "required": True, "anyOf": [
        {"type": "enum", "enum": ["asc", "desc"], "required": False}, NULL]}
    assert got["tags"] == {"type": "anyOf", "required": True, "anyOf": [
        {"type": "array", "items": {"type": "string", "required": False}, "required": False}, NULL]}


def test_it_keeps_the_required_flag_the_client_wrote_and_the_strict_flag():
    converted = direct.tools_to_retool([tool(NULLABLE, required=["query"])])[0]
    assert converted["strict"] is True
    assert converted["params"]["max_results"]["required"] is False and converted["params"]["query"]["required"] is True


def test_a_union_with_null_gets_one_null_branch_beside_its_own_branches():
    got = params({"a": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                        "description": "One or many"}})["a"]
    assert got["type"] == "anyOf" and got["description"] == "One or many" and got["required"] is True
    assert [branch["type"] for branch in got["anyOf"]] == ["string", "array", "null"]


def test_the_null_is_kept_at_every_depth_of_the_schema():
    got = params({"filters": {"type": "object", "required": ["author", "year"], "properties": {
        "author": {"type": "string"}, "year": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "ids": {"type": "array", "items": {"type": ["integer", "null"]}}}}})["filters"]
    assert got["properties"]["author"] == {"type": "string", "required": True}
    assert got["properties"]["year"] == {"type": "anyOf", "required": True, "anyOf": [
        {"type": "number", "required": False}, NULL]}
    assert got["properties"]["ids"]["items"] == {"type": "anyOf", "required": False, "anyOf": [
        {"type": "number", "required": False}, NULL]}


def test_a_field_that_is_only_null_and_a_field_that_is_never_null_are_left_alone():
    got = params({"nothing": {"type": "null"}, "plain": {"type": "integer"}, "words": {"enum": ["a", "b"]}})
    assert got["nothing"] == {"type": "null", "required": True}
    assert got["plain"] == {"type": "number", "required": True}
    assert got["words"] == {"type": "enum", "enum": ["a", "b"], "required": True}


@pytest.mark.parametrize("strict", [False, None])
def test_a_tool_that_is_not_strict_is_declared_as_before(strict):
    got = params(NULLABLE, strict=strict, required=["query"])
    assert got["max_results"] == {"type": "number", "required": False}
    assert got["since"] == {"type": "string", "description": "Start date", "required": False}
    assert got["order"] == {"type": "enum", "enum": ["asc", "desc"], "required": False}


def test_the_strictness_of_one_tool_does_not_leak_into_the_next():
    converted = direct.tools_to_retool([tool(NULLABLE, name="a"), tool(NULLABLE, strict=False, name="b"),
                                        tool(NULLABLE, name="c")])
    assert [item["params"]["since"]["type"] for item in converted] == ["anyOf", "string", "anyOf"]


def test_a_string_where_a_nullable_number_goes_is_still_repaired_and_the_word_null_is_null():
    spec = params({"n": {"anyOf": [{"type": "integer"}, {"type": "null"}]}, "b": {"type": ["boolean", "null"]},
                   "u": {"anyOf": [{"type": "number"}, {"type": "string"}, {"type": "null"}]}})
    assert json.loads(direct.coerce_arguments('{"n": "3", "b": "true", "u": "007"}', spec)) == {
        "n": 3, "b": True, "u": "007"}  # a union of two types is not guessed at
    assert json.loads(direct.coerce_arguments('{"n": "null", "b": "null"}', spec)) == {"n": None, "b": None}
    assert json.loads(direct.coerce_arguments('{"n": null, "b": null}', spec)) == {"n": None, "b": None}
    stream = direct.ArgStream(spec)
    assert stream.feed('{"n": "3", "b": "f') + stream.feed('alse"}') + stream.finish() == '{"n": 3, "b": false}'


SEARCH = tool({"query": {"type": "string"}, "max_results": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
               "since": {"type": ["string", "null"]}})


def test_the_declaration_that_goes_upstream_has_the_null_branches_and_the_nulls_come_back_whole(tmp_path, monkeypatch):
    reply = [{"event": "content_block_start", "type": "tool_call", "toolCallId": "s1", "toolName": "search"},
             {"event": "content_block_delta", "type": "tool_call",
              "partialInputs": '{"query": "cats", "max_results": null, "since": null}'},
             {"event": "content_block_end", "type": "tool_call", "toolCallId": "s1"}, *END]
    pool, runner, sent = harness(tmp_path, monkeypatch, [reply])

    async def scenario():
        try:
            return await run(runner, request(copy.deepcopy(FIRST), tools=[SEARCH]))
        finally:
            await close(pool)

    events = asyncio.run(scenario())
    declared = sent[0]["tools"][0]
    assert declared["strict"] is True
    assert declared["params"]["max_results"]["anyOf"][1] == NULL and declared["params"]["since"]["anyOf"][1] == NULL
    assert declared["params"]["query"] == {"type": "string", "required": True}
    done = [event["arguments"] for event in events if event["type"] == "tool_call" and event.get("done")]
    assert json.loads(done[0]) == {"query": "cats", "max_results": None, "since": None}
