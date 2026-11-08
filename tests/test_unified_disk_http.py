"""Actual unified HTTP -> Minara state binding survives Runtime recreation."""
import http.client
import json
import threading

import pytest

import config
import conversation_state as cs
import direct
import journal
import proxy
from test_minara_restart import append_result, initial

KEY = 'unified-disk-test-key-0123456789'
PUBLIC = 'claude-opus-5-5'


@pytest.fixture
def disk_service(tmp_path, monkeypatch):
    for key in ('RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF', 'RETOOL_PAGE_UUID', 'RETOOL_THREAD_ID',
                'RELAY_API_KEY', 'RELAY_HOST', 'RELAY_PORT'):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / 'config.json'
    state = tmp_path / 'conversations'
    descriptor = {'id': 'anthropic/claude-opus-5.5', 'public_id': PUBLIC, 'provider': 'anthropic', 'tools': True}
    accounts = [{'account_id': 'disk-retool', 'base': 'https://disk.invalid', 'page_uuid': 'page',
        'cookie': 'synthetic', 'xsrf': 'synthetic', 'provider_resource_name': '__managed__', 'priority': 10}]
    for name, priority in [('a', 0), ('b', 5)]:
        accounts.append({'account_id': 'disk-minara-' + name, 'upstream': 'minara', 'priority': priority,
            'base': 'https://api.minara.ai/v1', 'bearer_token': 'synthetic-' + name,
            'minara_models': [dict(descriptor)]})
    value = {'accounts': accounts, 'model': PUBLIC, 'relay_api_key': KEY,
        'workspace_root': str(tmp_path), 'project_context': False, 'exec_policy': 'off',
        'conversation_state': True, 'conversation_state_dir': str(state), 'cache_keepwarm_seconds': 0,
        'session_refresh_interval': 0, 'account_balance_interval': 0,
        'journal_path': str(tmp_path / 'journal.jsonl'), 'prompts_file': str(tmp_path / 'absent.json')}
    path.write_text(json.dumps(value), encoding='utf8')
    monkeypatch.setattr(direct, 'iter_events', lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError('Retool transport must not serve this pinned Minara request')))
    seen, services = [], []

    class Service:
        def __init__(self):
            self.runtime = proxy.Runtime(config.load(str(path)))
            self.closed = False
            for backend in self.runtime.pool.backends:
                if backend.cfg.get('upstream') != 'minara':
                    continue
                def post(url, *, _backend=backend, **kw):
                    body = kw['json']
                    seen.append((_backend.cfg['account_id'], body))
                    result = any(message.get('role') == 'tool' for message in body['messages'])
                    delta = {'content': 'DISK_UNIFIED_OK'} if result else {'tool_calls': [{'index': 0,
                        'id': 'disk_call', 'function': {'name': 'read_file', 'arguments': '{"path":"file","limit":3}'}}]}
                    data = {'choices': [{'index': 0, 'delta': delta,
                        'finish_reason': 'stop' if result else 'tool_calls'}],
                        'usage': {'prompt_tokens': 4, 'completion_tokens': 2}}
                    class Response:
                        status_code = 200
                        headers = {'content-type': 'text/event-stream'}
                        def iter_content(self, **_):
                            yield ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode()
                        def close(self):
                            pass
                    return Response()
                backend._minara_post = post
            self.server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(self.runtime), threading.BoundedSemaphore(8))
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            services.append(self)
        def call(self, route, body, affinity):
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
            try:
                connection.request('POST', route, json.dumps(body), {'Authorization': 'Bearer ' + KEY,
                    'Content-Type': 'application/json', 'X-Session-Id': affinity})
                response = connection.getresponse()
                raw = response.read().decode()
                assert response.status == 200, raw
                return json.loads(raw)
            finally:
                connection.close()
        def close(self):
            if self.closed:
                return
            self.closed = True
            self.server.shutdown()
            self.server.server_close()
            self.runtime.close()
            self.thread.join(2)
    try:
        yield Service, seen, path, state
    finally:
        for service in services:
            service.close()
        journal.JOURNAL.configure({}, None)


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'messages'])
def test_actual_http_minara_binding_only_disk_survives_priority_reversal(disk_service, dialect):
    Service, seen, path, state = disk_service
    first = Service()
    route, body = initial(dialect, PUBLIC)
    affinity = 'unified-disk-' + dialect
    reply = first.call(route, body, affinity)
    assert seen[-1][0] == 'disk-minara-a'
    call_id = append_result(body, reply, dialect)
    sessions = list(first.runtime.pool._conversations.values())
    assert len(sessions) == 1
    session = sessions[0]
    assert session._bound_route['upstream'] == 'minara'
    assert session._bound_route['id'] == PUBLIC
    assert session._native_history.branches == []
    assert first.runtime.pool._store.flush(5)
    files = list(state.glob('*.state'))
    assert len(files) == 1, 'HTTP orchestration must persist affinity-only state on actual session, not SessionLease'
    header, stored = cs.decode(files[0].read_bytes(), 100000)
    assert header['account'] == 'disk-minara-a'
    assert header['binding']['upstream'] == 'minara'
    assert header['binding']['model'] == PUBLIC
    assert header['binding']['upstream_model'] == 'anthropic/claude-opus-5.5'
    assert stored.branches == [] and stored.cache_key is None
    assert stored.prefix_tokens >= 0 and stored.warm_at == 0
    first.close()
    saved = json.loads(path.read_text(encoding='utf8'))
    saved['accounts'][1]['priority'] = 99
    saved['accounts'][2]['priority'] = 0
    path.write_text(json.dumps(saved), encoding='utf8')
    second = Service()
    result = second.call(route, body, affinity)
    assert 'DISK_UNIFIED_OK' in json.dumps(result)
    account, payload = seen[-1]
    assert account == 'disk-minara-a', 'restart must restore account binding, not choose newly preferred account b'
    assert payload['model'] == 'anthropic/claude-opus-5.5'
    assert call_id in json.dumps(payload) and 'RESTART_UNIQUE_RESULT' in json.dumps(payload)
    assert all('signature' not in json.dumps(message) for message in payload['messages'])
    assert len(seen) == 2
