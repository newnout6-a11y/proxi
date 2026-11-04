"""One five-POST budget, bounded diagnostics and accurate failure reporting."""
import asyncio
import io
import json
import threading
from types import SimpleNamespace
import urllib.error

import pytest

import client_context
import direct
import journal
from upstream_attempts import AttemptBudget
from upstream_diagnostics import AttemptTrace
from test_native_context_reuse import Backend, FIRST, request, run, setup


class Response(io.BytesIO):
    status = 200
    headers = {"request-id": "upstream-id", "cf-ray": "trace-AMS", "set-cookie": "never-store-cookie"}


def response(*events):
    return Response(b"\n".join(json.dumps(e).encode() for e in events) + b"\n")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    backend, pool, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[],
                                   base="https://example.invalid", cache_keepwarm_seconds=0,
                                   upstream_error_dir=str(tmp_path), upstream_empty_retries=4)
    monkeypatch.setattr(direct, "_EMPTY_DUMPED", set())
    backend.headers = lambda: {"cookie": "never-store-cookie"}
    calls, paid, traces, waits = [], [], [], []
    runner.credits = SimpleNamespace(record_turn=lambda key, cost: paid.append((key, cost)))
    real_emit = journal.emit

    def emit(kind, message="", level="info", **fields):
        traces.append((kind, fields))
        return real_emit(kind, message, level, **fields)

    monkeypatch.setattr(journal, "emit", emit)
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: waits.append(delay))
    state = SimpleNamespace(backend=backend, pool=pool, runner=runner, calls=calls, paid=paid,
                            traces=traces, waits=waits, success=None, before=None)

    def opener(req, **_kw):
        assert req.full_url.startswith("https://example.invalid/")
        calls.append(json.loads(req.data))
        if state.before:
            state.before(len(calls))
        events = []
        if state.success == len(calls):
            events.append({"event": "content_block_delta", "type": "text", "partialText": "OK"})
        events.extend([{"event": "metadata", "usage": {"inputTokens": 4, "outputTokens": 3 if events else 0,
                        "promptTokensReadFromCache": 89000, "creditsUsed": 0.25}}, {"event": "end_of_stream"}])
        return response(*events)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    return state


def send(state, holder=None):
    parsed = request(FIRST, system="CLIENT_SYSTEM " * 100)
    parsed.request_id = "budget-request"
    return asyncio.run(run(state.runner, parsed, holder))


def test_five_attempts_total_have_correlated_diagnostics_and_error_outcome(harness, tmp_path):
    events = send(harness)
    error = events[-1]["error"]
    assert len(harness.calls) == len(harness.paid) == error.upstream_attempts == 5
    assert sum(cost for _, cost in harness.paid) == 1.25
    assert error.code == "upstream_empty_turn" and error.status == 502
    assert error.stage == "upstream" and error.upstream_status == 200
    assert error.upstream_request_id == "upstream-id"
    summaries = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("*-response.json")]
    assert len(summaries) == 5 and {s["attempt"] for s in summaries} == {1, 2, 3, 4, 5}
    assert len({s["diagnostic_id"] for s in summaries}) == 5
    assert next(s for s in summaries if s["attempt"] == 5)["diagnostic_id"] == error.diagnostic_id
    assert all(s["outcome"] == "empty" and s["end_of_stream"] for s in summaries)
    assert len(list(tmp_path.glob("*-body.json"))) == 1
    assert not any("never-store-cookie" in p.read_text(encoding="utf-8") for p in tmp_path.iterdir())
    assert harness.waits == [0, 0.5, 1.0, 2.0, 4.0]
    ends = [d for k, d in harness.traces if k == "turn.end"]
    assert ends[-1]["reason"] == "error"
    assert any(k == "turn.error" for k, _ in harness.traces)
    assert all(body == harness.calls[0] for body in harness.calls)
    assert "<exec_command>" not in json.dumps(harness.calls[3])


@pytest.mark.parametrize("attempt", [1, 4, 5])
def test_success_at_or_before_fifth_attempt_stops_retries(harness, attempt):
    harness.success = attempt
    events = send(harness)
    assert len(harness.calls) == len(harness.paid) == attempt
    assert {"type": "text", "delta": "OK"} in events
    assert events[-1] == {"type": "end", "reason": "stop"}


