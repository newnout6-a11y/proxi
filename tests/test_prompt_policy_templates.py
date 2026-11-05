"""Head templates: variables, escapes, one-pass substitution, tidy blank lines, I5 billing, I9 ownership, I10 structured."""
import asyncio
import json
import random

import pytest

import client_context
import direct
import orchestrator
import prompt_policy as pp
import structured
from test_native_context_reuse import FIRST, request, run, setup
from test_prompt_policy_common import (OPENCODE_SYSTEM, Sources, compile_doc, compose, document, make_store,
                                       plan_for, profile_doc)
from test_service_requests import capture_direct

BILLING = "x-anthropic-billing-header: cc_version=2.1.310.a1b; cc_entrypoint=cli; cch=00000;"
CONTRACT = "[STRUCTURED OUTPUT FORMAT]\nReply with JSON."


def head_of(head, system="SYS", tmp_path=None, sources=None, texts=None, **profile):
    doc = document(texts=texts or {}, profiles={"p": {"head": head, **profile}})
    return compose(doc, tmp_path, system, sources)


# -- the plain template language --------------------------------------------------------------------------------------


def test_the_default_head_template_is_the_client_system(tmp_path):
    doc = document(profiles={"p": {}})
    head, parts, warnings = compose(doc, tmp_path, OPENCODE_SYSTEM)
    assert head == OPENCODE_SYSTEM and [(p.source, p.text) for p in parts] == [("client", OPENCODE_SYSTEM)]
    assert warnings == []


def test_literals_texts_and_the_client_system_are_separate_parts(tmp_path):
    head, parts, _ = head_of("Top.\n\n{{text:rules}}\n\n{{client_system}}\n--", "SYS", tmp_path,
                             texts={"rules": "R1\nR2"})
    assert head == "Top.\n\nR1\nR2\n\nSYS\n--"
    assert [(p.source, p.text) for p in parts] == [
        ("literal", "Top.\n\n"), ("text:rules", "R1\nR2"), ("literal", "\n\n"), ("client", "SYS"), ("literal", "\n--")]
    assert head == "".join(part.text for part in parts)


def test_whitespace_inside_the_braces_is_allowed(tmp_path):
    head, _, _ = head_of("{{ client_system }}|{{  text:t  }}", "S", tmp_path, texts={"t": "T"})
    assert head == "S|T"


def test_a_backslash_escapes_the_braces(tmp_path):
    head, parts, _ = head_of("\\{{client_system}} and {{client_system}} and \\{{text:t}} \\\\{{client_system}}", "S",
                             tmp_path, texts={"t": "T"})
    # a backslash right before {{ escapes it; an earlier backslash stays a backslash
    assert head == "{{client_system}} and S and {{text:t}} \\{{client_system}}"
    assert head == "".join(part.text for part in parts)


def test_a_single_brace_or_a_closing_pair_is_plain_text(tmp_path):
    head, _, _ = head_of("{ {{client_system}} } }} {x}", "S", tmp_path)
    assert head == "{ S } }} {x}"


def test_substitution_is_a_single_pass(tmp_path):
    texts = {"t": "{{client_system}} {{role}} {{text:t}} \\{{x}}"}
    head, _, _ = head_of("{{text:t}}|{{client_system}}", "{{text:t}} {{model}}", tmp_path, texts=texts)
    assert head == "{{client_system}} {{role}} {{text:t}} \\{{x}}|{{text:t}} {{model}}"


def test_a_text_is_literal_text_even_with_braces(tmp_path):
    texts = {"code": "if x {{ y }} then {{unknown}}"}
    compile_doc(document(texts=texts, profiles={"p": {"head": "{{text:code}}"}}), tmp_path)
    head, _, _ = head_of("{{text:code}}", "S", tmp_path, texts=texts)
    assert head == "if x {{ y }} then {{unknown}}"


# -- blank lines left by an empty substitution ------------------------------------------------------------------------


