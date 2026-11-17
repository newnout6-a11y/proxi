"""Filtered Minara turns are terminal, including empty and unsafe partial turns."""
import http.client
import json
import threading

import pytest

import config
import executor
import models
import proxy


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'messages'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('content', ['', 'Filtered partial text', '<exec_command>unsafe command</exec_command>'])
def test_filtered_completion_never_retries_or_executes(tmp_path, monkeypatch, dialect, stream, content):
    before = dict(models._MINARA_MODELS)
    for key in ('RELAY_API_KEY', 'RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF'):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'upstream': 'minara', 'bearer_token': 'synthetic-filter-token',
        'minara_models': ['openai/filter-test'], 'model': 'minara/openai/filter-test',
        'relay_api_key': 'synthetic-filter-relay-key', 'workspace_root': str(tmp_path),
        'project_context': False, 'exec_policy': 'allow_all', 'conversation_state': False,
        'cache_keepwarm_seconds': 0, 'journal_path': str(tmp_path / 'journal.jsonl')}), encoding='utf8')
    runtime = proxy.Runtime(config.load(str(path)))
    calls, commands = [], []
    monkeypatch.setattr(executor, 'run_command', lambda *a, **kw: commands.append(a) or (0, 'must-not-run'))

    class Response:
        status_code = 200
        headers = {'content-type': 'text/event-stream'}
        def iter_content(self, chunk_size):
            chunk = {'choices': [{'index': 0, 'delta': {'content': content} if content else {},
                                  'finish_reason': 'content_filter'}]}
            yield ('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode()
        def close(self):
            pass

    def post(*a, **kw):
        calls.append(kw['json'])
        return Response()
    runtime.backend._minara_post = post
    server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    if dialect == 'responses':
        route, body = '/v1/responses', {'input': 'hi'}
    elif dialect == 'messages':
        route, body = '/v1/messages', {'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 128}
    else:
        route, body = '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]}
    try:
        conn.request('POST', route, json.dumps(dict(body, model='minara/openai/filter-test', stream=stream)),
                     {'Authorization': 'Bearer synthetic-filter-relay-key', 'Content-Type': 'application/json'})
        response = conn.getresponse()
        raw = response.read().decode()
        assert response.status == 200, raw
        assert ('refusal' if dialect == 'messages' else 'content_filter') in raw, raw
        assert 'upstream_empty_turn' not in raw
        assert len(calls) == 1 and commands == []
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        runtime.close()
        thread.join(2)
        models._MINARA_MODELS.clear()
        models._MINARA_MODELS.update(before)


def test_filtered_server_search_call_is_never_executed(tmp_path):
    import asyncio
    import validate
    from accounts import UnmeteredAccount
    from orchestrator import TurnRunner
    from session import Backend, SessionPool
    from test_native_context_reuse import run
    before = dict(models._MINARA_MODELS)
    calls, posts = [], []

    class Search:
        def search(self, query, spec):
            calls.append(query)
            return []

    cfg = dict(config.DEFAULTS, upstream='minara', base='https://api.minara.ai/v1',
               bearer_token='synthetic-token', minara_models=[{'id': 'openai/filtered-tool', 'tools': True}],
               model='minara/openai/filtered-tool', workspace_root=str(tmp_path), project_context=False,
               cookie_values=[], exec_policy='off', cache_keepwarm_seconds=0, _web_search=Search())
    models.configure_minara([cfg])
    backend = Backend(cfg)
    manager = UnmeteredAccount()
    backend.credits = manager
    pool = SessionPool(backend)
    runner = TurnRunner(cfg, manager, pool)

    class Response:
        status_code = 200
        headers = {'content-type': 'text/event-stream'}
        def iter_content(self, chunk_size):
            frame = {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'filtered-search',
                'type': 'function', 'function': {'name': 'web_search', 'arguments': '{"query":"do not execute"}'}}]},
                'finish_reason': 'content_filter'}]}
            yield ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode()
        def close(self):
            pass

    def post(*args, **kwargs):
        posts.append(kwargs['json'])
        return Response()
    backend._minara_post = post
    parsed = validate.parse_anthropic({'model': cfg['model'], 'max_tokens': 64,
        'messages': [{'role': 'user', 'content': 'hi'}],
        'tools': [{'type': 'web_search_20250305', 'name': 'web_search'}]}, cfg)

    async def scenario():
        try:
            return await run(runner, parsed)
        finally:
            for session in pool._conversations.values():
                await session.aclose()
    try:
        events = asyncio.run(scenario())
        assert events[-1] == {'type': 'end', 'reason': 'content_filter'}
        assert calls == [] and len(posts) == 1
        assert not any(e['type'] == 'server_tool_result' for e in events)
    finally:
        models._MINARA_MODELS.clear()
        models._MINARA_MODELS.update(before)
