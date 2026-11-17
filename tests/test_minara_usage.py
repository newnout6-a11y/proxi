"""Provider monetary usage never becomes Retool credits or invented currency."""
import json
import types

import pytest

import adapters
import config
import models
from accounts import UnmeteredAccount
from credits import provider_cost_summary


def test_manager_snapshot_dedup_unknown_cost_and_turns():
    manager = UnmeteredAccount()
    manager.start_provider_turn()
    manager.record_provider_usage('a1', 'turn1', {})
    manager.record_provider_usage('a1', 'turn1', {'provider_cost': 0.1, 'provider_cost_currency': 'USD'})
    manager.record_provider_usage('a1', 'turn1', {'provider_cost': 0.2, 'provider_cost_currency': 'USD'})
    manager.record_provider_usage('a2', 'turn1', {})
    result = manager.usage()
    assert result['turns'] == 1 and result['attempts'] == 2
    assert result['provider_cost'] == 0.2 and not result['provider_cost_complete']
    assert result['session_used'] is None and manager.get_balance()['unit'] is None
    manager.record_provider_usage('a2', 'turn1', {'provider_cost': 0.3, 'provider_cost_currency': 'USD'})
    assert manager.usage()['provider_cost'] == 0.5 and manager.usage()['provider_cost_complete']


@pytest.mark.parametrize('currencies', [(None, None), ('USD', 'EUR')])
def test_costs_of_unknown_or_different_currency_not_summed(currencies):
    result = provider_cost_summary([{'provider_cost': 0.1, 'provider_cost_currency': currencies[0]},
                                    {'provider_cost': 0.2, 'provider_cost_currency': currencies[1]}])
    assert result['provider_cost'] is None and len(result['provider_cost_records']) == 2


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
def test_cost_metadata_preserved_by_each_adapter(dialect, stream):
    import validate
    from test_adapter_protocols import Writer
    before = dict(models._MINARA_MODELS)
    models.configure_minara([{'upstream': 'minara', 'minara_models': ['openai/usage-test']}])
    cfg = dict(config.DEFAULTS, model='minara/openai/usage-test')
    parse = getattr(validate, 'parse_' + dialect)
    body = {'input': 'hi'} if dialect == 'responses' else {'messages': [{'role': 'user', 'content': 'hi'}]}
    if dialect == 'anthropic':
        body['max_tokens'] = 64
    try:
        parsed = parse(body, cfg)
        writer = Writer() if stream else None
        adapter = adapters.for_dialect(dialect, parsed, writer)
        adapter.begin()
        adapter.feed({'type': 'usage_start'})
        adapter.feed({'type': 'usage_start'})  # duplicate orchestration/transport boundary
        adapter.feed({'type': 'request_sent'})
        adapter.feed({'type': 'text', 'delta': 'OK'})
        for _ in range(2):
            adapter.feed({'type': 'usage', 'usage': {'input_tokens': 2, 'output_tokens': 1,
                          'provider_cost': 0.12345, 'provider_cost_currency': None}})
        result = adapter.end('stop')
        encoded = json.dumps(result if not stream else writer.events)
        assert 'provider_cost' in encoded and '0.12345' in encoded
        price = adapter._price_usage()
        assert price['provider_cost'] == 0.12345 and price['provider_cost_complete']
        assert len(price['provider_cost_records']) == 1 and price['credits_used'] is None
    finally:
        models._MINARA_MODELS.clear()
        models._MINARA_MODELS.update(before)


def test_failed_attempt_after_known_attempt_keeps_partial_cost():
    parsed = types.SimpleNamespace(entry={'upstream': 'minara'}, show_thinking=False, model='minara/test')
    adapter = adapters.ChatAdapter(parsed)
    adapter.feed({'type': 'usage_start'})
    adapter.feed({'type': 'request_sent'})
    adapter.feed({'type': 'usage', 'usage': {'provider_cost': 0.1, 'provider_cost_currency': 'USD'}})
    adapter.feed({'type': 'usage_start'})
    adapter.feed({'type': 'request_sent'})
    adapter.feed({'type': 'text', 'delta': 'truncated'})
    result = adapter._price_usage()
    assert result['provider_cost'] == 0.1 and not result['provider_cost_complete']


