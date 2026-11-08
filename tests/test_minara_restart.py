"""Stop/restart real mixed-provider Runtime instances with persisted state.

No live credentials/network. Retool uses its real NDJSON transport with a fake
urllib response; Minara uses its real SSE transport with an injected fake POST.
"""
import http.client
import json
import threading

import pytest

import config
import conversation_state
import direct
import models
import proxy
from test_native_history import tool_reply
from test_review_fixes_pr19 import END, response, text
from test_review_fixes_proxy import FakeManager

KEY = 'restart-local-key-01234567890123456789'
LOCAL = 'claude-opus-5-5'
REMOTE = 'minara/openai/restart-test'


@pytest.fixture
def restartable(tmp_path, monkeypatch):
    prior = dict(models._MINARA_MODELS)
    for key in ('RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF', 'RETOOL_PAGE_UUID',
                'RETOOL_THREAD_ID', 'RELAY_API_KEY', 'RELAY_PORT', 'RELAY_HOST'):
        monkeypatch.delenv(key, raising=False)
    state = tmp_path / 'conversations'
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({
        'accounts': [
            {'account_id': 'restart-retool', 'base': 'https://restart.invalid', 'page_uuid': 'page',
             'cookie': 'accessToken=restart-synthetic-cookie', 'xsrf': 'synthetic-xsrf',
             'provider_resource_name': '__managed__'},
            {'account_id': 'restart-minara', 'upstream': 'minara', 'bearer_token': 'restart-synthetic-bearer',
             'minara_models': [{'id': 'openai/restart-test', 'tools': True}]}],
        'model': LOCAL, 'relay_api_key': KEY, 'workspace_root': str(tmp_path), 'project_context': False,
        'exec_policy': 'off', 'conversation_state': True, 'conversation_state_dir': str(state),
        'cache_keepwarm_seconds': 0, 'session_refresh_interval': 0, 'account_balance_interval': 0,
        'journal_path': str(tmp_path / 'journal.jsonl'), 'prompts_file': str(tmp_path / 'absent.json')}), encoding='utf-8')
    seen = []
    runtimes = []

    def wire(request, **kwargs):
        body = json.loads(request.data)
        assert request.full_url.startswith('https://restart.invalid/')
        assert 'Bearer' not in str(request.headers)
        seen.append(('retool', body))
        if any(message.get('role') == 'tool' for message in body['messages']):
            return response(text('RETOOL_RESTART_OK'), *END)
        return response(*tool_reply('restart_local_call', 'TRUSTED_RETOOL_SIGNATURE', 'file'))
    monkeypatch.setattr(direct.urllib.request, 'urlopen', wire)

    class Service:
        def __init__(self):
            self.runtime = proxy.Runtime(config.load(str(path)))
            backend = self.runtime.accounts.backends[0]
            manager = FakeManager(100.0)
            self.runtime.accounts.managers[backend] = manager
            backend.credits = manager
            self.runtime.credits = manager
            def post(url, **kwargs):
                assert url == 'https://api.minara.ai/v1/chat/completions'
                assert kwargs['headers']['Authorization'] == 'Bearer restart-synthetic-bearer'
                assert 'Cookie' not in kwargs['headers']
                body = kwargs['json']
                seen.append(('minara', body))
                continuation = any(message.get('role') == 'tool' for message in body['messages'])
                delta = ({'content': 'MINARA_RESTART_OK'} if continuation else {'tool_calls': [
                    {'index': 0, 'id': 'restart_remote_call', 'function': {'name': 'read_file',
                     'arguments': '{"path":"file","limit":3}'}}]})
                data = {'choices': [{'index': 0, 'delta': delta,
                                     'finish_reason': 'stop' if continuation else 'tool_calls'}],
                        'usage': {'prompt_tokens': 4, 'completion_tokens': 2}}
                class Response:
                    status_code = 200
                    headers = {'content-type': 'text/event-stream'}
                    def iter_content(self, **_):
                        yield ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode()
                    def close(self):
                        pass
                return Response()
            self.runtime.accounts.backends[1]._minara_post = post
            self.server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(self.runtime), threading.BoundedSemaphore(8))
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.closed = False
            runtimes.append(self)
        def call(self, route, body, affinity):
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
            try:
                conn.request('POST', route, json.dumps(body), {'Authorization': 'Bearer ' + KEY,
                    'Content-Type': 'application/json', 'X-Session-Id': affinity})
                result = conn.getresponse()
                raw = result.read().decode()
                assert result.status == 200, raw
                return json.loads(raw)
            finally:
                conn.close()
        def stop(self):
            if self.closed:
                return
            self.closed = True
            self.server.shutdown()
            self.server.server_close()
            self.runtime.close()
            self.thread.join(2)
    try:
        yield Service, seen, state
    finally:
        for service in runtimes:
            service.stop()
        models._MINARA_MODELS.clear()
        models._MINARA_MODELS.update(prior)


