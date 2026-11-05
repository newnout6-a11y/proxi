"""A model no account can serve is a settled 4xx that names the reason; 503 stays for "no usable account now"."""
import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

import apierr
import config
import models
import proxy
from session import Backend, SessionPool

KEY = 'route-errors-key'
M = '__managed__'
MESSAGE = [{'role': 'user', 'content': 'hi'}]
PAYLOADS = {
    'chat': ('/v1/chat/completions', lambda model: {'model': model, 'messages': MESSAGE}),
    'anthropic': ('/v1/messages', lambda model: {'model': model, 'max_tokens': 16, 'messages': MESSAGE}),
    'responses': ('/v1/responses', lambda model: {'model': model, 'input': 'hi'}),
}


def pool_config(accounts, **overrides):
    cfg = dict(config.DEFAULTS, base='https://root.invalid', ua='route-errors', cookie='synthetic-only',
               xsrf='synthetic-only', cookie_values=[], page_uuid='p-root', session_refresh_interval=0,
               cache_keepwarm_seconds=0, max_workers=8, accounts=accounts)
    cfg.update(overrides)
    return cfg


def acct(name, **extra):
    return dict(account_id=name, page_uuid='p-' + name, thread_id='t-' + name, base='https://root.invalid',
                cookie='accessToken=c-%s-123456; xsrfToken=q' % name, xsrf='q', **extra)


def entry(name):
    return models.resolve(name)[0]


def acquire_error(pool, name, **kwargs):
    async def scenario():
        with pytest.raises(apierr.ApiError) as caught:
            await pool.acquire('conversation', model_entry=entry(name), **kwargs)
        assert pool._slots._value == 8 and not pool._conversation_locks and not pool._conversation_refs
        return caught.value
    return asyncio.run(scenario())


# -- pool: settled refusals are 404 -------------------------------------------------------------
@pytest.mark.parametrize('name,fragment', [
    ('claude-fable-5', 'data_retention_enabled'),
    ('gpt-6-astra', 'provider_access.openai'),
    ('claude-opus-4-20250514', 'Retired by Anthropic on 2026-06-15'),
    ('claude-opus-4-1-20250805', 'Retired by Anthropic on 2026-08-05'),
    ('gpt-5.3-codex', 'Responses is required'),
    ('gpt-6-luna', 'Unavailable on the Retool React route'),
    ('zai-org/GLM-5.3', 'retool_fast_provider_resource_name'),
    ('gemini-2.5-pro', 'a real Google BYOK resource'),
])
def test_when_every_account_refuses_by_configuration_the_error_is_a_404_with_the_reason(name, fragment):
    pool = SessionPool(Backend(pool_config([acct('a', provider_resource_name=M, openai_provider_resource_name=M),
                                            acct('b', provider_resource_name=M, openai_provider_resource_name=M)])))
    for call in (lambda: pool._next_backend(entry(name)), lambda: acquire_error(pool, name)):
        try:
            error = call()
        except apierr.ApiError as raised:
            error = raised
        assert error.status == 404 and error.code == 'not_found_error'
        assert name in error.message and fragment in error.message
        assert error.retry_after is None


def test_identical_reasons_of_several_accounts_are_reported_once():
    pool = SessionPool(Backend(pool_config([acct('a', provider_resource_name=M), acct('b'),
                                            acct('c', provider_resource_name=M)])))
    with pytest.raises(apierr.ApiError) as caught:
        pool._next_backend(entry('gpt-6-sol'))
    assert caught.value.status == 404
    assert caught.value.message.count("needs config key 'openai_provider_resource_name'") == 1


def test_different_reasons_of_several_accounts_are_each_named():
    pool = SessionPool(Backend(pool_config([acct('a', openai_provider_resource_name=M), acct('b')])))
    with pytest.raises(apierr.ApiError) as caught:
        pool._next_backend(entry('gpt-6-astra'))
    message = caught.value.message
    assert caught.value.status == 404 and message.count('Requires a suitable BYOK provider resource.') == 2
    assert '"__managed__": Retool-managed keys never count as BYOK' in message
    assert 'Set openai_provider_resource_name to your own resource' in message


def test_one_capable_account_is_enough_to_route_around_refusals():
    pool = SessionPool(Backend(pool_config([acct('a'), acct('b', provider_resource_name=M)])))
    assert pool._next_backend(entry('claude-opus-5-5')) is pool.backends[1]


