"""A dictionary field of a strict tool reaches the model as a string it can fill, and the client gets the dictionary back (item 7, C26).

Retool's param shape has no map. The relay declared a field with `additionalProperties`, `patternProperties` or no properties as an
empty object, and a STRICT tool makes the provider write exactly the empty object for it: through the running relay, gpt-6.1-sol,
asked for the keys of real schemas (Claude Code's TaskCreate and TaskUpdate, Exa's agent_run, Codex's jev tools), wrote `{}`, `null`
or a run of blanks in 30 of 30 requests, in all three dialects, streamed or not (e.g. 11c81934, 59b20d32, 5923bd6b, 0c1586de).
A tool that is not strict is not held to it: the models filled the empty object with the keys they were asked for in 147 of 171
requests on the same schemas (the others: a tool gpt-6.1-sol declines, and fields an older converter wrote as strings), so a tool
that is not strict is declared as before. A strict tool declares each such field as a string whose description says to write a JSON
object in it (what the model fills: 273 of 297 requests on the real schemas, the others a model declining a tool, noise, and a union
with a plain string, below), and coerce_arguments / ArgStream turn the string back into the object before the client sees it.
"""
import copy
import json
import pathlib
import random

import pytest

import direct
import journal
import native_history
from prompt_support import after_tool_call, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_compaction_cache import OPENCODE
from test_review_fixes_pr19 import END, THINKING, signature, text

REAL = json.loads((pathlib.Path(__file__).parent / "real_map_tools.json").read_text(encoding="utf-8"))

# The dictionary fields of the real tools, as paths into the converted params ([] an item of an array).
MAPS = {
    "TaskCreate": ["metadata"],
    "TaskUpdate": ["metadata"],
    "AskUserQuestion": ["annotations", "answers"],
    "mcp__exa__agent_run": ["input.data[]", "input.exclusion[]", "outputSchema"],
    "jev_coding_loop": ["extras"],
    "jev_evaluate": ["questions"],
    "jev_step": ["candidates[].arguments", "extras"],
    "jev_tool_route": ["candidates[].arguments"],
}
# A dictionary that is one kind among several (here string | object | array) stays the object it was.
UNIONS = {"jev_evaluate": ["state<1>"]}

INPUTS = {
    "TaskCreate": {"subject": "Ship the release", "description": "Cut v2.0 and tag it",
                   "metadata": {"owner": "dana", "priority": "high", "ticket": "REL-42", "чек": "да"}},
    "TaskUpdate": {"taskId": "7", "metadata": {"owner": "lee", "ticket": "REL-43", "stale": None}},
    "AskUserQuestion": {"questions": [{"question": "Which database?", "header": "Database", "multiSelect": False,
                                       "options": [{"label": "Postgres", "description": "Relational"},
                                                   {"label": "SQLite", "description": "Embedded"}]}],
                        "answers": {"Which database?": "Postgres"},
                        "annotations": {"Which database?": {"notes": "needs JSONB"}}},
    "mcp__exa__agent_run": {"query": "List three open-source vector databases",
                            "outputSchema": {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}},
                                                                              "year": {"type": "integer"}}, "required": ["names"]},
                            "input": {"data": [{"name": "Qdrant", "stars": 20000}, {"name": "Milvus"}], "exclusion": [{"id": 1}]}},
    "jev_coding_loop": {"task": "fix parser", "observation": "tests fail", "extras": {"ticket": "REL-42", "dry_run": True, "retries": 2}},
    "jev_evaluate": {"state": {"step": 3, "done": False}, "questions": {"q1": {"type": "noul", "instructions": "Is the task done?"}}},
    "jev_step": {"task": "read file", "observation": "need content",
                 "candidates": [{"id": "c1", "name": "read", "description": "Read a file", "effect": "read_only",
                                 "arguments": {"path": "a.txt", "limit": 5, "nested": {"deep": [1, 2]}}},
                                {"id": "c2", "name": "ls", "description": "List", "effect": "read_only", "arguments": {}}],
                 "extras": {"k": "v"}},
    "jev_tool_route": {"task": "read file", "observation": "need content",
                       "candidates": [{"id": "c1", "name": "read", "description": "Read a file", "effect": "read_only",
                                       "arguments": {"path": "a.txt", "limit": 5}}]},
}


