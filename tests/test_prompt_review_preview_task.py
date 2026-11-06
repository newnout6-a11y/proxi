"""The preview shows the relay-task blocks exactly where the request gets them: only for a request the relay owns.

A client with tools and no system gets empty relay blocks at run time (orchestrator.RelaySources), and so does a client
whose tools a profile hides (plan I9: the request is judged by the body the client sent). The preview used to show the
relay-task placeholders for both."""
import copy
import json

import pytest

import prompt_policy
from prompt_support import client_request, head_of, prompt_relay  # noqa: F401 - prompt_relay is a fixture

DIALECTS = ["chat", "anthropic", "responses"]
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]
TASK = "{{relay_task}}{{exec_reminder}}"


def start(prompt_relay, tmp_path, profile, **cfg):
    doc = {"version": 1, "profiles": {"p": profile}, "rules": [{"name": "everything", "if": {}, "use": "p"}]}
    (tmp_path / "prompts.json").write_text(json.dumps(doc), encoding="utf-8")
    return prompt_relay(**cfg)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_client_with_tools_and_no_system_gets_no_relay_task_in_the_preview_either(prompt_relay, tmp_path, dialect):
    relay = start(prompt_relay, tmp_path, {"head": "BEFORE\n\n" + TASK + "\n\nAFTER"})
    body = client_request(dialect, USER, system="", tools=True)
    preview = relay.runtime.prompts.preview(copy.deepcopy(body), dialect, {})
    assert relay.post(dialect, body)[0] == 200
    sent = head_of(relay.posts[-1])
    assert preview["head"] == sent
    assert "[RELAY TASK" not in preview["head"] and prompt_policy._PLACEHOLDER_WARNING not in preview["warnings"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_profile_that_hides_every_tool_leaves_the_request_the_clients_in_the_preview_too(
        prompt_relay, tmp_path, dialect):
    relay = start(prompt_relay, tmp_path, {"head": "BEFORE\n\n" + TASK, "tools": {"edits": [{"tool": "*", "hide": True}]}})
    body = client_request(dialect, USER, system="", tools=True)
    preview = relay.runtime.prompts.preview(copy.deepcopy(body), dialect, {})
    assert relay.post(dialect, body)[0] == 200
    sent = head_of(relay.posts[-1])
    assert preview["head"] == sent == "BEFORE"
    assert "[LOCAL EXECUTION]" not in sent and "[RELAY TASK" not in preview["head"]


def test_a_request_the_relay_owns_still_shows_where_its_task_goes(prompt_relay, tmp_path):
    relay = start(prompt_relay, tmp_path, {"head": TASK})
    body = client_request("chat", USER, system="", tools=False)
    preview = relay.runtime.prompts.preview(copy.deepcopy(body), "chat", {})
    assert "[RELAY TASK" in preview["head"] and prompt_policy._PLACEHOLDER_WARNING in preview["warnings"]
