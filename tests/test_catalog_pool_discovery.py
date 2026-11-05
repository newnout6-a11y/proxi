"""Discovery and startup must describe the actual account pool."""
from types import SimpleNamespace

import pytest

import account_report
import admin
import models
import proxy
from test_review_fixes_proxy import multi  # noqa: F401


def test_startup_balance_includes_funded_secondary_when_primary_is_empty(multi):
    runtime = multi.runner_.runtime
    multi.managers_[0].balance['available'] = 0
    multi.managers_[1].balance['available'] = 321.5
    balance = proxy.startup_balance(runtime)
    assert balance['available'] == 321.5
    assert [row['available'] for row in balance['accounts']] == [0, 321.5]
    status, response, _ = multi.call('/v1/balance')
    assert status == 200 and response['available'] == balance['available']


def test_startup_reports_unknown_instead_of_turning_all_failures_into_zero(multi):
    for manager in multi.managers_:
        manager.balance['available'] = None
    result = proxy.startup_balance(multi.runner_.runtime)
    assert result['available'] is None and result['error']


@pytest.mark.parametrize('path,items,key', [('/v1/models', 'data', 'id'),
                                           ('/codex/models', 'models', 'slug'),
                                           ('/admin/api/models', 'data', 'id')])
def test_secondary_only_provider_is_discovered_and_disabled_secondary_is_hidden(multi, path, items, key):
    first, second = multi.backends
    runtime = multi.runner_.runtime
    runtime.cfg['provider_resource_name'] = 'root-anthropic'
    runtime.cfg.pop('retool_fast_provider_resource_name', None)
    for backend in (first, second):
        for resource in models.PROVIDER_RESOURCE_KEY.values():
            backend.cfg.pop(resource, None)
        backend.cfg['provider_resource_name'] = 'anthropic'
    second.cfg['retool_fast_provider_resource_name'] = 'secondary-only'
    status, body, _ = multi.call(path)
    assert status == 200
    glm = next(row for row in body[items] if row[key] == 'zai-org/GLM-5.3')
    if path.startswith('/admin'):
        assert glm['routable'] is True
    second.cfg['enabled'] = False
    status, body, _ = multi.call(path)
    assert status == 200
    if path.startswith('/admin'):
        assert next(row for row in body[items] if row[key] == 'zai-org/GLM-5.3')['routable'] is False
    else:
        assert 'zai-org/GLM-5.3' not in {row[key] for row in body[items]}


def test_claude_desktop_discovery_has_native_names_and_context_metadata(multi):
    status, body, _ = multi.call('/v1/models')
    assert status == 200
    opus = next(row for row in body['data'] if row['id'] == 'claude-opus-5-5')
    assert opus['display_name'] == opus['label'] == 'Claude Opus 5.5'
    # The route reserves 872K for input; Desktop must not read an unverified window as 1M.
    assert opus['max_input_tokens'] == 872000 and 'supports_1m' not in opus
    assert opus['context_window'] == 1000000
    assert opus['anthropic_family_tier'] == 'opus' and opus['is_family_default'] is True
    assert opus['description']


def test_all_disabled_pool_does_not_fall_back_to_root_resource_for_discovery():
    cfg = {'model': 'gpt-6-sol', 'openai_provider_resource_name': 'root'}
    entry = models.resolve('gpt-6-sol')[0]
    backends = [SimpleNamespace(cfg=dict(cfg, enabled=False))]
    assert models.catalog_routable(entry, cfg, backends) is False


def test_fable_gets_the_family_tier_desktop_documents():
    entry = models.resolve('claude-fable-5')[0]
    result = models.desktop_metadata(entry, [entry])
    assert result['display_name'] == 'Claude Fable 5'
    assert result['anthropic_family_tier'] == 'fable' and result['is_family_default'] is True


@pytest.mark.parametrize('model,tier', [('claude-fable-5', 'fable'), ('claude-mythos-5', 'mythos'),
                                        ('claude-haiku-4-5-20251001', 'haiku'), ('claude-sonnet-5-5', 'sonnet'),
                                        ('claude-opus-5-5', 'opus')])