@pytest.mark.parametrize("thinking_only", [False, True])
@pytest.mark.parametrize("choice", ["auto", "none"])
def test_retry_preserves_task_and_can_still_call_a_tool(harness, monkeypatch, thinking_only, choice):
    def opener(req, **kw):
        body = json.loads(req.data)
        harness.calls.append(body)
        if len(harness.calls) == 4:
            return response({"event": "content_block_start", "type": "tool_call", "toolCallId": "read-next", "toolName": "read_file"},
                            {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"path":"README.md"}'},
                            {"event": "content_block_end"}, {"event": "end_of_stream"})
        return response(*([{"event": "content_block_start", "type": "thinking"},
                           {"event": "content_block_delta", "type": "signature", "partialSignature": "opaque"}]
                          if thinking_only else []),
                        {"event": "metadata", "usage": {"outputTokens": 40 if thinking_only else 0, "creditsUsed": 0.25}},
                        {"event": "end_of_stream"})

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    parsed = request(FIRST, system="Use the current client instructions verbatim.")
    parsed.tool_choice = choice
    events = asyncio.run(run(harness.runner, parsed))
    assert len(harness.calls) == 4
    assert all(body == harness.calls[0] for body in harness.calls)
    assert harness.calls[0]["messages"][0]["content"][0]["text"] == parsed.system
    if choice == "auto":
        assert "toolChoice" not in harness.calls[0]
        assert events[-1] == {"type": "end", "reason": "tool_calls"}
        assert any(e.get("type") == "tool_call" and e.get("name") == "read_file" for e in events)
    else:
        assert all(body["toolChoice"] == "none" for body in harness.calls)
        assert events[-1]["type"] == "error"  # The fake provider violated an explicit choice=none.
    assert harness.waits == [0, 0.5, 1.0, 2.0]
    if thinking_only:
        attempts = [d for k, d in harness.traces if k == "upstream.attempt"]
        assert all(d["outcome"] == "thinking_only" for d in attempts[:3])


@pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "read_file"}}])
def test_empty_retries_respect_required_or_named_tool_choice(harness, choice):
    parsed = request(FIRST)
    parsed.tool_choice = choice
    events = asyncio.run(run(harness.runner, parsed))
    assert len(harness.calls) == 5
    assert "required client tool choice" in str(events[-1]["error"])
    assert all(body == harness.calls[0] for body in harness.calls)
    expected = "required" if choice == "required" else "read_file"
    assert all(body.get("toolChoice") == expected for body in harness.calls)


@pytest.mark.parametrize("kind", ["thinking", "whitespace"])
def test_unusable_response_exhausts_same_five_attempt_budget(harness, monkeypatch, kind):
    def opener(req, **kw):
        harness.calls.append(json.loads(req.data))
        events = ([{"event": "content_block_start", "type": "thinking"},
                   {"event": "content_block_delta", "type": "signature", "partialSignature": "opaque"}]
                  if kind == "thinking" else [{"event": "content_block_delta", "type": "text", "partialText": " \n"}])
        return response(*events, {"event": "metadata", "usage": {"outputTokens": 40, "creditsUsed": 0.25}},
                        {"event": "end_of_stream"})

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    events = send(harness)
    assert len(harness.calls) == events[-1]["error"].upstream_attempts == 5
    assert all(body == harness.calls[0] for body in harness.calls)
    assert events[-1]["error"].code == "upstream_empty_turn"
    assert harness.waits == [0, 0.5, 1.0, 2.0, 4.0]


@pytest.mark.parametrize("dialect", ["chat", "responses", "anthropic"])
def test_same_conversation_uses_current_system_and_tools_on_every_request(harness, dialect):
    harness.success = 1
    first = request(FIRST, system="OLD_CLIENT_INSTRUCTIONS", dialect=dialect)
    assert asyncio.run(run(harness.runner, first))[-1]["reason"] == "stop"
    harness.success = 2
    tools = [{"type": "function", "function": {"name": "new_tool", "description": "New current tool instructions.",
              "parameters": {"type": "object", "properties": {}}}}]
    second = request(FIRST + [{"role": "assistant", "content": "OK"}, {"role": "user", "content": "NEXT"}],
                     system="NEW_CLIENT_INSTRUCTIONS", tools=tools, dialect=dialect)
    assert asyncio.run(run(harness.runner, second))[-1]["reason"] == "stop"
    assert harness.calls[-1]["messages"][0]["content"][0]["text"] == "NEW_CLIENT_INSTRUCTIONS"
    assert [t["name"] for t in harness.calls[-1]["tools"]] == ["new_tool"]
    assert "OLD_CLIENT_INSTRUCTIONS" not in json.dumps(harness.calls[-1])
    assert harness.calls[0]["openAICacheKey"] != harness.calls[-1]["openAICacheKey"]


