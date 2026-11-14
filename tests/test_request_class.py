"""x-claude-code-request-class keeps Claude Code's own side requests out of the relay prompt policy."""
import asyncio

import pytest

import client_context
from test_native_context_reuse import FIRST, request, run, setup
from test_service_requests import capture_direct

POLICY = "RELAY ROLE AND RULES"
SYSTEM = "CLIENT SUMMARY CONTRACT\nAnswer with <summary> tags."


@pytest.mark.parametrize("value,expected", [
    ("main", "main"), ("subagent", "subagent"), ("workflow", "workflow"), ("compaction", "compaction"),
    ("auxiliary", "auxiliary"), ("AUXILIARY", "auxiliary"), ("  Compaction ", "compaction"),
    ("future-class_2", "future-class_2"),
])
def test_class_header_becomes_part_of_the_client_identity(value, expected):
    info = client_context.describe({}, {"X-Claude-Code-Request-Class": value, "user-agent": "claude-cli/2.1.317"})
    assert info["request_class"] == expected and info["client"] == "claude-code"


@pytest.mark.parametrize("value", ["", "   ", "aux iliary", "auxiliary;drop", "1auxiliary", "\x1b[31m", "ключ", "a" * 80])
def test_unusable_class_values_are_dropped(value):
    assert "request_class" not in client_context.describe({}, {"x-claude-code-request-class": value})


def test_missing_header_adds_nothing():
    assert "request_class" not in client_context.describe({}, {"user-agent": "claude-cli/2.1.317"})


def test_header_reaches_the_turn_context_over_http(relay):
    seen = []

    async def run_turn(parsed, out, holder):
        seen.append(client_context.current())
        out.put({"type": "text", "delta": "ok"})
        out.put({"type": "end", "reason": "stop"})
        out.put(None)

    relay.fake.run = run_turn
    body = {"model": "claude-opus-5-5", "max_tokens": 64, "system": SYSTEM,
            "messages": [{"role": "user", "content": "summarise"}]}
    for value in ("compaction", None):
        headers = {"x-claude-code-request-class": value} if value else {}
        status, _ = relay.json_any("/v1/messages", body, headers=headers)
        assert status == 200
    assert seen[0]["request_class"] == "compaction" and "request_class" not in seen[1]


def run_once(runner, system, request_class, *, key="header:client-a"):
    parsed = request(FIRST, tools=[], system=system, key=key)
    parsed.request_purpose = "client-text"
    identity = {"client": "claude-code"}
    if request_class:
        identity["request_class"] = request_class
    token = client_context.bind(identity)
    try:
        events = asyncio.run(run(runner, parsed))
    finally:
        client_context.reset(token)
    assert events[-1] == {"type": "end", "reason": "stop"}


def head_of(body):
    return "\n".join(block["text"] for block in body["messages"][0]["content"])


@pytest.mark.parametrize("mode", ["append", "replace"])
@pytest.mark.parametrize("request_class", ["auxiliary", "compaction"])
def test_side_requests_keep_their_own_system_prompt_untouched(tmp_path, monkeypatch, mode, request_class):
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, exec_policy="off",
                         cookie_values=[], relay_system_prompt=POLICY, relay_system_prompt_mode=mode)
    sent = capture_direct(monkeypatch)
    run_once(runner, SYSTEM, request_class)
    assert head_of(sent[0][1]) == SYSTEM and POLICY not in str(sent[0][1])


@pytest.mark.parametrize("request_class", [None, "main", "subagent", "workflow", "future-class"])
def test_every_other_request_still_receives_the_policy(tmp_path, monkeypatch, request_class):
    for mode, expected in (("append", SYSTEM + "\n\n" + POLICY), ("replace", POLICY)):
        _, _, runner = setup(tmp_path, transport="direct", project_context=False, exec_policy="off",
                             cookie_values=[], relay_system_prompt=POLICY, relay_system_prompt_mode=mode)
        sent = capture_direct(monkeypatch)
        run_once(runner, SYSTEM, request_class)
        assert head_of(sent[0][1]) == expected


@pytest.mark.parametrize("mode", ["standalone", "append"])
def test_header_changes_the_policy_only_not_sessions_cache_or_history(tmp_path, monkeypatch, mode):
    _, pool, runner = setup(tmp_path, transport="direct", project_context=False, exec_policy="off",
                            cookie_values=[], relay_system_prompt=POLICY, relay_system_prompt_mode=mode)
    sent = capture_direct(monkeypatch)
    run_once(runner, SYSTEM, None)
    run_once(runner, SYSTEM, "auxiliary")
    plain, aux = sent[0][1], sent[1][1]
    # Same upstream session scope, same cache anchoring, same conversation; only the head may differ.
    assert sent[0][0] is sent[1][0] and not pool._active
    assert {key: value for key, value in plain.items() if key not in ("messages", "openAICacheKey", "rit")} == \
           {key: value for key, value in aux.items() if key not in ("messages", "openAICacheKey", "rit")}
    assert plain["messages"][1:] == aux["messages"][1:]
    if mode == "standalone":
        assert plain == aux
    else:
        assert head_of(plain) == SYSTEM + "\n\n" + POLICY and head_of(aux) == SYSTEM


def test_classification_does_not_depend_on_the_header():
    from types import SimpleNamespace
    parsed = SimpleNamespace(system=SYSTEM, prompt="User: x", max_tokens=64, tools=[])
    plain = client_context.request_kind(parsed, {"client": "claude-code"})
    aux = client_context.request_kind(parsed, {"client": "claude-code", "request_class": "auxiliary"})
    assert plain == aux == "client-text"
    assert not client_context.keeps_client_prompt("client-text", {"request_class": "main"})
    assert client_context.keeps_client_prompt("client-text", {"request_class": "compaction"})
    assert client_context.keeps_client_prompt("session-title", {})
