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


@pytest.mark.parametrize('name', ['gpt-5.3-codex', 'claude-sonnet-4-20250514', 'gpt-6-luna',
                                  'claude-opus-4-20250514', 'claude-opus-4-1-20250805'])
def test_unsupported_endpoint_keeps_metadata_but_is_not_advertised_or_dispatched(name):
    model = entry(name)
    cfg = account(model['provider'], show_unrouted_models=True)
    assert model['context'] > 0
    assert models.catalog_routable(model, cfg) is False
    with pytest.raises(apierr.ApiError):
        SessionPool._check_route(SimpleNamespace(cfg=cfg), model)
    assert model['route_status'] == 'unsupported' and model['route_reason']


@pytest.mark.parametrize('name,retired', [('claude-opus-4-20250514', '2026-06-15'),
                                          ('claude-opus-4-1-20250805', '2026-08-05')])
def test_retired_opus_is_unsupported_with_its_date_and_a_byok_resource_does_not_bring_it_back(name, retired):
    model = entry(name)
    assert model['route_status'] == 'unsupported'
    assert 'Retired by Anthropic on ' + retired in model['route_reason']
    assert 'not available' in model['route_reason'] and 'own key' in model['route_reason']
    cfg = account('anthropic', provider_access={'anthropic': {'resource_name': 'resource-a', 'byok': True}})
    assert models.route_block_reason(model, cfg) == model['route_reason']
    assert not models.catalog_routable(model, cfg)
    assert not models.catalog_routable(model, dict(cfg, show_unrouted_models=True))
    with pytest.raises(apierr.ApiError):
        SessionPool._check_route(SimpleNamespace(cfg=cfg), model)


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
                                   'gpt-6-astra', 'claude-fable-5', 'claude-opus-4-20250514',
                                   'claude-opus-4-1-20250805'})
    _, payload = relay.json('/admin/api/models')
    by_id = {m['id']: m for m in payload['data']}
    assert by_id['gpt-5.3-codex']['routable'] is False
    assert by_id['gpt-5.3-codex']['route_reason']
    assert by_id['gpt-6-astra']['route_status'] == 'gated'
    assert by_id['claude-opus-4-20250514']['route_status'] == 'unsupported'


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


def test_byok_refusal_names_the_config_key_and_the_account_entry_to_edit():
    reason = models.route_block_reason(entry('gpt-6-astra'), account('openai'))
    assert reason.startswith('Requires a suitable BYOK provider resource.')
    assert 'provider_access.openai' in reason and 'openai_provider_resource_name' in reason
    assert '"byok": true' in reason and "this account's entry of config.json" in reason
    assert 'only when there is no accounts[]' in reason


def test_refusal_hint_covers_each_way_an_operator_can_be_stuck():
    astra, fable = entry('gpt-6-astra'), entry('claude-fable-5')
    nothing = models.route_block_reason(astra, {})
    assert nothing.endswith('then declare it with provider_access.openai = {"resource_name": "<value of '
                            'openai_provider_resource_name>", "byok": true}.')
    assert nothing.count("Set openai_provider_resource_name") == 1
    managed = models.route_block_reason(astra, account('openai', openai_provider_resource_name='__managed__'))
    assert '"__managed__": Retool-managed keys never count as BYOK' in managed
    mismatch = models.route_block_reason(astra, account('openai', provider_access={
        'openai': {'resource_name': 'another-resource', 'byok': True}}))
    assert 'resource_name does not match openai_provider_resource_name; they must be identical' in mismatch
    retention = models.route_block_reason(fable, account('anthropic', provider_resource_name='__managed__'))
    assert '"data_retention_enabled": true' in retention and 'never counts as BYOK' in retention
    assert 'or "data_retention_enabled": true' in models.route_block_reason(fable, account('anthropic'))


def test_following_the_hint_exactly_unlocks_the_model():
    cfg = account('openai', openai_provider_resource_name='real-resource')
    assert models.route_block_reason(entry('gpt-6-astra'), cfg)
    cfg['provider_access'] = {'openai': {'resource_name': 'real-resource', 'byok': True}}
    import config
    config._check_pool_fields(cfg, 'account #0')
    assert models.route_block_reason(entry('gpt-6-astra'), cfg) is None
    assert models.catalog_routable(entry('gpt-6-astra'), cfg)


def pool_for(tmp_path, **config_extra):
    from test_native_context_reuse import Backend
    return SessionPool(Backend(tmp_path, **config_extra))


@pytest.mark.parametrize('root,records,model', [
    # S3c: accounts carry no resource of their own, so the pool gives them the root's.
    ({'provider_resource_name': 'R'}, [{}, {}], 'claude-opus-5-5'),
    # S3d: one account owns an OpenAI resource; the root's Anthropic resource still reaches both.
    ({'provider_resource_name': 'R'}, [{}, {'openai_provider_resource_name': 'O'}], 'claude-opus-5-5'),
    ({'provider_resource_name': 'R'}, [{}, {'openai_provider_resource_name': 'O'}], 'gpt-6-sol'),
    # A tenant with another base never inherits the root's resources.
    ({'provider_resource_name': 'R'}, [{'base': 'https://other.invalid'}], 'claude-opus-5-5'),
    ({'provider_resource_name': 'R'}, [{'base': 'https://other.invalid', 'provider_resource_name': 'S'}],
     'claude-opus-5-5'),
    # Only the account entry carries access facts; the root's descriptor is ignored with accounts[].
    ({'openai_provider_resource_name': 'O', 'provider_access': {'openai': {'resource_name': 'O', 'byok': True}}},
     [{}], 'gpt-6-astra'),
    ({'openai_provider_resource_name': 'O'},
     [{'provider_access': {'openai': {'resource_name': 'O', 'byok': True}}}], 'gpt-6-astra'),
    ({'provider_resource_name': 'R'}, [{'enabled': False}, {}], 'claude-opus-5-5'),
    ({'provider_resource_name': 'R'}, [{'enabled': False}], 'claude-opus-5-5'),
])
def test_discovery_without_backends_agrees_with_the_pool_that_serves_requests(tmp_path, root, records, model):
    cfg = dict(root, base='https://example.invalid',
               accounts=[dict({'base': 'https://example.invalid', 'page_uuid': 'p%d' % index}, **record)
                         for index, record in enumerate(records)])
    pool = pool_for(tmp_path, **cfg)
    by_pool = models.catalog_routable(entry(model), pool.cfg, pool.backends)
    assert models.catalog_routable(entry(model), pool.cfg) is by_pool
    # A published model can be served by some enabled account, except in a pool with no
    # resources at all, which discovery deliberately shows in full so that requests explain it.
    configured = any(backend.cfg.get(key) for backend in pool.backends
                     for key in models.PROVIDER_RESOURCE_KEY.values())
    served = any(backend.cfg.get('enabled', True) and SessionPool._routable(backend, entry(model))
                 for backend in pool.backends)
    assert by_pool is (served or not configured)


def test_discovery_inherits_the_root_resource_within_one_tenant_only():
    cfg = {'base': 'https://example.invalid', 'provider_resource_name': 'R',
           'accounts': [{'base': 'https://example.invalid'}, {'base': 'https://other.invalid'}]}
    merged = [models.account_config(cfg, record) for record in cfg['accounts']]
    assert merged[0]['provider_resource_name'] == 'R' and 'provider_resource_name' not in merged[1]
    assert all('accounts' not in item for item in merged)
    assert models.catalog_routable(entry('claude-opus-5-5'), cfg)
