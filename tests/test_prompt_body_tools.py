"""tools.edits: description edits and hide, in every tool shape of the three dialects."""
import copy

import pytest

import prompt_body
from apierr import ApiError
from test_prompt_body_support import DIALECTS, FakeSnapshot, conversation, parse, profile, tool_objects

SPECS = [("bash", "Run a command."), ("read", "Read a file."), ("webfetch", "Fetch a page.")]
TURNS = [("user", "start"), ("assistant", "ok"), ("user", "more")]


def description(tool, dialect):
    return (tool["function"] if dialect == "chat" else tool).get("description")


def names(tools, dialect):
    return [(t["function"] if dialect == "chat" else t)["name"] for t in tools]


def edit(dialect, *edits, specs=SPECS, turns=TURNS, **extra):
    data = conversation(dialect, turns, tools=specs, **extra)
    result, changes = prompt_body.rewrite(data, dialect, profile(tools=edits), FakeSnapshot())
    return result, changes


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("rule,expected", [
    ({"op": "append", "text": "Ask first."}, "Run a command.\n\nAsk first."),
    ({"op": "prepend", "text": "Careful."}, "Careful.\n\nRun a command."),
    ({"op": "set", "text": "Only this."}, "Only this."),
    ({"op": "replace_text", "find": "command", "with": "shell command"}, "Run a shell command."),
    ({"op": "replace_regex", "pattern": r"(\w+) a (\w+)", "with": r"\2 \1"}, "command Run."),
    ({"op": "replace_regex", "pattern": "RUN", "flags": "i", "with": "Execute"}, "Execute a command."),
])
def test_each_description_operation_edits_the_matching_tool_only(dialect, rule, expected):
    result, changes = edit(dialect, {"tool": "bash", **rule})
    tools = result["tools"]
    assert [description(t, dialect) for t in tools] == [expected, "Read a file.", "Fetch a page."]
    assert names(tools, dialect) == ["bash", "read", "webfetch"]
    original = tool_objects(dialect, SPECS)
    for edited, before in zip(tools, original):
        stripped = copy.deepcopy(edited)
        (stripped["function"] if dialect == "chat" else stripped).pop("description")
        kept = copy.deepcopy(before)
        (kept["function"] if dialect == "chat" else kept).pop("description")
        assert stripped == kept
    assert len(changes) == 1 and changes[0]["scope"] == "tools" and changes[0]["tool"] == "bash"
    assert changes[0]["index"] == 0 and changes[0]["edit"] == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_change_counts_what_the_edit_added_and_removed(dialect):
    _, appended = edit(dialect, {"tool": "bash", "op": "append", "text": "Ask first."})
    assert appended[0]["added_chars"] == len("\n\nAsk first.") and appended[0]["removed_chars"] == 0
    _, replaced = edit(dialect, {"tool": "bash", "op": "replace_text", "find": "Run", "with": "Execute"})
    assert (replaced[0]["removed_chars"], replaced[0]["added_chars"]) == (3, 7)
    _, regex = edit(dialect, {"tool": "bash", "op": "replace_regex", "pattern": r"\w+ a", "with": "X"})
    assert (regex[0]["removed_chars"], regex[0]["added_chars"]) == (len("Run a"), 1)
    _, replaced_all = edit(dialect, {"tool": "bash", "op": "set", "text": "Short."})
    assert (replaced_all[0]["removed_chars"], replaced_all[0]["added_chars"]) == (len("Run a command."), len("Short."))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_tool_name_is_a_case_insensitive_glob(dialect):
    rule = {"op": "append", "text": "Note."}
    for pattern, expected in [("BASH", ["bash"]), ("b?sh", ["bash"]), ("*", ["bash", "read", "webfetch"]),
                              ("re*", ["read"]), ("*FETCH", ["webfetch"]), ("nothing", []), ("bas", [])]:
        result, changes = edit(dialect, {"tool": pattern, **rule})
        assert [c["tool"] for c in changes] == expected, pattern
        assert [description(t, dialect).endswith("Note.") for t in result["tools"]] == [
            name in expected for name in ("bash", "read", "webfetch")]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_rule_may_name_several_globs(dialect):
    result, changes = edit(dialect, {"tool": ["bash", "WEB*", 7, ""], "op": "append", "text": "Note."})
    assert [c["tool"] for c in changes] == ["bash", "webfetch"]
    assert edit(dialect, {"tool": None, "op": "append", "text": "Note."})[1] == []
    assert edit(dialect, {"op": "append", "text": "Note."})[1] == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_edits_apply_in_order_and_later_ones_see_earlier_ones(dialect):
    result, changes = edit(dialect,
                           {"tool": "bash", "op": "replace_text", "find": "Run", "with": "Execute"},
                           {"tool": "*", "op": "append", "text": "Be brief."},
                           {"tool": "bash", "op": "replace_text", "find": "Execute a command.", "with": "Do it."},
                           {"tool": "bash", "op": "prepend", "text": "Rule."})
    assert description(result["tools"][0], dialect) == "Rule.\n\nDo it.\n\nBe brief."
    assert [(c["edit"], c["tool"]) for c in changes] == [(0, "bash"), (1, "bash"), (1, "read"), (1, "webfetch"),
                                                         (2, "bash"), (3, "bash")]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_append_and_prepend_add_nothing_that_is_already_in_the_description(dialect):
    already = [("bash", "Run a command.\n\nAsk first.")]
    for rule in ({"op": "append", "text": "Ask first."}, {"op": "prepend", "text": "Ask first."},
                 {"op": "append", "text": "  Ask first.\n"}):
        result, changes = edit(dialect, {"tool": "bash", **rule}, specs=already)
        assert changes == [] and description(result["tools"][0], dialect) == already[0][1]
    for rule in ({"op": "append", "text": ""}, {"op": "append", "text": "  \n"}, {"op": "prepend"}, {"op": "append"}):
        assert edit(dialect, {"tool": "bash", **rule})[1] == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_tool_without_a_description_gets_the_text_alone(dialect):
    data = conversation(dialect, TURNS, tools=SPECS)
    for tool in data["tools"]:
        (tool["function"] if dialect == "chat" else tool).pop("description")
    result, changes = prompt_body.rewrite(data, dialect, profile(tools=[
        {"tool": "bash", "op": "append", "text": "Ask first."}, {"tool": "read", "op": "set", "text": "Read."},
        {"tool": "webfetch", "op": "replace_text", "find": "x", "with": "y"}]), FakeSnapshot())
    assert [description(t, dialect) for t in result["tools"]] == ["Ask first.", "Read.", None]
    assert [c["tool"] for c in changes] == ["bash", "read"]
    data["tools"][0].update(description=None) if dialect != "chat" else data["tools"][0]["function"].update(
        description=None)
    result, _ = prompt_body.rewrite(data, dialect, profile(tools=[
        {"tool": "bash", "op": "prepend", "text": "Ask first."}]), FakeSnapshot())
    assert description(result["tools"][0], dialect) == "Ask first."


