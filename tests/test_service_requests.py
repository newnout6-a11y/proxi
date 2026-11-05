"""Client maintenance calls keep their own system prefix and execution boundary."""
import asyncio
import copy
import json
import time

import pytest

import client_context
import direct
import executor
import journal
from session import RelaySession
from test_native_context_reuse import FIRST, request, run, setup


CLASSIFIER = ("You are a security monitor for autonomous AI coding agents.\n" + "stable policy\n" * 15020)
TITLE = "You write short session titles. Reply with only the tagged fields the prompt asks for."


def capture_direct(monkeypatch):
    sent = []

    async def stream(session, prompt, provider, tool_names=None):
        params = session._direct
        body = direct.build_body(session.cfg, provider, params["head"], prompt,
                                 params["tools"], params["tool_choice"], params["max_tokens"],
                                 params["chunks"], session=session)
        sent.append((session, body))
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(RelaySession, "stream", stream)
    return sent


@pytest.mark.parametrize("mode", ["per_conversation", "shared", "per_request"])
def test_classifier_has_own_cacheable_system_before_commands_and_no_parent_tools(tmp_path, monkeypatch, mode):
    _, pool, runner = setup(tmp_path, transport="direct", session_mode=mode,
                            project_context=True, exec_policy="allowlist", cookie_values=[])
    sent = capture_direct(monkeypatch)
    main = request(FIRST, system="MAIN_SYSTEM", dialect="anthropic")
    checks = [request([{"role": "user", "content": command}], tools=[], system=CLASSIFIER,
                      dialect="anthropic") for command in ("CHECK_COMMAND_A", "CHECK_COMMAND_B")]
    token = client_context.bind({"client": "claude-desktop"})

    async def scenario():
        for parsed in (main, *checks, main):
            events = await run(runner, parsed)
            assert events[-1] == {"type": "end", "reason": "stop"}
            assert not pool._active

    try:
        asyncio.run(scenario())
    finally:
        client_context.reset(token)
    first, second = sent[1][1], sent[2][1]
    assert first.get("tools") in (None, []) and second.get("tools") in (None, [])
    assert first["messages"][0] == {"role": "system", "content": [
        {"type": "text", "text": CLASSIFIER, "cache": True}]}
    assert first["messages"][0] == second["messages"][0]
    assert len(first["messages"]) == len(second["messages"]) == 2
    assert "CHECK_COMMAND_A" in str(first["messages"][1])
    assert "CHECK_COMMAND_B" in str(second["messages"][1])
    assert first["openAICacheKey"] == second["openAICacheKey"]
    assert first["openAICacheKey"] != sent[0][1]["openAICacheKey"]
    assert sent[0][1] == sent[-1][1]
    if mode != "per_request":
        assert sent[0][0] is sent[-1][0]
    assert sent[1][0] is not sent[0][0] and sent[2][0] is not sent[0][0]


@pytest.mark.parametrize("client,system,content,maximum", [
    ("claude-desktop", "", ".", 1),
    ("claude-desktop", TITLE, "Generate a title for this session: привет", 64),
    ("claude-code", CLASSIFIER, "Check a proposed Bash action", 64),
    ("opencode", "OpenCode title instructions", "Name this session", 64),
], ids=["connection-probe", "session-title", "auto-mode-check", "opencode-text"])
def test_managed_text_calls_never_load_project_or_enable_relay_exec(tmp_path, monkeypatch, client, system, content, maximum):
    _, _, runner = setup(tmp_path, transport="direct", project_context=True,
                         exec_policy="allowlist", cookie_values=[], command_timeout=3,
                         command_output_limit=4000, exec_allowlist=[])
    sent = capture_direct(monkeypatch)
    parsed = request([{"role": "user", "content": content}], tools=[], system=system)
    parsed.max_tokens = maximum
    monkeypatch.setattr(runner, "_instructions", lambda **_: pytest.fail("client maintenance read the project"))
    token = client_context.bind({"client": client})
    try:
        events = asyncio.run(run(runner, parsed))
    finally:
        client_context.reset(token)
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert parsed.system == system
    assert sent[0][1]["messages"][0]["role"] == ("system" if system else "user")
    assert "LOCAL EXEC" not in json.dumps(sent[0][1])
    if not system:
        assert len(sent[0][1]["messages"]) == 1 and "openAICacheKey" not in sent[0][1]


