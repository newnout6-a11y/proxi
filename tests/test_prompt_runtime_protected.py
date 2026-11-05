"""Protected jobs: a service request of a client goes through every profile unchanged (plan I6), end to end."""
import pytest

import prompt_policy
from prompt_bodies import (CLAUDE_SDK_HEADERS, OPENCODE_COMPACTION_V1, OPENCODE_HEADERS, OPENCODE_SUMMARY,
                           OPENCODE_TITLE_V1, OPENCODE_TITLE_V2, filler, service_body)
from prompt_support import client_request, fake_relay, head_of, profile, prompt_relay  # noqa: F401

RULES = "MY_RULES"
OPENCODE = {"user-agent": "opencode/2.2.5"}
PLUGIN = {**OPENCODE, "x-proxi-client": "opencode"}
CLAUDE = {"user-agent": "claude-cli/2.1.309 (external, sdk-cli)"}
SDK_TITLE = ("You are naming a coding session so the user can pick it out of a long list of sessions.\n"
             "Reply with a short title. " + filler("title", 2))
CLASSIFIER = "You are a security monitor for autonomous AI coding agents.\n" + filler("policy", 3)
OWN_PROMPT = "The client's own output contract for this job. " + filler("contract", 3)
PROBE = {"model": "claude-opus-5-5", "max_tokens": 1, "messages": [{"role": "user", "content": "."}]}


def anthropic_job(system, content="synthetic input"):
    return client_request("anthropic", [{"role": "user", "content": content}], system=system, tools=False)


# id, dialect, headers, request, the purpose the prompt layer sees, the system the model must still get
JOBS = [
    pytest.param("chat", OPENCODE, service_body(OPENCODE_TITLE_V2), "client-service", OPENCODE_TITLE_V2,
                 id="opencode-2-title"),
    pytest.param("chat", {**PLUGIN, "x-proxi-agent": "title"}, service_body(OPENCODE_TITLE_V1), "client-service",
                 OPENCODE_TITLE_V1, id="opencode-1-title"),
    pytest.param("chat", OPENCODE, service_body(OPENCODE_SUMMARY), "client-service", OPENCODE_SUMMARY,
                 id="opencode-summary"),
    pytest.param("chat", {**PLUGIN, "x-proxi-agent": "compaction"},
                 service_body(OPENCODE_COMPACTION_V1, "synthetic history"), "client-service", OPENCODE_COMPACTION_V1,
                 id="opencode-1-compaction"),
    pytest.param("chat", {**PLUGIN, "x-proxi-agent": "title"}, service_body("Name this chat in three words."),
                 "client-service", "Name this chat in three words.", id="opencode-title-with-the-users-own-prompt"),
    pytest.param("anthropic", CLAUDE, anthropic_job(SDK_TITLE), "session-title", SDK_TITLE, id="claude-code-title"),
    pytest.param("anthropic", CLAUDE, anthropic_job(CLASSIFIER), "auto-mode-check", CLASSIFIER,
                 id="claude-code-classifier"),
    pytest.param("anthropic", {**CLAUDE, "x-claude-code-request-class": "compaction"}, anthropic_job(OWN_PROMPT),
                 "client-text", OWN_PROMPT, id="claude-code-compaction"),
    pytest.param("anthropic", {**CLAUDE, "x-claude-code-request-class": "auxiliary"}, anthropic_job(OWN_PROMPT),
                 "client-text", OWN_PROMPT, id="claude-code-auxiliary"),
    pytest.param("anthropic", {"x-proxi-client": "claude-desktop"}, PROBE, "connection-probe", None,
                 id="desktop-connection-probe"),
]


def with_default_profile(relay):
    relay.prompts.publish(1, {"mine": profile("{{text:rules}}\n\n{{client_system}}", edits=False,
                                              texts={"rules": RULES})}, default_profile="mine")
    return relay


