"""Client catalog updates preserve unrelated configuration and are recoverable."""
import copy
import errno
import json
import os
from pathlib import Path

import pytest

import models
from scripts import sync_opencode_catalog as sync
from scripts.probe_client_settings import routable_entries
from scripts.sync_opencode_catalog import main, parse_settings, update_document, write_backed_up

HAIKU = 'retool-relay/claude-haiku-4-5-20251001'
RELAY = {'base': 'https://example.invalid', 'provider_resource_name': 'anthropic-resource',
         'openai_provider_resource_name': 'openai-resource', 'retool_fast_provider_resource_name': 'fast-resource'}


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


def relay_config(tmp_path, name='relay.json', **settings):
    path = tmp_path / name
    path.write_text(json.dumps(settings or RELAY), encoding='utf-8')
    return path


def client(tmp_path, capsys, *args, relay=None):
    """Run the CLI against copies under tmp_path; returns (exit code, summary, stderr)."""
    code = main(['--relay-config', str(relay or relay_config(tmp_path)),
                 '--settings', str(tmp_path / 'opencode.jsonc'), *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out.strip() else None, captured.err


def installed(tmp_path, document=None, **encoding):
    """Write a settings file holding a retool-relay provider, with the requested byte-level style."""
    document = document or {'provider': {'retool-relay': {'options': {'baseURL': 'http://localhost:8787/v1',
                                                                     'apiKey': 'relay-private'}, 'models': {}}}}
    text = json.dumps(document, indent=encoding.get('indent', 2), ensure_ascii=False)
    text = text.replace('\n', encoding.get('newline', '\n')) + encoding.get('end', '\n')
    path = tmp_path / 'opencode.jsonc'
    path.write_bytes(encoding.get('bom', b'') + text.encode('utf-8'))
    return path


def test_unchanged_catalog_is_not_rewritten_and_makes_no_second_backup(tmp_path, capsys):
    settings = installed(tmp_path)
    backups = tmp_path / 'backups'
    code, first, _ = client(tmp_path, capsys, '--write', '--backup-dir', str(backups))
    assert code == 0 and first['written'] is True and first['changed'] is True
    synchronized, stamp = settings.read_bytes(), settings.stat().st_mtime_ns
    code, second, _ = client(tmp_path, capsys, '--write', '--backup-dir', str(backups))
    assert code == 0 and second['written'] is False and second['changed'] is False and second['backup'] is None
    assert settings.read_bytes() == synchronized and settings.stat().st_mtime_ns == stamp
    assert len(list(backups.iterdir())) == 1


def test_up_to_date_commented_file_is_left_alone_instead_of_demanding_reformat(tmp_path, capsys):
    settings = installed(tmp_path)
    assert client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))[0] == 0
    commented = b'// private note\r\n' + settings.read_bytes().replace(b'\n}', b',\n}')
    settings.write_bytes(commented)
    code, summary, _ = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups2'))
    assert code == 0 and summary['written'] is False and settings.read_bytes() == commented
    assert not (tmp_path / 'backups2').exists()


def test_jsonc_comments_trailing_commas_and_comment_markers_inside_strings_are_read():
    raw = b'''\xef\xbb\xbf{
  // line comment with "quotes" and a trailing comma below
  "baseURL": "http://localhost:8787/v1", /* block // inside a comment */
  "note": "keep // and /* these */ in strings, even after an escaped \\" quote",
  "list": [1, 2, /* last */ ],
  "nested": {"a": 1,},
}
'''
    document, style, commented = parse_settings(raw, 'settings')
    assert commented is True
    assert document == {'baseURL': 'http://localhost:8787/v1',
                        'note': 'keep // and /* these */ in strings, even after an escaped " quote',
                        'list': [1, 2], 'nested': {'a': 1}}
    assert style == {'bom': b'\xef\xbb\xbf', 'newline': '\n', 'indent': '  ', 'final_newline': True}
    assert parse_settings(b'{"plain": true}', 'settings')[2] is False


def test_write_refuses_to_remove_comments_unless_reformat_is_allowed(tmp_path, capsys):
    settings = tmp_path / 'opencode.jsonc'
    original = (b'{\n  // keep my note\n  "model": "retool-relay/gpt-5.3-codex",\n'
                b'  "provider": {"retool-relay": {"options": {"apiKey": "relay-private"}}},\n}\n')
    settings.write_bytes(original)
    backups = tmp_path / 'backups'
    code, summary, error = client(tmp_path, capsys, '--write', '--backup-dir', str(backups))
    assert code == 1 and summary is None
    assert '--allow-reformat' in error and 'comments' in error and 'relay-private' not in error
    assert settings.read_bytes() == original and not backups.exists()
    code, preview, _ = client(tmp_path, capsys, '--output', str(tmp_path / 'preview.json'))
    assert code == 0 and preview['written'] is False and preview['comments_dropped'] is True
    code, summary, _ = client(tmp_path, capsys, '--write', '--allow-reformat', '--backup-dir', str(backups))
    assert code == 0 and summary['written'] is True and summary['comments_dropped'] is True
    assert b'keep my note' not in settings.read_bytes()
    assert [path.read_bytes() for path in backups.iterdir()] == [original]


