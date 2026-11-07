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
    # No selectors (the route ignores effort), so GLM shares the provider whose SDK default is "off".
    assert catalog['zai-org/GLM-5.3-Flash']['reasoningEfforts'] is False
    assert any(model['id'] == 'zai-org/GLM-5.3-Flash' for model in text['models'])
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


def test_preview_masks_credentials_and_comments_but_keeps_credential_references(tmp_path, capsys):
    masked, full = tmp_path / 'masked.yml', tmp_path / 'full.yml'
    code, summary, _, profile = client(tmp_path, capsys, '--output', str(masked))
    assert code == 0 and summary['preview_redacted'] is True and summary['written'] is False
    original = profile.read_bytes()
    text = masked.read_text(encoding='utf-8')
    for private in ('other-private', 'header-private', 'plain-private', 'block-private', 'second-private-line',
                    'Keep profile header', 'Keep selected model'):
        assert private not in text
    for kept in ('apiKeyEnv: KEEP_OTHER_KEY', 'apiKeyEnv: KEEP_RELAY_KEY', 'maxTokens: 4096', 'enabled: true'):
        assert kept in text
    # A comment stays where it was, so a dropped or repeated one is still visible; only its words are gone.
    assert text.splitlines()[0] == '# ***' and text.count('# ***') == 2
    provider = YAML().load(text)[1]['config']['providers']['other']
    assert provider['apiKey'] == '***' and provider['token'] == '***'
    assert dict(provider['headers']) == {'Authorization': '***', 'X-Custom': '***'}
    # 27 routes: 23 with selectable levels; GPT-4.1, Haiku 4.5 and both GLM routes publish none.
    assert len(YAML().load(text)[1]['config']['providers']['retool']['models']) == 23
    assert len(YAML().load(text)[1]['config']['providers']['retool-text']['models']) == 4
    code, summary, _, _ = client(tmp_path, capsys, '--output', str(full), '--include-secrets')
    assert code == 0 and summary['preview_redacted'] is False
    revealed = YAML().load(full.read_text(encoding='utf-8'))[1]['config']['providers']['other']
    assert revealed['apiKey'] == 'other-private' and revealed['token'] == 'block-private\nsecond-private-line'
    assert '# Keep profile header' in full.read_text(encoding='utf-8')
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
    assert masked == '''# ***\r
- id: x\r
  config:\r
    providers:\r
      other:\r
        apiKey: &shared '***'   # ***\r
        backup: {token: *shared, retries: 3}\r
        password: '***'\r
        pin: 123456\r
        cookie: ''\r
        authorization: '***'\r
        env: [FIRST_NAME, SECOND_NAME]\r
        environment: {PORT: 8080, NAME: '***'}\r
        last: end'''


LEAKY_PROFILE = '''# old key: SECRET-IN-COMMENT
- id: llm-pi-ai   # trailing SECRET-AFTER-ID
  config:
    # before providers SECRET-BEFORE
    providers:
      a:
        apiKey: SECRET-PLAIN
        apiKeyEnv: MY_ENV_NAME
        headers: {Authorization: Bearer SECRET-FLOW, X-Other: SECRET-HDR2} # after flow SECRET-AFTERFLOW
        env: [A=SECRET-ENVLIST, KEEP_NAME, SECRET_KEY=SECRET-ENVSECRET]
        url: https://user:SECRET-URL@host.example/v1?token=SECRET-QUERY&limit=5
        command: [run, --api-key, SECRET-ARG, --token=SECRET-EQ, --port, '8080', --verbose]
        quoted: "https://:SECRET-QUOTED@host.example/x" # after quoted SECRET-AFTERQUOTED
        auth: {type: api, key: SECRET-AUTHKEY}
        words: run --password SECRET-INLINE --retries 3
        script: |
          fetch https://u:SECRET-BLOCKURL@h.example/p
          keep # this is not a comment
        header: |  # header comment SECRET-BLOCKHDR
          plain text
        folded: >-
          text # not a comment either
        fragment: http://host.example/#fragment
        hashes: "quoted # not a comment"
        glued: value#notacomment
        port: 8080
        enabled: true
      retool:
        displayName: 'Retool Relay'
        reasoning: medium
        models: []
# end SECRET-END'''