def declared(name, strict, dialect="chat"):
    source = REAL[name]
    flag = {"strict": True} if strict else {}
    if dialect == "chat":
        return {"type": "function", "function": {"name": name, "description": source["description"],
                                                 "parameters": copy.deepcopy(source["parameters"]), **flag}}
    if dialect == "anthropic":
        return {"name": name, "description": source["description"], "input_schema": copy.deepcopy(source["parameters"]), **flag}
    return {"type": "function", "name": name, "description": source["description"],
            "parameters": copy.deepcopy(source["parameters"]), **flag}


def converted(name, strict):
    [tool] = direct.tools_to_retool([declared(name, strict)])
    return tool


def paths(node, where="", found=None):
    """{path: "string" | "object"} of the dictionary fields in a converted param: marked strings and empty objects."""
    found = {} if found is None else found
    if direct._marked(node):
        found[where] = "string"
    elif node.get("type") == "object":
        if not node.get("properties"):
            found[where] = "object"
        for key, sub in node.get("properties", {}).items():
            paths(sub, where + "." + key if where else key, found)
    elif node.get("type") == "array":
        paths(node["items"], where + "[]", found)
    elif node.get("type") == "anyOf":
        for number, branch in enumerate(node["anyOf"]):
            paths(branch, where + "<%d>" % number, found)
    return found


def all_paths(tool):
    found = {}
    for name, node in tool["params"].items():
        paths(node, name, found)
    return found


# --- the schema the model is shown --------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(MAPS))
def test_a_tool_that_is_not_strict_declares_every_dictionary_as_the_empty_object_it_always_did(name):
    assert all_paths(converted(name, strict=False)) == {
        path: "object" for path in MAPS[name] + UNIONS.get(name, [])}


@pytest.mark.parametrize("name", list(MAPS))
def test_a_strict_tool_declares_each_dictionary_as_a_string_that_says_what_to_write(name):
    tool = converted(name, strict=True)
    found = all_paths(tool)
    assert {path for path, kind in found.items() if kind == "string"} == set(MAPS[name])
    assert {path for path, kind in found.items() if kind == "object"} == set(UNIONS.get(name, []))

    def descriptions(node, where="", out=None):
        out = {} if out is None else out
        if direct._marked(node):
            out[where] = node["description"]
        for key, sub in (node.get("properties") or {}).items():
            descriptions(sub, where + "." + key if where else key, out)
        if node.get("type") == "array":
            descriptions(node["items"], where + "[]", out)
        return out

    notes = {}
    for key, node in tool["params"].items():
        descriptions(node, key, notes)
    assert set(notes) == set(MAPS[name])
    assert all(note.endswith(direct.MAP_NOTE) for note in notes.values())


def test_the_note_keeps_the_description_of_the_field_in_front_of_it_and_the_field_stays_optional_or_required_as_it_was():
    strict, loose = converted("TaskCreate", True)["params"]["metadata"], converted("TaskCreate", False)["params"]["metadata"]
    assert strict["description"] == loose["description"] + " " + direct.MAP_NOTE
    assert strict["required"] == loose["required"] is False
    assert strict["type"] == "string" and loose["type"] == "object"