@pytest.mark.parametrize('mode', ['per_conversation', 'shared'])
def test_shared_and_per_conversation_modes_agree_on_the_refusal(mode):
    pool = SessionPool(Backend(pool_config([acct('a', provider_resource_name=M)],
                                           session_mode=mode, thread_id='shared-thread')))
    error = acquire_error(pool, 'claude-fable-5')
    assert error.status == 404 and 'data_retention_enabled' in error.message


# -- pool: 503 only for "no usable account right now" ----------------------------------------------
def test_503_is_kept_for_an_account_that_could_serve_but_is_off():
    pool = SessionPool(Backend(pool_config([acct('a'), acct('b', provider_resource_name=M, enabled=False)])))
    with pytest.raises(apierr.ApiError) as caught:
        pool._next_backend(entry('claude-opus-5-5'))
    assert caught.value.status == 503 and 'no enabled account has a route' in caught.value.message


def test_503_is_kept_when_every_account_is_switched_off_and_the_model_is_fine():
    pool = SessionPool(Backend(pool_config([acct('a', provider_resource_name=M, enabled=False)])))
    error = acquire_error(pool, 'claude-opus-5-5')
    assert error.status == 503 and 'switched off' in error.message


def test_a_capable_account_without_credits_is_a_402_not_a_settled_refusal():
    pool = SessionPool(Backend(pool_config([acct('a', provider_resource_name=M)])))
    pool.backends[0].unavailable = {'reason': 'exhausted', 'available': 0, 'threshold': 20}
    error = acquire_error(pool, 'claude-opus-5-5')
    assert error.status == 402 and error.code == 'insufficient_credits'


def test_a_capable_account_that_is_otherwise_unusable_is_a_503():
    pool = SessionPool(Backend(pool_config([acct('a', provider_resource_name=M)])))
    pool.backends[0].unavailable = {'reason': 'paused'}
    error = acquire_error(pool, 'claude-opus-5-5')
    assert error.status == 503 and 'no usable account' in error.message


# -- the HTTP layer's own check, which gives a stream a real status --------------------------------
def test_model_refusal_is_none_for_a_servable_model_and_for_a_pool_that_has_no_resources_at_all():
    pool = SessionPool(Backend(pool_config([acct('a', provider_resource_name=M)])))
    assert pool.model_refusal(entry('claude-opus-5-5')) is None
    assert pool.model_refusal(None) is None
    assert pool.model_refusal(entry('claude-fable-5')).status == 404
    assert pool.model_refusal(entry('gpt-6-sol')).status == 404  # others are configured: this one has no resource
    bare = SessionPool(Backend(pool_config([acct('a'), acct('b')])))
    assert bare.model_refusal(entry('claude-opus-5-5')) is None  # nothing configured: acquire explains it
    assert bare.model_refusal(entry('gpt-5.3-codex')).status == 404  # a model fact holds everywhere


def test_model_refusal_leaves_a_disabled_but_capable_account_to_the_503_path():
    pool = SessionPool(Backend(pool_config([acct('a'), acct('b', provider_resource_name=M, enabled=False)])))
    assert pool.model_refusal(entry('claude-opus-5-5')) is None


@pytest.fixture
def relay_with_pool(tmp_path):
    started = []

    def start(accounts, **overrides):
        cfg = pool_config(accounts, relay_api_key=KEY, workspace_root=str(tmp_path),
                          command_log=str(tmp_path / 'c.log'), host='127.0.0.1', port=0,
                          client_metadata_home=str(tmp_path), **overrides)
        runtime = proxy.Runtime(cfg)
        server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = 'http://127.0.0.1:%d' % server.server_address[1]
        started.append((runtime, server))

        def call(path, payload):
            request = urllib.request.Request(base + path, data=json.dumps(payload).encode())
            request.add_header('content-type', 'application/json')
            request.add_header('authorization', 'Bearer ' + KEY)
            try:
                response = urllib.request.urlopen(request, timeout=20)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status, response.headers, response.read().decode()
        call.runtime = runtime
        return call

    yield start
    for runtime, server in started:
        server.shutdown()
        server.server_close()
        runtime.close()