@pytest.mark.parametrize("template, system, texts, expected", [
    ("{{text:e}}\n\n{{client_system}}", "S", {"e": ""}, "S"),
    ("{{client_system}}\n\n{{text:e}}", "S", {"e": ""}, "S"),
    ("{{text:e}}\n\n{{client_system}}\n\n{{text:e}}", "S", {"e": ""}, "S"),
    ("A\n\n{{text:e}}\n\nB", "S", {"e": ""}, "A\n\nB"),
    ("A\n{{text:e}}\nB", "S", {"e": ""}, "A\n\nB"),
    ("A\n\n\n\n{{text:e}}B", "S", {"e": ""}, "A\n\nB"),
    ("A\n\n{{text:e}}\n\n{{text:e}}\n\nB", "S", {"e": ""}, "A\n\nB"),
    ("{{client_system}}\n\n{{text:e}}\n\n{{text:f}}", "S", {"e": "", "f": "F"}, "S\n\nF"),
    ("{{client_system}}\n\n{{text:e}}\n\n{{text:f}}", "S", {"e": "E", "f": ""}, "S\n\nE"),
    ("{{text:e}}\n\n\n\n{{client_system}}", "S", {"e": ""}, "S"),
    ("{{text:e}}{{text:f}}{{client_system}}", "S", {"e": "", "f": ""}, "S"),
    # newlines the user wrote elsewhere, or that a value carries, are not touched
    ("X\n\n\n\nY {{text:e}}", "S", {"e": ""}, "X\n\n\n\nY "),
    ("\n\n{{client_system}}\n\n\n\nZ", "S", {"e": ""}, "\n\nS\n\n\n\nZ"),
    ("{{client_system}}\n\n{{text:e}}", "a\n\n\n\nb", {"e": ""}, "a\n\n\n\nb"),
    ("{{client_system}}\n\n{{text:f}}", "tail\n", {"f": "F"}, "tail\n\n\nF"),
    ("{{text:e}}\n\n{{client_system}}", "\n\nlead", {"e": ""}, "\n\nlead"),
])
def test_blank_lines_around_an_empty_substitution_are_tidied(tmp_path, template, system, texts, expected):
    head, parts, _ = head_of(template, system, tmp_path, texts=texts)
    assert head == expected
    assert head == "".join(part.text for part in parts)


def test_an_empty_client_system_leaves_no_blank_lines(tmp_path):
    head, _, _ = head_of("{{text:r}}\n\n{{client_system}}\n\n{{text:s}}", "", tmp_path, texts={"r": "R", "s": "S"})
    assert head == "R\n\nS"


# -- the variables ---------------------------------------------------------------------------------------------------


def test_roles_come_from_the_user_or_from_the_relay(tmp_path):
    sources = Sources()
    head, parts, _ = head_of("{{role}}|{{native_role}}", tmp_path=tmp_path, sources=sources)
    assert head == "BUILTIN-ROLE|BUILTIN-NATIVE"
    assert [p.source for p in parts] == ["relay:role", "literal", "relay:native_role"]
    doc = document(relay_texts={"role": "MY ROLE", "native_role": ""}, profiles={"p": {"head": "{{role}}|{{native_role}}"}})
    sources = Sources()
    head, _, _ = compose(doc, tmp_path, "SYS", sources)
    assert head == "MY ROLE|"
    assert sources.calls == ["structured"]


def test_runtime_values_come_from_the_plan_input(tmp_path):
    store = make_store(tmp_path, document(profiles={"p": {"head": "{{model}}/{{client}}/{{agent}}/{{purpose}}"}},
                                          default_profile="p"))
    from test_prompt_policy_common import inp
    plan = store.plan(inp(model="gpt-6-sol", client="codex", agent="reviewer", purpose="generation"))
    head, parts, _ = plan.compose_head("SYS", Sources())
    assert head == "gpt-6-sol/codex/reviewer/generation"
    assert {p.source for p in parts} == {"literal"}
    assert plan.input.model == "gpt-6-sol"


def test_a_plan_built_by_hand_has_no_runtime_values(tmp_path):
    head, _, _ = head_of("[{{model}}{{client}}{{agent}}{{purpose}}]", tmp_path=tmp_path)
    assert head == "[]"
    assert plan_for(profile_doc(), tmp_path).input is None


