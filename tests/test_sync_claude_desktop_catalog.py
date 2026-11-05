"""Desktop model-list sync follows the source Desktop reads, keeps the user's list and stays honest."""
import base64
import copy
import itertools
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import models
from scripts import sync_claude_desktop_catalog as sync
from scripts.sync_claude_desktop_catalog import (build_catalog, compatible_effort_cap, effective_source, encode_local,
                                                 load_desktop_catalog, main, write_policy)

SZ, EXPAND_SZ, BINARY, DWORD, MULTI_SZ = 1, 2, 3, 4, 7
KEY_SET_VALUE = 2
FIVE = ('low', 'medium', 'high', 'xhigh', 'max')
FOUR = ('low', 'medium', 'high', 'max')
LIBRARY_ID = '00000000-0000-4000-8000-0000000000a1'
USER_ROWS = [{'name': 'claude-opus-5-5', 'labelOverride': 'Opus 5.5 (relay)'},
             {'name': 'claude-sonnet-5-5', 'labelOverride': 'Sonnet 5.5 (relay)'},
             {'name': 'claude-haiku-4-5-20251001', 'labelOverride': 'Haiku 4.5 (relay)'}]
# Anthropic's catalog for Code and Cowork, as cached by Desktop 4.4212: no Sonnet 4.5, and
# Opus 4.5 only as a disabled chat model.
LISTED = [('claude-opus-5-5', FIVE), ('claude-sonnet-5-5', FIVE), ('claude-haiku-5-5', FIVE),
          ('claude-haiku-4-5-20251001', None), ('claude-sonnet-5', FIVE), ('claude-opus-5', FIVE),
          ('claude-opus-4-8', FIVE), ('claude-opus-4-7', FIVE), ('claude-opus-4-6', FOUR),
          ('claude-sonnet-4-6', FOUR)]
UNLISTED = {'claude-opus-4-5-20251101', 'claude-sonnet-4-5-20250929'}
NO_EFFORT = UNLISTED | {'claude-haiku-4-5-20251001'}
FOUR_LEVEL = {'claude-opus-4-6', 'claude-sonnet-4-6'}
previews = itertools.count()


class NoRegistry:
    def __getattr__(self, name):
        raise AssertionError('the test would use the real registry: winreg.' + name)


@pytest.fixture(autouse=True)
def isolated_desktop(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'winreg', NoRegistry())
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'LocalAppData'))


def registry(monkeypatch, hklm=None, hkcu=None):
    """In-memory winreg. A hive is {name: (data, type)}, or None when its Claude policy key is absent."""
    state = SimpleNamespace(hives={'HKLM': copy.deepcopy(hklm), 'HKCU': copy.deepcopy(hkcu)}, writes=[], opened=[])

    class Key:
        def __init__(self, hive):
            self.hive = hive

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def open_key(root, path, reserved=0, access=0):
        assert path == r'Software\Policies\Claude'
        assert root == 'HKCU' or not access & KEY_SET_VALUE, 'HKLM must never be opened for writing'
        state.opened.append((root, access))
        if state.hives[root] is None:
            raise FileNotFoundError(2, 'The system cannot find the file specified')
        return Key(root)

    def query_value(key, name):
        for stored, value in state.hives[key.hive].items():
            if stored.casefold() == name.casefold():
                return value
        raise FileNotFoundError(2, 'The system cannot find the file specified')

    def set_value(key, name, reserved, kind, value):
        assert key.hive == 'HKCU', 'HKLM must never be written'
        state.writes.append((name, kind, value))
        values = state.hives[key.hive]
        values[next((stored for stored in values if stored.casefold() == name.casefold()), name)] = (value, kind)

    def forbidden(*args, **kwargs):
        raise AssertionError('the script must not create or delete registry keys or values')

    monkeypatch.setitem(sys.modules, 'winreg', SimpleNamespace(
        HKEY_LOCAL_MACHINE='HKLM', HKEY_CURRENT_USER='HKCU', REG_SZ=SZ, REG_EXPAND_SZ=EXPAND_SZ, REG_BINARY=BINARY,
        REG_DWORD=DWORD, KEY_READ=0x20019, KEY_QUERY_VALUE=1, KEY_SET_VALUE=KEY_SET_VALUE, KEY_WOW64_64KEY=0x100,
        OpenKey=open_key, QueryInfoKey=lambda key: (0, len(state.hives[key.hive]), 0),
        EnumValue=lambda key, index: (lambda item: (item[0], *item[1]))(list(state.hives[key.hive].items())[index]),
        QueryValueEx=query_value, SetValueEx=set_value, CreateKey=forbidden, CreateKeyEx=forbidden,
        DeleteValue=forbidden, DeleteKey=forbidden))
    return state


def entries():
    cfg = {'provider_resource_name': '__managed__', 'openai_provider_resource_name': '__managed__',
           'retool_fast_provider_resource_name': '__managed__'}
    return [entry for entry in models.catalog() if models.catalog_routable(entry, cfg)]