@pytest.mark.parametrize("transport", ["direct", "agent"])
def test_classifier_output_cannot_run_a_local_relay_command(tmp_path, monkeypatch, transport):
    _, _, runner = setup(tmp_path, transport=transport, project_context=False,
                         exec_policy="allowlist", cookie_values=[], command_timeout=3,
                         command_output_limit=4000, exec_allowlist=[])

    async def stream(session, prompt, provider, tool_names=None):
        yield {"type": "text", "delta": "<exec_command>echo SHOULD_NOT_RUN</exec_command>"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(RelaySession, "stream", stream)
    monkeypatch.setattr(executor, "run_command", lambda *_a, **_kw: pytest.fail("classifier executed a relay command"))
    parsed = request([{"role": "user", "content": "Evaluate this action"}], tools=[], system=CLASSIFIER)
    token = client_context.bind({"client": "claude-code"})
    try:
        events = asyncio.run(run(runner, parsed))
    finally:
        client_context.reset(token)
    assert events[-1] == {"type": "end", "reason": "stop"}


def test_anonymous_relay_task_keeps_its_configured_project_and_execution_support(tmp_path, monkeypatch):
    _, _, runner = setup(tmp_path, transport="direct", project_context=True,
                         exec_policy="allowlist", cookie_values=[], command_timeout=3,
                         command_output_limit=4000, exec_allowlist=[], context_max_bytes=6000,
                         relay_system_prompt='USER_CONFIGURED_RELAY_SYSTEM')
    (tmp_path / "README.md").write_text("SYNTHETIC_PROJECT_CONTEXT", encoding="utf-8")
    calls, commands = [], []

    async def stream(session, prompt, provider, tool_names=None):
        calls.append(copy.deepcopy(session._direct))
        yield {"type": "text", "delta": "<exec_command>echo relay-task</exec_command>" if len(calls) == 1 else "done"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(RelaySession, "stream", stream)
    monkeypatch.setattr(executor, "run_command", lambda command, *_a, **_kw: (commands.append(command) or 0, "SYNTHETIC_OUTPUT"))
    parsed = request([{"role": "user", "content": "Run my relay task"}], tools=[], system="")
    events = asyncio.run(run(runner, parsed))
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert commands == ["echo relay-task"]
    assert "SYNTHETIC_PROJECT_CONTEXT" in calls[0]["head"] and "LOCAL EXEC REMINDER" in calls[0]["head"]
    assert 'USER_CONFIGURED_RELAY_SYSTEM' in calls[0]['head']
    assert "SYNTHETIC_OUTPUT" in str(calls[1]["chunks"])


def test_explicit_tools_with_choice_none_keep_the_client_schema(tmp_path, monkeypatch):
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[])
    sent = capture_direct(monkeypatch)
    first = request(FIRST)
    following = request(FIRST)
    following.tool_choice = "none"

    async def scenario():
        for parsed in (first, following):
            events = await run(runner, parsed)
            assert events[-1] == {"type": "end", "reason": "stop"}

    asyncio.run(scenario())
    assert sent[0][0] is sent[1][0]
    assert sent[0][1]["tools"] == sent[1][1]["tools"]
    assert sent[1][1]["toolChoice"] == "none"


@pytest.mark.parametrize("transport", ["direct", "agent"])
def test_empty_classifier_reply_is_not_rewritten_as_a_generic_coding_task(tmp_path, monkeypatch, transport):
    _, _, runner = setup(tmp_path, transport=transport, project_context=False, cookie_values=[])
    prompts = []

    async def stream(session, prompt, provider, tool_names=None):
        prompts.append(prompt)
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(RelaySession, "stream", stream)
    parsed = request([{"role": "user", "content": "Check the action"}], tools=[], system=CLASSIFIER)
    events = asyncio.run(run(runner, parsed))
    assert events[-1]["type"] == "error"
    assert len(prompts) == 1 and "[RETRY]" not in prompts[0]


@pytest.mark.parametrize("busy", [False, True])
def test_isolated_service_keeps_account_affinity_without_inheriting_parent_state(tmp_path, busy):
    from test_native_context_reuse import Backend

    _, pool, _ = setup(tmp_path, transport="direct", project_context=False)
    other = Backend(tmp_path, provider_resource_name="other-account")
    pool.backends.append(other)

    async def scenario():
        main = await pool.acquire("parent")
        parent = main.session
        parent.set_direct("PRIVATE_PARENT_HEAD", [{"name": "private_tool"}], "auto", 64)
        original = copy.deepcopy(parent._direct)
        if not busy:
            await pool.release(main)
        service = await pool.acquire("parent", isolated=True)
        try:
            assert service.backend is parent.backend
            assert service.session is not parent
            assert service._cached_head is None and not service.has_tools()
            assert parent._direct == original
        finally:
            await pool.release(service)
            await pool.release(main)
        assert not pool._active and pool._slots._value == 8

    asyncio.run(scenario())


@pytest.mark.parametrize("system,prompt,maximum,client,expected", [
    (CLASSIFIER, "User: inspect a Bash action", 64, "claude-code", "auto-mode-check"),
    ("SDK prefix\n\n" + CLASSIFIER, "User: inspect a Bash action", 64, "claude-desktop", "auto-mode-check"),
    (TITLE, "User: " + "long title input " * 300, 64, "claude-desktop", "session-title"),
    ("", "User: .", 1, "claude-desktop", "connection-probe"),
    ("", "User: hi", 64, "claude-code", "client-text"),
    ("", "User: .", 1, "unknown", "generation"),
    ("", "User: hi", 64, "", "generation"),
], ids=["classifier", "wrapped-classifier", "long-title", "desktop-probe", "short-chat", "unknown-probe", "anonymous-chat"])
def test_purpose_uses_known_client_signatures_instead_of_prompt_length(system, prompt, maximum, client, expected):
    from types import SimpleNamespace

    parsed = SimpleNamespace(system=system, prompt=prompt, max_tokens=maximum, tools=[])
    assert client_context.request_kind(parsed, {"client": client}) == expected
    parsed.tools = [{"type": "function"}]
    assert client_context.request_kind(parsed, {"client": client}) == "client-tools"


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 Claude/4.4212.0 Chrome/152.0.0.0 Electron/41.0.0",
    "claude-cli/2.1.309 (external, claude-desktop-3p, agent-sdk/0.3.309)",
])
def test_native_and_sdk_desktop_requests_are_identified_without_session_metadata(ua):
    assert client_context.describe({}, {"user-agent": ua})["client"] == "claude-desktop"