def test_every_part_source_of_the_contract(tmp_path):
    doc = document(texts={"t": "T"}, profiles={"p": {"head": (
        "{{text:t}}|{{client_system_raw}}|{{client_system}}|{{role}}|{{native_role}}|{{relay_task}}|{{local_exec}}|"
        "{{project_context}}|{{exec_reminder}}|{{structured}}"), "client_system": {"edits": [
            {"op": "replace_text", "find": "S", "with": "s"}]}}})
    sources = Sources(client_owned=False, structured=CONTRACT)
    head, parts, warnings = compose(doc, tmp_path, BILLING + "\nSYS", sources)
    assert [p.source for p in parts if p.source != "literal"] == [
        "billing", "text:t", "client", "client_edited", "relay:role", "relay:native_role", "relay:task",
        "relay:local_exec", "relay:project_context", "relay:exec_reminder", "relay:structured"]
    assert head.startswith(BILLING + "\n" + "T|SYS|sYs|BUILTIN-ROLE|BUILTIN-NATIVE|RELAY-TASK|LOCAL-EXEC|")
    assert head.endswith("|PROJECT-CONTEXT|EXEC-REMINDER|" + CONTRACT) and warnings == []
    assert head == "".join(part.text for part in parts)


def test_a_head_is_deterministic(tmp_path):
    doc = document(profiles={"p": {"head": "{{text:a}}{{client_system}}", "client_system": {"edits": [
        {"op": "remove_tag", "tag": "env"}]}}}, texts={"a": "A\n"})
    plan = plan_for(doc, tmp_path)
    first = plan.compose_head(OPENCODE_SYSTEM, Sources())
    for _ in range(5):
        assert plan.compose_head(OPENCODE_SYSTEM, Sources()) == first


def test_compose_head_refuses_a_legacy_plan():
    with pytest.raises(RuntimeError, match="legacy"):
        pp.Plan().compose_head("SYS", Sources())


def test_the_client_system_dropped_by_a_template_is_a_warning(tmp_path):
    head, _, warnings = head_of("own text", "client system", tmp_path)
    assert head == "own text" and any("not part of the head" in line for line in warnings)
    _, _, warnings = head_of("own text", "", tmp_path)
    assert warnings == []
    _, _, warnings = head_of("{{client_system_raw}}", "client", tmp_path)
    assert warnings == []


# -- I9: what only a request the relay owns may contain ----------------------------------------------------------------


@pytest.mark.parametrize("name", pp.RELAY_TASK_VARIABLES)
def test_relay_task_blocks_are_empty_for_a_client_request_and_never_asked_for(tmp_path, name):
    sources = Sources(client_owned=True)
    head, parts, warnings = head_of("{{client_system}}|{{%s}}|" % name, "SYS", tmp_path, sources=sources)
    assert head == "SYS||"
    assert sources.calls == ["structured"]
    assert warnings == ["{{%s}} is empty: the client owns this request (the relay adds no task blocks)" % name]


def test_a_client_request_never_gets_the_relay_task_by_any_template(tmp_path):
    sources = Sources(client_owned=True)
    head, _, warnings = head_of("{{relay_task}}\n\n{{client_system}}\n\n{{exec_reminder}}", "SYS", tmp_path,
                                sources=sources)
    assert head == "SYS" and len(warnings) == 2 and sources.calls == ["structured"]


@pytest.mark.parametrize("name, method", [("relay_task", "relay_task"), ("local_exec", "local_exec"),
                                          ("project_context", "project_context"), ("exec_reminder", "exec_reminder")])
def test_a_relay_owned_request_gets_the_block(tmp_path, name, method):
    sources = Sources(client_owned=False)
    head, parts, warnings = head_of("{{%s}}" % name, "", tmp_path, sources=sources)
    assert head == sources.texts[method] and warnings == []
    assert sources.calls == [method, "structured"]


def test_the_legacy_head_of_a_relay_task_is_the_task_plus_the_reminder(tmp_path):
    sources = Sources(client_owned=False)
    head, _, _ = head_of("{{relay_task}}{{exec_reminder}}", "", tmp_path, sources=sources)
    assert head == "RELAY-TASKEXEC-REMINDER"


