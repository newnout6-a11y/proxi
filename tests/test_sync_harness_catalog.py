"""Harness catalog refreshes preserve provider selection and unrelated settings."""
import copy
import json
from io import StringIO

import pytest
from ruamel.yaml import YAML

import models
from scripts.probe_client_settings import routable_entries
from scripts.sync_harness_catalog import encode_document, main, mask_profile, update_document

RELAY = {'base': 'https://example.invalid', 'provider_resource_name': 'anthropic-resource',
         'openai_provider_resource_name': 'openai-resource', 'retool_fast_provider_resource_name': 'fast-resource'}


def source():
    relay = {'baseURL': 'http://127.0.0.1:8787/v1', 'apiKeyEnv': 'KEEP_RELAY_KEY',
             'reasoning': 'medium', 'compat': {'thinkingFormat': 'deepseek'},
             'models': [{'id': 'gpt-6-sol', 'custom': True,
                         'reasoningEfforts': {'xhigh': 'xhigh'}}]}
    return [{'id': 'llm', 'config': {'provider': 'other', 'model': 'other/model'}},
            {'id': 'llm-pi-ai', 'config': {'providers': {
                'other': {'apiKeyEnv': 'KEEP_OTHER_KEY', 'models': [{'id': 'private'}]},
                'retool': relay}}},
            {'id': 'private-plugin', 'config': {'source': 'unchanged source'}}]


def test_refresh_keeps_selection_other_providers_credentials_and_custom_fields():
    before = source()
    original = copy.deepcopy(before)
    entries = [models.resolve(name)[0] for name in ('gpt-6-sol', 'gpt-4.1', 'zai-org/GLM-5.3-Flash')]
    result = update_document(before, entries)
    assert before == original
    assert result[0] == before[0] and result[2] == before[2]
    providers = result[1]['config']['providers']
    assert providers['other'] == before[1]['config']['providers']['other']
    relay = providers['retool']
    assert relay['apiKeyEnv'] == 'KEEP_RELAY_KEY' and relay['baseURL'] == 'http://127.0.0.1:8787/v1'
    assert relay['compat'] == before[1]['config']['providers']['retool']['compat']
    assert relay['reasoning'] == 'medium'
    text = providers['retool-text']
    assert text['reasoning'] == 'off' and text['apiKeyEnv'] == relay['apiKeyEnv']
    catalog = {model['id']: model for group in (relay, text) for model in group['models']}
    assert set(catalog) == {entry['id'] for entry in entries}
    assert catalog['gpt-6-sol']['custom'] is True
    assert 'xhigh' not in catalog['gpt-6-sol']['reasoningEfforts']
    assert catalog['gpt-6-sol']['reasoningEfforts']['off'] == 'none'
    assert catalog['gpt-4.1']['reasoningEfforts'] is False
    assert set(catalog['zai-org/GLM-5.3-Flash']['reasoningEfforts']) == {'low', 'medium', 'high', 'max'}
    assert catalog['zai-org/GLM-5.3-Flash']['input'] == ['text', 'image']


def test_alias_to_another_provider_does_not_change_the_other_provider():
    before = source()
    providers = before[1]['config']['providers']
    providers['other'] = providers['retool']
    result = update_document(before, [models.resolve('gpt-4.1')[0]])
    assert result[1]['config']['providers']['other'] == providers['other']
    assert result[1]['config']['providers']['retool'] != providers['other']


def test_shared_plugin_config_is_detached_before_the_proxi_catalog_changes():
    before = source()
    before[2]['config'] = before[1]['config']
    original = copy.deepcopy(before[2])
    result = update_document(before, [models.resolve('gpt-4.1')[0]])
    assert result[2] == original


@pytest.mark.parametrize('document', [{}, [], [{'id': 'llm-pi-ai', 'config': {}}], source() + [source()[1]]])
def test_unknown_or_ambiguous_schema_is_not_updated(document):
    with pytest.raises(ValueError):
        update_document(document, [models.resolve('gpt-4.1')[0]])


def test_empty_or_duplicate_catalog_is_not_updated():
    entry = models.resolve('gpt-4.1')[0]
    for entries in ([], [entry, entry]):
        with pytest.raises(ValueError):
            update_document(source(), entries)