def test_a_chat_tool_without_the_function_wrapper_is_edited_too():
    data = conversation("chat", TURNS, tools=SPECS)
    data["tools"] = tool_objects("responses", SPECS)
    result, changes = prompt_body.rewrite(data, "chat", profile(tools=[
        {"tool": "bash", "op": "append", "text": "Ask first."}, {"tool": "webfetch", "hide": True}]), FakeSnapshot())
    assert [(t["name"], t["description"]) for t in result["tools"]] == [
        ("bash", "Run a command.\n\nAsk first."), ("read", "Read a file.")]
    assert parse("chat", result).tools[0]["function"]["description"] == "Run a command.\n\nAsk first."


def test_a_custom_tool_and_extra_tool_keys_are_edited_in_place():
    data = conversation("responses", TURNS)
    data["tools"] = [{"type": "custom", "name": "apply_patch", "description": "Patch files.",
                      "format": {"type": "grammar", "syntax": "lark", "definition": "start: /x/"}},
                     {"type": "function", "name": "shell", "description": "Run.", "strict": True,
                      "parameters": {"type": "object"}}]
    result, changes = prompt_body.rewrite(data, "responses", profile(tools=[
        {"tool": "apply_patch", "op": "append", "text": "Small patches."}]), FakeSnapshot())
    assert result["tools"][0] == {"type": "custom", "name": "apply_patch",
                                  "description": "Patch files.\n\nSmall patches.", "format": data["tools"][0]["format"]}
    assert result["tools"][1] == data["tools"][1] and len(changes) == 1
    anthropic = conversation("anthropic", TURNS, tools=SPECS)
    anthropic["tools"][1]["cache_control"] = {"type": "ephemeral"}
    result, _ = prompt_body.rewrite(anthropic, "anthropic", profile(tools=[
        {"tool": "read", "op": "append", "text": "Note."}]), FakeSnapshot())
    assert result["tools"][1]["cache_control"] == {"type": "ephemeral"}
    assert list(result["tools"][1]) == list(anthropic["tools"][1])