def test_http_logs_distinguish_a_long_title_from_primary_chat(relay, capsys):
    payload = {"model": "claude-opus-5-5", "max_tokens": 64, "system": TITLE,
               "messages": [{"role": "user", "content": "long title input " * 300}]}
    before = journal.JOURNAL.last_seq
    status, _ = relay.json_any("/v1/messages", payload)
    assert status == 200
    assert relay.fake.seen[0].request_purpose == "session-title"
    deadline = time.monotonic() + 2
    while not any(e["kind"] == "http.request" and e["data"].get("request_purpose") == "session-title"
                  for e in journal.JOURNAL.query(since=before)):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert "PURPOSE: session-title" in capsys.readouterr().out


@pytest.mark.parametrize("extra,level", [
    ({"temperature": 1, "metadata": {"user_id": json.dumps({"session_id": "S", "device_id": "D"})}}, "debug"),
    ({"temperature": 0.2, "top_p": 0.9, "top_k": 5, "metadata": {"k": "v"}}, "debug"),
    ({"temperature": 1, "logprobs": True}, "warn"),
    ({"logit_bias": {"50256": -100}}, "warn"),
    ({"temperature": 1, "prompt_cache_key": "PRIVATE-KEY"}, "debug"),
    ({"temperature": 1, "output_config": {"task_budget": {"type": "tokens", "total": 1000}}}, "warn"),
], ids=["claude-code", "sampling-only", "logprobs", "logit-bias", "cache-key", "task-budget"])
def test_routine_ignored_parameters_are_quiet_but_material_ones_still_warn(relay, monkeypatch, extra, level):
    log = journal.Journal()
    monkeypatch.setattr(journal, "JOURNAL", log)
    payload = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}], **extra}
    with relay.request("/v1/messages", payload) as response:
        assert response.status == 200
        names = response.headers["x-proxi-ignored-parameters"].split(",")
    expected = {"output_config.task_budget" if key == "output_config" else key for key in extra}
    assert set(names) == expected
    entries = [e for e in log.query() if e["kind"] == "request.compatibility"]
    assert len(entries) == 1 and entries[0]["level"] == level
    assert entries[0]["data"]["parameters"] == names
    assert "PRIVATE-KEY" not in json.dumps(entries) and '"D"' not in json.dumps(entries)


