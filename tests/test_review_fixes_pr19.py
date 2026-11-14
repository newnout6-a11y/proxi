"""Regression tests for the review of PR #19 (findings 19-1 .. 19-8)."""
import asyncio
import io
import json
import os
import ssl
import threading
import time
from types import SimpleNamespace
import urllib.error

import pytest

import adapters
import apierr
import client_context
import config
import direct
import journal
import upstream_refresh
import validate
from scripts import replay_upstream
from upstream_attempts import AttemptBudget
from upstream_diagnostics import AttemptTrace, prune_diagnostics
from test_native_context_reuse import FIRST, request, run, setup


class Response(io.BytesIO):
    status = 200
    headers = {"request-id": "upstream-id"}


def response(*events):
    return Response(b"\n".join(json.dumps(e).encode() for e in events) + b"\n")


def text(value):
    return {"event": "content_block_delta", "type": "text", "partialText": value}


END = [{"event": "metadata", "usage": {"inputTokens": 4, "outputTokens": 1, "creditsUsed": 0.1}},
       {"event": "end_of_stream"}]
THINKING = [{"event": "content_block_start", "type": "thinking"}]


def signature(value):
    return {"event": "content_block_delta", "type": "signature", "partialSignature": value}


@pytest.fixture(autouse=True)
def fresh_run_state(monkeypatch):
    monkeypatch.setattr(direct, "_EMPTY_DUMPED", set())
    monkeypatch.setattr(direct, "_DUMP_RECENT", {})
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)


def stream_direct(tmp_path, monkeypatch, script, **cfg_extra):
    """Run direct._stream_body with one scripted response per POST."""
    cfg = {"base": "https://fixture.invalid", "upstream_error_dir": str(tmp_path / "diag"), **cfg_extra}
    calls = []

    def opener(req, **_kw):
        calls.append(req.data)
        item = script[min(len(calls), len(script)) - 1]
        if isinstance(item, Exception):
            raise item
        return item() if callable(item) else response(*item)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {}), _cancelled=False)

    async def go():
        return [event async for event in direct._stream_body(cfg, session, {"model": "claude-opus-5-5"})]

    return calls, go


# --- 19-1: exec steps do not consume the five-POST retry budget -------------------------

def _exec_runner(tmp_path, monkeypatch, steps, **overrides):
    diag = tmp_path / "diag"
    backend, pool, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[],
                                  base="https://example.invalid", cache_keepwarm_seconds=0,
                                  upstream_error_dir=str(diag), exec_policy="allow_all", exec_allowlist=[],
                                  command_timeout=10, command_output_limit=4000, show_exec_output=False,
                                  **overrides)
    backend.headers = lambda: {"cookie": "x"}
    runner.credits = SimpleNamespace(record_turn=lambda key, cost: None)
    calls = []

    def opener(req, **_kw):
        calls.append(1)
        n = len(calls)
        body = ("<exec_command>echo step%d</exec_command>" % n) if n <= steps else "done"
        return response(text(body), *END)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    return runner, calls, diag


def test_seven_successful_exec_steps_do_not_hit_the_attempt_limit(tmp_path, monkeypatch):
    runner, calls, diag = _exec_runner(tmp_path, monkeypatch, steps=7)
    events = asyncio.run(run(runner, request(FIRST, tools=[], system='')))
    assert len(calls) == 8
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert not any(e["type"] == "error" for e in events)
    assert not list(diag.glob("*-response.json")) if diag.exists() else True


def test_retries_inside_one_exec_step_still_share_the_five_post_budget(tmp_path, monkeypatch):
    runner, calls, _ = _exec_runner(tmp_path, monkeypatch, steps=1, upstream_empty_retries=9)
    original = direct.urllib.request.urlopen

    def opener(req, **kw):
        # Step 1 answers with a command; the follow-up step then stays empty.
        return original(req, **kw) if not calls else (calls.append(1) or response(*END))

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    events = asyncio.run(run(runner, request(FIRST, tools=[], system='')))
    assert len(calls) == 1 + 5
    assert events[-1]["error"].code == "upstream_empty_turn"
    assert events[-1]["error"].upstream_attempts == 6