def test_sources_are_asked_lazily_and_once(tmp_path):
    sources = Sources(client_owned=False)
    head, _, _ = head_of("{{local_exec}} {{local_exec}} {{role}} {{role}}", "SYS", tmp_path, sources=sources)
    assert head == "LOCAL-EXEC LOCAL-EXEC BUILTIN-ROLE BUILTIN-ROLE"
    assert sources.calls == ["local_exec", "relay_role", "structured"]
    sources = Sources(client_owned=False)
    head_of("{{client_system}}", "SYS", tmp_path, sources=sources)
    assert sources.calls == ["structured"]
    sources = Sources(client_owned=False)
    head_of("{{structured}}", "SYS", tmp_path, sources=sources)
    assert sources.calls == ["structured"]


def test_a_source_that_does_not_say_who_owns_the_request_is_treated_as_client_owned(tmp_path):
    class Bare(Sources):
        def __init__(self):
            super().__init__()
            del self.client_owned

    sources = Bare()
    head, _, _ = head_of("{{relay_task}}|{{client_system}}", "SYS", tmp_path, sources=sources)
    assert head == "|SYS" and sources.calls == ["structured"]


# -- I5: the billing header of Claude Code ----------------------------------------------------------------------------


def build_system_blocks(head):
    body = direct.build_body({"direct_cache": False}, {"model": "m"}, head, "hi", [], "auto", 16)
    return [block["text"] for block in body["messages"][0]["content"]]


def test_the_billing_header_stays_first_and_a_block_of_its_own_in_any_template(tmp_path):
    system = BILLING + "\nYou are Claude Code.\nrest"
    for template in ("{{client_system}}", "{{text:r}}\n\n{{client_system}}", "{{client_system}}\n\n{{text:r}}",
                     "{{text:r}}", "{{client_system_raw}}", "{{text:r}} {{client_system}} {{text:r}}"):
        head, parts, _ = head_of(template, system, tmp_path, texts={"r": "RULES"})
        assert head.startswith(BILLING + "\n"), template
        assert parts[0].source == "billing" and parts[0].text == BILLING + "\n"
        assert head == "".join(part.text for part in parts)
        blocks = build_system_blocks(head)
        assert blocks[0] == BILLING and len(blocks) == 2 and BILLING not in blocks[1]


def test_the_template_renders_from_the_system_without_the_billing_line(tmp_path):
    head, _, _ = head_of("[{{client_system}}][{{client_system_raw}}]", BILLING + "\nBODY", tmp_path)
    assert head == BILLING + "\n[BODY][BODY]"


def test_edits_never_touch_the_billing_header(tmp_path):
    system = BILLING + "\nYou are Claude Code.\n\n# Code comments\nx\n"
    head, _, _ = head_of("{{client_system}}", system, tmp_path, client_system={"edits": [
        {"op": "remove_regex", "pattern": "x-anthropic.*", "flags": ""}, {"op": "replace_text", "find": "cch", "with": "zz"},
        {"op": "remove_section", "heading": "# Code comments"}, {"op": "prepend", "text": "TOP"}]})
    assert head == BILLING + "\nTOP\n\nYou are Claude Code."


def test_the_billing_header_alone_or_with_an_empty_rest(tmp_path):
    for system in (BILLING, BILLING + "\n"):
        head, parts, _ = head_of("{{client_system}}", system, tmp_path)
        assert head == system and [p.source for p in parts] == ["billing"]
    head, _, _ = head_of("{{text:r}}", BILLING, tmp_path, texts={"r": "RULES"})
    assert head == BILLING + "\nRULES"


def test_a_line_that_only_mentions_the_header_is_not_a_header(tmp_path):
    for system in ("lead\n" + BILLING + "\nrest", " " + BILLING + "\nrest", "x-anthropic-billing-header\nrest"):
        head, parts, _ = head_of("{{text:r}}\n\n{{client_system}}", system, tmp_path, texts={"r": "RULES"})
        assert head == "RULES\n\n" + system and parts[0].source == "text:r"


def test_billing_header_and_structured_contract(tmp_path):
    system = BILLING + "\nBODY"
    sources = Sources(structured=CONTRACT)
    head, parts, _ = head_of("{{text:r}}\n\n{{client_system}}", system, tmp_path, sources=sources, texts={"r": "R"})
    assert head == BILLING + "\nR\n\nBODY\n\n" + CONTRACT
    head, _, _ = head_of("{{text:r}}", BILLING, tmp_path, sources=Sources(structured=CONTRACT), texts={"r": "R"})
    assert head == BILLING + "\nR\n\n" + CONTRACT


