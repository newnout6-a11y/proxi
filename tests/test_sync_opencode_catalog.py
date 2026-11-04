"""Client catalog updates preserve unrelated configuration and are recoverable."""
import copy
import json

import pytest

import models
from scripts.sync_opencode_catalog import update_document, write_backed_up


def source():
    return {'model': 'retool-relay/gpt-6', 'small_model': 'other/model',
            'custom': {'model': 'retool-relay/gpt-6'},
            'agent': {'worker': {'model': 'retool-relay/gpt-5.3-codex', 'permission': {'edit': 'deny'}},
                      'other': {'model': 'other/private-model'}},
            'provider': {'other': {'options': {'apiKey': 'private-value'}},
                         'retool-relay': {'options': {'apiKey': 'private-relay', 'baseURL': 'http://localhost:8787/v1'},
                                          'blacklist': ['gpt-6-sol'], 'whitelist': ['gpt-6'],
                                          'models': {'gpt-6': {}, 'gpt-5.3-codex': {},
                                            'gpt-6-sol': {'options': {'reasoningEffort': 'high', 'custom': True},
                                                          'cost': {'input': 0, 'output': 0}}}}}}


def test_sync_replaces_only_relay_models_and_stale_relay_model_references():
    before = source()
    original = copy.deepcopy(before)
    entries = [models.resolve(name)[0] for name in ('gpt-6-sol', 'zai-org/GLM-5.3-Flash', 'gpt-4.1')]
    result = update_document(before, entries, default_model='gpt-6-sol')
    assert before == original
    assert result['provider']['other'] == original['provider']['other']
    assert result['provider']['retool-relay']['options'] == original['provider']['retool-relay']['options']
    assert result['small_model'] == 'other/model'
    assert result['custom'] == original['custom']
    assert result['agent']['other'] == original['agent']['other']
    assert result['agent']['worker'] == {'model': 'retool-relay/gpt-6-sol', 'permission': {'edit': 'deny'}}
    assert result['model'] == 'retool-relay/gpt-6-sol'
    catalog = result['provider']['retool-relay']['models']
    assert set(catalog) == {entry['id'] for entry in entries}
    assert result['provider']['retool-relay']['whitelist'] == list(catalog)
    assert 'blacklist' not in result['provider']['retool-relay']
    assert catalog['gpt-6-sol']['options'] == {'reasoningEffort': 'high', 'custom': True}
    assert 'cost' not in catalog['gpt-6-sol']
    assert catalog['zai-org/GLM-5.3-Flash']['reasoning'] is True
    assert set(catalog['zai-org/GLM-5.3-Flash']['variants']) == {'low', 'medium', 'high', 'max'}
    assert catalog['gpt-4.1']['variants'] == {} and catalog['gpt-4.1']['options']['reasoningEffort'] == 'none'


def test_empty_catalog_and_unknown_settings_schema_do_not_replace_configuration():
    with pytest.raises(ValueError):
        update_document(source(), [])
    with pytest.raises(ValueError):
        update_document({'providers': {}}, [models.resolve('gpt-6-sol')[0]])


def test_atomic_write_preserves_exact_original_backup_and_leaves_no_temporary_files(tmp_path):
    path = tmp_path / 'config.json'
    original = b'{"private":"original"}\r\n'
    path.write_bytes(original)
    updated = b'{"private":"updated"}\n'
    backup = write_backed_up(path, original, updated, tmp_path / 'backups')
    assert path.read_bytes() == updated and backup.read_bytes() == original
    assert not list(tmp_path.glob('.proxi-catalog-*.tmp'))


def test_concurrent_edit_is_preserved_instead_of_being_overwritten(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('new user edit')
    with pytest.raises(RuntimeError):
        write_backed_up(path, b'old', b'generated', tmp_path / 'backups')
    assert path.read_text() == 'new user edit'
    assert not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('options,expected', [({'reasoning_effort': 'high', 'custom': True}, 'high'),
    ({'reasoningEffort': 'xhigh', 'body': {'reasoningEffort': 'xhigh', 'custom': True}}, 'medium'),
    ({'providerOptions': {'openai': {'reasoningEffort': 'high'}}, 'custom': True}, 'high'),
    ({'extra_body': {'reasoning': {'effort': 'high'}}, 'custom': True}, 'high')])
def test_sync_normalizes_aliases_and_stale_wrappers_before_validating(options, expected):
    from scripts.probe_client_settings import catalog_policy
    before = source()
    before['provider']['retool-relay']['models']['gpt-6-sol']['options'] = options
    entries = [models.resolve('gpt-6-sol')[0]]
    result = update_document(before, entries)
    actual = result['provider']['retool-relay']['models']['gpt-6-sol']['options']
    assert actual['reasoningEffort'] == expected
    assert 'reasoning_effort' not in actual
    assert catalog_policy(result, 'retool-relay/gpt-6-sol\n', 'opencode', allowed_entries=entries)['passed']
    if 'custom' in options:
        assert actual['custom'] is True
    else:
        assert actual['body']['custom'] is True


def test_edit_in_final_replacement_window_is_recoverable_and_not_reported_as_success(tmp_path, monkeypatch):
    from scripts import sync_opencode_catalog as sync
    path = tmp_path / 'config.json'
    original, generated, concurrent = b'original', b'generated', b'concurrent-user-edit'
    path.write_bytes(original)
    native = sync._replace_with_backup
    count = 0
    def race(target, replacement, backup):
        nonlocal count
        count += 1
        if count == 1:
            target.write_bytes(concurrent)
        return native(target, replacement, backup)
    monkeypatch.setattr(sync, '_replace_with_backup', race)
    with pytest.raises(RuntimeError, match='changed during replacement'):
        write_backed_up(path, original, generated, tmp_path / 'backups')
    assert path.read_bytes() == concurrent
    assert any(p.read_bytes() == concurrent for p in (tmp_path / 'backups').iterdir())
    assert not list(tmp_path.glob('.proxi-catalog-*.tmp'))