def test_budget_new_step_resets_only_the_step_counter(monkeypatch):
    budget = AttemptBudget()
    for _ in range(5):
        budget.take(lambda: False)
    with pytest.raises(apierr.ApiError) as limit:
        budget.take(lambda: False)
    assert limit.value.code == "upstream_attempt_limit"
    budget.new_step()
    assert budget.remaining == 5 and budget.take(lambda: False) == 6


def test_hard_total_cap_exists_only_when_max_exec_turns_is_set(tmp_path, monkeypatch):
    for turns, expected in ((0, None), (2, 20)):
        runner, calls, _ = _exec_runner(tmp_path, monkeypatch, steps=0, max_exec_turns=turns)
        holder = {}
        asyncio.run(run(runner, request(FIRST, tools=[]), holder))
        assert holder["_attempt_budget"].total_cap == expected


def test_total_cap_stops_a_runaway_request():
    budget = AttemptBudget(total_cap=3)
    for _ in range(3):
        budget.take(lambda: False)
        budget.new_step()
    with pytest.raises(apierr.ApiError) as limit:
        budget.take(lambda: False)
    assert limit.value.code == "upstream_attempt_limit" and "3 attempts" in limit.value.message


# --- 19-2 / 19-7: nothing from an unusable attempt reaches the client -------------------

def _blocks(dialect_cls, events):
    parsed = validate.Parsed()
    parsed.model, parsed.show_thinking = "claude-opus-5-5", True
    adapter = dialect_cls(parsed, None)
    for event in events:
        adapter.feed(event)
    return adapter


def test_signature_of_a_discarded_attempt_never_reaches_the_client(tmp_path, monkeypatch):
    calls, go = stream_direct(tmp_path, monkeypatch, [
        [*THINKING, signature("SIG_ATTEMPT_1"), *END],
        [*THINKING, signature("SIG_ATTEMPT_2"), text("hello"), *END]])
    events = asyncio.run(go())
    assert len(calls) == 2
    assert [e["signature"] for e in events if e["type"] == "thinking_signature"] == ["SIG_ATTEMPT_2"]
    assert sum(e["type"] == "thinking_start" for e in events) == 1
    types = [e["type"] for e in events]
    assert types.index("thinking_start") < types.index("thinking_signature") < types.index("text")
    adapter = _blocks(adapters.AnthropicAdapter, events)
    assert adapter._blocks == [{"type": "thinking", "thinking": "", "signature": "SIG_ATTEMPT_2"},
                               {"type": "text", "text": "hello"}]


def test_whitespace_only_text_of_a_discarded_attempt_is_not_streamed(tmp_path, monkeypatch):
    calls, go = stream_direct(tmp_path, monkeypatch, [[text(" \n"), *END], [text("hello"), *END]])
    events = asyncio.run(go())
    assert len(calls) == 2
    assert [e["delta"] for e in events if e["type"] == "text"] == ["hello"]
    assert [e["text"] for e in events if e["type"] == "final_text"] == ["hello"]


def test_final_unusable_attempt_still_ends_without_text(tmp_path, monkeypatch):
    calls, go = stream_direct(tmp_path, monkeypatch, [[*THINKING, signature("ONLY"), *END]])
    events = asyncio.run(go())
    assert len(calls) == 2
    assert [e["signature"] for e in events if e["type"] == "thinking_signature"] == ["ONLY"]
    assert events[-1] == {"type": "end", "reason": "stop"}


def test_billing_events_of_a_discarded_attempt_stay_live(tmp_path, monkeypatch):
    calls, go = stream_direct(tmp_path, monkeypatch, [[*THINKING, *END], [text("ok"), *END]])
    events = asyncio.run(go())
    assert sum(e["type"] == "credits" for e in events) == 2