@pytest.mark.parametrize("name", list(MAPS))
def test_nothing_but_the_dictionaries_changes_in_a_strict_tool(name):
    def blank(node):
        node = copy.deepcopy(node)
        if direct._marked(node) or (node.get("type") == "object" and not node.get("properties")):
            return {"map": True, "required": node["required"]}
        for key in ("properties",):
            if isinstance(node.get(key), dict):
                node[key] = {k: blank(v) for k, v in node[key].items()}
        if "items" in node:
            node["items"] = blank(node["items"])
        if "anyOf" in node:
            node["anyOf"] = [blank(branch) for branch in node["anyOf"]]
        return node

    strict, loose = converted(name, True), converted(name, False)
    assert strict["strict"] is True and loose["strict"] is False
    assert {k: blank(v) for k, v in strict["params"].items()} == {k: blank(v) for k, v in loose["params"].items()}
    assert strict["name"] == loose["name"] and strict["description"] == loose["description"]


@pytest.mark.parametrize("schema,expected", [
    ({"type": "object", "additionalProperties": {}}, True),
    ({"type": "object", "additionalProperties": {"type": "string"}}, True),
    ({"type": "object", "additionalProperties": True}, True),
    ({"additionalProperties": {"type": "string"}}, True),
    ({"type": "object", "patternProperties": {"^x": {"type": "string"}}}, True),
    ({"type": "object"}, True),
    ({"type": ["object", "null"], "additionalProperties": {}}, True),
    ({"type": "object", "properties": {}, "additionalProperties": {}}, True),
    ({"type": "object", "additionalProperties": False}, False),
    ({"type": "object", "properties": {}, "additionalProperties": False}, False),
    ({"type": "object", "properties": {"a": {"type": "string"}}}, False),
    ({"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": {"type": "string"}}, False),
    ({"type": "string"}, False), ({"type": "array", "items": {}}, False), ({"type": ["string", "object"]}, False), ({}, False)])
def test_what_counts_as_a_dictionary(schema, expected):
    assert direct._is_map(schema) is expected


def test_an_object_with_properties_or_one_that_must_stay_empty_is_not_made_a_string_in_a_strict_tool():
    schema = {"type": "object", "properties": {
        "fixed": {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False},
        "empty": {"type": "object", "additionalProperties": False},
        "free": {"type": "object", "additionalProperties": {"type": "number"}},
        "nullable": {"anyOf": [{"type": "object", "additionalProperties": {}}, {"type": "null"}]}}}
    [tool] = direct.tools_to_retool([{"type": "function", "function": {"name": "t", "parameters": schema, "strict": True}}])
    kinds = {name: ("map" if direct._marked(node) else node["type"]) for name, node in tool["params"].items()}
    assert kinds == {"fixed": "object", "empty": "object", "free": "map", "nullable": "map"}


# --- turning the string back into the dictionary ---------------------------------------------------------------------------

def spec(name):
    return converted(name, strict=True)["params"]


def encode(value, node):
    """The input with each dictionary written as a JSON string, as a model writes it into the declared field."""
    if direct._marked(node):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict) and node.get("type") == "object":
        return {k: encode(v, (node.get("properties") or {}).get(k, {})) for k, v in value.items()}
    if isinstance(value, list) and node.get("type") == "array":
        return [encode(item, node.get("items") or {}) for item in value]
    return value


def written(name):
    return encode(INPUTS[name], {"type": "object", "properties": spec(name)})


@pytest.mark.parametrize("name", list(MAPS))
def test_the_client_gets_the_dictionaries_the_model_wrote_as_strings(name):
    raw = json.dumps(written(name), ensure_ascii=False)
    assert any(isinstance(value, str) for value in json.loads(raw).values()) or name == "jev_evaluate"
    assert json.loads(direct.coerce_arguments(raw, spec(name))) == INPUTS[name]