def test_native_tool_descriptions_are_not_truncated(harness):
    description = "tool instructions " * 120 + "REQUIRED_TOOL_SUFFIX"
    parameter_description = "parameter instructions " * 60 + "REQUIRED_PARAM_SUFFIX"
    tools = [{"type": "function", "function": {"name": "read_file", "description": description,
              "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": parameter_description}}}}}]
    harness.success = 1
    parsed = request(FIRST, tools=tools)
    assert asyncio.run(run(harness.runner, parsed))[-1]["reason"] == "stop"
    tool = harness.calls[0]["tools"][0]
    assert tool["description"] == description
    assert tool["params"]["path"]["description"] == parameter_description


def test_budget_and_dump_are_new_for_each_request_on_same_session(harness, tmp_path):
    first = send(harness)[-1]["error"]
    second = send(harness)[-1]["error"]
    assert len(harness.calls) == 10
    assert first.upstream_attempts == second.upstream_attempts == 5
    assert first.diagnostic_id != second.diagnostic_id
    assert len(list(tmp_path.glob("*-body.json"))) == 1  # one sample per kind+model per run


def test_cancel_during_backoff_never_sends_another_post(harness, monkeypatch):
    cancelled = threading.Event()

    def wait(budget, delay, is_cancelled):
        if delay:
            cancelled.set()
        assert not is_cancelled() or delay

    monkeypatch.setattr(AttemptBudget, "wait", wait)
    events = send(harness, {"cancel_event": cancelled})
    assert len(harness.calls) == 1
    assert events[-1] == {"type": "end", "reason": "cancelled"}
    assert not any(e["type"] == "error" for e in events)


def test_auth_rotation_post_counts_towards_same_five(harness, monkeypatch):
    original = direct.urllib.request.urlopen
    posts = []
    harness.backend.refresher = SimpleNamespace(refresh=lambda **_kw: True)

    def opener(req, **kw):
        posts.append(req)
        if len(posts) == 1:
            raise urllib.error.HTTPError(req.full_url, 401, "expired", {"request-id": "auth-id"}, io.BytesIO(b"expired"))
        return original(req, **kw)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    events = send(harness)
    assert len(posts) == 5 and len(harness.calls) == 4
    assert events[-1]["error"].upstream_attempts == 5


def test_assist_fallback_does_not_reset_budget(harness, monkeypatch):
    original = direct.urllib.request.urlopen
    posts = []
    monkeypatch.setattr(direct, "_ASSIST_ONLY", set(direct._ASSIST_ONLY))

    def opener(req, **kw):
        posts.append(json.loads(req.data))
        if len(posts) == 1:
            raise urllib.error.HTTPError(req.full_url, 400, "unavailable", {},
                                         io.BytesIO(b'{"message":"model is not available right now"}'))
        return original(req, **kw)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    events = send(harness)
    assert len(posts) == 5 and len(harness.calls) == 4
    assert "metadata" in posts[0] and all("metadata" not in body for body in posts[1:])
    assert events[-1]["error"].upstream_attempts == 5


def test_backoff_is_cancellable_before_waiting_for_full_delay():
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError):
        AttemptBudget(cancelled).take(lambda: False)


def test_truncated_response_exposes_its_response_diagnostic(harness, monkeypatch):
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *a, **k: response(
        {"event": "content_block_delta", "type": "text", "partialText": "partial"}))
    events = send(harness)
    error = events[-1]["error"]
    assert error.status == 502 and error.upstream_attempts == 1
    assert error.diagnostic_id and error.upstream_request_id == "upstream-id"
    assert harness.runner.pool._slots._value == 8


@pytest.mark.parametrize("network_error,status", [("disconnect", 502), ("wrapped_timeout", 504)])
def test_network_failure_is_an_upstream_error_with_a_diagnostic(harness, monkeypatch, network_error, status):
    import http.client

    def disconnect(*a, **kw):
        if network_error == "wrapped_timeout":
            raise urllib.error.URLError(TimeoutError("timeout"))
        raise http.client.RemoteDisconnected("server closed response")

    monkeypatch.setattr(direct.urllib.request, "urlopen", disconnect)
    error = send(harness)[-1]["error"]
    assert error.status == status
    assert error.code == ("upstream_error" if status == 502 else "upstream_timeout")
    assert error.diagnostic_id and error.upstream_attempts == 1


def test_parallel_client_requests_have_independent_five_attempt_budgets(harness):
    async def scenario():
        return await asyncio.gather(*(run(harness.runner, request(FIRST, key=key)) for key in ("first", "second")))

    results = asyncio.run(scenario())
    assert len(harness.calls) == len(harness.paid) == 10
    assert all(events[-1]["error"].upstream_attempts == 5 for events in results)
    assert results[0][-1]["error"].diagnostic_id != results[1][-1]["error"].diagnostic_id


