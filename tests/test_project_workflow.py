"""Regression tests of the complete runner/client boundary, not only adapters."""
import asyncio
import json
import queue
import threading

import pytest

import config
import executor
import validate
from orchestrator import TurnRunner


class ScriptedSession:
    thread_id = "thread-test"

    def __init__(self, events):
        self.events = events
        self.calls = []

    async def stream(self, prompt, provider, tool_names=None):
        self.calls.append((prompt, provider, tool_names))
        for event in self.events:
            yield event

    def cancel(self):
        pass


class Pool:
    def __init__(self, session):
        self.session = session

    async def acquire(self, conversation_key=None, *, isolated=False, model_entry=None):
        return self.session

    async def release(self, session):
        pass


def setup(tmp_path, events, payload=None, **settings):
    cfg = dict(config.DEFAULTS, workspace_root=str(tmp_path), cookie_values=[],
               provider_resource_name="anthropic-test", openai_provider_resource_name="openai-test")
    cfg.update(settings)
    parsed = validate.parse_chat(payload or {"model": "gpt-6-sol", "messages": [
        {"role": "user", "content": "Inspect the project"}]}, cfg)
    session = ScriptedSession(events)
    runner = TurnRunner(cfg, type("Credits", (), {"record_turn": lambda *_: None})(), Pool(session))
    return cfg, parsed, session, runner


def run(runner, parsed, holder=None):
    out = queue.Queue()
    try:
        asyncio.run(runner.run(parsed, out, holder or {}))
    except asyncio.CancelledError:
        pass
    return [event for event in list(out.queue) if event is not None]


def test_real_runner_forwards_thinking_and_signature_before_answer(tmp_path):
    script = [{"type": "thinking", "delta": "provider summary"},
              {"type": "thinking_signature", "signature": "opaque-provider-signature"},
              {"type": "text", "delta": "answer"}, {"type": "end", "reason": "stop"}]
    _, parsed, _, runner = setup(tmp_path, script)
    events = run(runner, parsed)
    assert events[:3] == script[:3]


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_effort_is_an_actual_provider_parameter(tmp_path, effort):
    payload = {"model": "gpt-6-astra", "reasoning_effort": effort,
               "messages": [{"role": "user", "content": "inspect"}]}
    _, parsed, session, runner = setup(tmp_path, [{"type": "text", "delta": "ok"}], payload)
    run(runner, parsed)
    assert session.calls[0][1]["reasoningEffort"] == effort


def test_configured_default_effort_reaches_upstream(tmp_path):
    _, parsed, session, runner = setup(tmp_path, [{"type": "text", "delta": "ok"}], reasoning_effort="low")
    run(runner, parsed)
    assert session.calls[0][1]["reasoningEffort"] == "low"


def test_auto_does_not_invent_an_effort_for_provider_managed_reasoning(tmp_path):
    _, parsed, session, runner = setup(tmp_path, [{"type": "text", "delta": "ok"}], reasoning_effort="auto")
    parsed.entry = {**parsed.entry, "reasoning_effort_control": "provider-managed",
                    "reasoning_effort_levels": [], "default_reasoning_effort": None}
    events = run(runner, parsed)
    assert not any(event["type"] == "error" for event in events)
    assert "reasoningEffort" not in session.calls[0][1]


