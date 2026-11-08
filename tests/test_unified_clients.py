"""Unified public slots choose providers invisibly and keep each dialogue sticky."""
import asyncio
import concurrent.futures
import http.client
import json
import threading

import pytest

import config
import direct
import models
import proxy
from test_review_fixes_proxy import FakeManager
from test_minara_mixed_clients import request

KEY = 'unified-client-local-key-0123456789012345'
PUBLIC = LOCAL = 'claude-opus-5-5'

@pytest.fixture
def unified(tmp_path, monkeypatch):
    prior = dict(models._MINARA_MODELS)
    for key in ('RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF', 'RETOOL_PAGE_UUID',
                'RETOOL_THREAD_ID', 'RELAY_API_KEY', 'RELAY_PORT', 'RELAY_HOST'):
        monkeypatch.delenv(key, raising=False)
    value = {
        'accounts': [
            {'account_id': 'mixed-retool', 'base': 'https://tenant.invalid', 'page_uuid': 'test-page',
             'cookie': 'accessToken=synthetic-retool-cookie', 'xsrf': 'synthetic-xsrf',
             'provider_resource_name': '__managed__'},
            {'account_id': 'mixed-minara', 'upstream': 'minara', 'bearer_token': 'synthetic-minara-bearer',
             'minara_models': [{'id': 'anthropic/claude-opus-5.5', 'public_id': PUBLIC, 'provider': 'anthropic', 'tools': True, 'reasoning': True, 'reasoning_effort_levels': ['high', 'max']}]}],
        'model': LOCAL, 'relay_api_key': KEY, 'workspace_root': str(tmp_path), 'project_context': False,
        'exec_policy': 'off', 'conversation_state': False, 'cache_keepwarm_seconds': 0,
        'session_refresh_interval': 0, 'account_balance_interval': 0,
        'journal_path': str(tmp_path / 'journal.jsonl'), 'max_workers': 24,
        'prompts_file': str(tmp_path / 'absent.json')}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    cfg = config.load(str(path))
    observed = []
    cancelled = threading.Event()
    waiting = threading.Event()
    active = 0
    peak = 0

    async def events(cfg, session, provider, prompt, head, tools, choice, cap, chunks=None):
        nonlocal active, peak
        upstream = cfg.get('upstream', 'retool')
        assert upstream == 'retool'
        assert cfg['cookie'] == 'accessToken=synthetic-retool-cookie'
        assert not cfg.get('bearer_token')
        payload = {'messages': session._native_client_messages, 'tools': tools}
        observed.append({'upstream': upstream, 'payload': payload, 'thread': session.thread_id})
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.04)
            yield {'type': 'usage_start'}
            tool_results = any(message.get('role') == 'tool' for message in payload['messages'] or [])
            if tools and not tool_results:
                yield {'type': 'tool_call', 'id': 'call_' + upstream, 'name': 'echo',
                       'arguments': '{"word":"OK"}'}
                reason = 'tool_calls'
            else:
                yield {'type': 'text', 'delta': upstream.upper() + '_OK'}
                reason = 'stop'
            if upstream == 'retool':
                yield {'type': 'credits', 'value': 0.1, 'usage': {'input_tokens': 2, 'output_tokens': 3}}
            else:
                yield {'type': 'usage', 'usage': {'input_tokens': 2, 'output_tokens': 3, 'provider_cost': 0.001}}
            yield {'type': 'end', 'reason': reason}
        finally:
            active -= 1

    monkeypatch.setattr(direct, 'iter_events', events)
    runtime = proxy.Runtime(cfg)
    manager = FakeManager(100.0)
    backend = runtime.accounts.backends[0]
    runtime.accounts.managers[backend] = manager
    backend.credits = manager
    runtime.credits = manager
    def post(url, **kwargs):
        assert url == 'https://api.minara.ai/v1/chat/completions'
        assert kwargs['headers']['Authorization'] == 'Bearer synthetic-minara-bearer'
        assert 'Cookie' not in kwargs['headers'] and 'x-xsrf-token' not in kwargs['headers']
        assert kwargs['impersonate'] == 'chrome131' and kwargs['stream'] is True
        assert kwargs['allow_redirects'] is False
        payload = kwargs['json']
        assert payload['model'] == 'anthropic/claude-opus-5.5'
        observed.append({'upstream': 'minara', 'payload': payload, 'thread': None})
        class Response:
            status_code = 200
            headers = {'content-type': 'text/event-stream'}
            def close(self):
                cancelled.set()
            def iter_content(self, chunk_size):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    import time
                    time.sleep(0.04)
                    if 'WAIT_FOR_CANCEL' in json.dumps(payload):
                        waiting.set()
                        cancelled.wait(3)
                        return
                    results = any(message['role'] == 'tool' for message in payload['messages'])
                    if payload.get('tools') and not results:
                        fn = payload['tools'][0]['function']
                        delta = {'tool_calls': [{'index': 0, 'id': 'call_minara', 'function': {
                            'name': fn['name'], 'arguments': '{"word":"OK"}'}}]}
                        reason = 'tool_calls'
                    else:
                        delta, reason = {'content': 'MINARA_OK'}, 'stop'
                    data = {'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}],
                            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'cost': 0.001}}
                    yield ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode()
                finally:
                    active -= 1
        return Response()
    runtime.accounts.backends[1]._minara_post = post
    server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(runtime), threading.BoundedSemaphore(24))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Client:
        def call(self, route, body=None, affinity='default', parent=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
            try:
                headers = {'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json',
                           'X-Session-Id': affinity}
                if parent:
                    headers['X-Proxi-Parent-Session-Id'] = parent
                connection.request('POST' if body is not None else 'GET', route,
                                   json.dumps(body) if body is not None else None, headers)
                response = connection.getresponse()
                return response.status, response.read().decode()
            finally:
                connection.close()

    client = Client()
    client.runtime, client.observed = runtime, observed
    client.waiting, client.cancelled = waiting, cancelled
    client.peak = lambda: peak
    try:
        yield client
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()
        thread.join(2)
        models._MINARA_MODELS.clear()
        models._MINARA_MODELS.update(prior)


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'messages'])
def test_same_public_slot_parent_child_tool_continuation_stays_on_own_provider(unified, dialect):
    route, parent = request(dialect, PUBLIC)
    status, raw = unified.call(route, parent, 'main')
    assert status == 200 and 'RETOOL_OK' in raw, raw
    route, child = request(dialect, PUBLIC)
    schema = {'type': 'object', 'properties': {'word': {'type': 'string'}}, 'required': ['word']}
    if dialect == 'chat':
        child['tools'] = [{'type': 'function', 'function': {'name': 'echo', 'parameters': schema}}]
    elif dialect == 'responses':
        child['tools'] = [{'type': 'function', 'name': 'echo', 'parameters': schema}]
    else:
        child['tools'] = [{'name': 'echo', 'input_schema': schema}]
    status, raw = unified.call(route, child, 'child', parent='main')
    assert status == 200, raw
    result = json.loads(raw)
    if dialect == 'chat':
        answer = result['choices'][0]['message']
        call_id = answer['tool_calls'][0]['id']
        child['messages'] += [answer, {'role': 'tool', 'tool_call_id': call_id, 'content': 'UNIFIED_UNIQUE'}]
    elif dialect == 'responses':
        answer = next(v for v in result['output'] if v['type'] == 'function_call')
        call_id = answer['call_id']
        child['input'] = [{'role': 'user', 'content': 'Say OK'}, answer,
                          {'type': 'function_call_output', 'call_id': call_id, 'output': 'UNIFIED_UNIQUE'}]
    else:
        answer = next(v for v in result['content'] if v['type'] == 'tool_use')
        call_id = answer['id']
        child['messages'] += [{'role': 'assistant', 'content': result['content']},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call_id, 'content': 'UNIFIED_UNIQUE'}]}]
    assert call_id == 'call_minara'
    status, raw = unified.call(route, child, 'child', parent='main')
    assert status == 200 and 'MINARA_OK' in raw, raw
    assert 'UNIFIED_UNIQUE' in json.dumps(unified.observed[-1]['payload'])
    status, raw = unified.call(route, parent, 'main')
    assert status == 200 and 'RETOOL_OK' in raw, raw
    assert [v['upstream'] for v in unified.observed] == ['retool', 'minara', 'minara', 'retool']
    assert unified.observed[0]['thread'] == unified.observed[-1]['thread']