@pytest.mark.parametrize("dialect,headers,request_body,purpose,system", JOBS)
def test_a_service_job_reaches_the_model_exactly_as_without_the_prompt_layer(
        prompt_relay, dialect, headers, request_body, purpose, system):
    relay = with_default_profile(fake_relay(prompt_relay))
    # the baseline: no store at all, the code of before the prompt layer
    store, relay.runtime.prompts, relay.runtime.runner.prompts = relay.prompts, None, None
    assert relay.post(dialect, request_body, headers)[0] == 200
    relay.runtime.prompts = relay.runtime.runner.prompts = store

    assert relay.post(dialect, request_body, headers)[0] == 200
    (inp,), (plan,) = store.inputs, store.plans
    assert inp.purpose == purpose
    assert plan.protected and plan.profile == "passthrough" and plan.rule.startswith("protected:")
    assert head_of(relay.posts[1]) == system and head_of(relay.posts[0]) == system
    assert relay.posts[1] == relay.posts[0]
    assert (relay.posts[1]["messages"][0]["role"] == "system") == (system is not None)


def test_the_same_profile_does_change_the_head_of_the_chat_itself(prompt_relay):
    relay = with_default_profile(fake_relay(prompt_relay))
    request_body = client_request("chat", [{"role": "user", "content": "work"}], system="CHAT_SYSTEM")
    assert relay.post("chat", request_body, OPENCODE)[0] == 200
    assert head_of(relay.posts[0]) == RULES + "\n\nCHAT_SYSTEM"
    (plan,) = relay.prompts.plans
    assert not plan.protected and plan.rule == "default"


@pytest.mark.parametrize("headers,request_class", [
    ({"x-claude-code-request-class": "compaction"}, "compaction"),
    ({"x-claude-code-request-class": "auxiliary"}, "auxiliary"),
    ({}, "")])
def test_the_request_class_of_claude_code_reaches_the_plan(prompt_relay, headers, request_class):
    relay = with_default_profile(fake_relay(prompt_relay))
    assert relay.post("anthropic", anthropic_job(OWN_PROMPT), {**CLAUDE, **headers})[0] == 200
    assert relay.prompts.inputs[0].request_class == request_class
    assert relay.prompts.plans[0].protected == bool(request_class)


def test_a_pin_belongs_to_the_conversation_and_the_purpose_of_the_request(prompt_relay):
    relay = with_default_profile(fake_relay(prompt_relay))
    session = {"x-session-affinity": "ses-1", **OPENCODE}
    chat = client_request("chat", [{"role": "user", "content": "work"}], system="CHAT_SYSTEM")
    assert relay.post("chat", chat, session)[0] == 200
    assert relay.post("chat", service_body(OPENCODE_TITLE_V2), session)[0] == 200
    assert sorted(relay.prompts.pins) == [("header:ses-1", "client-service"), ("header:ses-1", "client-tools")]
    assert relay.prompts.unpin("header:ses-1") == 2


def test_a_request_without_a_conversation_is_never_pinned(prompt_relay):
    relay = with_default_profile(fake_relay(prompt_relay))
    request_body = client_request("chat", [{"role": "user", "content": "work"}], system="CHAT_SYSTEM", tools=False)
    assert relay.post("chat", request_body)[0] == 200
    relay.prompts.publish(2, {"mine": profile("NEW\n\n{{client_system}}")}, default_profile="mine")
    assert relay.post("chat", request_body)[0] == 200
    assert relay.prompts.inputs[0].conversation_key is None and relay.prompts.pins == {}
    assert head_of(relay.posts[0]) == RULES + "\n\nCHAT_SYSTEM" and head_of(relay.posts[1]) == "NEW\n\nCHAT_SYSTEM"


def test_a_service_job_of_the_desktop_runner_keeps_its_billing_header_block(prompt_relay):
    # The billing header of Claude Code stays its own first block in every head (plan I5), a profile or not.
    relay = with_default_profile(fake_relay(prompt_relay))
    billing = "x-anthropic-billing-header: cc_version=2.1.309.synthetic; cc_entrypoint=sdk-cli;"
    system = [{"type": "text", "text": billing}, {"type": "text", "text": "Synthetic main prompt of the runner."}]
    body = {"model": "claude-opus-5-5", "max_tokens": 64, "system": system, "stream": False,
            "messages": [{"role": "user", "content": "work"}]}
    assert relay.post("anthropic", body, {**CLAUDE, "x-session-affinity": "sdk-1"})[0] == 200
    first = relay.posts[0]["messages"][0]["content"]
    assert [block["text"] for block in first] == [billing, RULES + "\n\nSynthetic main prompt of the runner."]
    assert "cache" not in first[0] and first[1]["cache"] is True
    assert relay.prompts.plans[0].profile == "mine" and prompt_policy.sha8(billing)