# -- I10: the structured-output contract is always there --------------------------------------------------------------


def test_the_contract_is_appended_when_the_template_does_not_place_it(tmp_path):
    sources = Sources(structured=CONTRACT)
    head, parts, _ = head_of("{{client_system}}", "SYS", tmp_path, sources=sources)
    assert head == "SYS\n\n" + CONTRACT
    assert [(p.source, p.text) for p in parts] == [("client", "SYS"), ("literal", "\n\n"), ("relay:structured", CONTRACT)]
    head, _, _ = head_of("own text", "SYS", tmp_path, sources=Sources(structured=CONTRACT))
    assert head == "own text\n\n" + CONTRACT


def test_the_contract_stands_alone_when_nothing_else_is_in_the_head(tmp_path):
    head, parts, _ = head_of("{{client_system}}", "", tmp_path, sources=Sources(structured=CONTRACT))
    assert head == CONTRACT and [p.source for p in parts] == ["relay:structured"]


def test_the_contract_goes_where_the_template_puts_it_and_only_there(tmp_path):
    sources = Sources(structured=CONTRACT)
    head, parts, _ = head_of("{{structured}}\n\n{{client_system}}", "SYS", tmp_path, sources=sources)
    assert head == CONTRACT + "\n\nSYS" and head.count(CONTRACT) == 1
    assert sources.calls == ["structured"]
    head, _, _ = head_of("A {{structured}} B {{structured}}", "SYS", tmp_path, sources=Sources(structured=CONTRACT))
    assert head == "A %s B %s" % (CONTRACT, CONTRACT)


def test_no_contract_means_no_tail(tmp_path):
    head, parts, _ = head_of("{{client_system}}", "SYS", tmp_path, sources=Sources(structured=""))
    assert head == "SYS" and len(parts) == 1


# -- structured_contract ----------------------------------------------------------------------------------------------

