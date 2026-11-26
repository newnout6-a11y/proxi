"""One price, one currency: a Minara turn is counted in Minara credits, a universal provider's turn in US dollars, and
neither is counted again in the other."""
from __future__ import annotations

import http.client
import json
import threading
import types

import httpx
import pytest

import account_report
import config as config_module
import credits
import journal
import proxy
from accounts import UnmeteredAccount
from test_journal_costs import Feed, begin, end, start, usage


def test_only_a_minara_usage_is_counted_in_minara_credits():
    priced = {"provider_cost": 0.05, "provider_cost_currency": "USD"}
    # Minara's usage names the path that answered (minara.py: "chat" or "messages"); its dollars are 220 credits each.
    assert credits.minara_credits(dict(priced, channel_path="chat")) == pytest.approx(11.0)
    assert credits.minara_credits(dict(priced, channel_path="messages")) == pytest.approx(11.0)
    # A universal provider's dollars are dollars: as "11.0 кредитов Minara" they were counted twice (audit A07-02).
    assert credits.minara_credits(priced) is None


# What the orchestrator writes for one Minara reply (both fields, since stage 4) and for one OpenRouter reply.
MINARA_EVENT = dict(credits_reported=False, minara_credits=11.0, cost_usd=0.05, provider_cost=0.05)
PROVIDER_EVENT = dict(credits_reported=False, minara_credits=None, cost_usd=0.02, provider_cost=0.02)


def test_a_minara_reply_is_counted_once_in_minara_credits_and_a_provider_reply_once_in_usd():
    feed = Feed()
    totals = feed.play([begin("m"), start("m", 1), usage("m", 1, None, **MINARA_EVENT), end("m"),
                        begin("p"), start("p", 1), usage("p", 1, None, **PROVIDER_EVENT), end("p")])
    assert totals["minara_credits"] == pytest.approx(11.0)
    assert totals["cost_usd"] == pytest.approx(0.02)  # was 0.07: the Minara reply again, in dollars
    assert totals["credits_unknown"] == 0 and totals["credits_complete"] is True
    traces = {trace["turn_id"]: trace for trace in feed.log.turns()}
    assert (traces["m"]["minara_credits"], traces["m"]["cost_usd"]) == (pytest.approx(11.0), None)
    assert (traces["p"]["minara_credits"], traces["p"]["cost_usd"]) == (None, pytest.approx(0.02))


def test_a_running_total_of_provider_cost_is_never_read_as_a_dollar_spend():
    # provider_cost is the attempt's running total in the upstream's currency, not this event's spend: an event that says
    # nothing in cost_usd says nothing in dollars.
    feed = Feed()
    totals = feed.play([begin("m"), start("m", 1),
                        usage("m", 1, None, credits_reported=False, minara_credits=1.5, provider_cost=0.0068),
                        usage("m", 1, None, credits_reported=False, minara_credits=1.5, provider_cost=0.0136), end("m")])
    assert totals["cost_usd"] == 0 and totals["minara_credits"] == pytest.approx(3.0)


def test_the_usage_report_keeps_minara_money_out_of_the_dollar_total():
    def manager(cost):
        account = UnmeteredAccount()
        account.start_provider_turn()
        account.record_provider_usage("a", "turn", {"provider_cost": cost, "provider_cost_currency": "USD"})
        return account

    rows = [(types.SimpleNamespace(cfg={"account_id": "m", "upstream": "minara"}), manager(0.5)),
            (types.SimpleNamespace(cfg={"account_id": "p", "upstream": "openai-chat", "provider_id": "openrouter"}),
             manager(0.02))]
    report = account_report.usage_report(None, rows)
    assert report["cost_usd"] == pytest.approx(0.02)
    assert report["accounts"][0]["provider_cost"] == pytest.approx(0.5)  # Minara's own row still says what it cost


def _sse(text, cost):
    frames = [{"choices": [{"index": 0, "delta": {"content": text}}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 2000, "completion_tokens": 1000, "cost": cost}}]
    return ("".join("data: %s\n\n" % json.dumps(frame) for frame in frames) + "data: [DONE]\n\n").encode()


def test_a_universal_provider_turn_is_journaled_in_dollars_only(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    journal.JOURNAL.reset()

    def handler(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"usage": 5.0, "limit": 25.0, "limit_remaining": 20.0}})
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=_sse("OK", 0.05))

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "relay_api_key": "ledger-test-key-0000000000", "conversation_state": False, "project_context": False,
        "exec_policy": "off",
        "providers": [{"id": "openrouter", "preset": "openrouter", "api_key": "sk-or-v1-ledger-secret-12345",
                       "models": ["anthropic/claude-sonnet-4.6"]}]}), encoding="utf-8")
    cfg = config_module.load(str(path))
    cfg["journal_path"] = str(tmp_path / "journal.jsonl")
    cfg["accounts"][0]["_http_transport"] = httpx.MockTransport(handler)
    runtime = proxy.Runtime(cfg)
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=20)
    try:
        conn.request("POST", "/v1/chat/completions",
                     json.dumps({"model": "openrouter/anthropic/claude-sonnet-4.6",
                                 "messages": [{"role": "user", "content": "hi"}]}),
                     {"Authorization": "Bearer ledger-test-key-0000000000", "Content-Type": "application/json"})
        answer = conn.getresponse()
        assert answer.status == 200, answer.read()
        answer.read()
        priced = [event for event in journal.JOURNAL.query(categories={"upstream"})
                  if event["kind"] == "upstream.usage" and (event.get("data") or {}).get("cost_usd") is not None]
        assert priced, "the turn was priced"
        for event in priced:
            assert event["data"]["minara_credits"] is None, event
            assert "Minara" not in event["message"], event["message"]
        totals = journal.JOURNAL.metrics(window=3600, bucket=60)["totals"]
        assert totals["cost_usd"] == pytest.approx(0.05) and totals["minara_credits"] == 0
        trace = journal.JOURNAL.turns(limit=5)[0]
        assert trace["cost_usd"] == pytest.approx(0.05) and trace["minara_credits"] is None
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        runtime.close()
