"""The relay role and its mode are live settings: PATCH /admin/api/settings, then the very next request carries them."""
import json
import urllib.request

import pytest

import context
import direct
import journal
from test_admin import admin_relay  # noqa: F401 - one account behind the real HTTP layer, runner and pool
from test_review_fixes_pr19 import END, response, text
from test_review_fixes_proxy import FakeManager

CLIENT = "CLIENT_SYSTEM: keep to the project."
ROLE = "Роль: инженер.\nПравила: отвечай кратко."
BODY = {"model": "claude-opus-5-5", "stream": False,
        "messages": [{"role": "system", "content": CLIENT}, {"role": "user", "content": "hello"}],
        "tools": [{"type": "function", "function": {"name": "read", "description": "Read a file",
                                                    "parameters": {"type": "object", "properties": {}}}}]}
LIVE = {"relay_system_prompt", "relay_system_prompt_mode"}


@pytest.fixture
def live(admin_relay, monkeypatch):
    """admin_relay with a scripted Retool: every upstream POST is recorded and answered, nothing leaves the process."""
    runtime = admin_relay.runtime
    for backend in runtime.accounts.backends:
        manager = FakeManager(100.0)
        runtime.accounts.managers[backend] = manager
        backend.credits = manager
    runtime.credits = runtime.accounts.managers[runtime.accounts.backends[0]]
    admin_relay.cfg.update(cache_keepwarm_seconds=0, upstream_error_dir=str(admin_relay.path.parent),
                           client_metadata_home=str(admin_relay.path.parent))
    sent, original = [], urllib.request.urlopen

    def opener(request, **kwargs):
        # urlopen is one module attribute: the test's own calls to the relay pass through to the real one.
        if "example.invalid" not in request.full_url:
            return original(request, **kwargs)
        sent.append(json.loads(request.data))
        return response(text("OK"), *END)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    admin_relay.sent = sent
    return admin_relay


def ask(relay, body=BODY):
    """The system head of the upstream POST this request produced ('' when it went without one)."""
    before = len(relay.sent)
    status, answer, _ = relay.request("/v1/chat/completions", body, method="POST")
    assert status == 200, answer
    assert len(relay.sent) == before + 1
    first = relay.sent[-1]["messages"][0]
    return first["content"][0]["text"] if first["role"] == "system" else ""


def test_the_role_and_its_mode_are_live_settings_in_the_schema(admin_relay):
    schema = admin_relay.request("/admin/api/settings")[1]["schema"]
    assert LIVE <= set(admin_relay.runtime.admin.schema)
    assert {key for key, field in schema.items() if not field["restart_required"]} >= LIVE
    assert schema["max_workers"]["restart_required"] is True  # the rest of the settings still wait for a restart


def test_a_saved_role_and_mode_apply_without_a_restart(admin_relay):
    status, body, _ = admin_relay.patch({"relay_system_prompt_mode": "append", "relay_system_prompt": ROLE})
    assert status == 200 and set(body["applied"]) == LIVE
    assert body["pending_restart"] == [] and body["restart_required"] is False
    assert admin_relay.cfg["relay_system_prompt"] == ROLE and admin_relay.cfg["relay_system_prompt_mode"] == "append"
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    assert saved["relay_system_prompt"] == ROLE and saved["relay_system_prompt_mode"] == "append"
    # What the screen reads back agrees with what the relay runs: nothing is waiting.
    state = admin_relay.request("/admin/api/settings")[1]
    assert state["pending_restart"] == [] and state["effective"]["relay_system_prompt"] == ROLE


def test_one_setting_saved_alone_is_live_too(admin_relay):
    status, body, _ = admin_relay.patch({"relay_system_prompt_mode": "replace"})
    assert status == 200 and body["applied"] == ["relay_system_prompt_mode"] and body["pending_restart"] == []
    status, body, _ = admin_relay.patch({"relay_system_prompt": None})
    assert status == 200 and body["applied"] == ["relay_system_prompt"] and body["pending_restart"] == []
    assert admin_relay.cfg["relay_system_prompt"] is None and admin_relay.cfg["relay_system_prompt_mode"] == "replace"


def test_the_next_request_goes_upstream_with_the_new_head(live):
    assert ask(live) == CLIENT  # standalone: a client that brings its own instructions keeps them
    assert live.patch({"relay_system_prompt_mode": "append", "relay_system_prompt": ROLE})[0] == 200
    assert ask(live) == CLIENT + "\n\n" + ROLE
    assert live.patch({"relay_system_prompt_mode": "replace"})[0] == 200
    assert ask(live) == ROLE
    assert live.patch({"relay_system_prompt": "Вторая роль"})[0] == 200
    assert ask(live) == "Вторая роль"
    assert live.patch({"relay_system_prompt": None})[0] == 200  # null is the built-in role
    assert ask(live) == context.DEFAULT_RELAY_SYSTEM_PROMPT
    assert live.patch({"relay_system_prompt": ""})[0] == 200  # a deliberate "no role": replace leaves no head at all
    assert ask(live) == ""
    assert live.patch({"relay_system_prompt_mode": "standalone"})[0] == 200
    assert ask(live) == CLIENT
    assert len(live.sent) == 7


def test_a_changed_head_is_a_new_cache_prefix_and_an_unchanged_one_is_not(live):
    ask(live)
    ask(live)
    assert live.sent[0]["openAICacheKey"] == live.sent[1]["openAICacheKey"]
    assert live.patch({"relay_system_prompt_mode": "append", "relay_system_prompt": ROLE})[0] == 200
    ask(live)
    ask(live)
    assert live.sent[2]["openAICacheKey"] == live.sent[3]["openAICacheKey"] != live.sent[1]["openAICacheKey"]


def test_a_service_request_keeps_its_own_prompt_whatever_the_live_role_says(live):
    assert live.patch({"relay_system_prompt_mode": "replace", "relay_system_prompt": ROLE})[0] == 200
    title = {"model": "claude-opus-5-5", "max_tokens": 64, "system": "You write short session titles. Reply with only "
             "the tagged fields the prompt asks for.", "messages": [{"role": "user", "content": "Name this chat"}]}
    status, answer, _ = live.request("/v1/messages", title, method="POST")
    assert status == 200, answer
    assert live.sent[-1]["messages"][0]["content"][0]["text"] == title["system"]


def test_the_settings_event_says_both_were_applied_and_logs_no_role_text(admin_relay):
    journal.JOURNAL.reset()
    assert admin_relay.patch({"relay_system_prompt_mode": "append", "relay_system_prompt": ROLE})[0] == 200
    event = next(item for item in journal.JOURNAL.query() if item["kind"] == "admin.settings")
    assert set(event["data"]["applied"]) == LIVE and event["data"]["restart_required"] == []
    assert "Роль: инженер" not in json.dumps(journal.JOURNAL.query(), ensure_ascii=False)