@pytest.mark.parametrize("name", list(MAPS))
@pytest.mark.parametrize("seed", range(6))
def test_streamed_in_any_pieces_the_arguments_come_out_the_same_as_in_one_piece(name, seed):
    raw = json.dumps(written(name), ensure_ascii=bool(seed % 2))
    rng, stream, out, position = random.Random(seed), direct.ArgStream(spec(name)), [], 0
    while position < len(raw):
        step = rng.choice([1, 2, 3, 7, 19, 64])
        out.append(stream.feed(raw[position:position + step]))
        position += step
    streamed = "".join(out) + stream.finish()
    assert json.loads(streamed) == INPUTS[name] == json.loads(direct.coerce_arguments(raw, spec(name)))


def test_a_dictionary_at_the_top_level_streams_as_soon_as_its_string_closes_and_a_nested_one_waits_for_the_end():
    top = direct.ArgStream(spec("TaskCreate"))
    first = top.feed('{"subject": "a", "metadata": "{\\"k\\": \\"v\\"}", "description": "d"')
    assert '"metadata": {"k": "v"}' in first and first.endswith('"description": "d"')  # not held to the end of the call
    nested = direct.ArgStream(spec("jev_step"))
    assert nested.feed('{"task": "t", "candidates": [{"arguments": "{\\"p\\": 1}"}]}') == ""  # nothing yet
    assert json.loads(nested.finish()) == {"task": "t", "candidates": [{"arguments": {"p": 1}}]}


@pytest.mark.parametrize("name,field,given,expected", [
    ("TaskCreate", "metadata", "", {}),
    ("TaskCreate", "metadata", "   ", {}),
    ("TaskCreate", "metadata", "{}", {}),
    ("TaskCreate", "metadata", " {\"a\": 1} ", {"a": 1}),
    ("TaskCreate", "metadata", "null", None),
    ("TaskCreate", "metadata", {"already": "an object"}, {"already": "an object"}),
    ("TaskCreate", "metadata", None, None),
    ("TaskCreate", "metadata", "owner=dana", "owner=dana"),
    ("TaskCreate", "metadata", "[1, 2]", "[1, 2]"),
    ("TaskCreate", "metadata", "\"just a string\"", "\"just a string\""),
    ("TaskCreate", "metadata", "{\"a\": NaN}", "{\"a\": NaN}"),
    ("TaskCreate", "metadata", "{\"a\": Infinity}", "{\"a\": Infinity}"),
    ("TaskCreate", "metadata", "{\"a\": 1", "{\"a\": 1"),
    ("TaskCreate", "metadata", "{\"a\": \"\u00e9\u4e2d\"}", {"a": "\u00e9\u4e2d"}),
    ("TaskCreate", "metadata", "{\"a\": {\"b\": [1, {\"c\": null}]}}", {"a": {"b": [1, {"c": None}]}}),
])
def test_what_becomes_of_each_kind_of_value_in_a_dictionary_field(name, field, given, expected):
    raw = json.dumps({"subject": "s", field: given}, ensure_ascii=False)
    assert json.loads(direct.coerce_arguments(raw, spec(name)))[field] == expected


def test_a_string_in_a_field_that_is_not_a_dictionary_is_left_alone():
    raw = json.dumps({"subject": "{\"looks\": \"like json\"}", "description": "{}", "metadata": "{\"a\": 1}"})
    got = json.loads(direct.coerce_arguments(raw, spec("TaskCreate")))
    assert got == {"subject": "{\"looks\": \"like json\"}", "description": "{}", "metadata": {"a": 1}}


def test_a_call_of_a_tool_that_is_not_strict_is_not_touched():
    raw = json.dumps({"subject": "s", "metadata": "{\"a\": 1}"})
    assert direct.coerce_arguments(raw, converted("TaskCreate", False)["params"]) == raw


def test_a_dictionary_in_a_union_with_a_plain_string_passes_through_as_the_model_wrote_it():
    for value in ({"step": 3}, "plain text", ["a"]):
        raw = json.dumps({"state": value, "questions": "{}"})
        got = json.loads(direct.coerce_arguments(raw, spec("jev_evaluate")))
        assert got["state"] == value and got["questions"] == {}


