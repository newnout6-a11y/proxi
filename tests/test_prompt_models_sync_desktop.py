"""Claude Desktop's model list gets rows for the virtual models <model>@<profile> a prompts file publishes.

Only Claude routes can be Desktop rows, and a virtual row follows its base model: Anthropic's catalog lists it
when it lists the base, its picker levels are the base's, and it is never the family default. Everything runs
against a synthetic configLibrary, a synthetic Anthropic catalog and a fake prompts store.
"""
import json

import pytest

import models
from prompt_models_support import FakeSnapshot, store  # noqa: F401  (store is a fixture)
from scripts.sync_claude_desktop_catalog import build_catalog
from test_sync_claude_desktop_catalog import (USER_ROWS, anthropic_catalog, cli, entries, isolated_desktop,  # noqa: F401
                                              local_library, registry, relay_config)

PUBLISH = {'mine': {'publish_as_model': ['claude-opus-5-5', 'claude-opus-4-6', 'claude-haiku-4-5-20251001',
                                         'claude-opus-4-5-20251101', 'gpt-6-sol']}}
LISTED_COPIES = ['claude-opus-5-5@mine', 'claude-opus-4-6@mine', 'claude-haiku-4-5-20251001@mine']


def library(tmp_path, monkeypatch, rows=USER_ROWS):
    """A synthetic configLibrary with a model list, and no registry policy."""
    registry(monkeypatch)
    return local_library(tmp_path / 'configLibrary', rows)[0]


def sync(tmp_path, capsys, path, *args):
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--desktop-catalog',
                              anthropic_catalog(tmp_path), '--config-library', path.parent, '--write',
                              '--backup-dir', tmp_path / 'backups', *args)
    return code, report, error


def saved_rows(path):
    return {row['name']: row for row in json.loads(path.read_bytes())['inferenceModels']}


