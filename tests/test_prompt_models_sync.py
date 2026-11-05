"""The OpenCode and Harness catalogs list the virtual models <model>@<profile> a prompts file publishes.

The scripts are run against synthetic files in tmp_path; the prompts store is a fake, so no real configuration
and no real prompts file is read.
"""
import copy
import json
import os
import sys
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

import models
import prompt_policy
from prompt_models_support import FakeSnapshot, store  # noqa: F401  (store is a fixture)
from scripts import probe_client_settings as probe
from scripts import sync_virtual_models
from scripts.probe_client_settings import catalog_policy, routable_entries
from scripts.sync_opencode_catalog import update_document
from test_client_policy_probe import fake_client
from test_sync_harness_catalog import PROFILE, client as harness_client
from test_sync_opencode_catalog import RELAY, client as opencode_client, installed, relay_config

HAIKU = 'claude-haiku-4-5-20251001'
PUBLISH = {'mine': {'publish_as_model': ['claude-opus-5-5', 'gpt-6-sol', 'gpt-4.1', HAIKU, 'zai-org/GLM-5.3']}}


def base_ids(tmp_path):
    return [entry['id'] for entry in routable_entries(relay_config(tmp_path, 'ids.json'))]


def written_models(tmp_path):
    saved = json.loads((tmp_path / 'opencode.jsonc').read_text(encoding='utf-8'))
    return saved, saved['provider']['retool-relay']['models']


def sync_files(tmp_path, capsys, *args, document=None):
    installed(tmp_path, document)
    return opencode_client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'), *args)


# -- the OpenCode catalog -------------------------------------------------------------------------

def test_virtual_models_join_the_opencode_catalog_with_the_efforts_and_limits_of_their_base(tmp_path, capsys, store):
    base = base_ids(tmp_path)
    store.current = FakeSnapshot(PUBLISH)
    code, summary, error = sync_files(tmp_path, capsys)
    assert code == 0 and summary['written'] is True, error
    assert summary['virtual_models'] == 5 and summary['models'] == len(base) + 5
    assert summary['prompts'] == str(store.path)
    saved, catalog = written_models(tmp_path)
    virtual = [name for name in catalog if name.endswith('@mine')]
    assert sorted(virtual) == sorted(name + '@mine' for name in PUBLISH['mine']['publish_as_model'])
    assert list(catalog)[:len(base)] == base and list(catalog)[len(base):] == virtual  # after the models they copy
    assert saved['provider']['retool-relay']['whitelist'] == list(catalog)
    for name in virtual:
        item, origin = catalog[name], catalog[name.partition('@')[0]]
        assert item['name'] == origin['name'] + ' (profile mine)'
        for key in ('limit', 'tool_call', 'attachment', 'reasoning', 'variants', 'options'):
            assert item[key] == origin[key], (name, key)
    assert list(catalog['claude-opus-5-5@mine']['variants']) == ['low', 'medium', 'high', 'max']
    assert catalog['claude-opus-5-5@mine']['options']['reasoningEffort'] == 'medium'
    assert catalog['gpt-4.1@mine']['variants'] == {} and catalog['gpt-4.1@mine']['options']['reasoningEffort'] == 'none'


