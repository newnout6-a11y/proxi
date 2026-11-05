"""Harness catalog refreshes preserve provider selection and unrelated settings."""
import copy
from io import StringIO

import pytest
from ruamel.yaml import YAML

import models
from scripts.sync_harness_catalog import encode_document, main, update_document


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
