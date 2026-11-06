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
    assert opus['max_input_tokens'] == 1000000 and opus['supports_1m'] is True
    assert opus['anthropic_family_tier'] == 'opus' and opus['is_family_default'] is True
    assert opus['description']


def test_all_disabled_pool_does_not_fall_back_to_root_resource_for_discovery():
    cfg = {'model': 'gpt-6-sol', 'openai_provider_resource_name': 'root'}
    entry = models.resolve('gpt-6-sol')[0]
    backends = [SimpleNamespace(cfg=dict(cfg, enabled=False))]
    assert models.catalog_routable(entry, cfg, backends) is False


def test_desktop_does_not_invent_an_undocumented_family_tier():
    entry = models.resolve('claude-fable-5')[0]
    result = models.desktop_metadata(entry, [entry])
    assert result['display_name'] == 'Claude Fable 5'
    assert 'anthropic_family_tier' not in result