def test_virtual_rows_follow_the_user_rows_and_take_their_base_models_selectors(tmp_path, monkeypatch, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    path = library(tmp_path, monkeypatch)
    code, report, error = sync(tmp_path, capsys, path)
    assert code == 0 and report['written'] is True, error
    rows = json.loads(path.read_bytes())['inferenceModels']
    names = [row['name'] for row in rows]
    assert names[:3] == [row['name'] for row in USER_ROWS]
    # Anthropic's catalog does not list Opus 4.5 for Code, so its copy is not listed; GPT is never a Desktop row.
    virtual = [name for name in names if '@' in name]
    assert virtual == LISTED_COPIES and names[-3:] == virtual and len(set(names)) == len(names)
    by_name = {row['name']: row for row in rows}
    assert by_name['claude-opus-5-5@mine'] == {
        'name': 'claude-opus-5-5@mine', 'labelOverride': 'Claude Opus 5.5 (profile mine)', 'isFamilyDefault': False,
        'anthropicFamilyTier': 'opus', 'maxEffort': 'high'}
    assert by_name['claude-opus-4-6@mine']['maxEffort'] == 'max'  # Opus 4.6's own four-level picker, not Opus 5's
    assert by_name['claude-opus-4-6@mine']['isFamilyDefault'] is False
    haiku = by_name['claude-haiku-4-5-20251001@mine']
    assert 'maxEffort' not in haiku and haiku['anthropicFamilyTier'] == 'haiku' and haiku['isFamilyDefault'] is False
    for name in ('claude-opus-5-5', 'claude-haiku-4-5-20251001'):
        assert by_name[name]['isFamilyDefault'] is True  # the base models stay the family defaults
    assert report['virtual_models'] == 3 and report['prompts'] == str(store.path)
    assert report['unlisted_skipped'] == ['claude-opus-4-5-20251101', 'claude-sonnet-4-5-20250929',
                                          'claude-opus-4-5-20251101@mine']
    assert any('Virtual models are listed as <model>@<profile>' in warning and 'codex' in warning
               for warning in report['warnings'])


def test_include_unlisted_lists_the_copy_of_an_unlisted_base_without_a_selector(tmp_path, monkeypatch, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    path = library(tmp_path, monkeypatch)
    code, report, error = sync(tmp_path, capsys, path, '--include-unlisted')
    assert code == 0, error
    row = saved_rows(path)['claude-opus-4-5-20251101@mine']
    assert 'maxEffort' not in row and row['anthropicFamilyTier'] == 'opus' and row['isFamilyDefault'] is False
    assert report['unlisted_skipped'] == [] and report['virtual_models'] == 4


def test_without_a_snapshot_the_desktop_list_is_what_it_was(tmp_path, monkeypatch, capsys, store):
    path = library(tmp_path, monkeypatch)
    code, report, error = sync(tmp_path, capsys, path)
    assert code == 0 and report['models'] == 9, error
    assert 'virtual_models' not in report and 'prompts' not in report
    assert not [name for name in saved_rows(path) if '@' in name]
    assert not any('Virtual models' in warning for warning in report['warnings'])
    assert report['unlisted_skipped'] == ['claude-opus-4-5-20251101', 'claude-sonnet-4-5-20250929']


def test_only_gpt_published_means_no_desktop_rows_and_no_advisory(tmp_path, monkeypatch, capsys, store):
    store.current = FakeSnapshot({'mine': {'publish_as_model': ['gpt-*']}})
    path = library(tmp_path, monkeypatch)
    code, report, error = sync(tmp_path, capsys, path)
    assert code == 0 and report['models'] == 9 and report['virtual_models'] == 0, error
    assert not any('Virtual models' in warning for warning in report['warnings'])


def test_no_virtual_models_leaves_the_rows_out_and_a_second_run_changes_nothing(tmp_path, monkeypatch, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    path = library(tmp_path, monkeypatch)
    code, report, error = sync(tmp_path, capsys, path, '--no-virtual-models')
    assert code == 0 and 'virtual_models' not in report and store.constructed == [], error
    assert not [name for name in saved_rows(path) if '@' in name]
    code, report, error = sync(tmp_path, capsys, path)
    assert code == 0 and report['written'] is True and report['virtual_models'] == 3, error
    code, report, error = sync(tmp_path, capsys, path)
    assert code == 0 and report['written'] is False and report['changed'] is False and report['backup'] is None


def test_a_row_of_a_profile_that_no_longer_publishes_is_removed(tmp_path, monkeypatch, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    path = library(tmp_path, monkeypatch)
    assert sync(tmp_path, capsys, path)[0] == 0
    store.current = FakeSnapshot({'mine': {'publish_as_model': ['claude-opus-5-5']}})
    code, report, error = sync(tmp_path, capsys, path)
    assert code == 0 and report['written'] is True, error
    assert report['removed'] == ['claude-opus-4-6@mine', 'claude-haiku-4-5-20251001@mine']
    assert [name for name in saved_rows(path) if '@' in name] == ['claude-opus-5-5@mine']


def test_a_hand_edited_virtual_row_keeps_its_own_fields_like_any_row(tmp_path, monkeypatch, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    path = library(tmp_path, monkeypatch,
                   [*USER_ROWS, {'name': 'claude-opus-5-5@mine', 'labelOverride': 'My Opus', 'supports1m': True}])
    code, report, error = sync(tmp_path, capsys, path)
    assert code == 0, error
    assert saved_rows(path)['claude-opus-5-5@mine'] == {
        'name': 'claude-opus-5-5@mine', 'labelOverride': 'My Opus', 'supports1m': True, 'isFamilyDefault': False,
        'anthropicFamilyTier': 'opus', 'maxEffort': 'high'}
    assert 'claude-opus-5-5@mine' not in report['added']


@pytest.mark.parametrize('name', ['claude-opus-5-5@mine', 'claude-opus@mine', 'retool-relay/claude-opus-5-5@mine#high'])
def test_a_virtual_model_can_be_desktops_default(tmp_path, monkeypatch, capsys, store, name):
    store.current = FakeSnapshot(PUBLISH)
    path = library(tmp_path, monkeypatch)
    code, report, error = sync(tmp_path, capsys, path, '--default-model', name)
    assert code == 0, error
    assert report['default_model'] == 'claude-opus-5-5@mine'
    assert json.loads(path.read_bytes())['inferenceModels'][0]['name'] == 'claude-opus-5-5@mine'


def test_a_default_model_that_is_not_published_is_refused_and_nothing_is_written(tmp_path, monkeypatch, capsys, store):
    store.current = FakeSnapshot(PUBLISH)
    path = library(tmp_path, monkeypatch)
    code, report, error = sync(tmp_path, capsys, path, '--default-model', 'claude-opus-5-5@other')
    assert code == 1 and report is None and 'claude-opus-5-5@other is not in the generated list' in error
    assert not (tmp_path / 'backups').exists()


def test_a_prompts_file_that_did_not_load_stops_the_desktop_sync(tmp_path, monkeypatch, capsys, store):
    store.path = tmp_path / 'prompts.json'
    store.path.write_text('{', encoding='utf-8')
    path = library(tmp_path, monkeypatch)
    code, report, error = sync(tmp_path, capsys, path)
    assert code == 1 and report is None and 'could not be loaded' in error
    assert not (tmp_path / 'backups').exists()


def test_build_catalog_takes_the_virtual_rows_of_an_entry_list_that_carries_them():
    available = entries()
    virtual = models.virtual_entries(available, FakeSnapshot(PUBLISH))
    plain = build_catalog(available, USER_ROWS)
    rows = build_catalog(available + virtual, USER_ROWS)
    assert rows[:len(plain)] == plain  # the other rows, their order and their fields do not move
    assert [row['name'] for row in rows[len(plain):]] == [
        'claude-opus-5-5@mine', 'claude-opus-4-5-20251101@mine', 'claude-opus-4-6@mine',
        'claude-haiku-4-5-20251001@mine']
    with pytest.raises(ValueError, match='not in the generated list'):
        build_catalog(available + virtual, USER_ROWS, default_model='claude-opus-5-5@elsewhere')
