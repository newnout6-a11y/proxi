"""Review fixes of the admin side of prompt control: what GET /admin/api/prompts publishes for the screen."""
import pytest

import prompt_policy
from admin_prompts_harness import DOC, api, get, put  # noqa: F401 - `api` is a fixture
from test_admin import admin_relay  # noqa: F401 - the relay the api fixture builds on


# -- R2-05: the form can offer "hide a tool" ----------------------------------------------------------------------------
def test_the_tool_operations_the_form_offers_include_hiding_a_tool(api):
    ops = get(api)[1]["builtin"]["edit_ops"]["tools"]
    assert ops == [*prompt_policy.TOOL_EDIT_OPS, "hide"]
    assert len(ops) == len(set(ops))


def test_hide_is_listed_once_even_when_the_engine_names_it_itself(api, monkeypatch):
    monkeypatch.setattr(prompt_policy, "TOOL_EDIT_OPS", (*prompt_policy.TOOL_EDIT_OPS, "hide"))
    assert get(api)[1]["builtin"]["edit_ops"]["tools"].count("hide") == 1


def test_a_document_that_hides_a_tool_is_one_the_engine_accepts(admin_relay):
    document = {"version": 1, "profiles": {"quiet": {"head": "{{client_system}}",
                                                     "tools": {"edits": [{"tool": "bash", "hide": True}]}}}}
    status, body, _ = put(admin_relay, document)
    assert status == 200, body


# -- R2-07: the warnings of the engine reach the screen when it opens, not only after a save --------------------------------
ENGINE_WARNING = "profiles.mine hides a tool the client may need: bash"


def test_the_state_lists_the_warnings_of_the_engine_after_those_of_the_document(api):
    document = {**DOC, "protected": {"enabled": False}}
    api.store.snapshot_warnings = [ENGINE_WARNING]
    status, body, _ = put(api, document)
    assert status == 200
    state = get(api)[1]
    assert state["warnings"] == body["warnings"]
    assert len(state["warnings"]) == 2 and "protected.enabled" in state["warnings"][0]
    assert state["warnings"][1] == ENGINE_WARNING


def test_a_warning_the_document_and_the_engine_both_give_is_listed_once(api):
    api.store.hold({**DOC, "protected": {"enabled": False}})
    first = get(api)[1]["warnings"]
    api.store.snap.warnings = [first[0], ENGINE_WARNING, ENGINE_WARNING]
    assert get(api)[1]["warnings"] == [first[0], ENGINE_WARNING]


def test_the_state_has_no_warnings_for_a_clean_revision_and_for_none(api):
    assert get(api)[1]["warnings"] == []
    api.store.hold(DOC)
    assert get(api)[1]["warnings"] == []


def test_the_real_engine_warns_about_a_hidden_tool_in_the_state_as_in_the_save(admin_relay):
    document = {"version": 1, "profiles": {"quiet": {"head": "{{client_system}}",
                                                     "tools": {"edits": [{"tool": "bash", "hide": True}]}}}}
    status, saved, _ = put(admin_relay, document)
    assert status == 200 and any("hides a tool" in line for line in saved["warnings"])
    state = get(admin_relay)[1]
    assert state["warnings"] == saved["warnings"] and state["rev"] == saved["rev"]


# -- R2-10: the rule for the name of a text is published like the one for a profile ----------------------------------------
def test_the_name_rule_of_a_text_is_the_engines(api):
    builtin = get(api)[1]["builtin"]
    assert builtin["text_name"] == prompt_policy.TEXT_NAME.pattern
    assert builtin["profile_name"] == prompt_policy.PROFILE_NAME.pattern


@pytest.mark.parametrize("name,fits", [
    ("my_rules", True), ("a", True), ("Правила-1.2", True), ("x" * 64, True), ("_x", True),
    ("rules#1", False), ("a/b", False), ("", False), ("x" * 65, False), (".x", False), ("-x", False),
    ("a b", False), ("a:b", False)])
def test_what_the_published_rule_lets_through_is_what_the_engine_accepts(admin_relay, name, fits):
    import re
    rule = re.compile(get(admin_relay)[1]["builtin"]["text_name"])
    assert bool(rule.fullmatch(name)) is fits
    status, body, _ = put(admin_relay, {"version": 1, "texts": {name: "text"}}, None)
    assert (status == 200) is fits, body
    if not fits:
        assert status == 400 and body["error"]["param"].startswith("texts")