def namespaced():
    data = conversation("responses", TURNS)
    data["tools"] = [{"type": "function", "name": "shell", "description": "Run.", "parameters": {"type": "object"}},
                     {"type": "namespace", "name": "mcp", "description": "A server.", "tools": [
                         {"type": "function", "name": "search", "description": "Search.", "parameters": {"type": "object"}},
                         {"type": "function", "name": "fetch", "description": "Fetch.", "parameters": {"type": "object"}}]}]
    return data


def test_a_tool_inside_a_namespace_is_matched_by_its_name_or_by_namespace_dot_name():
    data = namespaced()
    for pattern in ("search", "mcp.search", "MCP.*"):
        result, changes = prompt_body.rewrite(data, "responses", profile(tools=[
            {"tool": pattern, "op": "append", "text": "Note."}]), FakeSnapshot())
        nested = result["tools"][1]["tools"]
        assert nested[0]["description"].endswith("Note.")
        assert nested[1]["description"].endswith("Note.") == (pattern == "MCP.*")
        assert result["tools"][0]["description"] == "Run." and result["tools"][1]["description"] == "A server."
        assert {c["tool"] for c in changes} <= {"mcp.search", "mcp.fetch"} and changes[0]["index"] == 1
    parsed = parse("responses", result)
    assert [t["function"]["name"] for t in parsed.tools] == ["shell", "mcp.search", "mcp.fetch"]