def claude_ids():
    return {entry['id'] for entry in entries() if entry['provider'] == 'anthropic'}


def local_library(directory, rows=USER_ROWS, *, newline='\n', trailing=False, bom=False):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / '_meta.json').write_text(json.dumps(
        {'entries': [{'id': LIBRARY_ID, 'name': 'Relay'}], 'appliedId': LIBRARY_ID}), encoding='utf-8')
    document = {'inferenceGatewayBaseUrl': 'http://127.0.0.1:8787', 'inferenceGatewayApiKey': 'synthetic-test-value',
                'modelDiscoveryEnabled': False, 'modelPrefer1mContext': True, 'inferenceModels': rows,
                'defaultModelEffort': 'medium', 'inferenceProvider': 'gateway', 'coworkEgressAllowedHosts': '*'}
    text = json.dumps(document, indent=2, ensure_ascii=False).replace('\n', newline) + (newline if trailing else '')
    data = (b'\xef\xbb\xbf' if bom else b'') + text.encode('utf-8')
    path = directory / (LIBRARY_ID + '.json')
    path.write_bytes(data)
    return path, data


def relay_config(tmp_path, **extra):
    path = tmp_path / 'relay.json'
    saved = {'base': 'https://example.invalid', 'page_uuid': 'page-1', 'ua': 'test-agent', 'cookie': 'a=b',
             'xsrf': 'x', 'provider_resource_name': '__managed__', 'openai_provider_resource_name': '__managed__',
             'retool_fast_provider_resource_name': '__managed__'}
    saved.update(extra)
    path.write_text(json.dumps(saved), encoding='utf-8')
    return path


def anthropic_catalog(directory, surfaces=None, *, envelope=True):
    """A catalog.json (or Desktop's documentBytes cache envelope) with {surface: [(id, levels, disabled)]}."""
    surfaces = surfaces or {'ccd': LISTED, 'cc': LISTED, 'cowork': LISTED,
                            'chat': [('claude-opus-4-5-20251101', ('low', 'medium', 'high'), True)]}

    def model(spec):
        mid, levels, disabled = (tuple(spec) + (False,))[:3]
        thinking = ({'type': 'effort', 'effort_options': [{'id': level, 'name': level.title()} for level in levels]}
                    if levels else {'type': 'none'})
        return dict(id=mid, thinking=thinking, **({'disabled': True} if disabled else {}))

    document = {'schema_version': 1, 'surfaces': {surface: {'model_selector_config': [
        {'id': surface, 'models': [model(spec) for spec in specs]}]} for surface, specs in surfaces.items()}}
    payload = ({'format': 1, 'documentBytes': base64.b64encode(json.dumps(document).encode()).decode()}
               if envelope else document)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'published.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def cli(capsys, *argv):
    code = main([str(item) for item in argv])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    return code, (json.loads(lines[-1]) if lines else None), captured.err


def preview(capsys, tmp_path, *argv, relay=None):
    """Run a read-only preview and return (report, generated rows)."""
    output = tmp_path / ('preview-%d.json' % next(previews))
    code, report, error = cli(capsys, '--relay-config', relay or relay_config(tmp_path), '--output', output, *argv)
    assert code == 0, error
    return report, json.loads(output.read_text(encoding='utf-8'))


def policy_values(rows, **extra):
    return {'inferenceProvider': ('gateway', SZ), 'inferenceModels': (json.dumps(rows), SZ), **extra}


def test_the_configuration_id_is_visibly_synthetic():
    # An id that opens like a real installation's would make a copied fixture look like private data.
    assert LIBRARY_ID.startswith('00000000-')


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
    assert {item['name']: item.get('maxEffort') for item in result} == {
        name: ('max' if name in FOUR_LEVEL else None if name in NO_EFFORT else 'high') for name in claude_ids()}
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


def test_fable_gets_the_family_tier_desktop_documents():
    # Desktop's configuration reference lists haiku|sonnet|opus|fable|mythos; a stale tier is replaced.
    fable = models.resolve('claude-fable-5')[0]
    result = build_catalog([fable], [{'name': fable['id'], 'anthropicFamilyTier': 'opus'}])
    assert result[0]['anthropicFamilyTier'] == 'fable' and result[0]['isFamilyDefault'] is True


@pytest.mark.parametrize('previous', [{}, [None], 'invalid'])
def test_invalid_policy_is_refused(previous):
    with pytest.raises(ValueError):
        build_catalog(entries(), previous)


def test_empty_desktop_route_set_is_never_written():
    with pytest.raises(ValueError):
        build_catalog([models.resolve('gpt-6-sol')[0]], [])


def test_duplicated_routes_are_never_written():
    opus = models.resolve('claude-opus-5-5')[0]
    with pytest.raises(ValueError, match='duplicated'):
        build_catalog([opus, opus], [])


def test_write_requires_backup_before_any_registry_read():
    with pytest.raises(SystemExit) as error:
        main(['--write'])
    assert error.value.code == 2


# Bug 11: the source Desktop actually reads