def test_budget_cannot_overrun_when_claimed_from_multiple_workers(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import apierr

    monkeypatch.setattr(AttemptBudget, "wait", lambda *args: None)
    budget = AttemptBudget()

    def take(_index):
        try:
            return budget.take(lambda: False)
        except apierr.ApiError as error:
            assert error.code == "upstream_attempt_limit"
            return None

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(take, range(20)))
    assert sorted(n for n in results if n is not None) == [1, 2, 3, 4, 5]
    assert budget.used == 5


def test_confirmed_account_failover_does_not_reset_budget(harness, tmp_path, monkeypatch):
    second = Backend(tmp_path, provider_resource_name="other-account")
    second.cfg.update(harness.backend.cfg)
    second.headers = lambda: {}
    harness.pool.backends.append(second)
    first_post = True
    original = direct.urllib.request.urlopen

    def opener(req, **kw):
        nonlocal first_post
        if first_post:
            first_post = False
            raise urllib.error.HTTPError(req.full_url, 503, "down", {}, io.BytesIO(b"down"))
        return original(req, **kw)

    def check(backend):
        backend.unavailable = {"reason": "exhausted", "available": 1, "threshold": 20}
        return True

    harness.runner.accounts = SimpleNamespace(check_balance_now=check)
    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    events = send(harness)
    assert len(harness.calls) == 4  # plus the first failed HTTP POST
    assert events[-1]["error"].upstream_attempts == 5


def test_summary_is_bounded_and_keeps_signals_without_content(tmp_path):
    budget = AttemptBudget()
    budget.used = 1
    trace = AttemptTrace({"upstream_error_dir": str(tmp_path), "cookie": "auth=secret-credential-value"},
                         budget, "request", "model", b"{}")
    trace.response(Response())
    for i in range(300):
        trace.observe(json.dumps({"event": "unknown%d" % i, "text": "private content" * 10000}))
    trace.observe(json.dumps({"event": "content_block_delta", "type": "thinking", "partialThinking": "private reasoning"}))
    trace.observe(json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "private text"}}))
    trace.observe(json.dumps({"event": "metadata", "stopReason": "secret-credential-value", "finish_reason": "refusal",
                              "usage": {"outputTokens": float('nan')}}))
    trace.observe('{malformed')
    trace.observe('[]')
    trace.observe('{"event":{},"type":[]}')
    trace.observe('{"event":"end_of_stream"}')
    summary = trace.finish("empty")
    p = tmp_path / (trace.id + "-response.json")
    raw = p.read_text(encoding="utf-8")
    assert len(raw) < 16000
    assert len(summary["event_shapes"]) <= 25 and len(summary["unknown_events"]) <= 25
    assert summary["malformed_lines"] == summary["scalar_lines"] == 1
    assert summary["unsupported_event_shapes"]["content_block_delta"] == 1
    assert any(s.get("value") == "refusal" for s in summary["signals"])
    assert summary["thinking_delta_chars"] == len("private reasoning")
    assert summary["text_delta_chars"] == len("private text")
    assert all(s not in raw for s in ("private reasoning", "private text", "private content", "secret-credential-value", "never-store-cookie"))


def test_diagnostic_disk_failure_does_not_hide_empty_error(harness, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    harness.backend.cfg["upstream_error_dir"] = str(blocker / "dir")
    events = send(harness)
    assert len(harness.calls) == 5
    assert events[-1]["error"].code == "upstream_empty_turn"
    assert any(k == "upstream.attempt" and not d["diagnostic_saved"] for k, d in harness.traces)


@pytest.mark.parametrize("stream", [False, True])
def test_http_error_exposes_final_diagnostic_and_attempt_count(relay, stream):
    budget = AttemptBudget()
    budget.used = 5
    budget.last = {"diagnostic_id": "response-diag", "http_status": 200, "upstream_request_id": "remote-id"}
    relay.fake.script = [budget.error()]
    payload = {"model": "claude-opus-5-5", "messages": FIRST, "stream": stream}
    if stream:
        _, body = relay.sse("/v1/chat/completions", payload)
        errors = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ") and '"error"' in line]
        result = errors[-1]
        assert '"finish_reason": "stop"' not in body
    else:
        status, result = relay.json_any("/v1/chat/completions", payload)
        assert status == 502
    assert result["diagnostic_id"] == "response-diag" and result["upstream_attempts"] == 5
    assert result["error"]["code"] == "upstream_empty_turn"