def test_hiding_namespaced_tools_removes_an_emptied_namespace_and_nothing_else():
    data = namespaced()
    data["tools"].append({"type": "namespace", "name": "empty", "tools": []})
    result, changes = prompt_body.rewrite(data, "responses", profile(tools=[{"tool": "mcp.search", "hide": True}]),
                                          FakeSnapshot())
    assert [t["name"] for t in result["tools"]] == ["shell", "mcp", "empty"]
    assert [t["name"] for t in result["tools"][1]["tools"]] == ["fetch"]
    result, changes = prompt_body.rewrite(data, "responses", profile(tools=[{"tool": "mcp.*", "hide": True}]),
                                          FakeSnapshot())
    assert [t["name"] for t in result["tools"]] == ["shell", "empty"]
    assert [(c["op"], c["tool"]) for c in changes] == [("hide", "mcp.search"), ("hide", "mcp.fetch")]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_hide_removes_the_tools_and_keeps_the_order_of_the_rest(dialect):
    result, changes = edit(dialect, {"tool": "web*", "hide": True})
    assert names(result["tools"], dialect) == ["bash", "read"]
    assert changes == [{"scope": "tools", "op": "hide", "edit": 0, "index": 2, "tool": "webfetch",
                        "removed_chars": changes[0]["removed_chars"], "added_chars": 0}]
    assert changes[0]["removed_chars"] > len("Fetch a page.")
    result, changes = edit(dialect, {"tool": ["bash", "webfetch"], "hide": True})
    assert names(result["tools"], dialect) == ["read"] and [c["index"] for c in changes] == [0, 2]
    parsed = parse(dialect, result)
    assert [t["function"]["name"] for t in parsed.tools] == ["read"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_hide_may_be_written_as_an_op_too(dialect):
    result, changes = edit(dialect, {"tool": "web*", "op": "hide"}, {"tool": "read", "op": "hide", "hide": True})
    assert names(result["tools"], dialect) == ["bash"]
    assert [(c["op"], c["tool"]) for c in changes] == [("hide", "webfetch"), ("hide", "read")]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_hidden_tool_is_not_edited_by_the_rules_that_follow_and_edits_before_it_still_count(dialect):
    result, changes = edit(dialect, {"tool": "bash", "op": "append", "text": "Note."},
                           {"tool": "bash", "hide": True}, {"tool": "bash", "op": "set", "text": "Never."})
    assert names(result["tools"], dialect) == ["read", "webfetch"]
    assert [(c["op"], c["edit"]) for c in changes] == [("append", 0), ("hide", 1)]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_hiding_every_tool_leaves_an_empty_list_when_nothing_demands_a_call(dialect):
    result, changes = edit(dialect, {"tool": "*", "hide": True}, tool_choice="auto" if dialect != "anthropic"
                           else {"type": "auto"})
    assert result["tools"] == [] and len(changes) == 3
    assert parse(dialect, result).tools == []


CHOICES = {
    "chat": {"type": "function", "function": {"name": "bash"}},
    "responses": {"type": "function", "name": "bash"},
    "anthropic": {"type": "tool", "name": "bash"},
}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_tool_that_tool_choice_names_is_never_hidden(dialect):
    result, changes = edit(dialect, {"tool": "bash", "hide": True}, {"tool": "read", "hide": True},
                           tool_choice=copy.deepcopy(CHOICES[dialect]))
    assert names(result["tools"], dialect) == ["bash", "webfetch"]
    assert changes[0] == {"scope": "tools", "op": "hide", "edit": 0, "index": 0, "tool": "bash", "skipped": True,
                          "reason": "tool_choice names the tool"}
    assert changes[1]["op"] == "hide" and "skipped" not in changes[1]
    assert parse(dialect, result).tool_choice == {"type": "function", "function": {"name": "bash"}}


def test_a_namespaced_tool_named_by_tool_choice_is_kept():
    data = namespaced()
    data["tool_choice"] = {"type": "function", "namespace": "mcp", "name": "search"}
    result, changes = prompt_body.rewrite(data, "responses", profile(tools=[{"tool": "mcp.search", "hide": True}]),
                                          FakeSnapshot())
    assert result == data and changes[0]["reason"] == "tool_choice names the tool"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_tool_the_history_already_called_is_never_hidden(dialect):
    turns = [("user", "start"), ("call", "c1", "webfetch"), ("result", "c1", "page"), ("assistant", "ok"),
             ("user", "more")]
    result, changes = edit(dialect, {"tool": "web*", "hide": True}, {"tool": "read", "hide": True}, turns=turns)
    assert names(result["tools"], dialect) == ["bash", "webfetch"]
    assert changes[0]["skipped"] and changes[0]["reason"] == "the conversation already called the tool"
    assert changes[0]["tool"] == "webfetch" and changes[1]["tool"] == "read" and "skipped" not in changes[1]


def test_calls_in_the_other_shapes_a_history_can_hold_count_as_called():
    legacy = conversation("chat", TURNS, tools=SPECS)
    legacy["messages"].insert(2, {"role": "assistant", "content": None, "function_call": {"name": "read",
                                                                                          "arguments": "{}"}})
    flat = conversation("chat", TURNS, tools=SPECS)
    flat["messages"].insert(2, {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "read"}]})
    custom = conversation("responses", TURNS, tools=SPECS)
    custom["input"].insert(1, {"type": "custom_tool_call", "call_id": "c1", "name": "read", "input": "x"})
    spaced = conversation("responses", TURNS, tools=SPECS)
    spaced["input"].insert(1, {"type": "function_call", "call_id": "c1", "name": "search", "namespace": "mcp",
                               "arguments": "{}"})
    for data, dialect in ((legacy, "chat"), (flat, "chat"), (custom, "responses")):
        result, changes = prompt_body.rewrite(data, dialect, profile(tools=[{"tool": "read", "hide": True}]),
                                              FakeSnapshot())
        assert changes[0]["skipped"] and names(result["tools"], dialect) == ["bash", "read", "webfetch"]
    nested = namespaced()
    nested["input"].insert(1, spaced["input"][1])
    result, changes = prompt_body.rewrite(nested, "responses", profile(tools=[{"tool": "mcp.search", "hide": True},
                                                                              {"tool": "fetch", "hide": True}]),
                                          FakeSnapshot())
    assert [(c["tool"], bool(c.get("skipped"))) for c in changes] == [("mcp.search", True), ("mcp.fetch", False)]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_when_tool_choice_demands_a_call_the_last_tool_stays(dialect):
    choice = {"chat": "required", "responses": "required", "anthropic": {"type": "any"}}[dialect]
    result, changes = edit(dialect, {"tool": "*", "hide": True}, tool_choice=choice)
    assert names(result["tools"], dialect) == ["webfetch"]
    assert [c.get("skipped") for c in changes] == [None, None, True]
    assert changes[2]["reason"] == "tool_choice demands a call and no tool would be left"
    assert parse(dialect, result).tool_choice == "required"
    # With another tool in the way nothing stands in the way of hiding the first ones.
    spare, spare_changes = edit(dialect, {"tool": "bash", "hide": True}, tool_choice=choice)
    assert names(spare["tools"], dialect) == ["read", "webfetch"] and "skipped" not in spare_changes[0]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("tools", [None, [], "none"])