@pytest.mark.parametrize('encoding', [
    {'bom': b'\xef\xbb\xbf', 'newline': '\r\n', 'indent': 4},
    {'newline': '\r\n', 'indent': 2, 'end': ''},
    {'bom': b'\xef\xbb\xbf', 'indent': '\t', 'end': ''},
    {'indent': 3}])
def test_write_keeps_bom_line_endings_indentation_and_final_newline_of_the_original(tmp_path, capsys, encoding):
    installed(tmp_path, **encoding)
    code, summary, error = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 0 and summary['written'] is True, error
    data = (tmp_path / 'opencode.jsonc').read_bytes()
    bom, newline, end = encoding.get('bom', b''), encoding.get('newline', '\n'), encoding.get('end', '\n')
    assert data.startswith(bom) and (bom == b'' or not data.startswith(bom * 2))
    text = data[len(bom):].decode('utf-8')
    assert text.endswith(newline) == bool(end)
    assert text.count('\n') == (text.count('\r\n') if newline == '\r\n' else text.count('\n'))
    indent = encoding.get('indent', 2)
    unit = indent if isinstance(indent, str) else ' ' * indent
    assert '\n' + unit + '"provider"' in text.replace('\r\n', '\n')
    assert json.loads(text)['provider']['retool-relay']['whitelist']


def test_missing_settings_file_is_reported_without_traceback(tmp_path, capsys):
    code, summary, error = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 1 and summary is None
    assert error.startswith('error: OpenCode settings file not found') and 'opencode.jsonc' in error
    assert 'Traceback' not in error and not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('raw,expected', [
    (b'{"apiKey": "relay-private" "x": 1}', 'line 1, column 28'),
    (b'{\n  "apiKey": "relay-private",\n  "model": }\n', 'line 3, column 12'),
    (b'{"apiKey": "relay-private", /* never closed', 'unterminated /* comment at line 1'),
    (b'[1, 2]', 'must contain a JSON object'),
    (b'', 'line 1, column 1'),
    (b'\xff\xfe{\x00}\x00', 'not valid UTF-8')])
def test_broken_settings_are_reported_with_position_but_without_their_content(tmp_path, capsys, raw, expected):
    settings = tmp_path / 'opencode.jsonc'
    settings.write_bytes(raw)
    code, summary, error = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 1 and summary is None and expected in error
    assert 'relay-private' not in error and 'Traceback' not in error
    assert settings.read_bytes() == raw and not (tmp_path / 'backups').exists()


def test_invalid_relay_configuration_is_reported_cleanly(tmp_path, capsys):
    installed(tmp_path)
    broken = tmp_path / 'broken.json'
    broken.write_text('{"cookie": "private-cookie" ', encoding='utf-8')
    for relay, expected in ((broken, 'not valid JSON'), (tmp_path / 'absent.json', 'not found')):
        code, summary, error = client(tmp_path, capsys, relay=relay)
        assert code == 1 and summary is None and expected in error and 'private-cookie' not in error