@pytest.mark.parametrize('machine,user,expected', [
    (None, None, 'local'),
    (None, {}, 'local'),
    (None, policy_values([]), 'hkcu'),
    (None, {'disableAutoUpdates': ('true', SZ)}, 'local'),
    (None, {'DisableAutoUpdates': ('true', SZ), 'releaseChannel': ('stable', SZ)}, 'local'),
    (None, {'disableAutoUpdates': ('true', SZ), 'inferenceModels': ('[]', SZ)}, 'hkcu'),
    (policy_values([]), policy_values([]), 'hklm'),
    (policy_values([]), None, 'hklm'),
    ({'releaseChannel': ('stable', SZ)}, policy_values([]), 'local'),
    ({'': ('anything', SZ)}, policy_values([]), 'local'),
    ({'inferenceProvider': ('', SZ)}, policy_values([]), 'hklm'),
    ({'coworkTabEnabled': (1, DWORD)}, policy_values([]), 'hklm'),
    ({'inferenceModels': ('%X%', EXPAND_SZ)}, policy_values([]), 'hklm'),
    ({'inferenceProvider': (b'gateway', BINARY), 'x': (['a'], MULTI_SZ)}, policy_values([]), 'hkcu'),
    ({}, policy_values([]), 'hkcu')])
def test_effective_source_follows_desktop_precedence(machine, user, expected):
    assert effective_source(machine, user) == expected


def test_local_configuration_is_replaced_atomically_when_no_policy_exists(tmp_path, monkeypatch, capsys):
    state = registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    before = json.loads(data)
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--desktop-catalog',
                              anthropic_catalog(tmp_path), '--config-library', path.parent, '--write',
                              '--backup-dir', tmp_path / 'backups')
    assert code == 0, error
    assert report['source'] == 'local' and report['target'] == str(path)
    saved = json.loads(path.read_bytes())
    assert {key: value for key, value in saved.items() if key != 'inferenceModels'} == {
        key: value for key, value in before.items() if key != 'inferenceModels'}
    rows = saved['inferenceModels']
    assert isinstance(rows, list) and rows[:3] == [
        dict(USER_ROWS[0], isFamilyDefault=True, anthropicFamilyTier='opus', maxEffort='high'),
        dict(USER_ROWS[1], isFamilyDefault=True, anthropicFamilyTier='sonnet', maxEffort='high'),
        dict(USER_ROWS[2], isFamilyDefault=True, anthropicFamilyTier='haiku')]
    assert len(rows) == 9 == report['models']
    assert Path(report['backup']).read_bytes() == data
    assert path.read_bytes().endswith(b'}') and not list(path.parent.glob('.proxi-catalog-*'))
    assert state.writes == [] and all(hive == 'HKLM' and access & KEY_SET_VALUE == 0
                                      for hive, access in state.opened if hive == 'HKLM')


def test_a_string_encoded_local_list_stays_a_string(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary', json.dumps(USER_ROWS))
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--desktop-catalog',
                              anthropic_catalog(tmp_path), '--config-library', path.parent, '--write',
                              '--backup-dir', tmp_path / 'backups')
    assert code == 0, error
    encoded = json.loads(path.read_bytes())['inferenceModels']
    assert isinstance(encoded, str) and len(json.loads(encoded)) == 9 and ' ' not in encoded.split('"name"')[0]


def test_user_policy_in_effect_is_the_only_target_and_the_local_file_is_left_alone(tmp_path, monkeypatch, capsys):
    state = registry(monkeypatch, hkcu=policy_values(USER_ROWS, inferenceGatewayApiKey=('synthetic', SZ)))
    path, data = local_library(tmp_path / 'configLibrary')
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--desktop-catalog',
                              anthropic_catalog(tmp_path), '--config-library', path.parent, '--write',
                              '--backup-dir', tmp_path / 'backups')
    assert code == 0, error
    assert report['source'] == 'hkcu' and report['target'].startswith('HKCU\\')
    assert len(state.writes) == 1 and state.writes[0][:2] == ('inferenceModels', SZ)
    assert json.loads(state.writes[0][2])[0]['labelOverride'] == 'Opus 5.5 (relay)'
    assert state.hives['HKCU']['inferenceGatewayApiKey'] == ('synthetic', SZ)
    assert path.read_bytes() == data
    saved = json.loads(Path(report['backup']).read_text(encoding='utf-8'))
    assert saved['hive'] == 'HKCU' and saved['value'] == json.dumps(USER_ROWS)