@pytest.mark.parametrize("cls", [adapters.AnthropicAdapter, adapters.ChatAdapter])
def test_adapter_usage_start_cannot_extend_the_previous_attempts_thinking(cls):
    events = []
    for sig, body in (("SIG_ATTEMPT_1", None), ("SIG_ATTEMPT_2", "hello")):
        events += [{"type": "usage_start"}, {"type": "thinking_start"},
                   {"type": "thinking_signature", "signature": sig}]
        if body:
            events.append({"type": "text", "delta": body})
    adapter = _blocks(cls, events)
    if cls is adapters.AnthropicAdapter:
        assert [b.get("signature") for b in adapter._blocks if b["type"] == "thinking"] == [
            "SIG_ATTEMPT_1", "SIG_ATTEMPT_2"]
        assert "SIG_ATTEMPT_1SIG_ATTEMPT_2" not in json.dumps(adapter._blocks)
    else:
        assert adapter.thinking == ""


# --- 19-3: bounded diagnostics --------------------------------------------------------

def _dump(tmp_path, model="claude-opus-5-5", purpose=None):
    session = SimpleNamespace(_request_tag="x")
    token = client_context.bind({"request_purpose": purpose} if purpose else {})
    try:
        # Distinct bodies per kind: identical bodies are deduplicated by sha256.
        payload = json.dumps({"conversation": "AAAA", "kind": [purpose, model]}).encode()
        return direct._dump_empty_once(payload, {"budget": AttemptBudget()},
                                       {"upstream_error_dir": str(tmp_path)}, session, model)
    finally:
        client_context.reset(token)


def test_twenty_empty_requests_leave_one_body_dump(tmp_path):
    for _ in range(20):
        _dump(tmp_path)
    assert len(list(tmp_path.glob("*-body.json"))) == 1


def test_empty_dump_sample_is_kept_per_request_kind_and_model(tmp_path):
    _dump(tmp_path), _dump(tmp_path), _dump(tmp_path, model="other-model")
    _dump(tmp_path, purpose="client-tools"), _dump(tmp_path, purpose="client-tools")
    assert len(list(tmp_path.glob("*-body.json"))) == 3