def initial(dialect, model):
    schema = {'type': 'object', 'properties': {'path': {'type': 'string'}, 'limit': {'type': 'integer'}},
              'required': ['path']}
    body = {'model': model, 'max_tokens': 64}
    if dialect == 'responses':
        body.pop('max_tokens')
        body.update(input='Read file', max_output_tokens=64,
                    tools=[{'type': 'function', 'name': 'read_file', 'parameters': schema}])
        route = '/v1/responses'
    elif dialect == 'messages':
        body.update(messages=[{'role': 'user', 'content': 'Read file'}],
                    tools=[{'name': 'read_file', 'input_schema': schema}])
        route = '/v1/messages'
    else:
        body.update(messages=[{'role': 'user', 'content': 'Read file'}],
                    tools=[{'type': 'function', 'function': {'name': 'read_file', 'parameters': schema}}])
        route = '/v1/chat/completions'
    return route, body


def append_result(body, reply, dialect):
    if dialect == 'chat':
        assistant = reply['choices'][0]['message']
        call_id = assistant['tool_calls'][0]['id']
        body['messages'] += [assistant, {'role': 'tool', 'tool_call_id': call_id, 'content': 'RESTART_UNIQUE_RESULT'}]
    elif dialect == 'responses':
        call = next(item for item in reply['output'] if item['type'] == 'function_call')
        call_id = call['call_id']
        body['input'] = [{'role': 'user', 'content': 'Read file'}, call,
                         {'type': 'function_call_output', 'call_id': call_id, 'output': 'RESTART_UNIQUE_RESULT'}]
    else:
        call = next(item for item in reply['content'] if item['type'] == 'tool_use')
        call_id = call['id']
        body['messages'] += [{'role': 'assistant', 'content': reply['content']}, {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': call_id, 'content': 'RESTART_UNIQUE_RESULT'}]}]
    return call_id


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'messages'])
def test_mixed_runtime_disk_restart_keeps_retool_signed_state_and_minara_cold_history(restartable, dialect):
    Service, seen, state = restartable
    first = Service()
    requests = []
    for model in (LOCAL, REMOTE):
        route, body = initial(dialect, model)
        affinity = 'restart-' + dialect + model
        reply = first.call(route, body, affinity)
        call_id = append_result(body, reply, dialect)
        requests.append((model, route, body, affinity, call_id))
    assert first.runtime.pool._store.flush(5)
    files = list(state.glob('*' + conversation_state.SUFFIX))
    assert files, 'Retool signed continuation must actually be written to disk'
    # Minara deliberately does not invent/persist Retool signed history.
    assert any(session._native_history.branches for session in first.runtime.pool._conversations.values()
               if session.cfg.get('upstream', 'retool') == 'retool')
    assert all(not session._native_history.branches for session in first.runtime.pool._conversations.values()
               if session.cfg.get('upstream') == 'minara')
    old_sessions = set(map(id, first.runtime.pool._conversations.values()))
    first.stop()
    second = Service()
    for model, route, body, affinity, call_id in requests:
        reply = second.call(route, body, affinity)
        rendered = json.dumps(reply)
        assert ('RETOOL_RESTART_OK' if model == LOCAL else 'MINARA_RESTART_OK') in rendered
        upstream, payload = seen[-1]
        assert upstream == ('retool' if model == LOCAL else 'minara')
        payload_text = json.dumps(payload)
        assert call_id in payload_text and 'RESTART_UNIQUE_RESULT' in payload_text
        if model == LOCAL:
            assert 'TRUSTED_RETOOL_SIGNATURE' in payload_text
        else:
            assert 'TRUSTED_RETOOL_SIGNATURE' not in payload_text
            assert all('signature' not in json.dumps(item) for item in payload['messages'])
    assert old_sessions.isdisjoint(map(id, second.runtime.pool._conversations.values()))