def test_the_generated_opencode_catalog_passes_the_catalog_policy_with_the_virtual_models(tmp_path, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    assert sync_files(tmp_path, capsys)[0] == 0
    saved, catalog = written_models(tmp_path)
    entries = routable_entries(relay_config(tmp_path, 'ids.json'))
    entries += models.virtual_entries(entries, store.current)
    listing = ''.join('retool-relay/%s\n' % name for name in catalog)
    policy = catalog_policy(saved, listing, 'opencode', allowed_entries=entries, require_complete=True)
    assert policy['passed'] is True and policy['forbidden_models'] == [] and policy['missing_models'] == []
    # Without them in the allowed set the same catalog is rejected: the policy knows nothing about profiles.
    assert catalog_policy(saved, listing, 'opencode', allowed_entries=entries[:-5], require_complete=True)['passed'] is False


def test_without_a_snapshot_the_opencode_sync_is_what_it_was(tmp_path, capsys, store):
    code, summary, error = sync_files(tmp_path, capsys)
    assert code == 0 and 'virtual_models' not in summary and 'prompts' not in summary, error
    _, catalog = written_models(tmp_path)
    assert list(catalog) == base_ids(tmp_path) and store.constructed  # the store was asked, and had nothing
    first = (tmp_path / 'opencode.jsonc').read_bytes()
    store.current = FakeSnapshot({'mine': {'publish_as_model': False}})
    code, summary, _ = opencode_client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 0 and summary['written'] is False and summary['changed'] is False
    assert (tmp_path / 'opencode.jsonc').read_bytes() == first


def test_no_virtual_models_leaves_them_out_and_never_asks_for_the_prompts(tmp_path, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    code, summary, error = sync_files(tmp_path, capsys, '--no-virtual-models')
    assert code == 0 and 'virtual_models' not in summary, error
    assert list(written_models(tmp_path)[1]) == base_ids(tmp_path) and store.constructed == []


def test_an_explicit_prompts_file_is_handed_to_the_store_as_given(tmp_path, capsys, store):
    prompts = tmp_path / 'my-prompts.json'
    prompts.write_text('{}', encoding='utf-8')
    store.current = FakeSnapshot(PUBLISH)
    store.path = prompts
    code, summary, error = sync_files(tmp_path, capsys, '--prompts', str(prompts))
    assert code == 0 and summary['prompts'] == str(prompts), error
    (cfg, path), = store.constructed
    assert path == str(prompts) and cfg['_config_path'] == str(tmp_path / 'relay.json')
    assert cfg['strict_models'] is True  # the relay's own defaults, as the relay builds its store


def test_a_missing_explicit_prompts_file_stops_the_sync_before_anything_is_written(tmp_path, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    settings = installed(tmp_path)
    before = settings.read_bytes()
    code, summary, error = opencode_client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'),
                                           '--prompts', str(tmp_path / 'absent.json'))
    assert code == 1 and summary is None and 'prompts file not found' in error and 'absent.json' in error
    assert settings.read_bytes() == before and store.constructed == []


def test_a_prompts_file_that_did_not_load_stops_the_sync_instead_of_dropping_the_virtual_models(tmp_path, capsys, store):
    broken = tmp_path / 'prompts.json'
    broken.write_text('{"profiles": {"mine": ', encoding='utf-8')
    store.path = broken
    store.current = None
    store._errors = [{'message': 'profiles.mine.head: unknown variable {{nope}}', 'param': 'profiles.mine.head'}]
    settings = installed(tmp_path)
    before = settings.read_bytes()
    code, summary, error = opencode_client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 1 and summary is None
    assert str(broken) in error and 'could not be loaded' in error and 'unknown variable' in error
    assert '--no-virtual-models' in error and settings.read_bytes() == before
    code, summary, _ = opencode_client(tmp_path, capsys, '--no-virtual-models')
    assert code == 0 and 'virtual_models' not in summary  # the way out the error names


def test_a_prompts_file_the_store_refuses_is_reported_as_invalid(tmp_path, capsys, monkeypatch):
    def refuse(cfg, path=None):
        raise prompt_policy.PromptConfigError('profiles.mine.head', 'unknown variable')
    monkeypatch.setattr(prompt_policy, 'PromptStore', refuse)
    installed(tmp_path)
    code, summary, error = opencode_client(tmp_path, capsys)
    assert code == 1 and summary is None
    assert 'prompts file is invalid' in error and 'profiles.mine.head' in error


def test_a_removed_profile_takes_its_models_and_the_references_to_them_out_of_the_catalog(tmp_path, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    document = {'model': 'retool-relay/claude-opus-5-5@mine#high', 'small_model': 'retool-relay/' + HAIKU + '@mine',
                'agent': {'build': {'model': 'retool-relay/claude-opus@mine'},
                          'plan': {'model': 'retool-relay/gpt-6@mine#low', 'permission': {'edit': 'deny'}}},
                'command': {'review': {'template': 'Review', 'model': 'retool-relay/gpt-4.1@mine'}},
                'provider': {'retool-relay': {'options': {'baseURL': 'http://localhost:8787/v1'}, 'models': {}}}}
    assert sync_files(tmp_path, capsys, document=document)[0] == 0
    saved, catalog = written_models(tmp_path)
    assert saved['model'] == 'retool-relay/claude-opus-5-5@mine#high'  # the variant survives: the copy publishes it
    assert saved['small_model'] == 'retool-relay/' + HAIKU + '@mine'
    assert saved['agent']['build']['model'] == 'retool-relay/claude-opus-5-5@mine'  # alias resolved, profile kept
    assert saved['agent']['plan'] == {'model': 'retool-relay/gpt-6-sol@mine#low', 'permission': {'edit': 'deny'}}
    assert saved['command']['review']['model'] == 'retool-relay/gpt-4.1@mine'
    store.current = FakeSnapshot({'mine': {}})  # the profile stops publishing
    code, summary, _ = opencode_client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 0 and summary['written'] is True and 'virtual_models' not in summary
    saved, catalog = written_models(tmp_path)
    assert not [name for name in catalog if '@' in name]
    assert saved['model'] == 'retool-relay/claude-opus-5-5'
    assert saved['small_model'] == 'retool-relay/' + HAIKU
    assert saved['agent']['build']['model'] == 'retool-relay/claude-opus-5-5'
    assert saved['agent']['plan'] == {'model': 'retool-relay/claude-opus-5-5', 'permission': {'edit': 'deny'}}
    assert saved['command']['review']['model'] == 'retool-relay/claude-opus-5-5'


def test_update_document_treats_a_virtual_entry_like_any_other_published_model():
    snapshot = FakeSnapshot({'mine': {'publish_as_model': ['claude-opus-5-5']}})
    entries = [models.resolve(name)[0] for name in ('claude-opus-5-5', HAIKU)]
    entries += models.virtual_entries(entries, snapshot)
    before = {'model': 'retool-relay/claude-opus-5-5@mine#max', 'provider': {'retool-relay': {'models': {
        'claude-opus-5-5@mine': {'custom': True, 'cost': {'input': 0, 'output': 0}}}}}}
    original = copy.deepcopy(before)
    result = update_document(before, entries)
    assert before == original
    item = result['provider']['retool-relay']['models']['claude-opus-5-5@mine']
    assert item['custom'] is True and 'cost' not in item and item['name'] == 'Claude Opus 5.5 (profile mine)'
    assert result['model'] == 'retool-relay/claude-opus-5-5@mine#max'
    assert list(result['provider']['retool-relay']['models']) == ['claude-opus-5-5', HAIKU, 'claude-opus-5-5@mine']


# -- the Harness catalog --------------------------------------------------------------------------

def harness_models(profile):
    document = YAML().load(profile.read_text(encoding='utf-8'))
    providers = next(plugin for plugin in document if plugin.get('id') == 'llm-pi-ai')['config']['providers']
    return {name: {item['id']: item for item in providers[name]['models']} for name in ('retool', 'retool-text')}


def test_virtual_models_join_the_harness_groups_of_their_base_models(tmp_path, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    code, summary, error, profile = harness_client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 0 and summary['written'] is True, error
    assert summary['virtual_models'] == 5 and summary['prompts'] == str(store.path)
    groups = harness_models(profile)
    everything = {**groups['retool'], **groups['retool-text']}
    assert {name for name in everything if '@' in name} == {name + '@mine' for name in PUBLISH['mine']['publish_as_model']}
    for name in (n for n in everything if n.endswith('@mine')):
        origin = name.partition('@')[0]
        same_group = groups['retool'] if origin in groups['retool'] else groups['retool-text']
        assert name in same_group, name  # selectors decide the group, and a copy has its base's selectors
        item = everything[name]
        assert item['name'] == everything[origin]['name'] + ' (profile mine)'
        for key in ('contextWindow', 'maxTokens', 'input', 'reasoningEfforts'):
            assert item[key] == everything[origin][key], (name, key)
    assert list(groups['retool']['claude-opus-5-5@mine']['reasoningEfforts']) == ['low', 'medium', 'high', 'max']
    assert groups['retool-text']['gpt-4.1@mine']['reasoningEfforts'] is False


def test_the_harness_sync_without_virtual_models_is_what_it_was(tmp_path, capsys, store):
    code, summary, error, profile = harness_client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 0 and 'virtual_models' not in summary, error
    groups = harness_models(profile)
    assert not [name for group in groups.values() for name in group if '@' in name]
    store.current = FakeSnapshot(PUBLISH)
    asked = len(store.constructed)
    code, summary, _, _ = harness_client(tmp_path, capsys, '--no-virtual-models')
    assert code == 0 and summary['changed'] is False and len(store.constructed) == asked
    code, summary, _, _ = harness_client(tmp_path, capsys)
    assert code == 0 and summary['changed'] is True and summary['virtual_models'] == 5


def test_a_prompts_file_that_did_not_load_stops_the_harness_sync_too(tmp_path, capsys, store):
    store.path = tmp_path / 'prompts.json'
    store.path.write_text('not json', encoding='utf-8')
    code, summary, error, profile = harness_client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 1 and summary is None and 'could not be loaded' in error
    assert profile.read_text(encoding='utf-8') == PROFILE


# -- the offline probe expects the same catalog ---------------------------------------------------

def probe_expectation(tmp_path, monkeypatch, capsys, *args):
    fake_client(monkeypatch)
    seen = {}
    monkeypatch.setattr(probe, 'catalog_policy', lambda config, listing, mode, **kwargs: seen.update(kwargs) or {'passed': True})
    relay = tmp_path / 'relay.json'
    relay.write_text(json.dumps(RELAY), encoding='utf-8')
    probe.main(['opencode', '--executable', sys.executable, '--relay-config', str(relay), *args])
    capsys.readouterr()
    return {entry['id'] for entry in seen['allowed_entries']}


def test_the_probe_expects_the_virtual_models_the_relay_lists(tmp_path, monkeypatch, capsys, store):
    store.current = FakeSnapshot({'mine': {'publish_as_model': ['gpt-6-sol']}})
    expected = probe_expectation(tmp_path, monkeypatch, capsys)
    assert 'gpt-6-sol' in expected and 'gpt-6-sol@mine' in expected
    assert 'gpt-6-sol@mine' not in probe_expectation(tmp_path, monkeypatch, capsys, '--no-virtual-models')


def test_the_probe_without_a_snapshot_expects_the_catalog_models_only(tmp_path, monkeypatch, capsys, store):
    expected = probe_expectation(tmp_path, monkeypatch, capsys)
    assert expected == {entry['id'] for entry in routable_entries(relay_config(tmp_path, 'ids.json'))}


# -- the shared loader ----------------------------------------------------------------------------

def test_the_loader_builds_the_store_the_way_the_relay_does(tmp_path, store, monkeypatch):
    relay = relay_config(tmp_path)
    store.current = FakeSnapshot(PUBLISH)
    snapshot, path = sync_virtual_models.load_snapshot(relay)
    assert snapshot is store.current and path == str(store.path)
    (cfg, explicit), = store.constructed
    assert explicit is None and cfg['_config_path'] == str(relay) and cfg['base'] == RELAY['base']
    # config.load stores an absolute path, and a relative prompts_file is read from beside it.
    monkeypatch.chdir(tmp_path)
    sync_virtual_models.load_snapshot('relay.json')
    assert store.constructed[-1][0]['_config_path'] == os.path.abspath('relay.json')


def test_no_file_and_no_snapshot_is_not_an_error(tmp_path, store):
    assert sync_virtual_models.load_snapshot(relay_config(tmp_path)) == (None, str(store.path))
    entries, summary = sync_virtual_models.with_virtual_models(
        [models.resolve('gpt-6-sol')[0]], SimpleNamespace(relay_config=relay_config(tmp_path)))
    assert [entry['id'] for entry in entries] == ['gpt-6-sol'] and summary == {}


@pytest.mark.parametrize('content,fragment', [(None, 'cannot read relay configuration'),
                                              ('{not json', 'cannot read relay configuration'),
                                              ('[1]', 'must contain a JSON object')])
def test_an_unreadable_relay_configuration_is_a_plain_error(tmp_path, store, content, fragment):
    path = tmp_path / 'relay.json'
    if content is not None:
        path.write_text(content, encoding='utf-8')
    with pytest.raises(ValueError, match=fragment):
        sync_virtual_models.load_snapshot(path)


def test_prompts_and_no_virtual_models_exclude_each_other():
    import argparse
    parser = argparse.ArgumentParser()
    sync_virtual_models.add_arguments(parser)
    assert parser.parse_args(['--prompts', 'x.json']).prompts == 'x.json'
    assert parser.parse_args(['--no-virtual-models']).no_virtual_models is True
    with pytest.raises(SystemExit):
        parser.parse_args(['--prompts', 'x.json', '--no-virtual-models'])


# -- the real prompt store (skipped while PromptStore does not load files in this tree) ------------

def test_a_real_prompts_file_is_loaded_by_the_real_store_and_published_to_the_catalogs(tmp_path, capsys):
    prompts = tmp_path / 'real-prompts.json'
    prompts.write_text(json.dumps({
        'version': 1, 'profiles': {'mine': {'head': '{{client_system}}',
                                            'publish_as_model': ['gpt-6-sol', 'claude-opus-*']}}}), encoding='utf-8')
    try:
        snapshot, path = sync_virtual_models.load_snapshot(relay_config(tmp_path), prompts)
    except ValueError as error:
        if 'could not be loaded' not in str(error):
            raise
        pytest.skip('prompt_policy.PromptStore does not load a prompts file in this tree')
    assert snapshot is not None and path == str(prompts)
    assert 'mine' in snapshot.profile_names()
    installed(tmp_path)
    code, summary, error = opencode_client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'),
                                           '--prompts', str(prompts))
    assert code == 0 and summary['written'] is True and summary['prompts'] == str(prompts), error
    catalog = written_models(tmp_path)[1]
    assert 'gpt-6-sol@mine' in catalog and 'claude-opus-5-5@mine' in catalog and 'gpt-5.5@mine' not in catalog
