"""provider_access: errors name the provider and the field; the root descriptor is validated and its limit is said aloud."""
import json

import pytest

import apierr
import config
import models
from session import Backend, SessionPool

HEX = 'abcdef12-0000-4000-8000-123456789abc'


def account(name='a', **extra):
    return dict(account_id=name, base='https://tenant.invalid', page_uuid='p-' + name,
                cookie='accessToken=c-%s-123456; xsrfToken=q' % name, xsrf='q', **extra)


def load(tmp_path, monkeypatch, **settings):
    path = tmp_path / 'config.json'
    saved = {'base': 'https://tenant.invalid', 'page_uuid': 'p0', 'ua': 'ua', 'cookie': 'accessToken=fake-token',
             'xsrf': 'fake-xsrf', 'workspace_root': str(tmp_path), **settings}
    path.write_text(json.dumps(saved), encoding='utf-8')
    monkeypatch.setenv('RELAY_API_KEY', 'local-test-key')
    for name in ('RETOOL_COOKIE', 'RETOOL_XSRF', 'RETOOL_BASE', 'RETOOL_PAGE_UUID'):
        monkeypatch.delenv(name, raising=False)
    return config.load(str(path))


@pytest.mark.parametrize('access,fragments', [
    ('text', ["'provider_access' must be an object"]),
    ({'unknown': {'resource_name': 'x'}},
     ["provider_access has an unknown provider 'unknown'", 'anthropic, google, openai, retool-fast']),
    ({'openai': []}, ['provider_access.openai must be an object']),
    ({'openai': {'resource_name': 'x', 'extra': 1}},
     ['provider_access.openai has unknown field(s): extra', 'allowed: resource_name, byok, data_retention_enabled']),
    ({'openai': {}}, ['provider_access.openai.resource_name must be a non-empty string',
                      'the same value as openai_provider_resource_name']),
    ({'anthropic': {'resource_name': 5}}, ['provider_access.anthropic.resource_name must be a non-empty string',
                                           'the same value as provider_resource_name']),
    ({'openai': {'resource_name': 'x', 'byok': 'yes'}}, ['provider_access.openai.byok must be a boolean']),
    ({'anthropic': {'resource_name': 'x', 'data_retention_enabled': 1}},
     ['provider_access.anthropic.data_retention_enabled must be a boolean']),
])
def test_errors_name_the_account_the_provider_and_the_field(access, fragments):
    with pytest.raises(apierr.ApiError) as caught:
        config._check_pool_fields({'provider_access': access}, 'account #3')
    assert caught.value.message.startswith('account #3: ')
    for fragment in fragments:
        assert fragment in caught.value.message


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'google', 'retool-fast'])
def test_byok_is_refused_for_a_managed_resource_for_every_provider(provider):
    access = {provider: {'resource_name': '__managed__', 'byok': True}}
    with pytest.raises(apierr.ApiError) as caught:
        config._check_pool_fields({'provider_access': access}, 'account #0')
    assert ("provider_access.%s.byok cannot be true when resource_name is '__managed__'" % provider
            in caught.value.message)
    assert 'never BYOK' in caught.value.message


def test_managed_with_byok_false_or_retention_and_a_real_resource_with_byok_are_accepted():
    config._check_pool_fields({'provider_access': {'anthropic': {'resource_name': '__managed__',
                                                                 'data_retention_enabled': True}}}, 'a')
    config._check_pool_fields({'provider_access': {'openai': {'resource_name': '__managed__', 'byok': False}}}, 'a')
    config._check_pool_fields({'provider_access': {'openai': {'resource_name': HEX, 'byok': True}}}, 'a')


