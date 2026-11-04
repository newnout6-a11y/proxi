"""Route availability is separate from metadata, account balance and provider gates."""
from types import SimpleNamespace

import pytest

import apierr
import models
from session import SessionPool


def account(provider='openai', **extra):
    return {models.PROVIDER_RESOURCE_KEY[provider]: 'resource-a', **extra}


def entry(name):
    return models.resolve(name)[0]


@pytest.mark.parametrize('name', ['gpt-5.3-codex', 'claude-sonnet-4-20250514', 'gpt-6-luna'])
def test_unsupported_endpoint_keeps_metadata_but_is_not_advertised_or_dispatched(name):
    model = entry(name)
    cfg = account(model['provider'], show_unrouted_models=True)
    assert model['context'] > 0
    assert models.catalog_routable(model, cfg) is False
    with pytest.raises(apierr.ApiError):
        SessionPool._check_route(SimpleNamespace(cfg=cfg), model)
    assert model['route_status'] == 'unsupported' and model['route_reason']


@pytest.mark.parametrize('name', ['gpt-5', 'gpt-5-mini', 'gpt-5-nano', 'gpt-5.1', 'gpt-5.2',
                                  'claude-haiku-4-5-20251001', 'gpt-5.4', 'claude-opus-5', 'zai-org/GLM-5.3'])
def test_working_legacy_assist_and_transient_routes_remain(name):
    model = entry(name)
    assert models.catalog_routable(model, account(model['provider'])) is True


@pytest.mark.parametrize('name,provider,gate', [('gpt-6-astra', 'openai', 'byok'),
    ('claude-fable-5', 'anthropic', 'data_retention_enabled')])
def test_gates_are_separate_and_can_be_satisfied_by_enabled_secondary(name, provider, gate):
    model = entry(name)
    primary = account(provider)
    secondary = account(provider, provider_access={provider: {'resource_name': 'resource-a', gate: True}})
    assert not models.catalog_routable(model, primary)
    cfg = {'accounts': [primary, secondary]}
    assert models.catalog_routable(model, cfg)
    assert models.catalog_routable(model, primary, [SimpleNamespace(cfg=primary), SimpleNamespace(cfg=secondary)])
    secondary['enabled'] = False
    assert not models.catalog_routable(model, cfg)
    assert not models.catalog_routable(model, primary, [SimpleNamespace(cfg=primary), SimpleNamespace(cfg=secondary)])


def test_fable_can_use_byok_but_astra_does_not_use_anthropic_retention_permission():
    cfg = {'openai_provider_resource_name': 'openai-resource', 'provider_resource_name': 'anthropic-resource',
           'provider_access': {'anthropic': {'resource_name': 'anthropic-resource', 'byok': True,
                                           'data_retention_enabled': True}}}
    assert models.catalog_routable(entry('claude-fable-5'), cfg)
    assert not models.catalog_routable(entry('gpt-6-astra'), cfg)


def test_access_metadata_is_resource_bound_and_never_inherited_between_accounts(tmp_path):
    from test_native_context_reuse import Backend
    first = account(provider_access={'openai': {'resource_name': 'resource-a', 'byok': True}})
    stale = dict(first, openai_provider_resource_name='different-resource')
    assert not models.catalog_routable(entry('gpt-6-astra'), stale)
    backend = Backend(tmp_path, **first, accounts=[dict(base='https://one.invalid', **first),
        dict(base='https://two.invalid', **account())])
    pool = SessionPool(backend)
    assert SessionPool._routable(pool.backends[0], entry('gpt-6-astra'))
    assert not SessionPool._routable(pool.backends[1], entry('gpt-6-astra'))


@pytest.mark.parametrize('path,key,id_field', [('/v1/models', 'data', 'id'), ('/codex/models', 'models', 'slug')])
def test_public_discovery_hides_blocked_routes_and_admin_retains_the_reason(relay, path, key, id_field):
    relay.cfg.update(openai_provider_resource_name='__managed__', provider_resource_name='__managed__')
    _, payload = relay.json(path)
    names = {m[id_field] for m in payload[key]}
    assert 'claude-opus-5-5' in names and 'claude-haiku-4-5-20251001' in names
    assert not names.intersection({'gpt-5.3-codex', 'claude-sonnet-4-20250514', 'gpt-6-luna',
                                   'gpt-6-astra', 'claude-fable-5'})
    _, payload = relay.json('/admin/api/models')
    by_id = {m['id']: m for m in payload['data']}
    assert by_id['gpt-5.3-codex']['routable'] is False
    assert by_id['gpt-5.3-codex']['route_reason']
    assert by_id['gpt-6-astra']['route_status'] == 'gated'


def test_newly_verified_models_have_exact_ids_and_route_efforts():
    terra = entry('gpt-6.1-terra')
    assert terra['reasoning_effort_levels'] == ['none', 'low', 'medium', 'high', 'max']
    with pytest.raises(apierr.ApiError):
        models.resolve('gpt-6.1-terra', 'xhigh')
    old = entry('gpt-4.1')
    assert old['reasoning'] is False and old['reasoning_effort_levels'] == []
    assert (old['context'], old['max_output']) == (1047576, 32768)
    assert models.resolve('gpt-4.1', 'none')[1] is None
    with pytest.raises(apierr.ApiError):
        models.resolve('gpt-4.1', 'high')


@pytest.mark.parametrize('value', [[], {'openai': {'resource_name': 'x', 'byok': 'yes'}},
    {'unknown': {'resource_name': 'x'}}, {'openai': {'resource_name': 1}},
    {'openai': {'resource_name': 'x', 'unknown': True}}])
def test_invalid_resource_access_descriptor_is_rejected(value):
    import config
    with pytest.raises(apierr.ApiError):
        config._check_pool_fields({'provider_access': value}, 'account')


def test_resource_access_descriptor_validation_and_managed_google_rejection():
    import config
    config._check_pool_fields({'provider_access': {'openai': {'resource_name': 'x', 'byok': True}}}, 'account')
    model = entry('gemini-2.5-pro')
    assert not models.catalog_routable(model, {'google_provider_resource_name': '__managed__'})
    with pytest.raises(apierr.ApiError):
        SessionPool._check_route(SimpleNamespace(cfg={'google_provider_resource_name': '__managed__'}), model)
