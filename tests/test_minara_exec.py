"""Real relay execution loop over scripted Minara SSE; no external network/provider."""
import http.client
import json
import sys
import threading

import pytest

import config
import models
import proxy


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'messages'])
def test_minara_exec_output_is_used_by_next_upstream_request(tmp_path, monkeypatch, dialect):
    prior = dict(models._MINARA_MODELS)
    for key in ('RELAY_API_KEY', 'RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF'):
        monkeypatch.delenv(key, raising=False)
    key = 'synthetic-exec-relay-key'
    path = tmp_path / 'config.json'
    value = {'upstream': 'minara', 'bearer_token': 'synthetic-upstream-token',
             'minara_models': ['openai/exec-test'], 'model': 'minara/openai/exec-test',
             'relay_api_key': key, 'workspace_root': str(tmp_path), 'project_context': False,
             'exec_policy': 'allow_all', 'max_exec_turns': 2, 'show_exec_output': False,
             'command_timeout': 10, 'cache_keepwarm_seconds': 0, 'conversation_state': False,
             'journal_path': str(tmp_path / 'journal.jsonl'), 'command_log': str(tmp_path / 'commands.log')}
    path.write_text(json.dumps(value), encoding='utf-8')
    cfg = config.load(str(path))
    runtime = proxy.Runtime(cfg)
    bodies = []
    command = f'"{sys.executable}" -c "print(731942)"'

    class Response:
        status_code = 200
        headers = {'content-type': 'text/event-stream'}
        def __init__(self, content):
            self.content = content
        def iter_content(self, chunk_size):
            frame = {'choices': [{'index': 0, 'delta': {'content': self.content}, 'finish_reason': 'stop'}]}
            yield ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode()
        def close(self):
            pass

    def post(url, **kwargs):
        body = kwargs['json']
        bodies.append(body)
        assert len(bodies) <= 2, 'execution loop repeated without consuming its result'
        if len(bodies) == 1:
            return Response('<exec_command>' + command + '</exec_command>')
        sent = json.dumps(body['messages'])
        assert '731942' in sent and 'Exit code: 0' in sent and 'Output:' in sent
        return Response('Execution completed with output 731942.')

    runtime.backend._minara_post = post
    server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
    messages = [{'role': 'user', 'content': 'ORIGINAL_EXEC_TASK'},
                {'role': 'assistant', 'content': 'HISTORY_ASSISTANT'},
                {'role': 'user', 'content': 'LATEST_EXEC_TASK'}]
    if dialect == 'responses':
        route, body = '/v1/responses', {'input': messages}
    elif dialect == 'messages':
        route, body = '/v1/messages', {'messages': messages, 'max_tokens': 128}
    else:
        route, body = '/v1/chat/completions', {'messages': messages}
    try:
        conn.request('POST', route, json.dumps(dict(body, model=cfg['model'])),
                     {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        raw = response.read().decode()
        assert response.status == 200, raw
        assert 'Execution completed with output 731942.' in raw
        assert len(bodies) == 2
        assert bodies[0]['messages'][0]['role'] == 'system'
        assert bodies[1]['messages'][0] == bodies[0]['messages'][0]
        for body in bodies:
            sent = json.dumps(body['messages'])
            assert sent.count('ORIGINAL_EXEC_TASK') == 1
            assert sent.count('HISTORY_ASSISTANT') == 1
            assert sent.count('LATEST_EXEC_TASK') == 1
        assert [m['role'] for m in bodies[0]['messages'][1:]] == ['user', 'assistant', 'user']
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        runtime.close()
        thread.join(2)
        models._MINARA_MODELS.clear()
        models._MINARA_MODELS.update(prior)


def test_minara_server_search_continuation_preserves_call_and_result(tmp_path, monkeypatch):
    import asyncio
    import validate
    import web_search
    from accounts import UnmeteredAccount
    from orchestrator import TurnRunner
    from session import Backend, SessionPool
    from test_native_context_reuse import run
    prior = dict(models._MINARA_MODELS)
    cfg = dict(config.DEFAULTS, upstream='minara', base='https://api.minara.ai/v1',
               bearer_token='synthetic-search-token', minara_models=[{'id': 'openai/search-test', 'tools': True}],
               model='minara/openai/search-test', workspace_root=str(tmp_path), project_context=False,
               cookie_values=[], exec_policy='off', cache_keepwarm_seconds=0, conversation_state=False)
    calls, bodies = [], []

    class Search:
        def search(self, query, spec):
            calls.append(query)
            return [web_search.Result('Verified search result', 'https://example.org/result', None,
                                      'UNIQUE_SEARCH_RESULT_584021')]

    cfg['_web_search'] = Search()
    models.configure_minara([cfg])
    backend = Backend(cfg)
    manager = UnmeteredAccount()
    backend.credits = manager
    pool = SessionPool(backend)
    runner = TurnRunner(cfg, manager, pool)

    class Response:
        status_code = 200
        headers = {'content-type': 'text/event-stream'}
        def __init__(self, delta, finish):
            self.delta, self.finish = delta, finish
        def iter_content(self, chunk_size):
            frame = {'choices': [{'index': 0, 'delta': self.delta, 'finish_reason': self.finish}]}
            yield ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode()
        def close(self):
            pass

    def post(url, **kwargs):
        bodies.append(kwargs['json'])
        assert len(bodies) <= 2
        if len(bodies) == 1:
            return Response({'tool_calls': [{'index': 0, 'id': 'search-call', 'type': 'function',
                            'function': {'name': 'web_search', 'arguments': '{"query":"offline search"}'}}]}, 'tool_calls')
        replay = bodies[-1]['messages']
        assert replay[-2]['role'] == 'assistant'
        assert replay[-2]['tool_calls'][0]['id'] == 'search-call'
        assert replay[-1]['role'] == 'tool' and replay[-1]['tool_call_id'] == 'search-call'
        assert 'UNIQUE_SEARCH_RESULT_584021' in json.dumps(replay[-1])
        return Response({'content': 'SEARCH_COMPLETE'}, 'stop')

    backend._minara_post = post
    parsed = validate.parse_anthropic({'model': cfg['model'], 'max_tokens': 256,
        'messages': [{'role': 'user', 'content': 'ORIGINAL_SEARCH_TASK'}],
        'tools': [{'type': 'web_search_20250305', 'name': 'web_search'}]}, cfg)
    parsed.conversation_key = 'search-test'

    async def scenario():
        try:
            return await run(runner, parsed)
        finally:
            for session in list(pool._conversations.values()):
                await session.aclose()
    try:
        events = asyncio.run(scenario())
        assert events[-1] == {'type': 'end', 'reason': 'stop'}
        assert any(event.get('delta') == 'SEARCH_COMPLETE' for event in events)
        assert any(event['type'] == 'server_tool_result' for event in events)
        assert calls == ['offline search'] and len(bodies) == 2
        assert bodies[0]['messages'][0] == bodies[1]['messages'][0]
    finally:
        models._MINARA_MODELS.clear()
        models._MINARA_MODELS.update(prior)