SCHEMA = {"kind": "json_schema", "name": "answer", "description": "d", "source": "test",
          "schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}
OBJECT = {"kind": "json_object", "source": "test"}


@pytest.mark.parametrize("spec", [SCHEMA, OBJECT, {**SCHEMA, "name": None}])
@pytest.mark.parametrize("tools, choice", [(False, "auto"), (True, "auto"), (True, "none"), (True, "required")])
def test_the_default_contract_is_the_structured_module_not_a_copy(spec, tools, choice):
    expected = structured.instructions(spec, tools=tools, tool_choice=choice)
    assert pp.structured_contract(None, spec, tools=tools, tool_choice=choice) == expected


def test_the_default_contract_follows_the_structured_module(monkeypatch):
    monkeypatch.setattr(structured, "instructions", lambda spec, tools=False, tool_choice="auto": "CHANGED %s" % tools)
    assert pp.structured_contract(None, SCHEMA, tools=True, tool_choice="auto") == "CHANGED True"


def test_no_spec_no_contract(tmp_path):
    assert pp.structured_contract(None, None, tools=False, tool_choice="auto") == ""
    snapshot = compile_doc(document(relay_texts={"structured": "{{default}}"}), tmp_path)
    assert pp.structured_contract(snapshot, None, tools=True, tool_choice="auto") == ""


def test_the_wrapper_places_the_default_the_schema_and_its_name(tmp_path):
    wrapper = "PRE {{schema_name}}: {{schema}}\n{{default}}\nPOST \\{{x}}"
    snapshot = compile_doc(document(relay_texts={"structured": wrapper}), tmp_path)
    default = structured.instructions(SCHEMA, tools=False, tool_choice="auto")
    expected = "PRE answer: " + json.dumps(SCHEMA["schema"], separators=(",", ":")) + "\n" + default + "\nPOST {{x}}"
    assert pp.structured_contract(snapshot, SCHEMA, tools=False, tool_choice="auto") == expected


def test_the_wrapper_of_a_json_object_has_no_name_and_an_open_schema(tmp_path):
    snapshot = compile_doc(document(relay_texts={"structured": "[{{schema_name}}]{{schema}}"}), tmp_path)
    assert pp.structured_contract(snapshot, OBJECT, tools=False, tool_choice="auto") == '[]{"type":"object"}'


def test_the_wrapper_is_filled_in_one_pass(tmp_path):
    snapshot = compile_doc(document(relay_texts={"structured": "{{schema_name}}|{{default}}"}), tmp_path)
    spec = {**SCHEMA, "name": "{{default}}"}
    contract = pp.structured_contract(snapshot, spec, tools=False, tool_choice="auto")
    assert contract.startswith("{{default}}|[STRUCTURED OUTPUT FORMAT]")


@pytest.mark.parametrize("wrapper", ["{{schema_name}}", "Reply with JSON.", "x {{schema_name}} y", "\\{{default}}"])
def test_a_wrapper_that_tells_nothing_of_the_contract_is_refused(tmp_path, wrapper):
    with pytest.raises(pp.PromptConfigError) as raised:
        compile_doc(document(relay_texts={"structured": wrapper}), tmp_path)
    assert raised.value.path == "relay_texts.structured"
    assert "{{default}} or {{schema}}" in raised.value.message


@pytest.mark.parametrize("wrapper", ["{{default}}", "{{schema}}", "{{schema_name}}{{schema}}", "  {{default}}  "])
def test_a_wrapper_with_the_default_or_the_schema_never_comes_out_blank(tmp_path, wrapper):
    snapshot = compile_doc(document(relay_texts={"structured": wrapper}), tmp_path)
    for spec in (SCHEMA, {**SCHEMA, "name": ""}, {**SCHEMA, "name": None}, OBJECT):
        assert pp.structured_contract(snapshot, spec, tools=False, tool_choice="auto").strip()


def test_a_snapshot_without_a_wrapper_gives_the_default(tmp_path):
    snapshot = compile_doc(document(), tmp_path)
    assert pp.structured_contract(snapshot, SCHEMA, tools=True, tool_choice="auto") == structured.instructions(
        SCHEMA, tools=True, tool_choice="auto")


# -- passthrough is the legacy head, byte for byte ---------------------------------------------------------------------

SYSTEMS = {
    "plain": "CLIENT_SYSTEM\nline two",
    "markdown": OPENCODE_SYSTEM,
    "billing": BILLING + "\nYou are Claude Code.\nrest of it",
    "billing-only": BILLING,
    "unicode": "Привет\n\n## Заголовок\nтекст 日本語",
    "trailing newlines": "text\n\n",
    "braces": "use {{client_system}} and {{ x }} literally",
}
SPECS = {"none": None, "schema": SCHEMA, "object": OBJECT}


@pytest.mark.parametrize("dialect", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("name", sorted(SYSTEMS))
@pytest.mark.parametrize("spec", sorted(SPECS))
@pytest.mark.parametrize("with_tools", [True, False], ids=["tools", "text"])
def test_passthrough_equals_the_legacy_standalone_head(tmp_path, monkeypatch, dialect, name, spec, with_tools):
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, exec_policy="off", cookie_values=[])
    sent = capture_direct(monkeypatch)
    parsed = request(FIRST, tools=None if with_tools else [], system=SYSTEMS[name], dialect=dialect)
    parsed.request_purpose = "client-tools" if with_tools else "generation"
    parsed.structured_output = SPECS[spec]
    events = asyncio.run(run(runner, parsed))
    assert not any(event["type"] == "error" for event in events), events
    legacy = sent[0][0]._direct["head"]

    snapshot = compile_doc(document(), tmp_path)
    plan = pp.Plan(rev=1, profile="passthrough", snapshot=snapshot)
    contract = pp.structured_contract(snapshot, parsed.structured_output, tools=bool(parsed.tools),
                                      tool_choice=parsed.tool_choice)
    head, parts, warnings = plan.compose_head(orchestrator._client_system(parsed), Sources(structured=contract))
    assert head == legacy
    assert head == "".join(part.text for part in parts) and warnings == []
    cfg = {"direct_cache": True}
    assert direct.build_body(cfg, {"model": "m"}, head, "x", [], "auto", 8)["messages"][0] == \
        direct.build_body(cfg, {"model": "m"}, legacy, "x", [], "auto", 8)["messages"][0]


def test_passthrough_equals_legacy_for_a_text_job_that_is_only_a_contract(tmp_path, monkeypatch):
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, exec_policy="off", cookie_values=[])
    sent = capture_direct(monkeypatch)
    parsed = request(FIRST, tools=[], system="", dialect="chat")
    parsed.request_purpose = "generation"
    parsed.structured_output = SCHEMA
    asyncio.run(run(runner, parsed))
    snapshot = compile_doc(document(), tmp_path)
    contract = pp.structured_contract(snapshot, SCHEMA, tools=False, tool_choice="auto")
    head, _, _ = pp.Plan(rev=1, profile="passthrough", snapshot=snapshot).compose_head("", Sources(structured=contract))
    assert head == sent[0][0]._direct["head"] == contract