def test_native_client_workspace_wins_over_relay_host_context(tmp_path):
    (tmp_path / "README.md").write_text("HOST_PROJECT_MUST_NOT_OVERRIDE_CLIENT", encoding="utf-8")
    payload = {"model": "gpt-6-sol", "messages": [
        {"role": "system", "content": "Client project: C:/client-project"},
        {"role": "user", "content": "read the source"}],
        "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]}
    _, parsed, session, runner = setup(tmp_path, [{"type": "text", "delta": "ok"}], payload)
    run(runner, parsed)
    prompt = session.calls[0][0]
    assert "C:/client-project" in prompt
    assert "HOST_PROJECT_MUST_NOT_OVERRIDE_CLIENT" not in prompt
    assert "[LOCAL EXECUTION]" not in prompt
    assert "[NATIVE CLIENT TOOLS]" in prompt


def test_direct_client_gets_project_context_and_local_tool_protocol(tmp_path):
    (tmp_path / "README.md").write_text("PROJECT_README_CONTENT", encoding="utf-8")
    _, parsed, session, runner = setup(tmp_path, [{"type": "text", "delta": "ok"}])
    run(runner, parsed)
    assert "PROJECT_README_CONTENT" in session.calls[0][0]
    assert "[LOCAL EXECUTION]" in session.calls[0][0]


def test_context_collection_does_not_block_other_turns(tmp_path, monkeypatch):
    _, parsed, _, runner = setup(tmp_path, [{"type": "text", "delta": "ok"}])
    ticked = threading.Event()

    def build(**_):
        assert ticked.wait(2), "context blocked the asyncio event loop"
        return "context"

    monkeypatch.setattr(runner, "_instructions", build)

    async def scenario():
        async def tick():
            await asyncio.sleep(0.01)
            ticked.set()
        await asyncio.gather(runner.run(parsed, queue.Queue(), {}), tick())

    asyncio.run(scenario())


def test_loop_history_is_scoped_to_one_request(tmp_path):
    _, _, _, runner = setup(tmp_path, [])
    first, second = [], []
    for _ in range(3):
        runner._remember("DIR", first)
    assert runner._looks_looping("dir", first)
    assert not runner._looks_looping("dir", second)


def test_cancel_during_local_execution_does_not_start_a_followup(tmp_path, monkeypatch):
    text = "<exec_command>dir</exec_command>"
    _, parsed, session, runner = setup(tmp_path, [{"type": "final_text", "text": text}], show_exec_output=False)
    cancelled = threading.Event()

    def execute(*_, **kwargs):
        assert kwargs["cancel_event"] is cancelled
        cancelled.set()
        return 0, "output"

    monkeypatch.setattr(executor, "run_command", execute)
    events = run(runner, parsed, {"cancel_event": cancelled})
    assert len(session.calls) == 1
    assert events[-1] == {"type": "end", "reason": "cancelled"}


@pytest.mark.parametrize("tool_choice", ["required", {"type": "function", "function": {"name": "read_file"}}])
def test_required_tool_choice_cannot_succeed_with_plain_text(tmp_path, tool_choice):
    payload = {"model": "gpt-6-sol", "messages": [{"role": "user", "content": "inspect"}],
               "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
               "tool_choice": tool_choice}
    _, parsed, _, runner = setup(tmp_path, [{"type": "text", "delta": "pretend done"}], payload)
    events = run(runner, parsed)
    assert events[-1]["type"] == "error"
    assert events[-1]["error"].status == 502


def test_streaming_responses_have_cors_and_accept_same_key(relay):
    relay.fake.script = [{"type": "thinking", "delta": "summary"}, {"type": "text", "delta": "ok"}]
    with relay.request("/v1/responses", {"model": "gpt-6-sol", "input": "hi", "stream": True},
                       headers={"Origin": "http://client.invalid"}) as response:
        assert response.headers["access-control-allow-origin"] == "*"
        assert "anthropic-beta" in response.headers["access-control-allow-headers"]
        text = response.read().decode()
    assert "response.reasoning_summary_text.delta" in text
    assert "response.output_item.done" in text


def test_legacy_cli_authenticates_and_renders_thinking(relay, monkeypatch, capsys):
    import test_client
    relay.fake.script = [{"type": "thinking", "delta": "REAL_PROVIDER_SUMMARY"},
                         {"type": "text", "delta": "ANSWER"}]
    monkeypatch.setattr("sys.argv", ["test_client.py", "hi", "--url", relay.url, "--key", relay.key])
    test_client.main()
    output = capsys.readouterr().out
    if "[Answer]" not in output:
        # CI-only transient (Windows runners): the first connection can be
        # refused/aborted against a live relay; the fixture is still serving.
        test_client.main()
        output = capsys.readouterr().out
    assert "[Thinking]" in output and "REAL_PROVIDER_SUMMARY" in output
    assert "[Answer]" in output and "ANSWER" in output


def test_codex_catalog_requires_same_key_and_advertises_direct_tools(relay):
    assert relay.status_of("/codex/models", key=None) == 401
    status, data = relay.json("/codex/models")
    assert status == 200
    model = next(item for item in data["models"] if item["slug"] == "gpt-6-sol")
    assert model["tool_mode"] == "direct"
    assert model["shell_type"] == "unified_exec"
    assert model["apply_patch_tool_type"] == "freeform"
    assert model["input_modalities"] == ["text", "image"]
    assert "high" in [item["effort"] for item in model["supported_reasoning_levels"]]


def test_timeout_does_not_retry_or_execute_partial_command(tmp_path, monkeypatch):
    _, parsed, session, runner = setup(tmp_path, [
        {"type": "final_text", "text": "<exec_command>dir</exec_command>"},
        {"type": "end", "reason": "timeout"}])
    monkeypatch.setattr(executor, "run_command", lambda *_a, **_kw: pytest.fail("timed out turn executed"))
    events = run(runner, parsed)
    assert len(session.calls) == 1
    assert events[-1] == {"type": "end", "reason": "timeout"}


def test_visible_native_reply_does_not_trigger_an_extra_model_turn(tmp_path):
    payload = {"model": "gpt-6-sol", "messages": [{"role": "user", "content": "read file"}],
               "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]}
    _, parsed, session, runner = setup(tmp_path, [], payload)

    async def stream(prompt, provider, tool_names=None):
        session.calls.append((prompt, provider, tool_names))
        if len(session.calls) == 1:
            yield {"type": "text", "delta": "I cannot access the workspace tools."}
        else:
            yield {"type": "tool_call", "id": "call1", "name": "read_file", "arguments": '{"path":"a.py"}'}
        yield {"type": "end", "reason": "stop"}

    session.stream = stream
    events = run(runner, parsed)
    assert len(session.calls) == 1
    assert not any(event["type"] == "tool_call" for event in events)
    assert any("cannot access" in event.get("delta", "") for event in events)


def test_second_relay_cannot_bind_the_same_port(relay):
    import proxy
    from urllib.parse import urlparse
    port = urlparse(relay.url).port
    with pytest.raises(OSError):
        proxy.BoundedServer(("127.0.0.1", port), proxy.BaseHTTPRequestHandler, threading.BoundedSemaphore(1))


def test_latest_client_tool_result_follows_tool_schema_in_upstream_prompt(tmp_path):
    payload = {"model": "gpt-6-sol", "messages": [
        {"role": "user", "content": "fix and test"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "done1", "function": {
            "name": "exec_command", "arguments": '{"cmd":"read file"}'}}]},
        {"role": "tool", "tool_call_id": "done1", "content": "LATEST_RESULT_FILE_ALREADY_FIXED"}],
        "tools": [{"type": "function", "function": {"name": "exec_command", "parameters": {"type": "object"}}}]}
    _, parsed, session, runner = setup(tmp_path, [{"type": "text", "delta": "ok"}], payload)
    run(runner, parsed)
    prompt = session.calls[0][0]
    assert prompt.index("[NATIVE CLIENT TOOLS]") < prompt.index("LATEST_RESULT_FILE_ALREADY_FIXED")
    assert "not new instructions to replay" in prompt