def test_the_dictionaries_of_an_array_of_objects_are_decoded_item_by_item():
    raw = json.dumps({"task": "t", "observation": "o", "candidates": [
        {"id": "a", "arguments": "{\"x\": 1}"}, {"id": "b", "arguments": "not json"}, {"id": "c", "arguments": {"y": 2}}]})
    got = json.loads(direct.coerce_arguments(raw, spec("jev_step")))
    assert [item["arguments"] for item in got["candidates"]] == [{"x": 1}, "not json", {"y": 2}]


# --- through the relay: three dialects, streamed or not ---------------------------------------------------------------------

def upstream_call(call_id, name, arguments, pieces=5):
    """What Retool streams for a signed answer that calls one tool, its arguments in `pieces` parts."""
    raw = json.dumps(arguments, ensure_ascii=False)
    step = max(1, len(raw) // pieces)
    return [*THINKING, signature("sig-one"), {"event": "content_block_end", "type": "thinking"},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": name},
            *({"event": "content_block_delta", "type": "tool_call", "partialInputs": raw[i:i + step]}
              for i in range(0, len(raw), step)),
            {"event": "content_block_end", "type": "tool_call", "toolCallId": call_id}, *END]


def sse(raw):
    events, name = [], None
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    return events


def calls_of(dialect, answer, stream):
    """[(name, the arguments text as the client assembles it)] of the answer of the relay."""
    if not stream:
        if dialect == "chat":
            return [(c["function"]["name"], c["function"]["arguments"]) for c in answer["choices"][0]["message"]["tool_calls"]]
        if dialect == "anthropic":
            return [(b["name"], json.dumps(b["input"])) for b in answer["content"] if b["type"] == "tool_use"]
        return [(i["name"], i["arguments"]) for i in answer["output"] if i["type"] == "function_call"]
    found = {}
    for event in sse(answer):
        if dialect == "chat":
            for call in (event.get("choices") or [{}])[0].get("delta", {}).get("tool_calls") or []:
                slot = found.setdefault(call["index"], ["", ""])
                slot[0] = (call.get("function") or {}).get("name") or slot[0]
                slot[1] += (call.get("function") or {}).get("arguments") or ""
        elif dialect == "anthropic":
            if event.get("type") == "content_block_start" and event["content_block"]["type"] == "tool_use":
                found[event["index"]] = [event["content_block"]["name"], ""]
            elif event.get("type") == "content_block_delta" and event["delta"]["type"] == "input_json_delta":
                found[event["index"]][1] += event["delta"]["partial_json"]
        else:
            if event.get("type") == "response.output_item.added" and event["item"]["type"] == "function_call":
                found[event["item"]["id"]] = [event["item"]["name"], ""]
            elif event.get("type") == "response.function_call_arguments.delta":
                found[event["item_id"]][1] += event["delta"]
            elif event.get("type") == "response.function_call_arguments.done":
                assert found[event["item_id"]][1] == event["arguments"]  # what was streamed is what the done event says
    return [tuple(slot) for slot in found.values()]


def ask(relay, dialect, name, *, strict=True, stream=False, messages=None, script=None):
    tool = declared(name, strict, dialect)
    messages = messages or [{"role": "user", "content": "go"}]
    relay.scripts.append(script if script is not None else upstream_call("call-1", name, written(name) if strict else INPUTS[name]))
    body = {"model": "claude-opus-5-5", "stream": stream, "tools": [tool]}
    if dialect == "chat":
        body["messages"] = messages
    elif dialect == "anthropic":
        body.update(messages=messages, max_tokens=4096)
    else:
        body["input"] = messages
    status, answer, headers = relay.post(dialect, body, OPENCODE)  # one conversation: the header names it
    assert status == 200, answer
    relay.settle(headers)
    return answer


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "streamed"])
@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
@pytest.mark.parametrize("name", list(MAPS))
def test_the_client_receives_the_dictionaries_intact_in_every_dialect(prompt_relay, name, dialect, stream):
    relay = prompt_relay()
    answer = ask(relay, dialect, name, stream=stream)
    [(called, arguments)] = calls_of(dialect, answer, stream)
    assert called == name and json.loads(arguments) == INPUTS[name]
    sent = relay.posts[-1]["tools"][0]["params"]
    assert {path for path, kind in all_paths({"params": sent}).items() if kind == "string"} == set(MAPS[name])


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_a_tool_that_is_not_strict_goes_to_retool_as_before_and_its_call_reaches_the_client_as_the_model_wrote_it(prompt_relay, dialect):
    relay = prompt_relay()
    answer = ask(relay, dialect, "TaskCreate", strict=False)
    [(_, arguments)] = calls_of(dialect, answer, False)
    assert json.loads(arguments) == INPUTS["TaskCreate"]
    metadata = relay.posts[-1]["tools"][0]["params"]["metadata"]
    assert metadata["type"] == "object" and not metadata["properties"] and direct.MAP_NOTE not in metadata.get("description", "")
    assert relay.posts[-1]["tools"][0]["strict"] is False


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "streamed"])
def test_a_model_that_writes_the_object_itself_or_text_that_is_no_json_still_gets_an_answer(prompt_relay, stream):
    relay = prompt_relay()
    first = ask(relay, "chat", "TaskCreate", stream=stream, script=upstream_call("call-1", "TaskCreate", {
        "subject": "s", "description": "d", "metadata": {"owner": "dana"}}))
    assert json.loads(calls_of("chat", first, stream)[0][1])["metadata"] == {"owner": "dana"}
    second = ask(relay, "chat", "TaskCreate", stream=stream, script=upstream_call("call-2", "TaskCreate", {
        "subject": "s", "description": "d", "metadata": "owner is dana"}))
    assert json.loads(calls_of("chat", second, stream)[0][1])["metadata"] == "owner is dana"


