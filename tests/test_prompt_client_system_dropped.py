"""A profile whose head leaves out {{client_system}} is named loudly, for the clients that rely on that prompt.

A user's profile `vanta` (head {{text:vanta}}, rule for *glm-5.3*) swapped DeepSeek Harness's own 2.7k characters of
instructions for a 39k persona on 2026-11-06 (+85% credits in short sessions), and nothing said so: the only trace was a
debug-level warning inside every prompt.plan event. The relay keeps the head the profile asks for; it now says what
that head drops: a warning of the revision (the admin screen shows it), and one warn event per profile and client."""
import json

import pytest

import prompt_policy as pp
from prompt_support import client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_prompt_policy_common import compile_doc, document, plan_for

VANTA = document(texts={"vanta": "PERSONA-TEXT-9931"}, profiles={"vanta": {"head": "{{text:vanta}}"}},
                 rules=[{"name": "glm only", "if": {"model": ["*glm-5.3*"]}, "use": "vanta"}])


def dropped(snapshot):
    return [line for line in snapshot.warnings if "does not use {{client_system}}" in line]


# -- the revision's own warning (what the admin screen shows) ------------------------------------------------------------

def test_a_profile_a_rule_serves_without_client_system_is_warned_about_by_name(tmp_path):
    (warning,) = dropped(compile_doc(VANTA, tmp_path))
    assert warning.startswith("profiles.vanta: the head does not use {{client_system}}")
    assert "rule 'glm only'" in warning and "Harness" in warning and "{{client_system}}" in warning
    assert "PERSONA-TEXT-9931" not in warning


@pytest.mark.parametrize("head", ["{{client_system}}", "{{text:vanta}}\n\n{{client_system}}", "{{client_system_raw}}\n{{text:vanta}}"])
def test_a_head_that_keeps_the_client_system_is_not_warned_about(tmp_path, head):
    doc = document(texts={"vanta": "x"}, profiles={"vanta": {"head": head}},
                   rules=[{"name": "r", "use": "vanta"}])
    assert not dropped(compile_doc(doc, tmp_path))


def test_a_profile_no_rule_and_no_default_uses_is_not_warned_about(tmp_path):
    doc = document(texts={"vanta": "x"}, profiles={"vanta": {"head": "{{text:vanta}}"}})
    assert not dropped(compile_doc(doc, tmp_path))
    assert not dropped(compile_doc(dict(doc, rules=[{"name": "off", "enabled": False, "use": "vanta"}]), tmp_path))


def test_a_rule_only_for_requests_without_a_system_prompt_of_their_own_loses_nothing(tmp_path):
    doc = document(texts={"vanta": "x"}, profiles={"vanta": {"head": "{{text:vanta}}"}},
                   rules=[{"name": "bare", "if": {"has_client_system": False}, "use": "vanta"}])
    assert not dropped(compile_doc(doc, tmp_path))


def test_a_rule_only_for_requests_without_tools_takes_no_tool_rules_away(tmp_path):
    chat = {"name": "chat", "if": {"model": ["*glm-5.3*"], "tools": False}, "use": "vanta"}
    doc = document(texts={"vanta": "x"}, profiles={"vanta": {"head": "{{text:vanta}}"}}, rules=[chat])
    assert not dropped(compile_doc(doc, tmp_path))
    agents = {"name": "agents", "if": {"model": ["*glm-5.3*"], "tools": True}, "use": "vanta"}
    assert dropped(compile_doc(dict(doc, rules=[agents]), tmp_path))


def test_the_default_profile_and_several_rules_are_each_named_once(tmp_path):
    doc = document(texts={"t": "x"}, profiles={"mine": {"head": "{{text:t}}"}}, default_profile="mine",
                   rules=[{"name": "a", "if": {"model": "gpt-*"}, "use": "mine"},
                          {"name": "b", "if": {"model": "claude-*"}, "use": "mine"}])
    (warning,) = dropped(compile_doc(doc, tmp_path))
    assert "rule 'a', rule 'b', the default profile" in warning and "hand the model" in warning


def test_the_built_in_passthrough_never_warns(tmp_path):
    doc = document(rules=[{"name": "p", "use": "passthrough"}], default_profile="passthrough")
    assert not dropped(compile_doc(doc, tmp_path))


def test_the_warning_reaches_the_admin_screen_with_the_document_in_force(prompt_relay, tmp_path):
    (tmp_path / "prompts.json").write_text(json.dumps(VANTA), encoding="utf-8")
    relay = prompt_relay()
    snapshot = relay.runtime.prompts.snapshot()
    assert any(line.startswith("profiles.vanta: the head does not use") for line in snapshot.warnings)
    import admin
    assert any("profiles.vanta" in line for line in admin._revision_warnings(snapshot.document, snapshot))