LIVE_LIKE = [acct('a', provider_resource_name=M, openai_provider_resource_name=M),
             acct('b', provider_resource_name=M, openai_provider_resource_name=M,
                  retool_fast_provider_resource_name=M)]


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('dialect,model,fragment', [
    ('chat', 'claude-fable-5', 'provider_access.anthropic'),
    ('anthropic', 'claude-opus-4-20250514', 'Retired by Anthropic on 2026-06-15'),
    ('responses', 'gpt-6-luna', 'Unavailable on the Retool React route'),
    ('chat', 'gemini-2.5-pro', 'google_provider_resource_name'),
    ('anthropic', 'gpt-5.3-codex', 'Responses is required'),
])
def test_http_refusal_is_a_real_404_for_stream_and_non_stream_in_each_dialect(relay_with_pool, dialect, model,
                                                                              fragment, stream):
    call = relay_with_pool(LIVE_LIKE)
    path, build = PAYLOADS[dialect]
    status, headers, raw = call(path, dict(build(model), stream=stream))
    body = json.loads(raw)
    assert status == 404 and 'json' in headers['content-type'] and 'retry-after' not in headers
    error = body['error']
    assert error['type'] == 'not_found_error' and model in error['message'] and fragment in error['message']
    if dialect == 'anthropic':
        assert body['type'] == 'error' and set(error) == {'type', 'message'}
    else:
        assert error['code'] == 'not_found_error'
    assert body['request_id'] == headers['x-request-id']


def test_http_refusal_never_reaches_the_runner_or_an_account(relay_with_pool):
    call = relay_with_pool(LIVE_LIKE)
    calls = []

    class Runner:
        async def run(self, parsed, out, holder):
            calls.append(parsed)
            out.put(None)

    call.runtime.runner = Runner()
    assert call('/v1/chat/completions', {'model': 'claude-fable-5', 'messages': MESSAGE})[0] == 404
    assert not calls and not call.runtime.pool._active and not call.runtime.pool._conversations


@pytest.mark.parametrize('stream', [False, True])
def test_a_servable_model_passes_the_check_and_reaches_the_runner(relay_with_pool, stream):
    call = relay_with_pool(LIVE_LIKE)

    class Runner:
        async def run(self, parsed, out, holder):
            out.put({'type': 'text', 'delta': 'ok'})
            out.put({'type': 'end', 'reason': 'stop'})
            out.put(None)

    call.runtime.runner = Runner()
    status, _, raw = call('/v1/chat/completions', {'model': 'claude-opus-5-5', 'messages': MESSAGE, 'stream': stream})
    assert status == 200 and '"content": "ok"' in raw


def test_http_503_is_kept_when_the_only_capable_account_is_switched_off(relay_with_pool):
    accounts = [acct('a'), acct('b', provider_resource_name=M, enabled=False)]
    call = relay_with_pool(accounts)
    status, headers, raw = call('/v1/chat/completions', {'model': 'claude-opus-5-5', 'messages': MESSAGE})
    assert status == 503 and json.loads(raw)['error']['type'] == 'service_unavailable'


def test_unconfigured_pool_explains_the_missing_resource_as_a_404_event_in_the_stream(relay_with_pool):
    call = relay_with_pool([acct('a'), acct('b')])
    status, headers, raw = call('/v1/chat/completions', {'model': 'claude-opus-5-5', 'messages': MESSAGE,
                                                          'stream': True})
    assert status == 200 and 'text/event-stream' in headers['content-type']
    assert 'not_found_error' in raw and "needs config key 'provider_resource_name'" in raw
    status, _, raw = call('/v1/chat/completions', {'model': 'claude-opus-5-5', 'messages': MESSAGE})
    assert status == 404 and "needs config key 'provider_resource_name'" in raw


@pytest.mark.parametrize('stream', [False, True])
def test_a_request_without_a_model_gets_the_reason_when_the_default_model_is_hidden(relay_with_pool, stream):
    call = relay_with_pool(LIVE_LIKE, model='claude-fable-5')
    status, headers, raw = call('/v1/chat/completions', {'messages': MESSAGE, 'stream': stream})
    error = json.loads(raw)['error']
    assert status == 404 and error['type'] == 'not_found_error'
    assert "model 'claude-fable-5' is not available" in error['message'] and 'data_retention_enabled' in error['message']