@pytest.fixture
def http_usage(tmp_path, monkeypatch):
    import http.client
    import threading
    import proxy
    before = dict(models._MINARA_MODELS)
    for key in ('RELAY_API_KEY', 'RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF'):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'upstream': 'minara', 'bearer_token': 'synthetic-usage-token',
        'minara_models': ['openai/usage-test'], 'model': 'minara/openai/usage-test',
        'relay_api_key': 'synthetic-usage-relay-key', 'workspace_root': str(tmp_path),
        'project_context': False, 'exec_policy': 'allow_all', 'conversation_state': False,
        'cache_keepwarm_seconds': 0, 'journal_path': str(tmp_path / 'journal.jsonl')}), encoding='utf8')
    runtime = proxy.Runtime(config.load(str(path)))
    server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)

    def call(route, body=None):
        conn.request('POST' if body is not None else 'GET', route, json.dumps(body) if body is not None else None,
                     {'Authorization': 'Bearer synthetic-usage-relay-key', 'Content-Type': 'application/json'})
        response = conn.getresponse()
        return response.status, response.read().decode()
    try:
        yield runtime, call
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        runtime.close()
        thread.join(2)
        models._MINARA_MODELS.clear()
        models._MINARA_MODELS.update(before)


def scripted_response(text='OK', cost=0.125, currency=None, truncated=False):
    class Response:
        status_code = 200
        headers = {'content-type': 'text/event-stream'}
        def iter_content(self, chunk_size):
            usage = {'prompt_tokens': 2, 'completion_tokens': 1}
            if cost is not None:
                usage['cost'] = cost
            if currency is not None:
                usage['currency'] = currency
            frame = {'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': 'stop'}], 'usage': usage}
            yield ('data: ' + json.dumps(frame) + '\n\n').encode()
            if not truncated:
                # Duplicate usage snapshot is not another paid attempt.
                yield ('data: ' + json.dumps({'choices': [], 'usage': usage}) + '\n\ndata: [DONE]\n\n').encode()
        def close(self):
            pass
    return Response()


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'messages'])
@pytest.mark.parametrize('stream', [False, True])
def test_actual_http_provider_cost_and_truthful_account_turns(http_usage, dialect, stream):
    runtime, call = http_usage
    runtime.backend._minara_post = lambda *a, **kw: scripted_response()
    route = {'chat': '/v1/chat/completions', 'responses': '/v1/responses', 'messages': '/v1/messages'}[dialect]
    body = {'input': 'hi'} if dialect == 'responses' else {'messages': [{'role': 'user', 'content': 'hi'}]}
    body.update(model='minara/openai/usage-test', stream=stream)
    if dialect == 'messages':
        body['max_tokens'] = 64
    status, raw = call(route, body)
    assert status == 200 and '"provider_cost": 0.125' in raw, raw
    # Minara's usage.cost is US dollars (220 of its credits per 1.0, measured on 2026-11-09).
    assert '"provider_cost_currency": "USD"' in raw
    status, raw = call('/v1/usage')
    usage = json.loads(raw)
    assert usage['turns'] == 1 and usage['session_used'] is None
    assert usage['accounts'][0]['attempts'] == 1
    assert usage['accounts'][0]['provider_cost'] == 0.125
    assert usage['accounts'][0]['provider_cost_complete']


def test_two_exec_attempts_dedup_and_costs_separate_from_credits(http_usage, monkeypatch):
    import executor
    runtime, call = http_usage
    posts = []
    monkeypatch.setattr(executor, 'run_command', lambda *a, **kw: (0, 'EXEC_RESULT'))
    def post(*a, **kw):
        posts.append(kw['json'])
        return scripted_response('<exec_command>echo hi</exec_command>' if len(posts) == 1 else 'done',
                                 cost=0.1 if len(posts) == 1 else 0.2, currency='USD')
    runtime.backend._minara_post = post
    status, raw = call('/v1/chat/completions', {'model': 'minara/openai/usage-test',
        'messages': [{'role': 'user', 'content': 'inspect'}]})
    assert status == 200, raw
    usage = json.loads(raw)['usage']
    assert usage['provider_cost'] == 0.3 and usage['provider_cost_currency'] == 'USD'
    assert usage['credits_used'] is None and usage['provider_cost_complete']
    usage = json.loads(call('/v1/usage')[1])
    assert usage['turns'] == 1 and usage['accounts'][0]['attempts'] == 2
    assert usage['accounts'][0]['provider_cost'] == 0.3 and len(posts) == 2


