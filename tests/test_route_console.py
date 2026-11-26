"""The console names the provider that actually serves each request: Retool or Minara, per conversation."""
import json
import time

import console_log
import journal
from test_minara_mixed_clients import KEY, LOCAL, REMOTE, mixed  # noqa: F401


def test_route_line_has_explicit_provider_and_no_terminal_control_in_fields():
    value = console_log.route_line('r\n1', upstream='minara', account='abc123', model='public',
                                  upstream_model='anthropic/actual\x1b[31m', effort='high', child=True)
    assert 'provider=Minara account=abc123' in value
    assert 'model=public upstream_model=anthropic/actual' in value
    assert 'effort=high kind=subagent' in value
    assert '\n' not in value and '\x1b' not in value


def test_main_agent_on_retool_and_subagent_on_minara_each_show_their_own_provider(mixed, capsys):  # noqa: F811
    """The client picks the provider by model name: the main agent on a Retool model, its subagent on a minara/ one."""
    body = {'messages': [{'role': 'user', 'content': 'hi'}]}
    assert mixed.call('/v1/chat/completions', dict(body, model=LOCAL), 'main')[0] == 200
    assert mixed.call('/v1/chat/completions', dict(body, model=REMOTE), 'child')[0] == 200
    output = capsys.readouterr().out
    routes = [line for line in output.splitlines() if '[route]' in line]
    assert len(routes) == 2
    assert 'provider=Retool' in routes[0] and 'model=claude-opus-5-5' in routes[0]
    assert 'provider=Minara' in routes[1] and 'upstream_model=openai/mixed-test' in routes[1]
    assert 'synthetic-minara-bearer' not in output and 'synthetic-retool-cookie' not in output
    # The journal is shared by the tests of a worker: look at this test's two models only.
    events = [event for event in journal.JOURNAL._events if event.get('kind') == 'route.bound'
              and event['data'].get('model') in (LOCAL, 'minara/mixed-test')]
    assert {event['data']['upstream'] for event in events} >= {'minara', 'retool'}
    dumped = json.dumps(events)
    assert 'synthetic-minara-bearer' not in dumped and 'synthetic-retool-cookie' not in dumped


def test_jsonl_correlates_http_route_session_latency_and_usage_in_their_own_units(mixed, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RELAY_LOG_FORMAT", "jsonl")
    since = journal.JOURNAL.last_seq
    for model, session in ((LOCAL, "raw-local-session"), (REMOTE, "raw-remote-session")):
        body = {"model": model, "messages": [{"role": "user", "content": "private-prompt-marker"}]}
        assert mixed.call("/v1/chat/completions", body, session)[0] == 200
    until = time.monotonic() + 3
    while sum(event["kind"] == "http.request" for event in journal.JOURNAL.query(since=since)) < 2:
        assert time.monotonic() < until
        time.sleep(0.01)
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines()]
    assert not any(value in output for value in (KEY, "synthetic-minara-bearer", "synthetic-retool-cookie",
                                                 "raw-local-session", "raw-remote-session", "private-prompt-marker"))
    finals = [event for event in records if event["kind"] == "http.request"]
    assert len(finals) == 2
    assert len({event["conversation"] for event in finals}) == 2
    for final in finals:
        related = [event for event in records if event.get("request_id") == final["request_id"]]
        bound, = [event for event in related if event["kind"] == "route.bound"]
        session, = [event for event in related if event["kind"] == "session.acquire"]
        first, = [event for event in related if event["kind"] == "upstream.first_token"]
        usage, = [event for event in related if event["kind"] == "upstream.usage"]
        assert all(event["account"] == bound["account"] for event in (final, first, usage))
        assert all(event["turn_id"] == final["turn_id"] for event in (bound, session, first, usage))
        assert len(session["thread"]) == 8 and final["client_session"]
        assert final["http_status"] == final["status"] == 200 and final["level"] == "success"
        assert first["ttft_ms"] >= 0 and first["first"] == "text"
        assert usage["output_tokens"] == 3 and usage["input_tokens"] == 2
        if bound["upstream"] == "retool":
            assert usage["retool_credits"] == 0.1 and "minara_credits" not in usage
        else:
            assert usage["minara_credits"] == 0.22 and usage["provider_cost_currency"] == "USD"
            assert usage["cost_usd"] == 0.001 and "retool_credits" not in usage