def test_machine_policy_hides_the_user_policy_and_is_never_written(tmp_path, monkeypatch, capsys):
    hkcu = policy_values(USER_ROWS)
    state = registry(monkeypatch, hklm=policy_values(USER_ROWS), hkcu=hkcu)
    path, data = local_library(tmp_path / 'configLibrary')
    arguments = ['--relay-config', relay_config(tmp_path), '--desktop-catalog', anthropic_catalog(tmp_path),
                 '--config-library', path.parent]
    code, report, error = cli(capsys, *arguments, '--write', '--backup-dir', tmp_path / 'backups')
    assert code == 1 and 'HKLM' in error and 'never writes to HKLM' in error and 'Traceback' not in error
    assert state.writes == [] and state.hives['HKCU'] == hkcu and path.read_bytes() == data
    assert not (tmp_path / 'backups').exists()
    code, report, error = cli(capsys, *arguments)
    assert code == 0 and report['source'] == 'hklm' and not report['written']
    assert any('read-only' in warning for warning in report['warnings'])
    assert state.writes == []


def test_user_policy_is_not_written_once_machine_policy_appears(tmp_path, monkeypatch):
    original = json.dumps(USER_ROWS)
    state = registry(monkeypatch, hklm={'inferenceProvider': ('gateway', SZ)}, hkcu=policy_values(USER_ROWS))
    with pytest.raises(RuntimeError, match='Machine policy appeared'):
        write_policy(original, SZ, '[]', tmp_path / 'backups')
    assert state.writes == [] and not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('machine,user', [
    (None, {'disableAutoUpdates': ('true', SZ)}),
    ({'releaseChannel': ('stable', SZ)}, policy_values(USER_ROWS)),
    (None, None)])
def test_policies_without_configuration_keys_leave_the_local_file_as_the_target(tmp_path, monkeypatch, capsys,
                                                                                 machine, user):
    state = registry(monkeypatch, hklm=machine, hkcu=user)
    path, data = local_library(tmp_path / 'configLibrary')
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--desktop-catalog',
                              anthropic_catalog(tmp_path), '--config-library', path.parent, '--write',
                              '--backup-dir', tmp_path / 'backups')
    assert code == 0, error
    assert report['source'] == 'local' and state.writes == [] and path.read_bytes() != data


@pytest.mark.parametrize('source,machine,user,message', [
    ('hkcu', policy_values(USER_ROWS), policy_values(USER_ROWS), 'HKLM'),
    ('hkcu', None, None, 'never creates a policy'),
    ('hkcu', None, {'disableAutoUpdates': ('true', SZ)}, 'app-behavior'),
    ('local', None, policy_values(USER_ROWS), 'ignores the local configLibrary'),
    ('local', policy_values(USER_ROWS), None, 'HKLM'),
    ('hkcu', {'releaseChannel': ('stable', SZ)}, policy_values(USER_ROWS), 'ignores HKCU')])
def test_source_flag_cannot_override_what_desktop_uses(tmp_path, monkeypatch, capsys, source, machine, user, message):
    state = registry(monkeypatch, hklm=machine, hkcu=user)
    path, data = local_library(tmp_path / 'configLibrary')
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--config-library', path.parent,
                              '--source', source, '--write', '--backup-dir', tmp_path / 'backups')
    assert code == 1 and message in error and 'Traceback' not in error
    assert state.writes == [] and path.read_bytes() == data and not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('source,hkcu,expected', [('hkcu', policy_values(USER_ROWS), 'hkcu'), ('local', None, 'local'),
                                                  ('auto', policy_values(USER_ROWS), 'hkcu')])
def test_source_flag_accepts_the_matching_source(tmp_path, monkeypatch, capsys, source, hkcu, expected):
    registry(monkeypatch, hkcu=hkcu)
    path, data = local_library(tmp_path / 'configLibrary')
    report, rows = preview(capsys, tmp_path, '--desktop-catalog', anthropic_catalog(tmp_path),
                           '--config-library', path.parent, '--source', source)
    assert report['source'] == expected and rows[0]['name'] == 'claude-opus-5-5'


def test_policy_that_needs_no_list_is_never_created_from_nothing(tmp_path, monkeypatch, capsys):
    state = registry(monkeypatch, hkcu={'inferenceProvider': ('gateway', SZ)})
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--write',
                              '--backup-dir', tmp_path / 'backups')
    assert code == 1 and 'has no inferenceModels value' in error and 'discovery' in error
    assert state.writes == [] and not (tmp_path / 'backups').exists()


def test_model_list_stored_as_an_unreadable_registry_type_is_refused(tmp_path, monkeypatch, capsys):
    state = registry(monkeypatch, hkcu={'inferenceModels': ('[]', EXPAND_SZ)})
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--write',
                              '--backup-dir', tmp_path / 'backups')
    assert code == 1 and 'REG_SZ' in error and state.writes == []


def break_meta(path):
    (path.parent / '_meta.json').write_text('{broken', encoding='utf-8')


def drop_meta(path):
    (path.parent / '_meta.json').unlink()


def unknown_applied(path):
    (path.parent / '_meta.json').write_text(json.dumps({'appliedId': '..\\..\\outside'}), encoding='utf-8')


def missing_applied(path):
    path.unlink()


def broken_applied(path):
    path.write_text('[', encoding='utf-8')


def not_an_object(path):
    path.write_text('[]', encoding='utf-8')


def without_list(path):
    document = json.loads(path.read_bytes())
    document.pop('inferenceModels')
    path.write_text(json.dumps(document), encoding='utf-8')