def test_failed_empty_dump_write_is_not_marked_as_done(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    assert _dump(blocker / "dir") is None
    assert direct._EMPTY_DUMPED == set()
    assert _dump(tmp_path / "ok")
    assert len(list((tmp_path / "ok").glob("*-body.json"))) == 1


def test_dump_sets_are_pruned_to_the_configured_limit(tmp_path):
    cfg = {"upstream_error_dir": str(tmp_path), "upstream_error_max_dumps": 5}
    stamps = [direct._dump_rejected_body(b'{"i":%d}' % i, "bad", cfg=cfg, request_id="r%d" % i) for i in range(12)]
    assert len(list(tmp_path.glob("*-body.json"))) == 5
    assert len(list(tmp_path.glob("*-error.txt"))) == len(list(tmp_path.glob("*-metadata.json"))) == 5
    assert (tmp_path / (stamps[-1] + "-body.json")).exists()


def test_default_dump_retention_is_fifty(tmp_path):
    assert config.DEFAULTS["upstream_error_max_dumps"] == 50
    assert config.DEFAULTS["upstream_attempt_summaries_max"] == 200
    cfg = {"upstream_error_dir": str(tmp_path)}
    for i in range(53):
        direct._dump_rejected_body(b'{"i":%d}' % i, "bad", cfg=cfg, request_id="r%d" % i)
    assert len(list(tmp_path.glob("*-body.json"))) == 50


def test_attempt_summaries_prune_oldest_first(tmp_path):
    now = time.time()
    for i in range(10):
        path = tmp_path / ("s%02d-response.json" % i)
        path.write_text("{}")
        os.utime(path, (now - 100 + i, now - 100 + i))
    prune_diagnostics({"upstream_attempt_summaries_max": 3}, tmp_path)
    assert sorted(p.name for p in tmp_path.glob("*-response.json")) == [
        "s07-response.json", "s08-response.json", "s09-response.json"]


def test_successful_attempt_writes_no_summary_but_an_incident_does(tmp_path):
    cfg = {"upstream_error_dir": str(tmp_path)}
    summaries = {}
    for outcome in ("content", "empty", "thinking_only", "truncated", "error"):
        budget = AttemptBudget()
        budget.used = budget.step_used = 1
        trace = AttemptTrace(cfg, budget, "req", "model", b"{}")
        summaries[outcome] = trace.finish(outcome)
        assert (tmp_path / (trace.id + "-response.json")).exists() == (outcome != "content")
        assert summaries[outcome]["diagnostic_saved"] == (outcome != "content")


def test_error_pointing_at_a_content_attempt_still_finds_its_summary(tmp_path):
    budget = AttemptBudget()
    budget.used = budget.step_used = 1
    trace = AttemptTrace({"upstream_error_dir": str(tmp_path)}, budget, "req", "model", b"{}")
    trace.finish("content")
    assert not list(tmp_path.glob("*-response.json"))
    error = budget.attach(apierr.upstream_error("later failure"))
    assert (tmp_path / (error.diagnostic_id + "-response.json")).exists()


# --- 19-4: configurable retries of an empty answer ------------------------------------

@pytest.mark.parametrize("extra,posts", [({}, 2), ({"upstream_empty_retries": 0}, 1),
                                         ({"upstream_empty_retries": 2}, 3),
                                         ({"upstream_empty_retries": 99}, 5)])
def test_empty_answer_is_retried_a_configurable_number_of_times(tmp_path, monkeypatch, extra, posts):
    calls, go = stream_direct(tmp_path, monkeypatch, [END], **extra)
    asyncio.run(go())
    assert len(calls) == posts


def test_empty_retries_default_and_validation():
    assert config.DEFAULTS["upstream_empty_retries"] == 1
    assert json.load(open(os.path.join(os.path.dirname(config.__file__), "config.example.json"),
                          encoding="utf-8"))["upstream_empty_retries"] == 1


# --- 19-5: required tool_choice error carries diagnostics -----------------------------

@pytest.fixture
def runner_env(tmp_path, monkeypatch):
    backend, pool, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[],
                                  base="https://example.invalid", cache_keepwarm_seconds=0,
                                  upstream_error_dir=str(tmp_path))
    backend.headers = lambda: {}
    runner.credits = SimpleNamespace(record_turn=lambda key, cost: None)
    state = SimpleNamespace(runner=runner, script=[END], calls=[])

    def opener(req, **_kw):
        state.calls.append(1)
        return response(*state.script[min(len(state.calls), len(state.script)) - 1])

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    return state


@pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "read_file"}}])
def test_ignored_tool_choice_after_empty_answer_has_empty_turn_diagnostics(runner_env, tmp_path, choice):
    parsed = request(FIRST)
    parsed.tool_choice = choice
    error = asyncio.run(run(runner_env.runner, parsed))[-1]["error"]
    assert error.code == "upstream_empty_turn" and error.status == 502
    assert "required client tool choice" in error.message
    assert error.upstream_attempts == 2 and error.stage == "upstream"
    assert (tmp_path / (error.diagnostic_id + "-response.json")).exists()


def test_ignored_tool_choice_after_plain_text_has_a_named_code_and_diagnostics(runner_env, tmp_path):
    runner_env.script = [[text("I will not call a tool."), *END]]
    parsed = request(FIRST)
    parsed.tool_choice = "required"
    error = asyncio.run(run(runner_env.runner, parsed))[-1]["error"]
    assert error.code == "upstream_tool_choice_ignored" and error.status == 502
    assert error.upstream_attempts == 1 and error.diagnostic_id
    assert (tmp_path / (error.diagnostic_id + "-response.json")).exists()


# --- 19-6: TLS / OS errors while streaming are 502 --------------------------------------

@pytest.mark.parametrize("exc", [ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
                                 ssl.SSLError("bad record"), OSError("socket broke")])