def test_the_client_context_still_sees_the_request_as_before(tmp_path):
    parsed = request(FIRST, system="SYS")
    assert client_context.request_kind(parsed, {"client": "opencode"}) == "client-tools"

# -- properties over random templates --------------------------------------------------------------------------------

POOL = ["{{client_system}}", "{{client_system_raw}}", "{{text:a}}", "{{text:e}}", "{{role}}", "{{native_role}}",
        "{{relay_task}}", "{{local_exec}}", "{{project_context}}", "{{exec_reminder}}", "{{structured}}", "{{model}}",
        "\\{{", "\n", "\n\n", "\n\n\n", " ", "text", "}}", "{", "\\", "{{ client_system }}"]
RANDOM_SYSTEMS = ["", "SYS", BILLING + "\nREST", BILLING, BILLING + "\n", "a\n\n\nb\n", "x\r\ny", "\n\nlead", "{{role}}"]
SOURCE_TEXTS = ["", "T", "t\n\n", "\nn\n"]
VOCABULARY = {"client", "client_edited", "literal", "billing", "relay:role", "relay:native_role", "relay:task",
              "relay:local_exec", "relay:project_context", "relay:exec_reminder", "relay:structured", "text:a", "text:e"}


def test_random_templates_keep_the_contract_of_compose_head(tmp_path):
    chooser = random.Random(20261105)
    checked = 0
    for _ in range(600):
        template = "".join(chooser.choice(POOL) for _ in range(chooser.randint(1, 9)))
        doc = document(texts={"a": "A\n", "e": ""}, profiles={"p": {"head": template, "client_system": {"edits": [
            {"op": "replace_text", "find": "SYS", "with": "sys"}]}}})
        try:
            snapshot = compile_doc(doc, tmp_path)
        except pp.PromptConfigError:
            continue
        system = chooser.choice(RANDOM_SYSTEMS)
        texts = {name: chooser.choice(SOURCE_TEXTS) for name in
                 ("relay_role", "native_role", "relay_task", "local_exec", "project_context", "exec_reminder",
                  "structured")}
        owned = chooser.random() < 0.5
        sources = Sources(client_owned=owned, **texts)
        plan = pp.Plan(rev=1, profile="p", rule="r", snapshot=snapshot)
        head, parts, warnings = plan.compose_head(system, sources)
        checked += 1
        assert head == "".join(part.text for part in parts), template
        assert all(part.text for part in parts) and {part.source for part in parts} <= VOCABULARY, template
        again = plan.compose_head(system, Sources(client_owned=owned, **texts))
        assert again == (head, parts, warnings), template
        spans = pp.part_spans(parts)
        assert all(a["end"] == b["start"] for a, b in zip(spans, spans[1:])) and (not spans or spans[-1]["end"] == len(head))
        first = system.partition("\n")[0]
        if first.startswith("x-anthropic-billing-header:"):
            assert head.startswith(first) and parts[0].source == "billing", template
        else:
            assert not any(part.source == "billing" for part in parts)
        if not owned:
            assert not any(line.endswith("(the relay adds no task blocks)") for line in warnings)
        else:
            assert not {"relay_task", "local_exec", "project_context", "exec_reminder"} & set(sources.calls), template
        assert len(sources.calls) == len(set(sources.calls)), template
        assert not {"relay_role", "native_role"} & set(sources.calls) or "role" in template, template
    assert checked > 300