def test_codex_per_request_hints_do_not_warn_on_every_turn(relay, monkeypatch):
    log = journal.Journal()
    monkeypatch.setattr(journal, "JOURNAL", log)
    payload = {"model": "gpt-6-sol", "input": "hi", "store": False, "include": ["reasoning.encrypted_content"],
               "prompt_cache_key": "PRIVATE-KEY", "client_metadata": {"k": "v"},
               "prompt_cache_options": {"mode": "auto"}}
    with relay.request("/v1/responses", payload) as response:
        assert response.status == 200
        names = set(response.headers["x-proxi-ignored-parameters"].split(","))
    assert {"include", "prompt_cache_key", "client_metadata", "prompt_cache_options"} <= names
    entries = [e for e in log.query() if e["kind"] == "request.compatibility"]
    assert entries and all(e["level"] == "debug" for e in entries)
    assert "PRIVATE-KEY" not in json.dumps(entries)


@pytest.mark.parametrize("model", ["claude-haiku-4-5-20251001", "zai-org/GLM-5.3"])
def test_effort_sent_to_a_route_that_ignores_it_is_not_a_warning(relay, monkeypatch, model):
    log = journal.Journal()
    monkeypatch.setattr(journal, "JOURNAL", log)
    payload = {"model": model, "max_tokens": 2048, "thinking": {"type": "enabled", "budget_tokens": 1024},
               "messages": [{"role": "user", "content": "hi"}]}
    with relay.request("/v1/messages", payload) as response:
        assert response.status == 200
        names = response.headers["x-proxi-ignored-parameters"].split(",")
    assert "thinking.budget_tokens" in names
    entries = [e for e in log.query() if e["kind"] == "request.compatibility"]
    assert len(entries) == 1 and entries[0]["level"] == "debug"


def test_token_estimate_includes_large_tool_schema_and_remains_local(relay):
    payload = {"model": "claude-opus-5-5", "system": "System instructions",
               "messages": [{"role": "user", "content": "hi"}]}
    status, plain = relay.json_any("/v1/messages/count_tokens", payload)
    assert status == 200
    payload["tools"] = [{"name": "large_tool", "description": "tool documentation " * 1000,
                         "input_schema": {"type": "object", "properties": {}}}]
    with relay.request("/v1/messages/count_tokens", payload) as response:
        counted = json.loads(response.read())
        assert response.headers["x-proxi-token-count"] == "estimated"
    assert counted["input_tokens"] > plain["input_tokens"] + 4000
    assert not relay.fake.seen and relay.credits.used == []


TEXT_SEGMENTS = {
    "plain": (["text:a", "text:b"], 1),
    "thinking-first": (["think", "text:a"], 1),
    "text-thinking-text": (["text:a", "think", "text:b"], 2),
    "three-segments": (["text:a", "think", "text:b", "think", "text:c"], 3),
    "heartbeat-is-no-boundary": (["text:a", "beat", "text:b"], 1),
    "text-tool-text": (["text:a", "call", "text:b"], 2),
    "empty-delta-opens-nothing": (["text:", "text:a"], 1),
}


@pytest.mark.parametrize("name", sorted(TEXT_SEGMENTS))
def test_each_text_segment_opens_its_own_answer_span(tmp_path, monkeypatch, name):
    script, expected = TEXT_SEGMENTS[name]
    log = journal.Journal()
    monkeypatch.setattr(journal, "JOURNAL", log)
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[])

    async def stream(session, prompt, provider, tool_names=None):
        for item in script:
            kind, _, value = item.partition(":")
            if kind == "text":
                yield {"type": "text", "delta": value}
            elif kind == "think":
                yield {"type": "thinking_start"}
                yield {"type": "thinking", "delta": "planning"}
                yield {"type": "thinking_signature"}
            elif kind == "beat":
                yield {"type": "thinking_heartbeat", "elapsed": 5.0}
            else:
                yield {"type": "tool_call", "id": "call-1", "name": "read_file", "arguments": "{}"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(RelaySession, "stream", stream)
    events = asyncio.run(run(runner, request(FIRST)))
    assert not any(event["type"] == "error" for event in events)
    starts = [e for e in log.query() if e["kind"] == "upstream.text_start"]
    assert len(starts) == expected
