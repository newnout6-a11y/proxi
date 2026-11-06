"""R2-08 (runtime part): a profile that leaves a client with tools and no system prompt with an empty head says so in
prompt.plan and in the ring, without quoting any text."""
import json

import pytest

import orchestrator
from prompt_support import client_request, head_of, prompt_relay  # noqa: F401 - prompt_relay is a fixture

DOCUMENT = {"version": 1, "profiles": {
    "bare": {"head": "{{client_system}}"},
    "role": {"head": "{{native_role}}\n\n{{client_system}}"},
    "task": {"head": "{{client_system}}{{relay_task}}"}},
    "rules": [{"name": "by header", "if": {"header_profile": "*"}, "use": "$header"}]}
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]


def ask(prompt_relay, tmp_path, profile, *, system, tools):
    (tmp_path / "prompts.json").write_text(json.dumps(DOCUMENT), encoding="utf-8")
    relay = prompt_relay()
    mark = relay.mark()
    status, payload, _ = relay.post("chat", client_request("chat", USER, system=system, tools=tools),
                                    {"x-proxi-profile": profile})
    assert status == 200, payload
    (plan,) = relay.events(mark, "prompt.plan")
    (entry,) = relay.runtime.prompts.recent()
    return relay, plan["data"], entry


def test_an_empty_head_for_a_client_with_tools_is_said_in_the_plan_event_and_in_the_ring(prompt_relay, tmp_path):
    relay, plan, entry = ask(prompt_relay, tmp_path, "bare", system="", tools=True)
    assert head_of(relay.posts[-1]) is None and plan["head_chars"] == 0 and entry["head"] == ""
    assert plan["warnings"] == [orchestrator.EMPTY_HEAD_WARNING] == entry["warnings"]
    assert "{{native_role}}" in plan["warnings"][0]


@pytest.mark.parametrize("profile,system,tools", [
    ("bare", "CLIENT_SYSTEM", True),   # the client's own system is the head
    ("role", "", True),                # the template gives the client with tools the relay's role
    ("task", "", False),               # a relay task has a head of its own
    ("bare", "CLIENT_SYSTEM", False),
])
def test_a_head_that_is_not_empty_gets_no_warning(prompt_relay, tmp_path, profile, system, tools):
    _, plan, entry = ask(prompt_relay, tmp_path, profile, system=system, tools=tools)
    assert plan["head_chars"] > 0 and orchestrator.EMPTY_HEAD_WARNING not in plan["warnings"]
    assert orchestrator.EMPTY_HEAD_WARNING not in entry["warnings"]


def test_an_empty_head_for_a_client_without_tools_is_not_this_warning(prompt_relay, tmp_path):
    # no tools, no system, and a template that adds nothing: the legacy head of that request was the relay task, but
    # the warning is for the client that sent tools (whose legacy head would have been the native role)
    _, plan, entry = ask(prompt_relay, tmp_path, "bare", system="", tools=False)
    assert plan["head_chars"] == 0 and orchestrator.EMPTY_HEAD_WARNING not in plan["warnings"]


def test_the_warning_quotes_no_text_and_the_journal_event_keeps_no_text(prompt_relay, tmp_path):
    relay, plan, _ = ask(prompt_relay, tmp_path, "bare", system="", tools=True)
    assert "ORIGINAL_TASK" not in json.dumps(plan)
    assert orchestrator.EMPTY_HEAD_WARNING.count("{{") == orchestrator.EMPTY_HEAD_WARNING.count("}}") == 1
    assert relay.posts


def test_a_hidden_tool_set_is_a_client_with_tools_for_the_warning_too(prompt_relay, tmp_path):
    document = json.loads(json.dumps(DOCUMENT))
    document["profiles"]["hide"] = {"head": "{{client_system}}", "tools": {"edits": [{"tool": "*", "hide": True}]}}
    (tmp_path / "prompts.json").write_text(json.dumps(document), encoding="utf-8")
    relay = prompt_relay()
    mark = relay.mark()
    assert relay.post("chat", client_request("chat", USER, system="", tools=True), {"x-proxi-profile": "hide"})[0] == 200
    (plan,) = relay.events(mark, "prompt.plan")
    assert plan["data"]["warnings"] == [orchestrator.EMPTY_HEAD_WARNING]
