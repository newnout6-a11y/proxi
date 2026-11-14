"""Native Desktop model names, families and contiguous effort caps are honest."""
import copy
import json
import sys
from types import SimpleNamespace

import pytest

import models
from scripts.sync_claude_desktop_catalog import build_catalog, compatible_effort_cap, main, write_policy


def entries():
    cfg = {'provider_resource_name': '__managed__', 'openai_provider_resource_name': '__managed__',
           'retool_fast_provider_resource_name': '__managed__'}
    return [entry for entry in models.catalog() if models.catalog_routable(entry, cfg)]


def test_desktop_contains_all_eligible_claude_models_and_no_fabricated_provider_tiers():
    available = entries()
    previous = [{'name': 'claude-opus-5-5', 'maxEffort': 'xhigh', 'custom': True},
                {'name': 'claude-opus', 'labelOverride': 'stale alias'},
                {'name': 'gpt-6-astra', 'anthropicFamilyTier': 'opus'}]
    original = copy.deepcopy(previous)
    result = build_catalog(available, previous)
    assert previous == original
    assert {item['name'] for item in result} == {item['id'] for item in available if item['provider'] == 'anthropic'}
    assert len(result) == 11
    assert {item['anthropicFamilyTier'] for item in result} == {'opus', 'sonnet', 'haiku'}
    assert all(item['maxEffort'] == ('max' if item['name'] in ('claude-opus-4-6', 'claude-sonnet-4-6') else 'high')
               for item in result)
    assert next(item for item in result if item['name'] == 'claude-opus-5-5')['custom'] is True
    assert {item['name'] for item in result if item['isFamilyDefault']} == {
        'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-4-5-20251001'}


@pytest.mark.parametrize('levels,expected', [([], None), (['low'], 'low'),
    (['low', 'medium', 'high', 'max'], 'high'),
    (['low', 'medium', 'high', 'xhigh', 'max'], 'max'),
    (['none', 'low', 'medium', 'high'], 'high'), (['high'], None)])
def test_cap_does_not_create_a_missing_intermediate_effort(levels, expected):
    assert compatible_effort_cap(levels) == expected


def test_four_level_native_picker_retains_max_without_inventing_xhigh():
    levels = ['low', 'medium', 'high', 'max']
    assert compatible_effort_cap(levels, tuple(levels)) == 'max'


def test_fable_does_not_get_an_invented_family_when_a_real_resource_is_configured():
    fable = models.resolve('claude-fable-5')[0]
    result = build_catalog([fable], [{'name': fable['id'], 'anthropicFamilyTier': 'opus'}])
    assert 'anthropicFamilyTier' not in result[0]


@pytest.mark.parametrize('previous', [{}, [None], 'invalid'])
def test_invalid_policy_is_refused(previous):
    with pytest.raises(ValueError):
        build_catalog(entries(), previous)


def test_empty_desktop_route_set_is_never_written():
    with pytest.raises(ValueError):
        build_catalog([models.resolve('gpt-6-sol')[0]], [])


def test_write_requires_backup_before_any_registry_read():
    with pytest.raises(SystemExit) as error:
        main(['--write'])
    assert error.value.code == 2


def registry(monkeypatch, original, kind=1):
    state = {'value': original, 'kind': kind, 'writes': []}
    class Key:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def query(key, name):
        assert name == 'inferenceModels'
        return state['value'], state['kind']
    def set_value(key, name, reserved, value_kind, value):
        state['writes'].append((name, value_kind, value))
        state.update(value=value, kind=value_kind)
    fake = SimpleNamespace(HKEY_CURRENT_USER='HKCU', REG_SZ=kind, KEY_QUERY_VALUE=1, KEY_SET_VALUE=2,
        OpenKey=lambda *args: Key(), QueryValueEx=query, SetValueEx=set_value)
    monkeypatch.setitem(sys.modules, 'winreg', fake)
    return state


def test_registry_write_backs_up_exact_old_value_and_touches_only_the_model_list(tmp_path, monkeypatch):
    original, encoded = '[ { "name": "old" } ]', '[{"name":"new"}]'
    state = registry(monkeypatch, original)
    backup = write_policy(original, 1, encoded, tmp_path / 'backups')
    saved = json.loads(backup.read_text(encoding='utf-8'))
    assert saved['value'] == original and saved['type'] == 1
    assert state['writes'] == [('inferenceModels', 1, encoded)]


def test_concurrent_model_policy_change_is_not_overwritten(tmp_path, monkeypatch):
    state = registry(monkeypatch, 'new user policy')
    with pytest.raises(RuntimeError, match='changed during'):
        write_policy('old policy', 1, 'generated', tmp_path / 'backups')
    assert not state['writes'] and not (tmp_path / 'backups').exists()