def test_failed_upstream_attempt_is_counted_unknown_not_free(http_usage):
    runtime, call = http_usage
    runtime.backend._minara_post = lambda *a, **kw: scripted_response(cost=None, truncated=True)
    status, _ = call('/v1/chat/completions', {'model': 'minara/openai/usage-test',
        'messages': [{'role': 'user', 'content': 'hi'}]})
    assert status == 502
    usage = json.loads(call('/v1/usage')[1])
    account = usage['accounts'][0]
    assert usage['turns'] == 1 and account['attempts'] == 1
    assert account['provider_cost'] is None and not account['provider_cost_complete']


def test_mixed_account_report_never_adds_provider_money_to_credits():
    from account_report import usage_report
    provider = UnmeteredAccount()
    provider.start_provider_turn()
    provider.record_provider_usage('a', 'turn', {'provider_cost': 100.0, 'provider_cost_currency': 'USD'})
    retool = types.SimpleNamespace(usage=lambda: {'session_used': 2.0, 'turns': 1, 'recent_total': 2.0})
    rows = [(types.SimpleNamespace(cfg={'account_id': 'r'}), retool),
            (types.SimpleNamespace(cfg={'account_id': 'm', 'upstream': 'minara'}), provider)]
    result = usage_report(None, rows)
    assert result['session_used'] == 2.0 and result['turns'] == 2
    assert result['accounts'][1]['provider_cost'] == 100.0
    assert result['accounts'][1]['session_used'] is None


@pytest.mark.parametrize('count', [10000])
def test_lifetime_usage_totals_and_records_are_bounded(count):
    manager = UnmeteredAccount()
    for index in range(count):
        manager.start_provider_turn()
        manager.record_provider_usage(str(index), str(index), {'provider_cost': 0.1, 'provider_cost_currency': 'USD'})
        manager.finish_provider_attempt(str(index), str(index))
    usage = manager.usage()
    assert usage['turns'] == usage['attempts'] == count
    assert usage['provider_cost'] == pytest.approx(count * 0.1)
    assert usage['provider_cost_complete']
    assert len(usage['provider_cost_records']) <= 50
    assert len(manager._attempts) <= 50 and not manager._active
    manager.record_provider_usage(str(count-1), str(count-1), {'provider_cost': 0.1, 'provider_cost_currency': 'USD'}, final=True)
    assert manager.usage()['attempts'] == count


def test_unsent_admission_timeout_is_not_billable_attempt(http_usage):
    import threading
    runtime, call = http_usage
    posts = []
    runtime.backend._minara_post = lambda *a, **kw: posts.append(1)
    runtime.cfg['turn_timeout'] = 0.02
    runtime.loop._minara_worker_slots = threading.BoundedSemaphore(1)
    runtime.loop._minara_worker_slots.acquire()
    status, _ = call('/v1/chat/completions', {'model': 'minara/openai/usage-test',
        'messages': [{'role': 'user', 'content': 'hi'}]})
    usage = runtime.credits.usage()
    assert status == 504 and posts == []
    assert usage['turns'] == 1 and usage['attempts'] == 0
    assert usage['provider_cost_complete'] and not usage['unreported_usage']


def test_preheader_failure_counts_as_unknown_sent_attempt(http_usage):
    runtime, call = http_usage
    def post(*a, **kw):
        raise OSError('network failure after request began')
    runtime.backend._minara_post = post
    status, _ = call('/v1/chat/completions', {'model': 'minara/openai/usage-test',
        'messages': [{'role': 'user', 'content': 'hi'}]})
    usage = runtime.credits.usage()
    assert status == 502 and usage['attempts'] == 1
    assert not usage['provider_cost_complete']