@pytest.mark.parametrize('where', ['account', 'single', 'root-with-accounts'])
def test_load_refuses_byok_on_a_managed_resource(tmp_path, monkeypatch, where):
    bad = {'openai': {'resource_name': '__managed__', 'byok': True}}
    if where == 'account':
        settings = {'accounts': [account(provider_access=bad)]}
    elif where == 'single':
        settings = {'provider_access': bad}
    else:
        settings = {'accounts': [account()], 'provider_access': bad}
    with pytest.raises(apierr.ApiError) as caught:
        load(tmp_path, monkeypatch, **settings)
    assert "provider_access.openai.byok cannot be true when resource_name is '__managed__'" in caught.value.message
    assert caught.value.message.startswith({'account': 'account #0', 'single': 'the account',
                                            'root-with-accounts': 'top-level'}[where])


def test_a_malformed_root_descriptor_next_to_accounts_is_rejected_not_ignored(tmp_path, monkeypatch):
    for broken in ({'openai': {'resource_name': 'x', 'byok': 'yes'}}, [], 'text', {'nope': {}}):
        with pytest.raises(apierr.ApiError) as caught:
            load(tmp_path, monkeypatch, accounts=[account()], provider_access=broken)
        assert caught.value.message.startswith('top-level: ')


def test_a_root_descriptor_next_to_accounts_loads_but_is_reported_as_ignored(tmp_path, monkeypatch, capsys):
    root = {'openai': {'resource_name': HEX, 'byok': True}}
    cfg = load(tmp_path, monkeypatch, accounts=[account()], openai_provider_resource_name=HEX, provider_access=root)
    assert cfg['config_warnings'] == ['top-level provider_access is ignored when accounts[] is present; '
                                      'move it into the entry of the account that owns the resource']
    assert 'warning: top-level provider_access is ignored' in capsys.readouterr().err
    pool = SessionPool(Backend(cfg))
    assert not SessionPool._routable(pool.backends[0], models.resolve('gpt-6-astra')[0])


def test_the_same_descriptor_inside_the_account_unlocks_the_model_without_a_warning(tmp_path, monkeypatch, capsys):
    cfg = load(tmp_path, monkeypatch, accounts=[account(
        openai_provider_resource_name=HEX, provider_access={'openai': {'resource_name': HEX, 'byok': True}})])
    assert cfg['config_warnings'] == [] and capsys.readouterr().err == ''
    pool = SessionPool(Backend(cfg))
    assert SessionPool._routable(pool.backends[0], models.resolve('gpt-6-astra')[0])


def test_a_descriptor_naming_another_resource_is_reported(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, accounts=[
        account('a', openai_provider_resource_name=HEX,
                provider_access={'openai': {'resource_name': 'another-uuid', 'byok': True}}),
        account('b', provider_access={'anthropic': {'resource_name': 'x', 'byok': True}})])
    assert cfg['config_warnings'] == [
        'account #0: provider_access.openai.resource_name differs from openai_provider_resource_name, '
        'so the descriptor is ignored until they are identical',
        'account #1: provider_access.anthropic.resource_name differs from provider_resource_name, '
        'so the descriptor is ignored until they are identical']


def test_a_single_account_config_reports_a_mismatched_descriptor_too(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, openai_provider_resource_name=HEX,
               provider_access={'openai': {'resource_name': 'other', 'byok': True}})
    assert cfg['config_warnings'] == ['the account: provider_access.openai.resource_name differs from '
                                      'openai_provider_resource_name, so the descriptor is ignored until they '
                                      'are identical']


def test_the_root_resource_counts_for_an_account_that_inherits_it(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, openai_provider_resource_name=HEX,
               accounts=[account(provider_access={'openai': {'resource_name': HEX, 'byok': True}})])
    assert cfg['config_warnings'] == []


@pytest.mark.parametrize('settings', [{}, {'accounts': [account()]}, {'provider_access': {}},
                                      {'accounts': [account()], 'provider_access': {}}])
def test_working_configs_without_a_descriptor_load_silently(tmp_path, monkeypatch, capsys, settings):
    cfg = load(tmp_path, monkeypatch, **settings)
    assert cfg['config_warnings'] == [] and capsys.readouterr().err == ''
