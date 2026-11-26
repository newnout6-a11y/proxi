"""A prompt suggestion ([SUGGESTION MODE: ...] of Claude Code and Claude Desktop) runs on a fast light model, alone.

Audits A15-03..06, A02-03/09/12, A01-02/04/05 and design_routing_errors.md section 5 found _optimize_prompt_suggestion
(54b1023) half done: a chain with a model no catalog has (fast-auto) and two reasoning models; the chosen model's level
was its default when it had no none/minimal/low (a suggestion on max); an empty native_messages list that made Minara send
the instruction alone, with no conversation; message chunks with an image sent as a Python repr of base64; the background
mark wiped right after it was set; a second enforce_request that took a second RPM slot of a client key; and a check that
asked whether the model is served at all, not whether an account can take it now.
"""
import asyncio
import json
import threading
import urllib.request

import pytest

import apierr
import client_context
import config as config_module
import proxy
from conftest import KEY, FakeCredits, FakeRunner
from session import RelaySession
from test_native_context_reuse import request, run, setup
from test_service_requests import SUGGESTION_PROMPT

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
CLIENT_KEY = "sk-proxi-suggestion-client-key"


@pytest.fixture
def served(monkeypatch, tmp_path):
    cfg = dict(config_module.DEFAULTS)
    cfg.update({
        "base": "https://example.invalid", "page_uuid": "page-1", "thread_id": "thread-1",
        "ua": "test-agent", "cookie": "accessToken=abc123456789xyz; xsrfToken=q",
        "xsrf": "q", "relay_api_key": KEY, "workspace_root": str(tmp_path),
        "command_log": str(tmp_path / "commands.log"), "host": "127.0.0.1", "port": 0,
        "cookie_values": ["abc123456789xyz"],
        "client_keys": [{"id": "ide", "key": CLIENT_KEY, "rpm_limit": 2}],
    })
    runtime = proxy.Runtime(cfg)
    runtime.credits = FakeCredits()
    runtime.runner = FakeRunner()
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(payload, key=KEY):
        req = urllib.request.Request("http://127.0.0.1:%d/v1/messages" % server.server_address[1],
                                     data=json.dumps(payload).encode(), method="POST")
        req.add_header("content-type", "application/json")
        req.add_header("authorization", "Bearer " + key)
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, dict(response.headers), json.loads(response.read())

    runtime.call = call
    yield runtime
    server.shutdown()
    server.server_close()


def suggestion(model="claude-opus-5-5", earlier=None, effort="max"):
    return {"model": model, "max_tokens": 16000, "output_config": {"effort": effort}, "system": "AGENT SYSTEM",
            "tools": [{"name": "Read", "description": "read", "input_schema": {"type": "object", "properties": {}}}],
            "messages": [*(earlier or [{"role": "user", "content": "Проверь логи"},
                                       {"role": "assistant", "content": "Логи чистые."}]),
                         {"role": "user", "content": [{"type": "text", "text": SUGGESTION_PROMPT}]}]}


def test_the_suggestion_is_a_background_job_without_native_history(served):
    status, headers, _ = served.call(suggestion())
    seen = served.runner.seen[-1]
    assert status == 200 and seen.request_purpose == "prompt-suggestion"
    assert seen.model == "claude-haiku-4-5-20251001"
    # The mark the method sets survives (it was wiped to "" right after): no keepwarm, no tail written to a cache.
    assert seen.client_task == client_context.BACKGROUND
    # No empty list in place of the history: a transport that builds from native messages (Minara, the universal drivers)
    # must build from the slimmed chunks instead.
    assert seen.native_messages is None
    assert seen.structured_output is None and seen.tools == []


def test_an_image_in_the_conversation_is_named_not_sent_as_a_python_repr(served):
    earlier = [{"role": "user", "content": [
                   {"type": "text", "text": "что на скриншоте?"},
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}]},
               {"role": "assistant", "content": "Окно терминала."}]
    served.call(suggestion(earlier=earlier))
    seen = served.runner.seen[-1]
    assert all(isinstance(chunk, str) for chunk in seen.message_chunks)
    assert "imageData" not in seen.prompt and PNG[:20] not in seen.prompt and "{'type'" not in seen.prompt
    assert "что на скриншоте?" in seen.prompt and "[image]" in seen.prompt
    assert SUGGESTION_PROMPT in seen.prompt


def test_one_suggestion_takes_one_request_slot_of_a_client_key(served):
    served.call(suggestion(), key=CLIENT_KEY)
    stats = next(item for item in served.client_keys.list_public() if item["id"] == "ide")
    assert stats["requests"] == 1
    assert len(served.client_keys._rpm_stamps["ide"]) == 1
    # The second request of the minute is still served (rpm_limit 2): it got 429 before.
    assert served.call(suggestion(), key=CLIENT_KEY)[0] == 200


