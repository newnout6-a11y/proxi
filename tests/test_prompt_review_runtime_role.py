"""R2-01 / R1-06: the roles a profile's template names are the file's or the built-in ones, in the preview and in the
request alike; the role saved in the settings (config.json's relay_system_prompt) is the legacy head's alone."""
import copy
import json

import pytest

import context
import orchestrator
from prompt_support import client_request, head_of, prompt_relay  # noqa: F401 - prompt_relay is a fixture

CONFIGURED = "CONFIGURED ROLE FROM CONFIG JSON"
DIALECTS = ["chat", "anthropic", "responses"]
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]
TEMPLATE = "[{{role}}]\n[{{native_role}}]\n{{client_system}}"
SHAPES = {"system and tools": ("CLIENT_SYSTEM", True), "system only": ("CLIENT_SYSTEM", False),
          "tools only": ("", True), "nothing": ("", False)}


def document(**relay_texts):
    doc = {"version": 1, "profiles": {"roles": {"head": TEMPLATE}},
           "rules": [{"name": "everything", "if": {}, "use": "roles"}]}
    if relay_texts:
        doc["relay_texts"] = relay_texts
    return doc


def start(prompt_relay, tmp_path, doc, **cfg):
    (tmp_path / "prompts.json").write_text(json.dumps(doc), encoding="utf-8")
    return prompt_relay(relay_system_prompt=CONFIGURED, **cfg)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("shape", list(SHAPES))
def test_the_preview_shows_the_head_the_request_gets_with_a_role_saved_in_the_settings(
        prompt_relay, tmp_path, dialect, shape):
    system, tools = SHAPES[shape]
    relay = start(prompt_relay, tmp_path, document())
    body = client_request(dialect, USER, system=system, tools=tools)
    preview = relay.runtime.prompts.preview(copy.deepcopy(body), dialect, {})
    assert relay.post(dialect, body)[0] == 200
    sent = head_of(relay.posts[-1])
    assert preview["head"] == sent and preview["profile"] == "roles"
    expected = "[%s]\n[%s]\n%s" % (context.DEFAULT_RELAY_SYSTEM_PROMPT, orchestrator.DEFAULT_NATIVE_TOOLS_ROLE, system)
    assert sent == (expected if system else expected.rstrip("\n"))  # the newline an empty {{client_system}} leaves is tidied
    assert CONFIGURED not in sent and CONFIGURED not in preview["head"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_roles_of_the_file_are_the_ones_both_show(prompt_relay, tmp_path, dialect):
    relay = start(prompt_relay, tmp_path, document(role="FILE ROLE", native_role="FILE NATIVE ROLE"))
    body = client_request(dialect, USER, system="CLIENT_SYSTEM", tools=True)
    preview = relay.runtime.prompts.preview(copy.deepcopy(body), dialect, {})
    assert relay.post(dialect, body)[0] == 200
    assert preview["head"] == head_of(relay.posts[-1]) == "[FILE ROLE]\n[FILE NATIVE ROLE]\nCLIENT_SYSTEM"


def test_saving_a_role_in_the_settings_leaves_a_conversation_a_profile_holds_with_the_head_it_had(
        prompt_relay, tmp_path):
    relay = start(prompt_relay, tmp_path, document())
    headers = {"x-session-affinity": "dialog-1"}
    body = client_request("chat", USER, system="CLIENT_SYSTEM", tools=True)
    assert relay.post("chat", body, headers)[0] == 200
    relay.runtime.cfg["relay_system_prompt"] = "ROLE SAVED IN THE MIDDLE OF THE DIALOG"  # the live setting
    assert relay.post("chat", body, headers)[0] == 200
    first, second = relay.posts[0], relay.posts[1]
    assert head_of(first) == head_of(second)
    assert "ROLE SAVED" not in json.dumps(second) and CONFIGURED not in json.dumps(first)


def test_the_request_the_profile_does_not_serve_still_gets_the_role_of_the_settings(prompt_relay, tmp_path):
    never = {"version": 1, "profiles": {"roles": {"head": TEMPLATE}},
             "rules": [{"name": "never", "if": {"client": "harness"}, "use": "roles"}]}
    relay = start(prompt_relay, tmp_path, never)
    body = client_request("chat", USER, system="", tools=True)
    preview = relay.runtime.prompts.preview(copy.deepcopy(body), "chat", {})
    assert preview["profile"] is None
    assert relay.post("chat", body)[0] == 200
    # no rule matched: the legacy head, which reads the setting as it always did
    assert head_of(relay.posts[-1]) == CONFIGURED


def test_a_legacy_dollar_rule_keeps_the_role_of_the_settings_too(prompt_relay, tmp_path):
    legacy = {"version": 1, "profiles": {"roles": {"head": TEMPLATE}},
              "rules": [{"name": "old way", "if": {}, "use": "$legacy"}]}
    relay = start(prompt_relay, tmp_path, legacy)
    assert relay.post("chat", client_request("chat", USER, system="", tools=True))[0] == 200
    assert head_of(relay.posts[-1]) == CONFIGURED