def test_concurrent_main_and_children_all_use_single_public_model(unified):
    route, body = request('chat', PUBLIC)
    assert unified.call(route, body, 'main')[0] == 200
    def child(index):
        return unified.call(route, body, 'child-' + str(index), parent='main')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(child, range(8)))
    assert all(status == 200 for status, raw in results), results
    assert {v['upstream'] for v in unified.observed[1:]} == {'minara', 'retool'}
    assert unified.peak() >= 2
    assert unified.call(route, body, 'main')[0] == 200
    assert unified.observed[-1]['upstream'] == 'retool'


def test_unified_discovery_deduplicates_and_mcp_schema_reaches_minara(unified):
    status, raw = unified.call('/v1/models')
    assert status == 200
    entries = json.loads(raw)['data']
    assert sum(v['id'] == PUBLIC for v in entries) == 1
    assert not any(v['id'].startswith('minara/') for v in entries)
    route, body = request('responses', PUBLIC)
    assert unified.call(route, body, 'main')[0] == 200
    schema = {'type': 'object', 'properties': {'word': {'type': ['string', 'null'], 'enum': ['OK', None]},
        'mapping': {'type': 'object', 'additionalProperties': {'type': 'number'}}},
        'required': ['word'], 'additionalProperties': False}
    body['tools'] = [{'type': 'namespace', 'name': 'mcp__unified', 'tools': [
        {'type': 'function', 'name': 'echo', 'parameters': schema}]}]
    status, raw = unified.call(route, body, 'child', parent='main')
    assert status == 200, raw
    payload = unified.observed[-1]['payload']
    assert unified.observed[-1]['upstream'] == 'minara'
    assert payload['tools'][0]['function']['parameters'] == schema
    answer = next(v for v in json.loads(raw)['output'] if v['type'] == 'function_call')
    assert answer['name'] == 'echo' and answer['namespace'] == 'mcp__unified'