def broken_list(path):
    document = json.loads(path.read_bytes())
    document['inferenceModels'] = '[not json'
    path.write_text(json.dumps(document), encoding='utf-8')


@pytest.mark.parametrize('damage,message', [
    (drop_meta, '_meta.json does not exist'), (break_meta, 'not valid JSON'),
    (unknown_applied, 'appliedId'), (missing_applied, 'does not exist'), (broken_applied, 'not valid JSON'),
    (not_an_object, 'JSON object'), (without_list, 'has no inferenceModels list'), (broken_list, 'not valid JSON')])
def test_broken_local_configuration_gives_a_clear_error_instead_of_a_traceback(tmp_path, monkeypatch, capsys,
                                                                               damage, message):
    state = registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    damage(path)
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--config-library', path.parent,
                              '--write', '--backup-dir', tmp_path / 'backups')
    assert code == 1 and error.startswith('error: ') and message in error and 'Traceback' not in error
    assert state.writes == [] and not (tmp_path / 'backups').exists()


def test_missing_desktop_installation_is_explained(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path))
    assert code == 1 and error.startswith('error: ') and '_meta.json does not exist' in error
    monkeypatch.delenv('LOCALAPPDATA')
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path))
    assert code == 1 and '--config-library' in error


@pytest.mark.parametrize('prepare,message', [
    (lambda path: None, 'Relay configuration not found'),
    (lambda path: path.write_text('{', encoding='utf-8'), 'not valid JSON'),
    (lambda path: path.write_text('[]', encoding='utf-8'), 'JSON object'),
    (lambda path: path.write_text('{"accounts": [1]}', encoding='utf-8'), 'invalid accounts'),
    (lambda path: path.write_text(json.dumps({'accounts': [{'enabled': False}]}), encoding='utf-8'),
     'no enabled account'),
    (lambda path: path.write_text(json.dumps({'allow_effort_downgrade': 'yes'}), encoding='utf-8'), 'boolean')])
def test_unusable_relay_configuration_gives_a_clear_error(tmp_path, monkeypatch, capsys, prepare, message):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    relay = tmp_path / 'broken-relay.json'
    prepare(relay)
    code, report, error = cli(capsys, '--relay-config', relay, '--config-library', path.parent)
    assert code == 1 and message in error and 'Traceback' not in error and path.read_bytes() == data