def test_preview_masks_credentials_by_default_and_the_full_preview_is_explicit(tmp_path, capsys):
    settings = installed(tmp_path, {
        'provider': {'retool-relay': {'options': {'baseURL': 'http://localhost:8787/v1', 'apiKey': 'relay-private',
                                                  'headers': {'Authorization': 'Bearer header-private'}},
                                      'models': {}},
                     'other': {'options': {'apiKey': 'other-private', 'timeout': 600}, 'env': ['OTHER_API_KEY']}},
        'mcp': {'docs': {'type': 'remote', 'url': 'https://docs.example', 'headers': {'X-Token': 'mcp-private'},
                         'environment': {'DOCS_PASSWORD': 'password-private', 'PORT': 8080}}}})
    original = settings.read_bytes()
    masked, full = tmp_path / 'masked.json', tmp_path / 'full.json'
    code, summary, _ = client(tmp_path, capsys, '--output', str(masked))
    assert code == 0 and summary['preview'] == str(masked) and summary['preview_redacted'] is True
    text = masked.read_text(encoding='utf-8')
    for private in ('relay-private', 'header-private', 'other-private', 'mcp-private', 'password-private'):
        assert private not in text
    preview = json.loads(text)
    assert preview['provider']['retool-relay']['options'] == {'baseURL': 'http://localhost:8787/v1',
                                                              'apiKey': '***', 'headers': {'Authorization': '***'}}
    assert preview['provider']['other']['options'] == {'apiKey': '***', 'timeout': 600}
    assert preview['provider']['other']['env'] == ['OTHER_API_KEY']
    assert preview['mcp']['docs']['environment'] == {'DOCS_PASSWORD': '***', 'PORT': 8080}
    assert len(preview['provider']['retool-relay']['models']) == 27
    code, summary, _ = client(tmp_path, capsys, '--output', str(full), '--include-secrets')
    assert code == 0 and summary['preview_redacted'] is False
    revealed = json.loads(full.read_text(encoding='utf-8'))
    assert revealed['provider']['retool-relay']['options']['apiKey'] == 'relay-private'
    assert revealed['mcp']['docs']['headers'] == {'X-Token': 'mcp-private'}
    assert settings.read_bytes() == original


def test_written_settings_keep_credentials_and_an_existing_preview_is_never_replaced(tmp_path, capsys):
    settings = installed(tmp_path)
    preview = tmp_path / 'preview.json'
    preview.write_text('earlier preview')
    code, summary, error = client(tmp_path, capsys, '--output', str(preview), '--write',
                                  '--backup-dir', str(tmp_path / 'backups'))
    assert code == 1 and 'already exists' in error and summary is None
    assert preview.read_text() == 'earlier preview' and b'"relay-private"' in settings.read_bytes()
    assert not (tmp_path / 'backups').exists()
    code, summary, _ = client(tmp_path, capsys, '--output', str(tmp_path / 'new.json'), '--write',
                              '--backup-dir', str(tmp_path / 'backups'))
    assert code == 0 and b'"relay-private"' in settings.read_bytes()


def test_replacement_that_moved_the_original_to_the_backup_restores_it(tmp_path, monkeypatch):
    # ReplaceFileW error 1177 (ERROR_UNABLE_TO_MOVE_REPLACEMENT_2): the destination exists only under the backup name.
    path = tmp_path / 'config.json'
    path.write_bytes(b'original')

    def failed_replace(target, replacement, backup):
        os.replace(target, backup)
        raise OSError(None, 'The replacement file could not be renamed.', None, 1177)
    monkeypatch.setattr(sync, '_replace_with_backup', failed_replace)
    with pytest.raises(OSError) as error:
        write_backed_up(path, b'original', b'generated', tmp_path / 'backups')
    assert error.value.winerror == 1177 if os.name == 'nt' else error.value.errno is None
    assert path.read_bytes() == b'original'
    assert not list(tmp_path.glob('.proxi-catalog-*.tmp'))


def test_failed_restoration_names_the_backup_that_still_holds_the_original(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_bytes(b'original')

    def failed_replace(target, replacement, backup):
        os.replace(target, backup)
        raise OSError(None, 'The replacement file could not be renamed.', None, 1177)

    def locked(source, destination):
        raise PermissionError(errno.EACCES, 'locked by another program')
    monkeypatch.setattr(sync, '_replace_with_backup', failed_replace)
    monkeypatch.setattr(os, 'rename', locked)
    with pytest.raises(RuntimeError, match='original is saved as') as error:
        write_backed_up(path, b'original', b'generated', tmp_path / 'backups')
    backup = next((tmp_path / 'backups').iterdir())
    assert str(backup) in str(error.value) and backup.read_bytes() == b'original'


def test_replacement_failure_that_leaves_the_destination_in_place_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_bytes(b'original')

    def refused(target, replacement, backup):
        raise PermissionError(errno.EACCES, 'sharing violation')
    monkeypatch.setattr(sync, '_replace_with_backup', refused)
    with pytest.raises(PermissionError):
        write_backed_up(path, b'original', b'generated', tmp_path / 'backups')
    assert path.read_bytes() == b'original'
    assert not list((tmp_path / 'backups').iterdir())


@pytest.mark.skipif(os.name != 'nt', reason='ReplaceFileW exists only on Windows')
def test_windows_replacefile_error_1177_through_the_real_wrapper_restores_the_original(tmp_path, monkeypatch):
    import ctypes
    from types import SimpleNamespace
    path = tmp_path / 'config.json'
    path.write_bytes(b'original')

    def replace_file(target, replacement, backup, flags, exclude, reserved):
        os.replace(target, backup)  # the API displaced the destination before it failed
        return 0
    monkeypatch.setattr(ctypes, 'WinDLL', lambda name, use_last_error=False: SimpleNamespace(ReplaceFileW=replace_file))
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 1177)
    with pytest.raises(OSError) as error:
        write_backed_up(path, b'original', b'generated', tmp_path / 'backups')
    assert error.value.winerror == 1177 and path.read_bytes() == b'original'