def test_masking_covers_comments_urls_secret_arguments_env_items_and_auth_keys():
    masked = mask_profile(LEAKY_PROFILE.encode('utf-8')).decode('utf-8')
    assert 'SECRET-' not in masked and 'SECRET_KEY=***' in masked
    lines = masked.splitlines()
    assert lines[0] == '# ***' and lines[1] == '- id: llm-pi-ai   # ***' and lines[3] == '    # ***'
    assert lines[-1] == '# ***' and '        header: | # ***' in lines
    # A block scalar keeps its style and indentation; only the secret inside it changes.
    assert '        script: |' in lines and '          fetch https://***@h.example/p' in lines
    assert '          keep # this is not a comment' in lines
    assert '        headers: {Authorization: \'***\', X-Other: \'***\'} # ***' in lines
    assert '        quoted: "https://***@host.example/x" # ***' in lines
    for kept in ('apiKeyEnv: MY_ENV_NAME', 'port: 8080', 'enabled: true', 'fragment: http://host.example/#fragment',
                 'hashes: "quoted # not a comment"', 'glued: value#notacomment', 'text # not a comment either',
                 'keep # this is not a comment', 'plain text'):
        assert kept in masked
    providers = YAML().load(masked)[0]['config']['providers']
    shown = providers['a']
    assert shown['apiKey'] == '***' and dict(shown['auth']) == {'type': 'api', 'key': '***'}
    assert list(shown['env']) == ['A=***', 'KEEP_NAME', 'SECRET_KEY=***']
    assert shown['url'] == 'https://***@host.example/v1?token=***&limit=5'
    assert list(shown['command']) == ['run', '--api-key', '***', '--token=***', '--port', '8080', '--verbose']
    assert shown['words'] == 'run --password *** --retries 3'
    assert shown['script'] == 'fetch https://***@h.example/p\nkeep # this is not a comment\n'
    assert providers['retool']['displayName'] == 'Retool Relay'


def test_masking_a_profile_with_a_bom_and_crlf_keeps_both_and_masks_the_comments():
    raw = b'\xef\xbb\xbf' + LEAKY_PROFILE.replace('\n', '\r\n').encode('utf-8')
    masked = mask_profile(raw)
    assert masked.startswith(b'\xef\xbb\xbf# ***\r\n') and b'SECRET-' not in masked
    assert masked.count(b'\n') == masked.count(b'\r\n') and masked.endswith(b'# ***')


def test_a_comment_only_file_and_a_comment_without_a_line_end_are_masked():
    assert mask_profile(b'# SECRET only\n# SECRET again') == b'# ***\n# ***'
    assert mask_profile(b'- a # SECRET') == b'- a # ***'


def test_a_masked_preview_masked_again_does_not_change():
    once = mask_profile(LEAKY_PROFILE.encode('utf-8'))
    assert mask_profile(once) == once


def test_the_preview_of_a_profile_masks_everything_but_the_written_profile_keeps_it(tmp_path, capsys):
    leaky = LEAKY_PROFILE.replace('      retool:\n        displayName', "      retool:\n        apiKeyEnv: KEEP_RELAY_KEY\n        displayName")
    code, summary, error, profile = client(tmp_path, capsys, '--output', str(tmp_path / 'masked.yml'),
                                           text=leaky + '\n')
    assert code == 0, error
    assert 'SECRET-' not in (tmp_path / 'masked.yml').read_text(encoding='utf-8')
    code, summary, error, _ = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'))
    assert code == 0 and summary['written'] is True, error
    saved = profile.read_text(encoding='utf-8')
    assert 'SECRET-IN-COMMENT' in saved and 'SECRET-PLAIN' in saved and 'SECRET-URL' in saved


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


def test_a_model_that_moves_between_the_two_groups_keeps_its_own_fields():
    # the layout before the split: every model in retool, GLM with fields of its own
    before = source()
    relay = before[1]['config']['providers']['retool']
    relay['models'].append({'id': 'zai-org/GLM-5.3-Flash', 'compat': {'thinkingFormat': 'zai'}, 'cost': {'input': 1}})
    relay['models'].append({'id': 'gpt-4.1', 'cost': {'input': 2}})
    entries = [models.resolve(name)[0] for name in ('gpt-6-sol', 'gpt-4.1', 'zai-org/GLM-5.3-Flash')]
    result = update_document(before, entries)
    text = {model['id']: model for model in result[1]['config']['providers']['retool-text']['models']}
    assert text['zai-org/GLM-5.3-Flash']['compat'] == {'thinkingFormat': 'zai'}
    assert text['zai-org/GLM-5.3-Flash']['cost'] == {'input': 1} and text['gpt-4.1']['cost'] == {'input': 2}


