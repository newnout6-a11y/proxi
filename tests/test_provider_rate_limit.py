"""A universal provider's 429 is its own rate limit, not Retool's hourly quota (audit A02-08).

drivers/sse_utils.map_http_error turns any 429 of a provider into an account-quota error. The orchestrator then took it
for Retool's hourly managed-key limit: the provider was parked for account_rate_limit_cooldown (900 s, a ladder up to
1200 s) and the journal and the console said "часовой лимит Retool". With one provider configured (the usual universal
proxy), every request got the pool's 429 for a quarter of an hour after one per-minute limit.
"""
import http.client
import json
import threading

import httpx

import config as config_module
import journal
import proxy
from test_stage3_drivers_and_cache import _sse_bytes


def _served(tmp_path, monkeypatch, first_headers=None):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    calls = {"anth": 0, "or": 0}

    def anth_handler(request):
        calls["anth"] += 1
        return httpx.Response(429, headers=first_headers or {}, json={"error": {"message": "Rate limited"}})

    def or_handler(request):
        calls["or"] += 1
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=_sse_bytes([
            (None, {"choices": [{"index": 0, "delta": {"content": "Hello from the second provider"}}]}),
            (None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 6, "cost": 0.0005}}),
            (None, "[DONE]")]))

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "relay_api_key": "provider-429-key", "conversation_state": False, "project_context": False,
        "exec_policy": "off", "upstream_rate_limit_retries": 0,
        "providers": [
            {"id": "anth", "preset": "anthropic", "api_key": "sk-ant-test12345678", "models": ["claude-sonnet-4-6"]},
            {"id": "or", "preset": "openrouter", "api_key": "sk-or-v1-test12345678",
             "models": ["anthropic/claude-sonnet-4.6"]}],
        "routes": [{"id": "claude-prod", "targets": ["anth/claude-sonnet-4-6", "or/anthropic/claude-sonnet-4.6"]}],
    }), encoding="utf-8")
    cfg = config_module.load(str(path))
    cfg["journal_path"] = str(tmp_path / "journal.jsonl")
    cfg["accounts"][0]["_http_transport"] = httpx.MockTransport(anth_handler)
    cfg["accounts"][1]["_http_transport"] = httpx.MockTransport(or_handler)
    runtime = proxy.Runtime(cfg)
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    before = journal.JOURNAL.last_seq
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request("POST", "/v1/chat/completions",
                           json.dumps({"model": "claude-prod", "messages": [{"role": "user", "content": "Hi"}]}),
                           {"Authorization": "Bearer provider-429-key", "Content-Type": "application/json"})
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        status = response.status
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        runtime.close()
    limited = next(backend for backend in runtime.pool.backends if backend.cfg.get("provider_id") == "anth")
    return status, body, calls, limited, journal.JOURNAL.query(since=before, limit=5000)


def test_a_provider_429_without_retry_after_parks_it_briefly_and_moves_the_turn(tmp_path, monkeypatch, capsys):
    status, body, calls, limited, logged = _served(tmp_path, monkeypatch)
    assert status == 200 and body["choices"][0]["message"]["content"] == "Hello from the second provider"
    assert calls == {"anth": 1, "or": 1}
    # A short pause, never Retool's 15-minute hourly-limit cooldown or its ladder.
    assert limited.unavailable["reason"] == "rate_limited" and limited.unavailable["pause"] <= 60
    moved = [event for event in logged if event["kind"] == "account.failover"]
    assert len(moved) == 1 and "Retool" not in moved[0]["message"] and "Anthropic" in moved[0]["message"]
    assert moved[0]["data"]["reason"] == "rate_limited"
    out = capsys.readouterr().out
    assert "часовой лимит Retool, повтор" not in out


def test_a_provider_429_with_retry_after_parks_it_for_that_long(tmp_path, monkeypatch):
    status, _, calls, limited, _ = _served(tmp_path, monkeypatch, {"Retry-After": "30"})
    assert status == 200 and calls == {"anth": 1, "or": 1}
    assert (limited.unavailable["pause"], limited.unavailable["source"]) == (30.0, "retry_after")