@pytest.fixture
def posix_files(tmp_path):
    path, replacement, backup = tmp_path / 'config.json', tmp_path / 'new.tmp', tmp_path / 'config.bak'
    path.write_bytes(b'original')
    replacement.write_bytes(b'updated')
    return path, replacement, backup


def test_posix_replacement_backs_up_first_and_the_destination_never_disappears(posix_files, monkeypatch):
    path, replacement, backup = posix_files
    events = []
    link, replace = os.link, os.replace

    def watched_link(source, destination):
        events.append(('link', Path(source), Path(destination), path.exists()))
        return link(source, destination)

    def watched_replace(source, destination):
        events.append(('replace', Path(source), Path(destination), path.exists(), backup.exists()))
        return replace(source, destination)
    monkeypatch.setattr(os, 'link', watched_link)
    monkeypatch.setattr(os, 'replace', watched_replace)
    sync._replace_posix(path, replacement, backup)
    assert events == [('link', path, backup, True), ('replace', replacement, path, True, True)]
    assert path.read_bytes() == b'updated' and backup.read_bytes() == b'original'


def test_posix_failed_rename_keeps_the_destination_and_drops_the_unused_backup(posix_files, monkeypatch):
    path, replacement, backup = posix_files

    def refused(source, destination):
        raise PermissionError(errno.EACCES, 'read-only directory')
    monkeypatch.setattr(os, 'replace', refused)
    with pytest.raises(PermissionError):
        sync._replace_posix(path, replacement, backup)
    assert path.read_bytes() == b'original' and not backup.exists() and replacement.exists()


def test_posix_backup_is_copied_where_hard_links_are_unsupported(posix_files, monkeypatch):
    path, replacement, backup = posix_files

    def unsupported(source, destination):
        raise OSError(errno.EPERM, 'hard links are not supported')
    monkeypatch.setattr(os, 'link', unsupported)
    sync._replace_posix(path, replacement, backup)
    assert path.read_bytes() == b'updated' and backup.read_bytes() == b'original'


def test_posix_replacement_never_reuses_an_existing_backup_name(posix_files):
    path, replacement, backup = posix_files
    backup.write_bytes(b'older backup')
    with pytest.raises(FileExistsError):
        sync._replace_posix(path, replacement, backup)
    assert path.read_bytes() == b'original' and backup.read_bytes() == b'older backup'
    assert replacement.read_bytes() == b'updated'


def test_removed_models_in_small_model_and_background_agents_get_a_cheap_published_model():
    before = source()
    before['small_model'] = 'retool-relay/gpt-5.3-codex'
    before['agent'] = {'title': {'model': 'retool-relay/gpt-6-luna', 'temperature': 0},
                       'compaction': {'model': 'retool-relay/claude-sonnet-4-20250514'},
                       'summary': {'model': 'retool-relay/gpt-6-sol'},
                       'build': {'model': 'retool-relay/gpt-5.3-codex'}}
    entries = [models.resolve(name)[0] for name in ('gpt-6-sol', 'claude-opus-5-5', 'gpt-5-nano',
                                                    'claude-haiku-4-5-20251001')]
    result = update_document(before, entries)
    assert result['small_model'] == HAIKU
    assert result['agent']['title'] == {'model': HAIKU, 'temperature': 0}
    assert result['agent']['compaction'] == {'model': HAIKU}
    assert result['agent']['summary'] == {'model': 'retool-relay/gpt-6-sol'}
    assert result['agent']['build'] == {'model': 'retool-relay/claude-opus-5-5'}
    without_haiku = update_document(before, [entry for entry in entries if entry['id'] != 'claude-haiku-4-5-20251001'])
    assert without_haiku['small_model'] == 'retool-relay/gpt-5-nano'
    assert without_haiku['agent']['compaction'] == {'model': 'retool-relay/gpt-5-nano'}


def test_removed_model_override_is_dropped_when_no_cheap_model_is_published():
    before = source()
    before['small_model'] = 'retool-relay/gpt-5.3-codex'
    before['agent'] = {'title': {'model': 'retool-relay/gpt-5.3-codex', 'temperature': 0},
                       'build': {'model': 'retool-relay/gpt-5.3-codex'}}
    result = update_document(before, [models.resolve(name)[0] for name in ('gpt-6-sol', 'claude-opus-5-5')])
    assert 'small_model' not in result and result['agent']['title'] == {'temperature': 0}
    assert result['agent']['build'] == {'model': 'retool-relay/claude-opus-5-5'}