def indented(width):
    """A profile whose mappings are indented by `width` spaces, the way PyYAML writes it with indent=width."""
    pad = ' ' * width
    return '\n'.join(['# header', '-' + pad[1:] + 'id: llm-pi-ai', pad + 'config:', pad * 2 + 'providers:',
                      pad * 3 + 'other:', pad * 4 + 'apiKeyEnv: KEEP_OTHER_KEY',
                      pad * 3 + 'retool:', pad * 4 + "displayName: 'Retool Relay'", pad * 4 + 'apiKeyEnv: KEEP_RELAY_KEY',
                      pad * 4 + 'reasoning: medium', pad * 4 + 'models: []',
                      pad * 3 + 'tail:', pad * 4 + 'enabled: true', ''])


@pytest.mark.parametrize('width', [2, 3, 4])
def test_retool_text_is_added_at_the_indentation_of_the_profile_itself(tmp_path, capsys, width):
    # The new key was written two columns left of the retool body whatever the profile's indentation, so a profile
    # indented by three or four spaces failed every first sync ('not valid YAML') and needed retool-text by hand.
    code, summary, error, profile = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'),
                                           text=indented(width))
    assert code == 0 and summary['written'] is True, error
    lines = profile.read_text(encoding='utf-8').splitlines()
    pad = ' ' * width
    group = lines.index(pad * 3 + 'retool-text:')
    assert lines[group + 1].startswith(pad * 4 + 'displayName: ') and lines[-2:] == [pad * 3 + 'tail:',
                                                                                       pad * 4 + 'enabled: true']
    providers = YAML().load('\n'.join(lines))[0]['config']['providers']
    assert len(providers['retool']['models']) == 23 and len(providers['retool-text']['models']) == 4
    assert providers['other'] == {'apiKeyEnv': 'KEEP_OTHER_KEY'} and providers['tail'] == {'enabled': True}


@pytest.mark.parametrize('mark', ['&relay', '!!map'])
def test_an_anchor_or_a_tag_on_the_provider_key_line_is_kept_and_the_block_below_it_is_synced(tmp_path, capsys, mark):
    # The node of `retool: &relay` starts at its anchor, on the key line, so the splice replaced the key line too and
    # wrote the block at the anchor's column: every sync of such a profile failed, even with no retool-text to add.
    text = PROFILE.replace('      retool:\n', '      retool: %s\n' % mark)
    code, summary, error, profile = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'),
                                           text=text)
    assert code == 0 and summary['written'] is True, error
    saved = profile.read_text(encoding='utf-8')
    assert saved[:saved.index('        displayName: ')] == text[:text.index('        displayName: ')]
    providers = YAML().load(saved)[1]['config']['providers']
    assert len(providers['retool']['models']) == 23 and len(providers['retool-text']['models']) == 4
    yaml = YAML()
    yaml.preserve_quotes = True
    document = yaml.load(text)
    reasoning_only = encode_document(text.encode('utf-8'), document,
                                     update_document(document, [models.resolve('gpt-6-sol')[0]])).decode('utf-8')
    assert '      retool: %s\n        displayName: ' % mark in reasoning_only and 'retool-text' not in reasoning_only
    assert [item['id'] for item in YAML().load(reasoning_only)[1]['config']['providers']['retool']['models']] == [
        'gpt-6-sol']


@pytest.mark.parametrize('replace', [
    {'      retool:\n': '      retool: &relay\n', '      tail:\n        enabled: true\n': '      tail: *relay\n'},
    {'      retool:\n': '      retool: &relay\n', '      tail:\n': '      tail:\n        <<: *relay\n'},
    {'      other:\n': '      other: &base\n', "      retool:\n        displayName: 'Retool Relay'\n"
     '        baseURL: http://127.0.0.1:8787/v1\n        apiKeyEnv: KEEP_RELAY_KEY\n        reasoning: medium\n'
     '        models: []\n': '      retool: *base\n'},
], ids=['aliased by another provider', 'merged into another provider', 'an alias of another provider'])
def test_a_provider_block_shared_through_an_alias_is_refused_with_a_message_that_says_so(tmp_path, capsys, replace):
    # Splicing a shared block changes the other key too; that is refused, now with a message that names the layout
    # instead of 'not valid YAML' about a position in the generated text.
    text = PROFILE
    for old, new in replace.items():
        assert old in text
        text = text.replace(old, new)
    code, summary, error, profile = client(tmp_path, capsys, '--write', '--backup-dir', str(tmp_path / 'backups'),
                                           text=text)
    assert code == 1 and summary is None
    assert 'shares its block with another key through a YAML anchor and alias' in error
    assert profile.read_text(encoding='utf-8') == text and not (tmp_path / 'backups').exists()
