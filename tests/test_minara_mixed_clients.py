"""Mixed-provider HTTP clients: routing/concurrency and native tool continuations.

Only upstream event generators are fake. Config, runtime, account selection,
conversation leases, parsers, adapters and the loopback HTTP server are real.
"""
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

KEY = 'mixed-client-local-key-0123456789012345'
REMOTE = 'minara/openai/mixed-test'
LOCAL = 'claude-opus-5-5'


@pytest.fixture
def mixed(tmp_path, monkeypatch):
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
             'minara_models': [{'id': 'openai/mixed-test', 'tools': True}]}],
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


def test_concurrent_mixed_clients_keep_routes_credentials_and_accounting_separate(mixed):
    cases = [(dialect, model, stream) for dialect in ('chat', 'responses', 'messages')
             for model in (LOCAL, REMOTE) for stream in (False, True)]
    def run(case):
        dialect, model, stream = case
        route, body = request(dialect, model, stream)
        status, raw = mixed.call(route, body, '-'.join(map(str, case)))
        assert status == 200, raw
        expected = 'MINARA_OK' if model == REMOTE else 'RETOOL_OK'
        assert expected in raw
        assert ('RETOOL_OK' if model == REMOTE else 'MINARA_OK') not in raw
        if stream:
            assert 'data: [DONE]' in raw
        else:
            usage = json.loads(raw)['usage']
            if model == REMOTE:
                assert usage['credits_used'] is None
            else:
                assert usage['credits_used'] == 0.1
        assert 'synthetic-' not in raw
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as workers:
        list(workers.map(run, cases))
    assert len(mixed.observed) == 12
    assert mixed.peak() >= 2  # One shared event loop must allow both transports in flight.
    assert {item['upstream'] for item in mixed.observed} == {'retool', 'minara'}
    assert len({item['thread'] for item in mixed.observed if item['upstream'] == 'retool'}) == 6
    status, raw = mixed.call('/health')
    assert status == 200 and json.loads(raw)['accounts']['routable'] == 2
    status, raw = mixed.call('/v1/balance')
    assert status == 200, raw
    balance = json.loads(raw)
    assert balance['unknown_accounts'] == 1 and balance['partial'] is True
    remote = next(item for item in balance['accounts'] if item.get('upstream') == 'minara')
    # The balance is supported (GET /auth/me) and unknown here: tests never reach api.minara.ai.
    assert remote['available'] is None and remote['balance_supported'] is True


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'messages'])
@pytest.mark.parametrize('model', [LOCAL, REMOTE])
@pytest.mark.parametrize('cold', [False, True])
def test_native_tool_continuation_preserves_id_and_result_in_each_provider(mixed, dialect, model, cold):
    route, body = request(dialect, model)
    schema = {'type': 'object', 'properties': {'word': {'type': 'string'}}, 'required': ['word']}
    if dialect == 'chat':
        body['tools'] = [{'type': 'function', 'function': {'name': 'echo', 'parameters': schema}}]
    elif dialect == 'responses':
        body['tools'] = [{'type': 'function', 'name': 'echo', 'parameters': schema}]
    else:
        body['tools'] = [{'name': 'echo', 'input_schema': schema}]
    affinity = 'tool-' + dialect + model
    status, raw = mixed.call(route, body, affinity)
    assert status == 200, raw
    data = json.loads(raw)
    if dialect == 'chat':
        assistant = data['choices'][0]['message']
        call_id = assistant['tool_calls'][0]['id']
        body['messages'] += [assistant, {'role': 'tool', 'tool_call_id': call_id, 'content': 'UNIQUE_RESULT'}]
    elif dialect == 'responses':
        call = next(item for item in data['output'] if item['type'] == 'function_call')
        call_id = call['call_id']
        body['input'] = [{'role': 'user', 'content': 'Say OK'}, call,
                         {'type': 'function_call_output', 'call_id': call_id, 'output': 'UNIQUE_RESULT'}]
    else:
        call = next(item for item in data['content'] if item['type'] == 'tool_use')
        call_id = call['id']
        body['messages'] += [{'role': 'assistant', 'content': data['content']},
                             {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call_id,
                                                          'content': 'UNIQUE_RESULT'}]}]
    status, raw = mixed.call(route, body, affinity + '-cold' if cold else affinity)
    assert status == 200, raw
    assert ('MINARA_OK' if model == REMOTE else 'RETOOL_OK') in raw
    assert len(mixed.observed) == 2
    last = mixed.observed[-1]
    rendered = json.dumps(last['payload'], ensure_ascii=False)
    assert call_id in rendered and 'UNIQUE_RESULT' in rendered
    if model == LOCAL:
        assert (last['thread'] == mixed.observed[0]['thread']) is not cold
    if model == REMOTE:
        result = next(item for item in last['payload']['messages'] if item['role'] == 'tool')
        assert result['tool_call_id'] == call_id and result['content'] == 'UNIQUE_RESULT'

def test_same_conversation_identifier_different_routes_never_crosses_provider(mixed):
    def run(model):
        route, body = request('chat', model)
        return mixed.call(route, body, 'same-client-conversation')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        replies = list(workers.map(run, [LOCAL, REMOTE]))
    assert all(status == 200 for status, _ in replies), replies
    assert 'RETOOL_OK' in replies[0][1] and 'MINARA_OK' in replies[1][1]
    assert sorted(item['upstream'] for item in mixed.observed) == ['minara', 'retool']


def test_namespaced_mcp_tool_schema_survives_real_minara_payload(mixed):
    schema = {'type': 'object', 'properties': {'word': {'type': ['string', 'null'], 'enum': ['OK', None]},
              'mapping': {'type': 'object', 'additionalProperties': {'type': 'number'}}}, 'required': ['word'],
              'additionalProperties': False}
    route, body = request('responses', REMOTE)
    body['tools'] = [{'type': 'namespace', 'name': 'mcp__example', 'tools': [
        {'type': 'function', 'name': 'echo', 'description': 'Native schema', 'parameters': schema}]}]
    status, raw = mixed.call(route, body, 'namespaced-schema')
    assert status == 200, raw
    fn = mixed.observed[-1]['payload']['tools'][0]['function']
    assert fn['parameters'] == schema
    assert fn['name'] != 'echo' and 'echo' in fn['name']
    call = next(item for item in json.loads(raw)['output'] if item['type'] == 'function_call')
    assert call['name'] == 'echo' and call['namespace'] == 'mcp__example'


def test_cancelled_minara_does_not_block_parallel_retool_turn(mixed):
    route, body = request('chat', REMOTE)
    body['messages'][0]['content'] = 'WAIT_FOR_CANCEL'
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(mixed.call, route, body, 'cancel-minara')
        assert mixed.waiting.wait(3)
        async def cancel_remote():
            sessions = [session for session in mixed.runtime.pool._conversations.values()
                        if session.cfg.get('upstream') == 'minara']
            assert len(sessions) == 1
            sessions[0].cancel()
        asyncio.run_coroutine_threadsafe(cancel_remote(), mixed.runtime.loop).result(2)
        local_route, local_body = request('chat', LOCAL)
        status, raw = mixed.call(local_route, local_body, 'local-during-cancel')
        assert status == 200 and 'RETOOL_OK' in raw
        status, raw = pending.result(3)
        assert status == 503 and 'reply was cut' in raw
    assert mixed.cancelled.wait(2)
