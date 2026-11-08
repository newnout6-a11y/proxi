"""Mixed-provider HTTP clients: routing/concurrency and native tool continuations.

Only upstream event generators are fake. Config, runtime, account selection,
conversation leases, parsers, adapters and the loopback HTTP server are real.
"""
import asyncio
import http.client
import json
import threading

import pytest

import config
import direct
import models
import proxy
from test_review_fixes_proxy import FakeManager

KEY = 'mixed-client-local-key-0123456789012345'
REMOTE = 'minara/openai/mixed-test'
LOCAL = 'claude-opus-5-5'


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
             'minara_models': [{'id': 'openai/mixed-test', 'public_id': LOCAL, 'provider': 'anthropic', 'tools': True}]}],
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
        assert payload['model'] == 'openai/mixed-test'
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
        def call(self, route, body=None, affinity='default'):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
            try:
                headers = {'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json',
                           'X-Session-Id': affinity}
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


def request(dialect, model, stream=False):
    common = {'model': model, 'stream': stream}
    if dialect == 'responses':
        return '/v1/responses', dict(common, input='Say OK', max_output_tokens=64)
    return ('/v1/messages' if dialect == 'messages' else '/v1/chat/completions',
            dict(common, messages=[{'role': 'user', 'content': 'Say OK'}], max_tokens=64))



def test_same_public_slot_selects_both_providers_and_sticks_per_dialog(unified):
    client = unified
    status, raw = client.call('/v1/models')
    assert status == 200
    ids = [row['id'] for row in json.loads(raw)['data']]
    assert ids.count(LOCAL) == 1
    assert not any(name.startswith('minara/') for name in ids)
    seen = {}
    for index in range(6):
        route, body = request('chat', LOCAL)
        status, raw = client.call(route, body, 'child-' + str(index))
        assert status == 200, raw
        value = 'minara' if 'MINARA_OK' in raw else 'retool'
        seen[index] = value
    assert set(seen.values()) == {'minara', 'retool'}
    for index, upstream in seen.items():
        route, body = request('chat', LOCAL)
        status, raw = client.call(route, body, 'child-' + str(index))
        assert status == 200, raw
        assert (upstream.upper() + '_OK') in raw


def test_bound_route_does_not_mutate_public_response_identity(unified):
    for dialect in ('chat','responses','messages'):
        route, body = request(dialect, LOCAL, True)
        status, raw = unified.call(route, body, 'dialect-' + dialect)
        assert status == 200, raw
        assert LOCAL in raw
        assert 'data: [DONE]' in raw