def test_roundtrip_yaml_retains_comments_quoting_and_other_plugin_source():
    yaml = YAML()
    yaml.preserve_quotes = True
    document = yaml.load('''# Keep profile header
- id: llm
  config: {provider: other, model: 'other/model'} # Keep selected model
- id: llm-pi-ai
  config:
    providers:
      retool:
        apiKeyEnv: 'KEEP_KEY'
        reasoning: medium
        models: []
- id: private-plugin
  config:
    source: |-
      function untouched() { return "original"; }
''')
    result = update_document(document, [models.resolve('gpt-4.1')[0]])
    output = StringIO()
    yaml.dump(result, output)
    text = output.getvalue()
    assert '# Keep profile header' in text and '# Keep selected model' in text
    assert "'other/model'" in text and "'KEEP_KEY'" in text
    assert yaml.load(text)[2] == document[2]


def test_write_without_backup_is_rejected_before_reading_private_settings():
    with pytest.raises(SystemExit) as error:
        main(['--settings', 'not-read.yml', '--write'])
    assert error.value.code == 2


def test_existing_unrelated_text_provider_is_never_overwritten():
    before = source()
    before[1]['config']['providers']['retool-text'] = {'baseURL': 'https://other.invalid'}
    with pytest.raises(ValueError, match='Existing retool-text'):
        update_document(before, [models.resolve(name)[0] for name in ('gpt-6-sol', 'gpt-4.1')])


def test_byte_splice_preserves_other_dates_custom_tags_and_line_endings():
    raw = b'''# Keep exact header\r
- id: llm-pi-ai\r
  config:\r
    providers:\r
      other:\r
        headers: {version: 2023-06-01T00:00:00.000Z}\r
      retool:\r
        displayName: 'Retool Relay'\r
        baseURL: http://127.0.0.1:8787/v1\r
        reasoning: medium\r
        models: []\r
      tail:\r
        disabled: !custom false\r
'''
    yaml = YAML()
    yaml.preserve_quotes = True
    document = yaml.load(raw.decode())
    entries = [models.resolve(name)[0] for name in ('gpt-6-sol', 'gpt-4.1')]
    updated = update_document(document, entries)
    result = encode_document(raw, document, updated)
    assert result[:result.index(b'      retool:')] == raw[:raw.index(b'      retool:')]
    assert result[result.index(b'      tail:'):] == raw[raw.index(b'      tail:'):]
    assert b'2023-06-01T00:00:00.000Z' in result
    assert b'\n' not in result.replace(b'\r\n', b'')
    restored = yaml.load(result.decode())
    providers = restored[0]['config']['providers']
    assert len(providers['retool']['models']) == len(providers['retool-text']['models']) == 1


def test_resync_of_managed_split_catalog_does_not_duplicate_models_or_groups():
    entries = [models.resolve(name)[0] for name in ('gpt-6-sol', 'gpt-4.1')]
    first = update_document(source(), entries)
    assert update_document(first, entries) == first


PROFILE = '''# Keep profile header
- id: llm
  config: {provider: other, model: 'other/model'} # Keep selected model
- id: llm-pi-ai
  config:
    providers:
      other:
        apiKey: other-private
        apiKeyEnv: KEEP_OTHER_KEY
        headers: {Authorization: 'Bearer header-private', X-Custom: plain-private}
        token: |-
          block-private
          second-private-line
        maxTokens: 4096
        models: [{id: private}]
      retool:
        displayName: 'Retool Relay'
        baseURL: http://127.0.0.1:8787/v1
        apiKeyEnv: KEEP_RELAY_KEY
        reasoning: medium
        models: []
      tail:
        enabled: true
'''