def test_a_candidate_no_account_can_take_now_is_skipped(served, monkeypatch):
    real = served.pool.unavailable_error

    def unavailable(entry, **kwargs):
        if entry.get("id") == "claude-haiku-4-5-20251001":
            return apierr.accounts_rate_limited(600, 1, 1)
        return real(entry, **kwargs)

    monkeypatch.setattr(served.pool, "unavailable_error", unavailable)
    served.call(suggestion())
    seen = served.runner.seen[-1]
    # The next model of the chain, on its lowest level: never the haiku every account of which is at its limit.
    assert seen.model == "gpt-5-nano" and seen.effort == "low"
    assert seen.max_tokens >= 1024  # a reasoning model spends tokens before its few words


def test_same_runs_the_chat_model_on_its_lowest_level(served, monkeypatch):
    monkeypatch.setitem(served.cfg, "prompt_suggestion_model", "same")
    served.call(suggestion(model="gpt-5.5", effort="high"))
    seen = served.runner.seen[-1]
    assert seen.model == "gpt-5.5" and seen.effort == "low"


def test_the_lowest_level_is_taken_even_without_none_minimal_or_low(served, monkeypatch):
    monkeypatch.setitem(served.cfg, "prompt_suggestion_model", "gpt-5.5")
    import models
    original = models.resolve

    def resolve(name, *args, **kwargs):
        found, effort = original(name, *args, **kwargs)
        if found["id"] == "gpt-5.5":
            found = dict(found, reasoning_effort_levels=["max", "high", "medium"])
        return found, effort

    monkeypatch.setattr(models, "resolve", resolve)
    served.call(suggestion())
    seen = served.runner.seen[-1]
    assert seen.model == "gpt-5.5" and seen.effort == "medium"  # never the model's default (a suggestion on max)


def test_a_suggestion_cut_by_its_token_cap_still_gives_its_words(tmp_path, monkeypatch):
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[])

    async def capped(session, prompt, provider, tool_names=None):
        yield {"type": "text", "delta": "Запусти тесты"}
        yield {"type": "end", "reason": "length"}

    monkeypatch.setattr(RelaySession, "stream", capped)
    parsed = request([{"role": "user", "content": SUGGESTION_PROMPT}], tools=[], system="")
    parsed.request_purpose = "prompt-suggestion"
    events = asyncio.run(run(runner, parsed))
    assert {"type": "text", "delta": "Запусти тесты"} in events
    assert events[-1]["type"] == "end"


def test_a_suggestion_on_minara_carries_the_conversation(tmp_path, monkeypatch):
    # A15-03: native_messages=[] made minara.build_body send the instruction alone, with no message of the conversation.
    import config
    import models
    import types
    from test_minara_failover import Response, ok_stream
    monkeypatch.setattr(models, "_MINARA_MODELS", dict(models._MINARA_MODELS))
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RELAY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "accounts": [{"account_id": "suggest-1", "label": "Minara 1", "upstream": "minara",
                      "bearer_token": "synthetic-bearer-1", "minara_models": [{"id": "openai/suggest-test"}]}],
        "model": "minara/openai/suggest-test", "relay_api_key": KEY, "workspace_root": str(tmp_path),
        "project_context": False, "exec_policy": "off", "conversation_state": False, "cache_keepwarm_seconds": 0,
        "session_refresh_interval": 0, "account_balance_interval": 0, "minara_channel_retry_delays": [],
        "journal_path": str(tmp_path / "journal.jsonl"), "prompts_file": str(tmp_path / "absent.json"),
        "prompt_suggestion_model": "same"}), encoding="utf-8")
    runtime = proxy.Runtime(config.load(str(path)))
    bodies = []

    def post(url, **kwargs):
        bodies.append(kwargs["json"])
        return Response(200, ok_stream("Запусти тесты"))

    for backend in runtime.accounts.backends:
        backend._minara_post = post
        backend._minara_get = lambda url, **kwargs: types.SimpleNamespace(
            status_code=200, json=lambda: {"creditBalanceBreakdown": {"plan": "200", "extra": "0"}})
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/v1/messages" % server.server_address[1], method="POST",
                                     data=json.dumps(suggestion(model="minara/openai/suggest-test")).encode())
        req.add_header("content-type", "application/json")
        req.add_header("authorization", "Bearer " + KEY)
        with urllib.request.urlopen(req, timeout=20) as response:
            status, body = response.status, json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()
    assert status == 200 and body["content"][0]["text"] == "Запусти тесты"
    assert len(bodies) == 1
    roles = [message["role"] for message in bodies[0]["messages"]]
    assert roles[0] == "system" and "user" in roles
    sent = json.dumps(bodies[0]["messages"], ensure_ascii=False)
    assert "[SUGGESTION MODE:" in sent and "Проверь логи" in sent
    assert not bodies[0].get("tools")
