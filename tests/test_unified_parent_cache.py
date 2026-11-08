"""Minara child never rewrites or steals the Retool parent's signed prefix."""
import concurrent.futures
import copy
import http.client
import json
import threading

import pytest

import config
import direct
import journal
import proxy
from test_minara_restart import append_result, initial
from test_native_history import strip_cache, tool_reply
from test_review_fixes_pr19 import END, response, text
from test_review_fixes_proxy import FakeManager

PUBLIC = 'claude-opus-5-5'
KEY = 'parent-cache-local-key-0123456789'


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'messages'])
def test_retool_parent_signed_prefix_survives_simultaneous_minara_child(tmp_path, monkeypatch, dialect):
    for key in ('RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF', 'RETOOL_PAGE_UUID', 'RETOOL_THREAD_ID',
                'RELAY_API_KEY', 'RELAY_HOST', 'RELAY_PORT'):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'accounts': [
        {'account_id': 'cache-parent', 'base': 'https://parent.invalid', 'page_uuid': 'page',
         'cookie': 'synthetic', 'xsrf': 'synthetic', 'provider_resource_name': '__managed__'},
        {'account_id': 'cache-child', 'upstream': 'minara', 'bearer_token': 'synthetic',
         'minara_models': [{'id': 'anthropic/claude-opus-5.5', 'public_id': PUBLIC, 'provider': 'anthropic',
                           'tools': True, 'reasoning': True, 'reasoning_effort_levels': ['high', 'max']}]}],
        'model': PUBLIC, 'relay_api_key': KEY, 'workspace_root': str(tmp_path), 'project_context': False,
        'exec_policy': 'off', 'conversation_state': False, 'cache_keepwarm_seconds': 0,
        'session_refresh_interval': 0, 'account_balance_interval': 0, 'conversation_idle_timeout': 1,
        'journal_path': str(tmp_path / 'journal.jsonl'), 'prompts_file': str(tmp_path / 'absent.json')}), encoding='utf8')
    parent_bodies, child_bodies = [], []
    child_waiting, child_release = threading.Event(), threading.Event()

    def wire(request, **_):
        assert request.full_url.startswith('https://parent.invalid/')
        body = json.loads(request.data)
        parent_bodies.append(body)
        if len(parent_bodies) == 1:
            return response(*tool_reply('parent-call', 'PARENT_TRUSTED_SIGNATURE', 'file'))
        return response(text('PARENT_CONTINUED'), *END)
    monkeypatch.setattr(direct.urllib.request, 'urlopen', wire)
    runtime = proxy.Runtime(config.load(str(path)))
    manager = FakeManager(100)
    runtime.accounts.managers[runtime.pool.backends[0]] = manager
    runtime.pool.backends[0].credits = manager
    runtime.credits = manager

    def post(url, **kwargs):
        body = copy.deepcopy(kwargs['json'])
        child_bodies.append(body)
        assert body['model'] == 'anthropic/claude-opus-5.5'
        assert 'PARENT_TRUSTED_SIGNATURE' not in json.dumps(body)
        class Response:
            status_code = 200
            headers = {'content-type': 'text/event-stream'}
            def iter_content(self, **_):
                child_waiting.set()
                assert child_release.wait(10)
                yield b'data: {"choices":[{"index":0,"delta":{"content":"CHILD_DONE"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            def close(self):
                child_release.set()
        return Response()
    runtime.pool.backends[1]._minara_post = post
    server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(runtime), threading.BoundedSemaphore(8))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(route, body, affinity, parent=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
        try:
            headers = {'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json', 'X-Session-Id': affinity}
            if parent:
                headers['X-Proxi-Parent-Session-Id'] = parent
            connection.request('POST', route, json.dumps(body), headers)
            result = connection.getresponse()
            raw = result.read().decode()
            assert result.status == 200, raw
            return json.loads(raw)
        finally:
            connection.close()
    try:
        route, body = initial(dialect, PUBLIC)
        first = call(route, body, 'parent')
        parent_session = next(s for s in runtime.pool._conversations.values() if s.cfg.get('upstream', 'retool') == 'retool')
        parent_thread = parent_session.thread_id
        original_history = parent_session._native_history
        original_key = parent_session._last_cache_key
        append_result(body, first, dialect)
        child_body = {'model': PUBLIC, 'max_tokens': 64, 'messages': [{'role': 'user', 'content': 'Independent child'}]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as workers:
            child = workers.submit(call, '/v1/chat/completions', child_body, 'child', 'parent')
            assert child_waiting.wait(10)
            assert parent_session._cache_dependents == 1
            # While child physically waits, parent sends its actual signed continuation.
            continued = call(route, body, 'parent')
            assert 'PARENT_CONTINUED' in json.dumps(continued)
            assert parent_session.thread_id == parent_thread
            assert parent_session._native_history is original_history
            assert parent_session._last_cache_key == original_key
            child_release.set()
            assert 'CHILD_DONE' in json.dumps(child.result(10))
        assert len(parent_bodies) == 2 and len(child_bodies) == 1
        before, after = parent_bodies
        assert strip_cache(after['messages'][:len(before['messages'])]) == strip_cache(before['messages'])
        assert after['tools'] == before['tools']
        assert after['openAICacheKey'] == before['openAICacheKey']
        assistant = after['messages'][len(before['messages'])]
        assert assistant['role'] == 'assistant'
        assert assistant['content'][0]['signature'] == 'PARENT_TRUSTED_SIGNATURE'
        assert 'RESTART_UNIQUE_RESULT' in json.dumps(after['messages'][-1])
        assert 'PARENT_TRUSTED_SIGNATURE' not in json.dumps(child_bodies)
        assert parent_session._cache_dependents == 0
    finally:
        child_release.set()
        server.shutdown()
        server.server_close()
        runtime.close()
        thread.join(2)
        journal.JOURNAL.configure({}, None)
