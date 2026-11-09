"""The console names the provider that actually serves each request: Retool or Minara, per conversation."""
import json

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