def test_default_locations_are_under_localappdata(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    base = tmp_path / 'LocalAppData' / 'Claude-3p'
    path, data = local_library(base / 'configLibrary')
    anthropic_catalog(base / 'model-catalog')
    report, rows = preview(capsys, tmp_path)
    assert report['target'] == str(path) and len(rows) == 9 and set(report['unlisted_skipped']) == UNLISTED


def test_bootstrap_server_override_is_called_out(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    document = json.loads(data)
    document['bootstrapUrl'] = 'https://config.example.invalid/desktop'
    path.write_text(json.dumps(document), encoding='utf-8')
    report, rows = preview(capsys, tmp_path, '--config-library', path.parent)
    assert any('bootstrapUrl' in warning for warning in report['warnings'])


@pytest.mark.parametrize('newline,trailing,bom', [('\n', False, False), ('\r\n', True, True), ('\n', True, False)])
def test_local_encoding_keeps_bom_line_endings_and_final_newline(tmp_path, newline, trailing, bom):
    path, data = local_library(tmp_path / 'configLibrary', newline=newline, trailing=trailing, bom=bom)
    encoded = encode_local(data, json.loads(data.decode('utf-8-sig')))
    assert encoded == data


def test_concurrent_edit_of_the_local_file_is_not_overwritten(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    real = sync.write_backed_up

    def raced(target, original, updated, backup_dir):
        target.write_bytes(b'{"user": "edit made while the script was working"}')
        return real(target, original, updated, backup_dir)

    monkeypatch.setattr(sync, 'write_backed_up', raced)
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--config-library', path.parent,
                              '--write', '--backup-dir', tmp_path / 'backups')
    assert code == 1 and 'changed' in error and 'Traceback' not in error
    assert path.read_bytes() == b'{"user": "edit made while the script was working"}'


def test_registry_write_backs_up_exact_old_value_and_touches_only_the_model_list(tmp_path, monkeypatch):
    original, encoded = '[ { "name": "old" } ]', '[{"name":"new"}]'
    state = registry(monkeypatch, hkcu={'inferenceProvider': ('gateway', SZ), 'inferenceModels': (original, SZ),
                                        'inferenceGatewayApiKey': ('synthetic', SZ)})
    backup = write_policy(original, SZ, encoded, tmp_path / 'backups')
    saved = json.loads(backup.read_text(encoding='utf-8'))
    assert saved['value'] == original and saved['type'] == SZ and saved['hive'] == 'HKCU'
    assert state.writes == [('inferenceModels', SZ, encoded)]
    assert state.hives['HKCU'] == {'inferenceProvider': ('gateway', SZ), 'inferenceModels': (encoded, SZ),
                                   'inferenceGatewayApiKey': ('synthetic', SZ)}
    assert state.hives['HKLM'] is None


def test_concurrent_model_policy_change_is_not_overwritten(tmp_path, monkeypatch):
    state = registry(monkeypatch, hkcu=policy_values([]) | {'inferenceModels': ('new user policy', SZ)})
    with pytest.raises(RuntimeError, match='changed during'):
        write_policy('old policy', SZ, 'generated', tmp_path / 'backups')
    assert not state.writes and not (tmp_path / 'backups').exists()


# Bug 12: order, labels and the default model

def test_existing_order_labels_and_user_fields_survive_and_new_models_are_appended():
    previous = [{'name': 'claude-sonnet-5-5', 'labelOverride': 'Sonnet 5.5 (relay)'},
                {'name': 'claude-haiku-4-5-20251001', 'supports1m': True, 'behavesAs': 'claude-haiku-4-5'},
                'claude-opus-5-5']
    original = copy.deepcopy(previous)
    result = build_catalog(entries(), previous)
    assert previous == original
    assert [item['name'] for item in result[:3]] == ['claude-sonnet-5-5', 'claude-haiku-4-5-20251001',
                                                    'claude-opus-5-5']
    assert result[0]['labelOverride'] == 'Sonnet 5.5 (relay)'
    assert result[1]['supports1m'] is True and result[1]['behavesAs'] == 'claude-haiku-4-5'
    assert 'labelOverride' not in result[2]
    appended = result[3:]
    assert [item['name'] for item in appended] == [entry['id'] for entry in entries()
                                                   if entry['provider'] == 'anthropic'
                                                   and entry['id'] not in {item['name'] for item in result[:3]}]
    assert all(item['labelOverride'] == models.resolve(item['name'])[0]['label'] for item in appended)


def test_first_entry_changes_only_with_an_explicit_default_model():
    previous = [{'name': 'claude-sonnet-5-5', 'labelOverride': 'mine'}, {'name': 'claude-opus-4-8'}]
    assert build_catalog(entries(), previous)[0]['name'] == 'claude-sonnet-5-5'
    result = build_catalog(entries(), previous, default_model='claude-opus-4-8')
    assert [item['name'] for item in result[:2]] == ['claude-opus-4-8', 'claude-sonnet-5-5']
    assert result[1]['labelOverride'] == 'mine'
    assert build_catalog(entries(), previous, default_model='claude-opus')[0]['name'] == 'claude-opus-5-5'
    assert build_catalog(entries(), [], default_model='claude-haiku-4-5-20251001')[0]['name'] == (
        'claude-haiku-4-5-20251001')
    with pytest.raises(ValueError, match='--default-model'):
        build_catalog(entries(), previous, default_model='gpt-6-sol')


@pytest.mark.parametrize('first', ['gpt-6-sol', 'claude-opus', 'claude-sonnet-4-5-20250929'])
def test_a_default_that_cannot_be_kept_is_never_silently_replaced(first):
    available = [entry for entry in entries() if entry['id'] != 'claude-sonnet-4-5-20250929']
    previous = [{'name': first}, {'name': 'claude-sonnet-5-5'}]
    with pytest.raises(ValueError, match='--default-model'):
        build_catalog(available, previous)
    assert build_catalog(available, previous, default_model='claude-sonnet-5-5')[0]['name'] == 'claude-sonnet-5-5'


def test_cli_keeps_the_users_list_and_reports_what_changed(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary', [USER_ROWS[1], USER_ROWS[2]])
    report, rows = preview(capsys, tmp_path, '--config-library', path.parent, '--include-unlisted')
    assert [row['name'] for row in rows[:2]] == ['claude-sonnet-5-5', 'claude-haiku-4-5-20251001']
    assert rows[0]['labelOverride'] == 'Sonnet 5.5 (relay)' and report['default_model'] == 'claude-sonnet-5-5'
    assert report['added'][0] == 'claude-opus-5-5' and report['removed'] == []
    report, rows = preview(capsys, tmp_path, '--config-library', path.parent, '--include-unlisted',
                           '--default-model', 'claude-opus')
    assert report['default_model'] == 'claude-opus-5-5' and rows[1]['name'] == 'claude-sonnet-5-5'


# Bug 39: models Desktop knows for Code and Cowork

def test_desktop_catalog_limits_the_list_to_models_it_knows_for_code_and_cowork(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    catalog = anthropic_catalog(tmp_path)
    report, rows = preview(capsys, tmp_path, '--config-library', path.parent, '--desktop-catalog', catalog)
    assert {row['name'] for row in rows} == claude_ids() - UNLISTED and len(rows) == 9
    assert set(report['unlisted_skipped']) == UNLISTED
    report, rows = preview(capsys, tmp_path, '--config-library', path.parent, '--desktop-catalog', catalog,
                           '--include-unlisted')
    assert {row['name'] for row in rows} == claude_ids() and len(rows) == 11 and report['unlisted_skipped'] == []
    assert all('maxEffort' not in row for row in rows if row['name'] in UNLISTED)


def test_models_dropped_by_the_desktop_catalog_are_reported(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    rows = USER_ROWS + [{'name': 'claude-sonnet-4-5-20250929'}]
    path, data = local_library(tmp_path / 'configLibrary', rows)
    report, generated = preview(capsys, tmp_path, '--config-library', path.parent,
                                '--desktop-catalog', anthropic_catalog(tmp_path))
    assert report['removed'] == ['claude-sonnet-4-5-20250929']
    assert any('--include-unlisted' in warning and 'claude-sonnet-4-5-20250929' in warning
               for warning in report['warnings'])


def test_without_a_catalog_every_verified_claude_route_is_listed(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    report, rows = preview(capsys, tmp_path, '--config-library', path.parent)
    assert {row['name'] for row in rows} == claude_ids() and len(rows) == 11
    assert any('catalog not found' in warning for warning in report['warnings'])
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--config-library', path.parent,
                              '--desktop-catalog', tmp_path / 'missing.json')
    assert code == 1 and 'catalog not found' in error and 'Traceback' not in error


def test_unusable_catalog_is_an_error_when_requested_and_a_warning_when_defaulted(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    broken = tmp_path / 'LocalAppData' / 'Claude-3p' / 'model-catalog'
    broken.mkdir(parents=True)
    (broken / 'published.json').write_text('{"documentBytes": "!!"}', encoding='utf-8')
    report, rows = preview(capsys, tmp_path, '--config-library', path.parent)
    assert len(rows) == 11 and any('unusable' in warning for warning in report['warnings'])
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--config-library', path.parent,
                              '--desktop-catalog', broken / 'published.json')
    assert code == 1 and 'Unusable Desktop model catalog' in error and 'Traceback' not in error


def test_catalog_that_lists_none_of_the_routes_is_refused_with_a_hint(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    other = anthropic_catalog(tmp_path / 'other', {'ccd': [('claude-fable-5-1', FIVE)]})
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path), '--config-library', path.parent,
                              '--desktop-catalog', other)
    assert code == 1 and '--include-unlisted' in error


def test_catalog_loader_reads_raw_documents_and_only_the_code_and_cowork_surfaces(tmp_path):
    surfaces = {'ccd': [('claude-opus-5-5', FIVE), ('claude-haiku-4-5-20251001', None)],
                'cowork': [('claude-opus-5-5', ('low', 'high')), ('claude-gone', FIVE, True)],
                'chat': [('claude-chat-only', FIVE)]}
    for envelope in (True, False):
        known = load_desktop_catalog(anthropic_catalog(tmp_path / str(envelope), surfaces, envelope=envelope))
        assert known == {'claude-opus-5-5': FIVE, 'claude-haiku-4-5-20251001': ()}
    runtime_only = tmp_path / 'runtime.json'
    runtime_only.write_text(json.dumps({'surfaces': {'cc': {'model_selector_config': [{'models': [
        {'id': 'claude-x', 'runtime': {'effort_levels': ['max', 'low']}}, {'id': 'claude-y'}]}]}}}), encoding='utf-8')
    assert load_desktop_catalog(runtime_only) == {'claude-x': ('low', 'max'), 'claude-y': None}
    with pytest.raises(ValueError):
        load_desktop_catalog(anthropic_catalog(tmp_path / 'empty', {'chat': [('claude-opus-5-5', FIVE)]}))


# Bug 40: maxEffort follows the relay's effort mapping and Desktop's native picker

def test_max_effort_depends_on_effort_downgrade_and_the_native_picker():
    strict = {item['name']: item.get('maxEffort') for item in build_catalog(entries(), [])}
    mapped = {item['name']: item.get('maxEffort') for item in build_catalog(entries(), [], allow_downgrade=True)}
    assert strict == {name: ('max' if name in FOUR_LEVEL else None if name in NO_EFFORT else 'high')
                      for name in claude_ids()}
    assert mapped == {name: (None if name in NO_EFFORT else 'max') for name in claude_ids()}
    assert {name for name, cap in strict.items() if cap == 'high'} == {
        'claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-opus-4-7', 'claude-sonnet-5-5',
        'claude-sonnet-5'}


@pytest.mark.parametrize('levels,native,expected', [
    ([], FIVE, None), (['none'], FIVE, None), (['low', 'high'], FIVE, 'max'),
    (['low', 'medium', 'high', 'max'], FIVE, 'max'), (['low', 'medium', 'high', 'max'], FOUR, 'max'),
    (['low', 'medium', 'high', 'max'], (), None), (['low', 'medium', 'high', 'max'], None, None)])
def test_downgrade_mapping_makes_the_whole_native_picker_safe(levels, native, expected):
    assert compatible_effort_cap(levels, native, allow_downgrade=True) == expected


@pytest.mark.parametrize('name', ['claude-opus-4-5-20251101',
                                  'claude-sonnet-4-5-20250929', 'claude-opus-4-20250514',
                                  'claude-opus-4-1-20250805', 'claude-sonnet-4-20250514'])
@pytest.mark.parametrize('downgrade', [False, True])
def test_models_without_a_native_effort_selector_never_get_a_cap(name, downgrade):
    entry = models.resolve(name)[0]
    assert entry['reasoning_effort_levels']
    previous = [{'name': name, 'maxEffort': 'xhigh'}]
    assert 'maxEffort' not in build_catalog([entry], previous, allow_downgrade=downgrade)[0]


@pytest.mark.parametrize('downgrade', [False, True])
def test_haiku_publishes_no_levels_because_the_route_ignores_effort_so_it_gets_no_cap(downgrade):
    entry = models.resolve('claude-haiku-4-5-20251001')[0]
    assert entry['reasoning_effort_levels'] == [] and entry['reasoning_effort_control'] == 'accepted-ignored'
    previous = [{'name': entry['id'], 'maxEffort': 'xhigh'}]
    assert 'maxEffort' not in build_catalog([entry], previous, allow_downgrade=downgrade)[0]


@pytest.mark.parametrize('downgrade,expected', [(False, 'high'), (True, 'max')])
def test_unknown_future_model_assumes_the_full_picker_so_the_cap_protects_it(downgrade, expected):
    future = dict(id='claude-opus-9', provider='anthropic', label='Claude Opus 9', context=1000000,
                  reasoning_effort_levels=['low', 'medium', 'high', 'max'])
    assert build_catalog([future], [], allow_downgrade=downgrade)[0]['maxEffort'] == expected


def test_catalog_levels_take_precedence_over_the_builtin_table(tmp_path):
    # Opus 4.6 has a four-level picker in the builtin table; the catalog says five, so the cap drops to high.
    catalog = load_desktop_catalog(anthropic_catalog(tmp_path, {'ccd': [('claude-sonnet-5-5', FOUR),
                                                                        ('claude-opus-4-6', FIVE)]}))
    sonnet, opus = (models.resolve(name)[0] for name in ('claude-sonnet-5-5', 'claude-opus-4-6'))
    rows = build_catalog([sonnet, opus], [], desktop=catalog)
    assert rows[0]['maxEffort'] == 'max' and rows[1]['maxEffort'] == 'high'


def test_relay_effort_mapping_setting_reaches_the_generated_catalog(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    catalog = anthropic_catalog(tmp_path)
    for setting, expected in ((None, 'high'), (False, 'high'), (True, 'max')):
        extra = {} if setting is None else {'allow_effort_downgrade': setting}
        relay = tmp_path / ('relay-%s.json' % setting)
        relay.write_text(relay_config(tmp_path, **extra).read_text(encoding='utf-8'), encoding='utf-8')
        report, rows = preview(capsys, tmp_path, '--config-library', path.parent, '--desktop-catalog', catalog,
                               relay=relay)
        by_name = {row['name']: row for row in rows}
        assert by_name['claude-opus-5-5']['maxEffort'] == expected
        assert by_name['claude-sonnet-4-6']['maxEffort'] == 'max'
        assert 'maxEffort' not in by_name['claude-haiku-4-5-20251001']


# Bug 46: the relay's route filter sees the whole configuration

def test_route_filter_receives_root_keys_and_the_merged_accounts(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    seen = []
    real = models.catalog_routable
    monkeypatch.setattr(models, 'catalog_routable', lambda entry, cfg, backends=None: seen.append(cfg) or
                        real(entry, cfg, backends))
    relay = relay_config(tmp_path, show_unrouted_models=True, marker='root-key',
                         accounts=[{'base': 'https://example.invalid', 'page_uuid': 'page-1', 'cookie': 'a=b',
                                    'xsrf': 'x', 'provider_resource_name': 'resource-a'},
                                   {'enabled': False, 'base': 'https://other.invalid'}])
    preview(capsys, tmp_path, '--config-library', path.parent, relay=relay)
    assert seen and all(cfg['show_unrouted_models'] is True and cfg['marker'] == 'root-key' for cfg in seen)
    assert all(len(cfg['accounts']) == 1 and cfg['accounts'][0]['provider_resource_name'] == 'resource-a'
               and cfg['accounts'][0]['show_unrouted_models'] is True for cfg in seen)


def test_show_unrouted_models_decides_whether_unconfigured_providers_are_listed(tmp_path, monkeypatch, capsys):
    registry(monkeypatch)
    path, data = local_library(tmp_path / 'configLibrary')
    only_openai = {'provider_resource_name': '', 'retool_fast_provider_resource_name': ''}
    code, report, error = cli(capsys, '--relay-config', relay_config(tmp_path, **only_openai),
                              '--config-library', path.parent)
    assert code == 1 and 'Refusing an empty' in error
    report, rows = preview(capsys, tmp_path, '--config-library', path.parent,
                           relay=relay_config(tmp_path, show_unrouted_models=True, **only_openai))
    assert {row['name'] for row in rows} == claude_ids()
