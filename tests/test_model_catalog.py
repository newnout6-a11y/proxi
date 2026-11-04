"""Dated provider contracts, route effort controls and client recommendations."""
import pytest

import models


@pytest.mark.parametrize('model', [
    'gpt-5', 'gpt-5-mini', 'gpt-5-nano', 'gpt-5.1', 'gpt-5.2', 'gpt-5.3-codex',
])
def test_older_gpt_whole_context_is_not_the_copilot_input_reservation(model):
    entry, _ = models.resolve(model)
    assert entry['context'] == 400000
    assert entry['max_output'] == 128000


@pytest.mark.parametrize('model', [
    'gpt-5.4', 'gpt-5.5', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna',
    'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna', 'gpt-6.1-sol',
])
def test_newer_gpt_full_window_matches_current_retool_registry(model):
    entry, _ = models.resolve(model)
    assert entry['context'] == 1050000
    assert entry['max_output'] == 128000


@pytest.mark.parametrize('model', [
    'gpt-5.3-codex', 'gpt-5.4', 'gpt-5.5', 'gpt-5.6-sol',
    'gpt-5.6-terra', 'gpt-5.6-luna',
])
def test_route_levels_are_narrower_than_provider_wide_capabilities(model):
    assert models.resolve(model)[0]['reasoning_effort_levels'] == ['low', 'medium', 'high']


@pytest.mark.parametrize('model', ['claude-opus-4-5-20251101',
                                  'claude-sonnet-4-5-20250929',
                                  'claude-haiku-4-5-20251001'])
def test_verified_legacy_claude_has_its_own_limits(model):
    entry, _ = models.resolve(model)
    assert (entry['context'], entry['max_output']) == (200000, 64000)
    assert entry['metadata_status'] == 'verified'


@pytest.mark.parametrize('model,output', [
    ('claude-opus-4-20250514', 32000), ('claude-opus-4-1-20250805', 32000),
    ('claude-sonnet-4-20250514', 64000),
])
def test_unverified_legacy_route_uses_explicit_provisional_limits(model, output):
    entry, _ = models.resolve(model)
    assert (entry['context'], entry['max_output']) == (200000, output)
    assert entry['metadata_status'] == 'provisional'
    assert 'pending route verification' in entry['metadata_notes']


def test_glm_reasoning_capability_uses_live_verified_retool_route_efforts():
    entry, _ = models.resolve('zai-org/GLM-5.3')
    assert (entry['context'], entry['max_output']) == (1000000, 128000)
    assert entry['reasoning'] is True and entry['vision'] is True
    assert entry['reasoning_effort_levels'] == ['low', 'medium', 'high', 'max']
    assert entry['default_reasoning_effort'] == 'medium'
    assert entry['reasoning_effort_control'] == 'selectable'
    assert entry['reasoning_effort_checked_at'] == '2026-11-04'
    assert entry['metadata_status'] == 'partial'


def test_catalog_recommendations_are_valid_and_dated_without_removing_legacy_ids():
    entries = models.catalog()
    assert len(entries) == len({entry['id'] for entry in entries}) == 38
    assert sum(entry['provider'] == 'openai' for entry in entries) == 15
    assert sum(entry['provider'] == 'anthropic' for entry in entries) == 15
    for entry in entries:
        levels = entry['reasoning_effort_levels']
        assert entry['default_reasoning_effort'] in levels if levels else entry['default_reasoning_effort'] is None
        assert entry['metadata_checked_at'] == '2026-10-29'
        assert all(source.startswith('https://') for source in entry['metadata_source'])
        assert entry['metadata_status'] in {'verified', 'partial', 'provisional'}
        if entry['metadata_status'] != 'verified':
            assert entry['metadata_notes']


@pytest.mark.parametrize('model,default', [
    ('gpt-6-astra', 'high'), ('claude-fable-5', 'high'), ('gpt-6-sol', 'medium'),
    ('claude-opus-5-5', 'medium'), ('gpt-5-mini', 'low'),
    ('claude-haiku-4-5-20251001', 'low'),
])
def test_codex_uses_the_same_recommended_default_as_the_relay(model, default):
    entry, _ = models.resolve(model)
    assert entry['default_reasoning_effort'] == default
    assert models.codex_catalog([entry])['models'][0]['default_reasoning_level'] == default


def test_unrouted_unverified_models_do_not_acquire_verified_metadata_by_family():
    entry, _ = models.resolve('zai-org/GLM-5.3-Flash')
    assert entry['metadata_status'] == 'provisional'
    assert 'not stress-tested' in entry['metadata_notes']
    assert models.resolve('gemini-2.5-pro')[0]['metadata_status'] == 'provisional'
    assert models.resolve('gpt-5-mini')[0]['metadata_status'] == 'partial'


def test_codex_rejects_a_stale_recommendation_without_advertising_an_invalid_level():
    entry = dict(models.resolve('gpt-5.5')[0], default_reasoning_effort='max')
    native = models.codex_catalog([entry])['models'][0]
    assert native['default_reasoning_level'] == 'medium'
    assert 'max' not in [level['effort'] for level in native['supported_reasoning_levels']]