def test_every_tier_in_desktops_documented_enum_is_published(model, tier):
    entry = dict(models.resolve(model)[0]) if model != 'claude-mythos-5' else {
        'id': model, 'label': 'Claude Mythos 5', 'provider': 'anthropic', 'context': 1000000}
    result = models.desktop_metadata(entry, [entry])
    assert result['anthropic_family_tier'] == tier and result['is_family_default'] is True


def test_an_unknown_claude_family_gets_no_invented_tier():
    entry = {'id': 'claude-zeta-1', 'label': 'Claude Zeta 1', 'provider': 'anthropic', 'context': 200000}
    assert 'anthropic_family_tier' not in models.desktop_metadata(entry, [entry])


def test_desktop_is_not_told_that_an_unverified_window_is_1m(multi):
    status, body, _ = multi.call('/v1/models')
    assert status == 200
    rows = {row['id']: row for row in body['data']}
    assert not any(row.get('supports_1m') for row in rows.values())
    assert not any(row.get('max_input_tokens', 0) >= 1000000 for row in rows.values())
    # The effective input of a route is what Retool reserves for input, and the public window stays visible.
    for name, route_input in (('claude-opus-4-6', 872000), ('claude-sonnet-4-6', 936000), ('claude-opus-4-8', 872000)):
        assert rows[name]['max_input_tokens'] == route_input and rows[name]['context_window'] == 1000000
    # No verified route limit, no figure: the model's own window is not a route fact.
    assert 'max_input_tokens' not in rows['claude-sonnet-5-5'] and rows['claude-sonnet-5-5']['context_window'] == 1000000
    assert 'max_input_tokens' not in rows['claude-haiku-4-5-20251001']
    assert rows['claude-opus-4-6']['anthropic_family_tier'] == 'opus'


@pytest.mark.parametrize('path,items,key', [('/v1/models', 'data', 'id'), ('/codex/models', 'models', 'slug'),
                                           ('/admin/api/models', 'data', 'id')])
def test_shared_mode_publishes_only_what_the_serving_account_can_call(multi, path, items, key):
    first, second = multi.backends
    runtime = multi.runner_.runtime
    for backend in (first, second):
        for resource in models.PROVIDER_RESOURCE_KEY.values():
            backend.cfg.pop(resource, None)
        backend.cfg['provider_resource_name'] = 'anthropic'
    second.cfg['retool_fast_provider_resource_name'] = 'second-only'

    def glm_published():
        status, body, _ = multi.call(path)
        assert status == 200
        rows = [row for row in body[items] if row[key] == 'zai-org/GLM-5.3']
        return bool(rows) and rows[0].get('routable', True)

    assert glm_published()  # per_conversation: the pool can send it to the second account
    runtime.cfg['session_mode'] = 'shared'
    assert not glm_published()  # shared: only the first account answers, and it has no GLM resource
    first.cfg['retool_fast_provider_resource_name'] = 'first-too'
    assert glm_published()


def test_shared_mode_discovery_follows_the_first_account_even_when_it_is_switched_off():
    first = SimpleNamespace(cfg={'enabled': False, 'provider_resource_name': 'a'})
    second = SimpleNamespace(cfg={'enabled': True, 'provider_resource_name': 'a'})
    opus = models.resolve('claude-opus-5-5')[0]
    assert models.catalog_routable(opus, {'session_mode': 'per_conversation'}, [first, second])
    assert not models.catalog_routable(opus, {'session_mode': 'shared'}, [first, second])
    published, reason = models.catalog_visibility(opus, {'session_mode': 'shared'}, [first, second])
    assert (published, reason) == (False, 'no enabled account')


def test_shared_mode_without_backends_uses_the_first_account_record_like_the_pool():
    cfg = {'session_mode': 'shared', 'base': 'https://t.invalid', 'accounts': [
        {'base': 'https://t.invalid', 'provider_resource_name': 'a'},
        {'base': 'https://t.invalid', 'provider_resource_name': 'a', 'retool_fast_provider_resource_name': 'g'}]}
    glm = models.resolve('zai-org/GLM-5.3')[0]
    assert not models.catalog_routable(glm, cfg)
    assert models.catalog_routable(glm, dict(cfg, session_mode='per_conversation'))