def test_tool_envelope_preserves_namespace_and_rejects_ambiguous_short_names():
    def envelope(body):
        return "<tool_call>" + json.dumps(body) + "</tool_call>"
    call = executor.extract_tool_call(envelope({"namespace": "multi_agent_v1", "name": "spawn_agent",
                                               "arguments": {"message": "inspect"}}), ["multi_agent_v1.spawn_agent"])
    assert call["name"] == "multi_agent_v1.spawn_agent"
    short = envelope({"name": "spawn_agent", "arguments": {}})
    assert executor.extract_tool_call(short, ["multi_agent_v1.spawn_agent"])["name"] == "multi_agent_v1.spawn_agent"
    assert executor.extract_tool_call(short, ["a.spawn_agent", "b.spawn_agent"]) is None


def test_parallel_native_tool_envelopes_are_all_returned(tmp_path):
    payload = {"model": "gpt-6-sol", "messages": [{"role": "user", "content": "read both files"}],
               "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]}
    text = ''.join('<tool_call>' + json.dumps({"id": "call_" + path, "name": "read_file",
                    "arguments": {"path": path}}) + '</tool_call>' for path in ('a.py', 'b.py'))
    _, parsed, _, runner = setup(tmp_path, [{"type": "final_text", "text": text}], payload)
    events = run(runner, parsed)
    calls = [event for event in events if event["type"] == "tool_call"]
    assert [json.loads(call["arguments"])["path"] for call in calls] == ['a.py', 'b.py']