def test_unified_minara_usage_never_estimates_retool_category_prices(unified):
    import journal
    route, body = request('chat', PUBLIC)
    assert unified.call(route, body, 'first')[0] == 200
    before = journal.JOURNAL.last_seq
    assert unified.call(route, body, 'second')[0] == 200
    events = [e for e in journal.JOURNAL.query(since=before, limit=1000) if e['kind']=='upstream.usage']
    assert events
    for event in events:
        assert event['data']['cost_input'] is None
        assert event['data']['cost_output'] is None

@pytest.mark.parametrize('dialect', ['chat','responses','messages'])
def test_explicit_max_never_selects_high_only_provider_even_with_legacy_downgrade(unified, dialect):
    rt = unified.runtime
    rt.cfg['allow_effort_downgrade'] = True
    descriptor = rt.pool.backends[1].cfg['minara_models'][0]
    descriptor['reasoning_effort_levels'] = ['high']
    rt.cfg['_minara_models'] = models.build_minara_catalog([b.cfg for b in rt.pool.backends])
    route, body = request(dialect, PUBLIC)
    if dialect == 'chat':
        body['reasoning_effort'] = 'max'
    elif dialect == 'responses':
        body['reasoning'] = {'effort': 'max'}
    else:
        body['output_config'] = {'effort': 'max'}
    for i in range(3):
        status, raw = unified.call(route, body, 'max-' + dialect + str(i))
        assert status == 200, raw
        assert 'RETOOL_OK' in raw
    assert all(row['upstream'] == 'retool' for row in unified.observed)