# -- the runtime: one warn event per profile and client, for a request that carries tools -------------------------------

DOCUMENT = {"version": 1, "texts": {"persona": "PERSONA-TEXT-9931"},
            "profiles": {"persona": {"head": "{{text:persona}}"}, "keeper": {"head": "{{text:persona}}\n\n{{client_system}}"}},
            "rules": [{"name": "by header", "if": {"header_profile": "*"}, "use": "$header"}]}
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]
CODEX = {"x-proxi-client": "codex"}
HARNESS = {"x-proxi-client": "harness"}


def ask(relay, profile, *, system="CLIENT_SYSTEM_OF_THE_AGENT", tools=True, client=HARNESS):
    status, payload, _ = relay.post("chat", client_request("chat", USER, system=system, tools=tools),
                                    {"x-proxi-profile": profile, **client})
    assert status == 200, payload


def events(relay, mark):
    return [event["data"] for event in relay.events(mark, "prompt.client_system_dropped")]


@pytest.fixture
def relay(prompt_relay, tmp_path):
    (tmp_path / "prompts.json").write_text(json.dumps(DOCUMENT), encoding="utf-8")
    return prompt_relay()


def test_a_request_with_tools_through_a_profile_that_drops_the_client_system_is_named_once(relay):
    mark = relay.mark()
    ask(relay, "persona")
    ask(relay, "persona")
    (event,) = relay.events(mark, "prompt.client_system_dropped")
    assert event["level"] == "warn"
    assert event["data"]["profile"] == "persona" and event["data"]["client"] == "harness"
    assert event["data"]["chars"] == len("CLIENT_SYSTEM_OF_THE_AGENT")
    assert "{{client_system}}" in event["message"] and "PERSONA-TEXT-9931" not in json.dumps(event)
    assert "CLIENT_SYSTEM_OF_THE_AGENT" not in json.dumps(event)


def test_another_client_of_the_same_profile_is_named_on_its_own(relay):
    mark = relay.mark()
    ask(relay, "persona", client=HARNESS)
    ask(relay, "persona", client=CODEX)
    assert sorted(item["client"] for item in events(relay, mark)) == ["codex", "harness"]


def test_the_head_the_profile_asks_for_is_still_what_goes_upstream(relay):
    ask(relay, "persona")
    system = relay.posts[-1]["messages"][0]["content"]
    assert "CLIENT_SYSTEM_OF_THE_AGENT" not in json.dumps(system) and "PERSONA-TEXT-9931" in json.dumps(system)


@pytest.mark.parametrize("profile,system,tools", [
    ("keeper", "CLIENT_SYSTEM_OF_THE_AGENT", True),   # the profile keeps the client's prompt
    ("persona", "CLIENT_SYSTEM_OF_THE_AGENT", False),  # no tools: not a client that works through tools
    ("persona", "", True)])                            # nothing was dropped: the client had no system prompt
def test_no_event_where_nothing_the_client_needs_is_dropped(relay, profile, system, tools):
    mark = relay.mark()
    ask(relay, profile, system=system, tools=tools)
    assert not events(relay, mark)


def test_the_plan_event_keeps_the_per_request_warning(relay):
    mark = relay.mark()
    ask(relay, "persona")
    (plan,) = relay.events(mark, "prompt.plan")
    assert any("is not part of the head" in line for line in plan["data"]["warnings"])


def test_dropped_client_system_counts_the_characters_a_plan_leaves_out(tmp_path):
    plan = plan_for(VANTA, tmp_path, profile="vanta")
    assert pp.dropped_client_system(plan, "x" * 40) == 40
    assert pp.dropped_client_system(plan, "  \n") == 0 and pp.dropped_client_system(plan, "") == 0
    keeper = plan_for(document(profiles={"p": {"head": "{{client_system}}"}}), tmp_path)
    assert pp.dropped_client_system(keeper, "x" * 40) == 0
    assert pp.dropped_client_system(pp.Plan(), "x" * 40) == 0  # a legacy plan has no profile


def test_the_orchestrator_keeps_one_note_per_profile_and_client(relay):
    ask(relay, "persona", client=HARNESS)
    ask(relay, "persona", client=CODEX)
    assert relay.runtime.runner._dropped_noted == {("persona", "harness"), ("persona", "codex")}