# --- the conversation goes on from the kept reply ---------------------------------------------------------------------------

@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
@pytest.mark.parametrize("name", ["TaskCreate", "AskUserQuestion", "jev_step"])
def test_the_clients_echo_of_the_call_matches_the_kept_reply_so_the_history_resumes(prompt_relay, name, dialect):
    relay = prompt_relay()
    messages = [{"role": "user", "content": "go"}]
    first = ask(relay, dialect, name, messages=messages)
    messages = after_tool_call(dialect, messages, first)  # the client puts the call it received, dictionaries as objects, back
    echoed = json.loads(calls_of(dialect, first, False)[0][1])
    assert echoed == INPUTS[name]
    mark = relay.mark()
    ask(relay, dialect, name, messages=messages, script=[text("done"), *END])
    assert len(relay.events(mark, "native_history.resume")) == 1
    assert not relay.events(mark, "native_history.reset") and not relay.events(mark, "native_history.cold")
    # upstream gets its own reply back unchanged: the dictionaries as the strings it wrote
    replay = [m for m in relay.posts[-1]["messages"] if m["role"] == "assistant"][-1]["content"]
    call = next(block for block in replay if block["type"] == "tool_call")
    assert call["toolInput"] == written(name) and any(isinstance(v, str) for v in call["toolInput"].values())
    assert any(block["type"] == "thinking" and block["signature"] == "sig-one" for block in replay)


def test_what_the_relay_keeps_of_the_reply_and_what_it_says_the_client_has_agree_through_the_echo():
    blocks = [{"type": "tool_call", "toolCallId": "c1", "toolName": "TaskCreate", "toolInput": written("TaskCreate")}]
    echo = native_history.echo(blocks, {"TaskCreate": spec("TaskCreate")}, direct.coerce_arguments)
    assert echo["content"][0]["toolInput"] == INPUTS["TaskCreate"] and blocks[0]["toolInput"] == written("TaskCreate")