def client(tmp_path, capsys, *args, text=PROFILE, raw=None, relay=None):
    """Run the CLI against copies under tmp_path; returns (exit code, summary, stderr, profile path)."""
    profile = tmp_path / 'cordis.patch.yml'
    if raw is not None or not profile.exists():
        profile.write_bytes(raw if raw is not None else text.encode('utf-8'))
    if relay is None:
        relay = tmp_path / 'relay.json'
        relay.write_text(json.dumps(RELAY), encoding='utf-8')
    code = main(['--relay-config', str(relay), '--settings', str(profile), *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out.strip() else None, captured.err, profile


def test_unchanged_catalog_is_not_rewritten_and_makes_no_second_backup(tmp_path, capsys):
    backups = tmp_path / 'backups'
    code, first, _, profile = client(tmp_path, capsys, '--write', '--backup-dir', str(backups))
    assert code == 0 and first['written'] is True and first['changed'] is True
    synchronized, stamp = profile.read_bytes(), profile.stat().st_mtime_ns
    code, second, _, _ = client(tmp_path, capsys, '--write', '--backup-dir', str(backups))
    assert code == 0 and second['written'] is False and second['changed'] is False and second['backup'] is None
    assert profile.read_bytes() == synchronized and profile.stat().st_mtime_ns == stamp
    assert len(list(backups.iterdir())) == 1


def test_preview_masks_credentials_but_keeps_comments_and_credential_references(tmp_path, capsys):
    masked, full = tmp_path / 'masked.yml', tmp_path / 'full.yml'
    code, summary, _, profile = client(tmp_path, capsys, '--output', str(masked))
    assert code == 0 and summary['preview_redacted'] is True and summary['written'] is False
    original = profile.read_bytes()
    text = masked.read_text(encoding='utf-8')
    for private in ('other-private', 'header-private', 'plain-private', 'block-private', 'second-private-line'):
        assert private not in text
    for kept in ('# Keep profile header', '# Keep selected model', 'apiKeyEnv: KEEP_OTHER_KEY',
                 'apiKeyEnv: KEEP_RELAY_KEY', 'maxTokens: 4096', 'enabled: true'):
        assert kept in text
    provider = YAML().load(text)[1]['config']['providers']['other']
    assert provider['apiKey'] == '***' and provider['token'] == '***'
    assert dict(provider['headers']) == {'Authorization': '***', 'X-Custom': '***'}
    assert len(YAML().load(text)[1]['config']['providers']['retool']['models']) == 26
    assert len(YAML().load(text)[1]['config']['providers']['retool-text']['models']) == 1
    code, summary, _, _ = client(tmp_path, capsys, '--output', str(full), '--include-secrets')
    assert code == 0 and summary['preview_redacted'] is False
    revealed = YAML().load(full.read_text(encoding='utf-8'))[1]['config']['providers']['other']
    assert revealed['apiKey'] == 'other-private' and revealed['token'] == 'block-private\nsecond-private-line'
    assert profile.read_bytes() == original


def test_masking_keeps_anchors_aliases_numbers_and_every_other_byte():
    raw = '''# header\r
- id: x\r
  config:\r
    providers:\r
      other:\r
        apiKey: &shared anchored-private   # note\r
        backup: {token: *shared, retries: 3}\r
        password: !!str tagged-private\r
        pin: 123456\r
        cookie: ''\r
        authorization: "quoted-private"\r
        env: [FIRST_NAME, SECOND_NAME]\r
        environment: {PORT: 8080, NAME: value-private}\r
        last: end'''.encode('utf-8')
    masked = mask_profile(raw).decode('utf-8')
    assert masked == '''# header\r
- id: x\r
  config:\r
    providers:\r
      other:\r
        apiKey: &shared '***'   # note\r
        backup: {token: *shared, retries: 3}\r
        password: '***'\r
        pin: 123456\r
        cookie: ''\r
        authorization: '***'\r
        env: [FIRST_NAME, SECOND_NAME]\r
        environment: {PORT: 8080, NAME: '***'}\r
        last: end'''


def test_relay_root_options_reach_the_harness_catalog_filter(tmp_path, capsys):
    only_anthropic = {'base': 'https://example.invalid', 'provider_resource_name': 'anthropic-resource'}
    plain, shown = tmp_path / 'plain.json', tmp_path / 'shown.json'
    plain.write_text(json.dumps(only_anthropic))
    shown.write_text(json.dumps({**only_anthropic, 'show_unrouted_models': True}))
    code, summary, _, _ = client(tmp_path, capsys, relay=plain)
    assert code == 0 and summary['models'] == len(routable_entries(plain))
    code, wider, _, _ = client(tmp_path, capsys, relay=shown)
    assert code == 0 and wider['models'] == len(routable_entries(shown)) > summary['models']


HEAD = ['# header', '- id: llm-pi-ai', '  config:', '    providers:', '      retool:',
        "        displayName: 'Retool Relay'", '        reasoning: medium', '        models: []']


@pytest.mark.parametrize('newline,bom', [('\n', b''), ('\r\n', b''), ('\r\n', b'\xef\xbb\xbf')])
def test_last_provider_block_without_a_final_newline_is_spliced_and_that_ending_is_kept(newline, bom):
    raw = bom + newline.join(HEAD).encode('utf-8')
    yaml = YAML()
    yaml.preserve_quotes = True
    document = yaml.load(raw.decode('utf-8-sig'))
    entries = [models.resolve(name)[0] for name in ('gpt-6-sol', 'gpt-4.1')]
    encoded = encode_document(raw, document, update_document(document, entries))
    assert encoded.startswith(bom + b'# header') and not encoded.endswith(b'\n')
    text = encoded.decode('utf-8').removeprefix('\ufeff')
    assert text.count('\n') == (text.count('\r\n') if newline == '\r\n' else text.count('\n'))
    providers = yaml.load(text)[0]['config']['providers']
    assert [item['id'] for item in providers['retool']['models']] == ['gpt-6-sol']
    assert [item['id'] for item in providers['retool-text']['models']] == ['gpt-4.1']


def test_cli_updates_a_profile_whose_last_block_has_no_final_newline(tmp_path, capsys):
    raw = '\n'.join(HEAD).encode('utf-8')
    code, summary, error, profile = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'), raw=raw)
    assert code == 0 and summary['written'] is True, error
    saved = profile.read_bytes()
    assert not saved.endswith(b'\n') and b'retool-text:' in saved
    assert [path.read_bytes() for path in (tmp_path / 'backups').iterdir()] == [raw]


def test_cli_keeps_the_bom_and_crlf_of_a_profile(tmp_path, capsys):
    raw = b'\xef\xbb\xbf' + PROFILE.replace('\n', '\r\n').encode('utf-8')
    code, summary, error, profile = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'), raw=raw)
    assert code == 0 and summary['written'] is True, error
    saved = profile.read_bytes()
    assert saved.startswith(b'\xef\xbb\xbf#') and not saved.startswith(b'\xef\xbb\xbf\xef\xbb\xbf')
    assert saved.count(b'\n') == saved.count(b'\r\n') and b'retool-text:' in saved


def test_non_utf8_profile_is_reported_without_traceback(tmp_path, capsys):
    raw = b'- id: llm-pi-ai\n  name: \xe8\xea\xf3\n'
    code, summary, error, profile = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'), raw=raw)
    assert code == 1 and summary is None
    assert 'not valid UTF-8' in error and 'byte offset 24' in error and 'Traceback' not in error
    assert profile.read_bytes() == raw and not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('text,location', [
    ('- id: llm-pi-ai\n  config: {apiKey: private-value\n  other: [\n', 'line 3, column 8'),
    ('- id: llm-pi-ai\n  apiKey: first-private\n  apiKey: second-private\n', 'line 3, column 3'),
    ('- id: llm-pi-ai\n  note: "unterminated private-value\n', 'line 3, column 1')])
def test_invalid_yaml_is_reported_with_its_position_and_without_its_values(tmp_path, capsys, text, location):
    code, summary, error, profile = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'),
                                           raw=text.encode('utf-8'))
    assert code == 1 and summary is None and 'not valid YAML' in error and location in error
    for private in ('private-value', 'first-private', 'second-private'):
        assert private not in error
    assert 'Traceback' not in error and profile.read_text(encoding='utf-8') == text


def test_missing_profile_and_unexpected_structure_are_reported_cleanly(tmp_path, capsys):
    relay = tmp_path / 'relay.json'
    relay.write_text(json.dumps(RELAY))
    code = main(['--relay-config', str(relay), '--settings', str(tmp_path / 'absent.yml')])
    assert code == 1 and 'Harness profile not found' in capsys.readouterr().err
    code, summary, error, _ = client(tmp_path, capsys, raw=b'just: a mapping\n')
    assert code == 1 and 'Expected a Harness plugin patch list' in error and 'Traceback' not in error