def test_finalized_recent_dedup_keeps_active_overlap_and_lifetime_totals():
    manager = UnmeteredAccount()
    manager.start_provider_turn()
    manager.record_provider_usage('active', 'turn', {})
    for i in range(100):
        manager.record_provider_usage(str(i), 'turn', {'provider_cost': 1, 'provider_cost_currency': 'EUR'}, final=True)
    manager.record_provider_usage('active', 'turn', {'provider_cost': 2, 'provider_cost_currency': 'EUR'})
    manager.finish_provider_attempt('active', 'turn')
    manager.finish_provider_attempt('active', 'turn')
    usage = manager.usage()
    assert usage['attempts'] == 101 and usage['provider_cost'] == 102
    assert usage['turns'] == 1 and usage['provider_cost_complete']
    assert len(usage['provider_cost_records']) <= 50


def test_cancel_immediately_after_post_counts_sent_attempt(http_usage):
    runtime, call = http_usage
    calls = []
    def post(*a, **kw):
        calls.append(1)
        # The worker has crossed the physical attempt boundary, before headers.
        for session in runtime.pool._conversations.values():
            session._cancelled = True
        raise OSError('connection cut after POST boundary')
    runtime.backend._minara_post = post
    call('/v1/chat/completions', {'model': 'minara/openai/usage-test',
        'messages': [{'role': 'user', 'content': 'hi'}]})
    usage = runtime.credits.usage()
    assert calls == [1] and usage['attempts'] == 1
    assert not usage['provider_cost_complete'] and not runtime.credits._active



def test_finite_provider_values_cannot_overflow_public_json():
    manager = UnmeteredAccount()
    for key in ('one', 'two'):
        manager.record_provider_usage(key, 'turn', {'provider_cost': 1e308, 'provider_cost_currency': 'USD'}, final=True)
    usage = manager.usage()
    assert usage['provider_cost'] is None and usage['provider_cost_overflow']
    json.dumps(usage, allow_nan=False)
    summary = provider_cost_summary([{'provider_cost': 1e308, 'provider_cost_currency': 'USD'}] * 2)
    assert summary['provider_cost'] is None and summary['provider_cost_overflow']
    json.dumps(summary, allow_nan=False)


@pytest.mark.parametrize('new_cost', [0.0, 2.0])
def test_snapshot_currency_revision_removes_empty_currency_bucket(new_cost):
    manager = UnmeteredAccount()
    manager.record_provider_usage('one', 'turn', {'provider_cost': 1.0, 'provider_cost_currency': 'USD'})
    manager.record_provider_usage('one', 'turn', {'provider_cost': new_cost, 'provider_cost_currency': 'EUR'}, final=True)
    usage = manager.usage()
    assert usage['provider_cost'] == new_cost and usage['provider_cost_currency'] == 'EUR'
    assert 'USD' not in manager._currencies


def test_real_zero_currency_record_is_not_removed_by_other_snapshot_revision():
    manager = UnmeteredAccount()
    manager.record_provider_usage('zero', 'turn', {'provider_cost': 0.0, 'provider_cost_currency': 'USD'}, final=True)
    manager.record_provider_usage('one', 'turn', {'provider_cost': 1.0, 'provider_cost_currency': 'USD'})
    manager.record_provider_usage('one', 'turn', {'provider_cost': 2.0, 'provider_cost_currency': 'EUR'}, final=True)
    assert manager.usage()['provider_cost'] is None
    assert manager._currency_counts == {'USD': 1, 'EUR': 1}



def test_unknown_currency_singleton_after_revision_never_uses_removed_cost():
    manager = UnmeteredAccount()
    manager.record_provider_usage('a', 'turn', {'provider_cost': 1}, final=True)
    manager.record_provider_usage('b', 'turn', {'provider_cost': 2}, final=True)
    manager.record_provider_usage('b', 'turn', {'provider_cost': None}, final=True)
    usage = manager.usage()
    assert usage['provider_cost'] in (None, 1)
    assert usage['provider_cost'] != 2 and not usage['provider_cost_complete']