def test_a_body_without_a_usable_tools_list_has_nothing_to_edit(dialect, tools):
    data = conversation(dialect, TURNS)
    if tools is not None:
        data["tools"] = tools
    result, changes = prompt_body.rewrite(data, dialect, profile(tools=[
        {"tool": "*", "op": "append", "text": "x"}, {"tool": "*", "hide": True}]), FakeSnapshot())
    assert result == data and changes == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_tool_rule_that_cannot_run_is_skipped_with_a_reason_and_the_rest_still_apply(dialect):
    result, changes = edit(dialect, {"tool": "bash", "op": "bogus"}, {"tool": "bash", "op": "replace_text", "with": "x"},
                           {"tool": "bash", "op": "replace_regex", "pattern": "("},
                           {"tool": "bash", "op": "replace_regex"}, {"tool": "bash", "op": "drop_tag", "tag": "a"},
                           {"tool": "bash", "op": "append", "text": "Fine."})
    assert [(c["op"], c["edit"], c["reason"]) for c in changes[:-1]] == [
        ("bogus", 0, "unsupported op"), ("replace_text", 1, "missing find"),
        ("replace_regex", 2, "invalid pattern"), ("replace_regex", 3, "missing pattern"),
        ("drop_tag", 4, "unsupported op")]
    assert all(c["skipped"] and "tool" not in c for c in changes[:-1])
    assert changes[-1]["op"] == "append" and description(result["tools"][0], dialect).endswith("Fine.")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_rewritten_tool_set_reaches_the_parser_as_the_client_would_have_sent_it(dialect):
    result, _ = edit(dialect, {"tool": "bash", "op": "set", "text": "Only this."}, {"tool": "read", "hide": True})
    parsed = parse(dialect, result)
    assert [(t["function"]["name"], t["function"]["description"]) for t in parsed.tools] == [
        ("bash", "Only this."), ("webfetch", "Fetch a page.")]
    with pytest.raises(ApiError):
        parse(dialect, dict(result, tool_choice={"type": "function", "function": {"name": "read"}}
                            if dialect == "chat" else ({"type": "tool", "name": "read"} if dialect == "anthropic"
                                                       else {"type": "function", "name": "read"})))