def test_command_and_deprecated_mode_models_are_updated_like_agent_models():
    before = source()
    before['command'] = {'review': {'template': 'Review', 'agent': 'plan', 'model': 'retool-relay/gpt-5.3-codex'},
                         'short': {'template': 'Short', 'model': 'retool-relay/gpt-6'},
                         'external': {'template': 'External', 'model': 'other/model'},
                         'inherits': {'template': 'Inherits'}, 'odd': 'not an object'}
    before['mode'] = {'plan': {'model': 'retool-relay/claude-sonnet-4-20250514'}, 'other': 'text'}
    original = copy.deepcopy(before)
    result = update_document(before, [models.resolve(name)[0] for name in ('gpt-6-sol', 'claude-opus-5-5')])
    assert before == original
    assert result['command']['review'] == {'template': 'Review', 'agent': 'plan',
                                           'model': 'retool-relay/claude-opus-5-5'}
    assert result['command']['short']['model'] == 'retool-relay/gpt-6-sol'
    assert result['command']['external'] == original['command']['external']
    assert result['command']['inherits'] == {'template': 'Inherits'} and result['command']['odd'] == 'not an object'
    assert result['mode'] == {'plan': {'model': 'retool-relay/claude-opus-5-5'}, 'other': 'text'}


def test_variant_suffix_of_a_model_reference_survives_only_while_the_model_publishes_it():
    before = source()
    before.update(model='retool-relay/gpt-6-sol#high', small_model=HAIKU + '#xhigh')
    before['agent'] = {'reviewer': {'model': 'retool-relay/zai-org/GLM-5.3#max'}}
    before['command'] = {'plain': {'template': 'x', 'model': 'retool-relay/gpt-4.1#high'}}
    entries = [models.resolve(name)[0] for name in ('gpt-6-sol', 'claude-haiku-4-5-20251001', 'zai-org/GLM-5.3', 'gpt-4.1')]
    result = update_document(before, entries)
    assert result['model'] == 'retool-relay/gpt-6-sol#high'
    assert result['small_model'] == HAIKU
    assert result['agent']['reviewer']['model'] == 'retool-relay/zai-org/GLM-5.3#max'
    assert result['command']['plain']['model'] == 'retool-relay/gpt-4.1'


def test_cli_replaces_removed_main_small_agent_and_command_models_with_the_defaults(tmp_path, capsys):
    settings = installed(tmp_path, {
        'model': 'retool-relay/gpt-5.3-codex', 'small_model': 'retool-relay/gpt-6-luna',
        'agent': {'title': {'model': 'retool-relay/gpt-6-luna'}, 'build': {'model': 'retool-relay/gpt-5.3-codex'}},
        'command': {'review': {'template': 'Review', 'model': 'retool-relay/gpt-5.3-codex'}},
        'provider': {'retool-relay': {'options': {'baseURL': 'http://localhost:8787/v1'}, 'models': {}}}})
    code, summary, error = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 0 and summary['written'] is True, error
    saved = json.loads(settings.read_text(encoding='utf-8'))
    assert saved['model'] == 'retool-relay/claude-opus-5-5'
    assert saved['small_model'] == HAIKU and saved['agent']['title']['model'] == HAIKU
    assert saved['agent']['build']['model'] == 'retool-relay/claude-opus-5-5'
    assert saved['command']['review']['model'] == 'retool-relay/claude-opus-5-5'


def test_relay_root_options_reach_the_catalog_filter(tmp_path, capsys):
    only_anthropic = {'base': 'https://example.invalid', 'provider_resource_name': 'anthropic-resource'}
    hidden = {entry['id'] for entry in routable_entries(relay_config(tmp_path, 'plain.json', **only_anthropic))}
    shown = {entry['id'] for entry in routable_entries(
        relay_config(tmp_path, 'shown.json', **only_anthropic, show_unrouted_models=True))}
    assert 'claude-opus-5-5' in hidden and 'gpt-6-sol' not in hidden
    assert 'claude-opus-5-5' in shown and 'gpt-6-sol' in shown and hidden < shown
    installed(tmp_path)
    code, summary, _ = client(tmp_path, capsys, relay=tmp_path / 'plain.json')
    assert code == 0 and summary['models'] == len(hidden)
    code, summary, _ = client(tmp_path, capsys, relay=tmp_path / 'shown.json')
    assert code == 0 and summary['models'] == len(shown)
