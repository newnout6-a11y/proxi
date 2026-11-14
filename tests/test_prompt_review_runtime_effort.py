"""R2-12: an effort a profile sets is journaled as the profile's, beside the one the client sent; a compaction may still keep
the dialog's own effort over it (the order is client, profile, dialog)."""
import json

import pytest

from prompt_support import account, client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_compaction_cache import Chat, INSTRUCTION, SYSTEM, count, effort_fields
from test_native_history import tool_reply

DOCUMENT = {"version": 1, "profiles": {
    "low": {"head": "{{client_system}}", "generation": {"effort": "low"}},
    "max": {"head": "{{client_system}}", "generation": {"effort": "max"}},
    "silent": {"head": "{{client_system}}"}},
    "rules": [{"name": "by header", "if": {"header_profile": "*"}, "use": "$header"}]}
USER = [{"role": "user", "content": "hi"}]


def relay_with(prompt_relay, tmp_path, **cfg):
    (tmp_path / "prompts.json").write_text(json.dumps(DOCUMENT), encoding="utf-8")
    return prompt_relay(**cfg)


def turn_begin(relay, mark):
    (event,) = relay.events(mark, "turn.begin")
    return {**event["data"], "message": event["message"]}


def ask(relay, profile=None, *, model="claude-opus-5-5", dialect="chat", effort=None, system=SYSTEM):
    headers = {"x-proxi-profile": profile} if profile else {}
    extra = effort_fields(dialect, effort) if effort else {}
    mark = relay.mark()
    status, payload, answer = relay.post(dialect, client_request(dialect, USER, system=system, tools=False, model=model,
                                                                 **extra), headers)
    assert status == 200, payload
    relay.settle(answer)
    return turn_begin(relay, mark), relay.posts[-1]


def test_the_effort_of_a_profile_is_the_profiles_and_the_one_the_client_sent_is_kept_beside_it(prompt_relay, tmp_path):
    relay = relay_with(prompt_relay, tmp_path)
    begin, sent = ask(relay, "low", effort="high")
    assert sent["reasoningEffort"] == "low"
    assert (begin["requested_effort"], begin["effective_effort"], begin["effort"]) == ("high", "low", "low")
    assert (begin["effort_source"], begin["effort_resolution"]) == ("profile", "profile")
    assert "effort профиля «low», клиент просил high" in begin["message"]


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_it_is_so_in_every_dialect(prompt_relay, tmp_path, dialect):
    relay = relay_with(prompt_relay, tmp_path)
    begin, sent = ask(relay, "low", dialect=dialect, effort="high")
    assert (begin["requested_effort"], begin["effective_effort"], begin["effort_source"]) == ("high", "low", "profile")
    assert sent["reasoningEffort"] == "low"


def test_a_profile_that_sets_the_effort_beside_a_client_that_sent_none_is_still_the_profiles(prompt_relay, tmp_path):
    relay = relay_with(prompt_relay, tmp_path, reasoning_effort="auto")
    begin, sent = ask(relay, "max")
    assert sent["reasoningEffort"] == "max"
    assert begin["effective_effort"] == "max" and begin["effort_source"] == "profile"
    assert begin["requested_effort"] == "auto"  # what the relay would have asked without the profile


def test_a_profile_without_an_effort_and_a_request_without_a_profile_say_what_they_always_said(prompt_relay, tmp_path):
    relay = relay_with(prompt_relay, tmp_path)
    for profile in (None, "silent"):
        begin, sent = ask(relay, profile, effort="low")
        assert sent["reasoningEffort"] == "low"
        assert (begin["requested_effort"], begin["effective_effort"], begin["effort_source"],
                begin["effort_resolution"]) == ("low", "low", "client", "exact")


def test_an_effort_the_model_takes_nearest_to_is_still_the_profiles_when_the_relay_may_adjust_it(prompt_relay, tmp_path):
    relay = relay_with(prompt_relay, tmp_path, allow_effort_downgrade=True)
    begin, sent = ask(relay, "max", model="gpt-5.5", effort="low")
    assert sent["reasoningEffort"] == "high"  # gpt-5.5 has no max
    assert (begin["requested_effort"], begin["effective_effort"], begin["effort_source"]) == ("low", "high", "profile")


def test_a_route_that_ignores_effort_says_the_profiles_effort_was_left_to_the_provider(prompt_relay, tmp_path):
    glm = {**account(), "retool_fast_provider_resource_name": "__managed__"}
    relay = relay_with(prompt_relay, tmp_path, accounts=[glm])
    begin, sent = ask(relay, "low", model="zai-org/GLM-5.3", effort="high")
    assert "reasoningEffort" not in sent and begin["effective_effort"] is None
    assert (begin["requested_effort"], begin["effort_source"], begin["effort_resolution"]) == (
        "high", "profile", "provider_managed")


# -- a compaction after a profile: the dialog's effort still comes last ---------------------------------------------------------
def test_a_compaction_keeps_the_dialogs_effort_over_the_profiles_and_says_so(prompt_relay, tmp_path):
    relay = relay_with(prompt_relay, tmp_path)
    chat = Chat(relay, effort="max")
    chat.work(2)
    mark = relay.mark()
    summary = chat.compact(effort="high", headers={"x-proxi-profile": "low"})
    begin = turn_begin(relay, mark)
    # the client asked for high, the profile for low; the signed history of the dialog is on max, and the compaction continues it
    assert summary["reasoningEffort"] == "max"
    assert (begin["requested_effort"], begin["effective_effort"], begin["effort_source"],
            begin["effort_resolution"]) == ("high", "max", "dialog", "cache_continuity")
    (kept,) = relay.events(mark, "cache.effort_kept")
    assert (kept["data"]["requested_effort"], kept["data"]["dialog_effort"]) == ("high", "max")
    assert count(relay, mark, "native_history.resume") == 1 and count(relay, mark, "native_history.reset") == 0


def test_a_compaction_the_profile_already_puts_on_the_dialogs_effort_needs_no_help(prompt_relay, tmp_path):
    relay = relay_with(prompt_relay, tmp_path)
    chat = Chat(relay, effort="max")
    chat.work(2)
    mark = relay.mark()
    summary = chat.compact(effort="high", headers={"x-proxi-profile": "max"})
    begin = turn_begin(relay, mark)
    assert summary["reasoningEffort"] == "max"
    assert (begin["requested_effort"], begin["effective_effort"], begin["effort_source"]) == ("high", "max", "profile")
    assert not relay.events(mark, "cache.effort_kept") and count(relay, mark, "native_history.resume") == 1
