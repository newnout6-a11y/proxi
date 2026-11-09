"""Loaded configurations and live HTTP runtimes own independent Minara catalogs."""
import concurrent.futures
import contextlib
import http.client
import json
import threading

import pytest

import config
import journal
import models
import proxy
from apierr import ApiError

KEY = 'catalog-scope-test-key-0123456789'


def loaded(tmp_path, name, descriptors, **overrides):
    path = tmp_path / (name + '.json')
    value = dict(upstream='minara', bearer_token='synthetic-catalog-token', minara_models=descriptors,
                 model='minara/' + (descriptors[0] if isinstance(descriptors[0], str) else descriptors[0]['id']),
                 relay_api_key=KEY, workspace_root=str(tmp_path), client_metadata_home=str(tmp_path),
                 conversation_state=False, project_context=False, exec_policy='off', cache_keepwarm_seconds=0,
                 prompts_file=str(tmp_path / 'absent.json'), journal_path=str(tmp_path / (name + '.jsonl')))
    value.update(overrides)
    path.write_text(json.dumps(value), encoding='utf-8')
    return config.load(str(path))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ('RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF', 'RETOOL_PAGE_UUID', 'RETOOL_THREAD_ID',
                'RELAY_API_KEY', 'RELAY_HOST', 'RELAY_PORT', 'RELAY_TRANSPORT', 'RELAY_SESSION_MODE'):
        monkeypatch.delenv(key, raising=False)


@contextlib.contextmanager
def serving(cfg):
    runtime = proxy.Runtime(cfg)
    async def events(*args, **kwargs):
        yield {'type': 'text', 'delta': 'OK'}
        yield {'type': 'end', 'reason': 'stop'}
    # Transport is normalized but route resolution, account config, HTTP and adapters are real.
    import minara
    prior = minara.iter_events
    minara.iter_events = events
    server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(runtime), threading.BoundedSemaphore(8))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def call(route, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            connection.request('POST' if body is not None else 'GET', route,
                               json.dumps(body) if body is not None else None,
                               {'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    try:
        yield call
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()
        thread.join(2)
        minara.iter_events = prior
        journal.JOURNAL.configure(path=None, load=False)


def request(model, tools=False):
    body = {'model': model, 'messages': [{'role': 'user', 'content': 'hi'}]}
    if tools:
        body['tools'] = [{'type': 'function', 'function': {'name': 'echo', 'parameters': {'type': 'object'}}}]
    return body


def test_unrelated_successful_and_failed_load_cannot_change_live_catalog(tmp_path):
    cfg = loaded(tmp_path, 'a', ['openai/a'])
    with serving(cfg) as call:
        assert call('/v1/chat/completions', request('minara/openai/a'))[0] == 200
        loaded(tmp_path, 'b', ['openai/b'])
        with pytest.raises(ApiError):
            loaded(tmp_path, 'bad', ['openai/bad'], reasoning_effort='bogus')
        assert call('/v1/chat/completions', request('minara/openai/a'))[0] == 200
        ids = [row['id'] for row in call('/v1/models')[1]['data']]
        assert ids == ['minara/a']  # published without the vendor prefix; minara/openai/a stays an alias
        assert call('/v1/chat/completions', request('minara/openai/b'))[0] == 404
        assert models.default_model_warning(cfg) is None
        assert models.parse_model_name('minara/openai/a@profile', cfg).profile == 'profile'


def test_two_live_runtimes_keep_same_id_capabilities_and_catalogs_isolated(tmp_path):
    cfg_a = loaded(tmp_path, 'a', [{'id': 'openai/shared', 'tools': True}, 'openai/a'])
    cfg_b = loaded(tmp_path, 'b', [{'id': 'openai/shared', 'tools': False}, 'openai/b'])
    with serving(cfg_a) as a, serving(cfg_b) as b:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(call, '/v1/chat/completions', request('minara/openai/shared', tools=True))
                       for call in (a, b) for _ in range(4)]
            statuses = [future.result()[0] for future in futures]
        assert statuses == [200] * 4 + [422] * 4
        for call, own, other in ((a, 'a', 'b'), (b, 'b', 'a')):
            ids = [row['id'] for row in call('/v1/models')[1]['data']]
            assert set(ids) == {'minara/shared', 'minara/' + own}
            assert 'minara/' + other not in ids
            codex = call('/codex/models')[1]['models']
            assert {row['slug'] for row in codex} == set(ids)
            admin_catalog = call('/admin/api/models')[1]['data']
            own_row = next(row for row in admin_catalog if row['id'] == 'minara/shared')
            assert own_row['tools'] is (own == 'a')

def test_minara_client_catalog_labels_name_the_actual_upstream():
    entry = {'id': 'minara/openai/test', 'label': 'Test', 'upstream': 'minara',
             'provider': 'openai', 'context': None, 'reasoning': False}
    assert models.codex_catalog([entry])['models'][0]['description'] == 'Minara relay: Test'
    assert models.desktop_metadata(entry, [entry])['description'] == 'Minara relay: Test'
