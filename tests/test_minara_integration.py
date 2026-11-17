"""Minara route/account integration; all credentials and transports are synthetic."""
import json
import types

import pytest

import account_report
import config
import models
from accounts import AccountDispatcher, UnmeteredAccount
from apierr import ApiError
from redact import redact
from session import Backend, SessionPool


@pytest.fixture(autouse=True)
def isolated_catalog():
    prior = dict(models._MINARA_MODELS)
    yield
    models._MINARA_MODELS.clear()
    models._MINARA_MODELS.update(prior)


def minara(**extra):
    return dict(upstream='minara', bearer_token='synthetic-test-bearer',
                minara_models=[{'id': 'openai/test-model', 'tools': True}], **extra)


def load(tmp_path, monkeypatch, value):
    for name in ('RETOOL_COOKIE', 'RETOOL_XSRF', 'RETOOL_BASE', 'RETOOL_PAGE_UUID', 'RELAY_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return config.load(str(path)), json.loads(path.read_text(encoding='utf-8'))


def test_minara_only_config_needs_no_retool_credentials(tmp_path, monkeypatch):
    cfg, saved = load(tmp_path, monkeypatch, minara(model='minara/openai/test-model'))
    entry, effort = models.resolve(cfg['model'], cfg=cfg)
    assert entry['upstream_model'] == 'openai/test-model'
    assert effort is None
    assert models.provider_resource(cfg, entry) is None
    assert models.catalog_routable(entry, cfg)
    assert cfg['base'] == 'https://api.minara.ai/v1'
    import validate
    parsed = validate.parse_chat({'model': cfg['model'], 'messages': [
        {'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'},
        {'role': 'user', 'content': 'again'}]}, cfg)
    assert len(parsed.native_messages) == 3
    assert cfg['bearer_token'] in cfg['cookie_values']
    assert saved['account_id']
    backend = Backend(cfg)
    assert backend.refresher is None


def test_mixed_config_isolates_credentials_and_routes(tmp_path, monkeypatch):
    retool = dict(base='https://tenant.retool.com', page_uuid='page', cookie='session=abc', xsrf='xsrf',
                  provider_resource_name='__managed__')
    cfg, _ = load(tmp_path, monkeypatch, {'accounts': [retool, minara()]})
    pool = SessionPool(Backend(cfg))
    first, second = pool.backends
    assert first.refresher is not None and second.refresher is None
    assert not second.cfg.get('cookie') and not second.cfg.get('xsrf')
    assert not first.cfg.get('bearer_token')
    remote, _ = models.resolve('minara/openai/test-model', cfg=cfg)
    local, _ = models.resolve('claude-opus-5-5')
    assert models.refusal_reasons(remote, [first.cfg, second.cfg]) == ['route requires a Minara account', None]
    assert models.route_block_reason(local, second.cfg) == 'route requires a Retool account'


def test_env_token_not_written_to_config(tmp_path, monkeypatch):
    monkeypatch.setenv('MINARA_TEST_TOKEN', 'private-environment-value')
    value = minara()
    value.pop('bearer_token')
    value['bearer_token_env'] = 'MINARA_TEST_TOKEN'
    cfg, saved = load(tmp_path, monkeypatch, {'accounts': [value]})
    assert cfg['accounts'][0]['bearer_token'] == 'private-environment-value'
    assert 'bearer_token' not in saved['accounts'][0]
    assert cfg['accounts'][0]['account_id'] == saved['accounts'][0]['account_id']


@pytest.mark.parametrize('value', [dict(base='http://unsafe/v1'), dict(base='https://user:pass@host/v1'),
                                  dict(bearer_token='bad\ntoken'), dict(minara_models={'id': 'x'}),
                                  dict(minara_models=['unknown-without-vendor']),
                                  dict(minara_models=[{'id': 'x', 'tools': 'true'}])])
def test_invalid_provider_config_rejected(value):
    record = minara()
    record.update(value)
    with pytest.raises(ApiError):
        config.validate_minara_account(record)


def test_explicit_namespace_cannot_fall_back_to_retool():
    with pytest.raises(ApiError):
        models.resolve('minara/claude-opus-5-5', strict=False)
    models.configure_minara([minara()])
    assert models.parse_model_name('minara/openai/test-model@profile#high').profile == 'profile'


def test_no_background_retool_calls_for_minara():
    backend = Backend(minara())
    manager = UnmeteredAccount()
    dispatcher = AccountDispatcher([backend], {}, {backend: manager}, clock=lambda: 1000)
    assert dispatcher.due() == []
    entry = account_report.collect(types.SimpleNamespace(
        backend=backend, credits=manager, accounts=dispatcher))[0]
    assert entry['available'] is None and not entry['balance_supported']
    summary = account_report.summarize([entry])
    assert summary['ok'] and not summary['balance_known']
    assert account_report.aggregate_balance([entry['info']])['available'] is None


def test_bearer_redaction_handles_non_jwt_token():
    assert redact('Authorization: Bearer arbitrary-opaque-token') == 'Authorization: Bearer [redacted]'


def test_minara_usage_not_estimated_in_retool_credits():
    import credits
    result = credits.cost_breakdown({'input_tokens': 100, 'output_tokens': 20}, 'minara/openai/test-model')
    assert result['cost_input'] is None and result['cost_output'] is None
    assert result['prompt_tokens'] == 100


def test_minara_does_not_get_retool_claude_xhigh_workaround():
    models.configure_minara([dict(upstream='minara', minara_models=[{'id': 'anthropic/claude-test',
        'provider': 'anthropic', 'reasoning': True, 'reasoning_effort_levels': ['high']}])])
    with pytest.raises(ApiError):
        models.resolve_tolerant('minara/anthropic/claude-test', 'xhigh', xhigh_as='high')


@pytest.mark.parametrize('path,body', [
    ('/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]}),
    ('/v1/responses', {'input': 'hi'}),
    ('/v1/messages', {'max_tokens': 32, 'messages': [{'role': 'user', 'content': 'hi'}]})])
def test_real_relay_routes_all_dialects_to_minara(tmp_path, monkeypatch, path, body):
    import http.client
    import sys
    import threading
    import proxy
    seen = []

    async def events(cfg, session, provider, prompt, head, tools, choice, cap, chunks=None):
        seen.append((cfg['upstream'], provider['model']))
        yield {'type': 'text', 'delta': 'MINARA_OK'}
        yield {'type': 'usage', 'usage': {'input_tokens': 2, 'output_tokens': 3, 'cost_usd': 0.0001}}
        yield {'type': 'end', 'reason': 'stop'}

    monkeypatch.setitem(sys.modules, 'minara', types.SimpleNamespace(iter_events=events))
    cfg, _ = load(tmp_path, monkeypatch, minara(
        model='minara/openai/test-model', relay_api_key='test-relay-secret-key',
        conversation_state=False, project_context=False, exec_policy='off',
        cache_keepwarm_seconds=0, journal_path=str(tmp_path / 'journal.jsonl')))
    runtime = proxy.Runtime(cfg)
    balance_reads = []

    def auth_me(url, **kwargs):
        balance_reads.append((url, kwargs['headers']['Authorization']))
        return types.SimpleNamespace(status_code=200, json=lambda: {
            'creditBalanceBreakdown': {'plan': '120.5', 'extra': '200'}, 'creditBalance': {'total': '300', 'reward': '200'}})
    runtime.backend._minara_get = auth_me
    server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    try:
        conn.request('POST', path, json.dumps(dict(body, model=cfg['model'])),
                     {'Authorization': 'Bearer test-relay-secret-key', 'Content-Type': 'application/json'})
        response = conn.getresponse()
        raw = response.read().decode()
        assert response.status == 200, raw
        assert 'MINARA_OK' in raw
        assert seen == [('minara', 'openai/test-model')]
        conn.request('GET', '/health', headers={'Authorization': 'Bearer test-relay-secret-key'})
        response = conn.getresponse()
        health = json.loads(response.read())
        assert response.status == 200, health
        # Minara's credits come from GET /auth/me with the account's own Bearer (plan + extra).
        assert health['balance_known'] is True
        assert balance_reads and balance_reads[-1] == ('https://api.minara.ai/auth/me', 'Bearer synthetic-test-bearer')
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        runtime.close()
        thread.join(2)


@pytest.mark.parametrize('changes', [
    {'base': 'https://[bad'}, {'base': 'https://api.minara.ai:bad/v1'},
    {'base': 'https://api.minara.ai:99999/v1'}, {'base': 'https://api.minara.ai:0/v1'},
    {'base': 'https://api.minara.ai/v1\x00'}, {'base': 'https://api.minara.ai/v1\n'},
    {'bearer_token': 'abc\x00def'}, {'bearer_token': 'opaque-é-token'},
    {'bearer_token': 'x' * 16385}, {'minara_models': [{'id': 'x', 'label': 'bad\nlabel'}]},
    {'minara_models': [{'id': 'x', 'label': 'bad\ud800label'}]},
])
def test_header_and_url_validation_returns_api_error(changes):
    value = minara()
    value.update(changes)
    with pytest.raises(ApiError):
        config.validate_minara_account(value)


def test_retool_environment_cannot_redirect_minara_bearer(tmp_path, monkeypatch):
    import asyncio
    import minara as transport
    value = minara()
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setenv('RETOOL_BASE', 'https://unrelated-tenant.retool.com')
    monkeypatch.setenv('RETOOL_COOKIE', 'must-not-cross-upstreams')
    monkeypatch.setenv('RETOOL_XSRF', 'must-not-cross-upstreams')
    cfg = config.load(str(path))
    observed = []

    class Response:
        status_code = 200
        headers = {'content-type': 'text/event-stream'}
        def iter_content(self, **kwargs):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
            yield b'data: [DONE]\n\n'
        def close(self):
            pass

    def post(url, **kwargs):
        observed.append((url, kwargs['headers']['Authorization']))
        return Response()

    async def run():
        session = types.SimpleNamespace(backend=types.SimpleNamespace(_minara_post=post), _cancelled=False)
        async for _ in transport.iter_events(cfg, session, {'model': 'openai/test-model'}, 'hi', '', None, 'none', 10):
            pass

    asyncio.run(run())
    assert observed == [('https://api.minara.ai/v1/chat/completions', 'Bearer synthetic-test-bearer')]
    assert not cfg.get('cookie') and not cfg.get('xsrf')


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'messages'])
@pytest.mark.parametrize('choice', ['auto', 'required'])
def test_disabled_tool_capability_rejected_across_dialects(dialect, choice):
    import validate
    models.configure_minara([{'upstream': 'minara', 'minara_models': [{'id': 'openai/no-tools', 'tools': False}]}])
    cfg = dict(config.DEFAULTS, model='minara/openai/no-tools')
    schema = {'type': 'object', 'properties': {}}
    if dialect == 'chat':
        body = {'messages': [{'role': 'user', 'content': 'hi'}],
                'tools': [{'type': 'function', 'function': {'name': 'echo', 'parameters': schema}}]}
        parse = validate.parse_chat
    elif dialect == 'responses':
        body = {'input': 'hi', 'tools': [{'type': 'function', 'name': 'echo', 'parameters': schema}]}
        parse = validate.parse_responses
    else:
        body = {'max_tokens': 32, 'messages': [{'role': 'user', 'content': 'hi'}],
                'tools': [{'name': 'echo', 'input_schema': schema}]}
        parse = validate.parse_anthropic
    body['tool_choice'] = choice
    with pytest.raises(ApiError) as found:
        parse(body, cfg)
    assert found.value.status == 422 and found.value.param == 'tools'


def test_colon_model_id_catalog_request_roundtrip(tmp_path, monkeypatch):
    value = minara(model='minara/openai/test:high')
    value['minara_models'] = [{'id': 'openai/test:high', 'reasoning': True, 'reasoning_effort_levels': ['high']}]
    cfg, _ = load(tmp_path, monkeypatch, value)
    import validate
    parsed = validate.parse_chat({'model': cfg['model'], 'messages': [{'role': 'user', 'content': 'hi'}]}, cfg)
    # minara/<vendor>/<model> is the older spelling of the published minara/<model>.
    assert parsed.model == 'minara/test:high' and parsed.entry['upstream_model'] == 'openai/test:high'
    assert models.resolve(cfg['model'] + '#high', cfg=cfg)[1] == 'high'
    assert models.parse_model_name(cfg['model'] + '@profile#high', cfg=cfg).profile == 'profile'


def test_candidate_catalog_validation_is_pure():
    existing = dict(models._MINARA_MODELS)
    one = minara()
    two = minara()
    two['minara_models'] = [{'id': 'openai/test-model', 'tools': False}]
    with pytest.raises(ApiError):
        models.build_minara_catalog([one, two])
    assert models._MINARA_MODELS == existing