def test_stream_transport_errors_map_to_502_not_500(tmp_path, monkeypatch, exc):
    class Broken(Response):
        def __iter__(self):
            raise exc

    calls, go = stream_direct(tmp_path, monkeypatch, [lambda: Broken(b"")])
    with pytest.raises(apierr.ApiError) as caught:
        asyncio.run(go())
    assert caught.value.status == 502 and caught.value.code == "upstream_error"
    assert type(exc).__name__ in caught.value.message
    assert caught.value.diagnostic_id


def test_timeouts_stay_504(tmp_path, monkeypatch):
    class Slow(Response):
        def __iter__(self):
            raise TimeoutError("read timed out")

    calls, go = stream_direct(tmp_path, monkeypatch, [lambda: Slow(b"")])
    with pytest.raises(apierr.ApiError) as caught:
        asyncio.run(go())
    assert caught.value.status == 504


# --- 19-8: replay never writes the user's config -----------------------------------------

def _config_file(tmp_path, **extra):
    path = tmp_path / "cfg" / "config.json"
    path.parent.mkdir()
    saved = {"base": "https://tenant.invalid", "page_uuid": "page-1", "ua": "ua", "transport": "direct",
             "cookie": "accessToken=ORIGINALCOOKIE1; xsrfToken=xs1", "xsrf": "xs1",
             "relay_api_key": "relay-key", "workspace_root": str(tmp_path), **extra}
    path.write_text(json.dumps(saved, indent=1) + "\n", encoding="utf-8")
    return path


def test_replay_config_loader_does_not_migrate_or_lock_the_original(tmp_path):
    path = _config_file(tmp_path)  # no account_id: config.load() would write one into the file
    before, listing = path.read_bytes(), sorted(os.listdir(path.parent))
    cfg = replay_upstream.load_config_readonly(path)
    assert cfg["_persist_auth"] is False and "_config_path" not in cfg
    assert cfg["cookie"].startswith("accessToken=ORIGINALCOOKIE1")
    assert path.read_bytes() == before and sorted(os.listdir(path.parent)) == listing


def test_replay_never_persists_a_rotated_cookie(tmp_path, monkeypatch):
    path = _config_file(tmp_path, account_id="acc-1")
    before = path.read_bytes()
    cfg = config.load(str(path))  # persistence enabled, exactly what the old script handed to Refresher
    assert cfg["_persist_auth"] is True
    writes = []
    monkeypatch.setattr(config, "_write_file", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(upstream_refresh.Refresher, "_default",
                        lambda self: ["accessToken=ROTATEDCOOKIE2; Path=/"])
    posts = []

    def opener(req, **_kw):
        posts.append(req.headers.get("Cookie"))
        if len(posts) == 1:
            raise urllib.error.HTTPError(req.full_url, 401, "expired", {}, io.BytesIO(b"expired"))
        return response(text("hi"), *END)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    monkeypatch.setattr(replay_upstream.journal.JOURNAL, "configure", lambda *_a: None)
    source = tmp_path / "source.json"
    source.write_bytes(b'{"model": "claude-opus-5-5", "messages": [], "tools": []}\n')
    manifest = asyncio.run(replay_upstream.replay(cfg, source, tmp_path / "out"))
    assert len(posts) == 2 and "ROTATEDCOOKIE2" in posts[1]  # the retry used the in-memory rotation
    assert writes == [] and path.read_bytes() == before
    assert manifest["outcome"] == "content"


# --- 23: diagnostics failure cannot replace the original result --------------------------

def test_summary_serialisation_failure_does_not_raise(tmp_path):
    budget = AttemptBudget()
    budget.used = budget.step_used = 1
    trace = AttemptTrace({"upstream_error_dir": str(tmp_path), "cookie_values": [12345]},
                         budget, "req", "model", b"{}")
    summary = trace.finish("error")
    assert summary["outcome"] == "error" and summary["diagnostic_saved"] is False
    assert not list(tmp_path.glob("*-response.json"))


def test_nan_in_summary_does_not_raise(tmp_path):
    budget = AttemptBudget()
    budget.used = budget.step_used = 1
    trace = AttemptTrace({"upstream_error_dir": str(tmp_path)}, budget, "req", float("nan"), b"{}")
    assert trace.finish("empty")["outcome"] == "empty"
