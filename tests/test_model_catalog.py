"""Dated provider contracts, route effort controls and client recommendations."""
import datetime

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


@pytest.mark.parametrize('model,output,retired', [
    ('claude-opus-4-20250514', 32000, '2026-06-15'), ('claude-opus-4-1-20250805', 32000, '2026-08-05'),
    ('claude-sonnet-4-20250514', 64000, '2026-06-15'),
])
def test_retired_legacy_route_keeps_explicit_provisional_limits_and_its_retirement_date(model, output, retired):
    entry, _ = models.resolve(model)
    assert (entry['context'], entry['max_output']) == (200000, output)
    assert entry['metadata_status'] == 'provisional'
    assert 'never verified on a route' in entry['metadata_notes']
    assert 'Retired by Anthropic on ' + retired in entry['metadata_notes']
    assert entry['retires_at'] == retired and entry['deprecated_at'] < retired


def test_glm_effort_is_accepted_but_not_published_because_the_route_ignores_it():
    entry, _ = models.resolve('zai-org/GLM-5.3')
    assert (entry['context'], entry['max_output']) == (1000000, 128000)
    assert entry['reasoning'] is True and entry['vision'] is True
    assert entry['reasoning_effort_levels'] == [] and entry['default_reasoning_effort'] is None
    assert entry['reasoning_effort_control'] == 'accepted-ignored'
    assert entry['reasoning_effort_checked_at'] == '2026-11-05'
    assert entry['metadata_status'] == 'partial'
    assert 'no consistent change' in entry['metadata_notes']


def test_catalog_recommendations_are_valid_and_dated_without_removing_legacy_ids():
    entries = models.catalog()
    assert len(entries) == len({entry['id'] for entry in entries}) == 40
    assert sum(entry['provider'] == 'openai' for entry in entries) == 17
    assert sum(entry['provider'] == 'anthropic' for entry in entries) == 15
    for entry in entries:
        levels = entry['reasoning_effort_levels']
        assert entry['default_reasoning_effort'] in levels if levels else entry['default_reasoning_effort'] is None
        assert entry['metadata_checked_at'] == ('2026-11-05' if entry['id'] in {'gpt-6.1-terra', 'gpt-4.1'} else '2026-10-29')
        assert all(source.startswith('https://') for source in entry['metadata_source'])
        assert entry['metadata_status'] in {'verified', 'partial', 'provisional'}
        if entry['metadata_status'] != 'verified':
            assert entry['metadata_notes']


@pytest.mark.parametrize('model,default', [
    ('gpt-6-astra', 'high'), ('claude-fable-5', 'high'), ('gpt-6-sol', 'medium'),
    ('claude-opus-5-5', 'medium'), ('gpt-5-mini', 'low'), ('gpt-5-nano', 'low'),
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


def test_small_fast_models_recommend_low_wherever_they_publish_levels():
    levels = ['low', 'medium', 'high', 'max']
    assert models._recommended_effort('zai-org/GLM-5.3-Flash', levels) == 'low'
    assert models._recommended_effort('gpt-5-nano', levels) == 'low'
    assert models._recommended_effort('zai-org/GLM-5.3', levels) == 'medium'
    assert models._recommended_effort('zai-org/GLM-5.3-Flash', []) is None


def test_route_checked_at_is_set_only_for_routes_that_were_probed():
    entries = models.catalog()
    unprobed = {entry['id'] for entry in entries if 'route_checked_at' not in entry}
    assert unprobed == {entry['id'] for entry in entries if entry['provider'] == 'google'}
    assert all(entry['route_status'] == 'unverified' for entry in entries if entry['id'] in unprobed)
    assert all(entry['route_checked_at'] == '2026-11-05' for entry in entries if entry['id'] not in unprobed)


def test_sonnet_45_carries_its_retirement_schedule_and_is_hidden_from_that_day(monkeypatch):
    entry = models.resolve('claude-sonnet-4-5-20250929')[0]
    assert (entry['deprecated_at'], entry['retires_at']) == ('2026-09-30', '2026-11-30')
    assert '2026-11-30' in entry['metadata_notes'] and '2026-11-30' in entry['route_reason']
    assert entry['route_status'] == 'verified' and entry['metadata_status'] == 'verified'
    cfg = {'provider_resource_name': '__managed__'}
    monkeypatch.setattr(models, '_today', lambda: datetime.date(2026, 11, 29))
    assert models.route_block_reason(entry, cfg) is None and models.catalog_routable(entry, cfg)
    monkeypatch.setattr(models, '_today', lambda: datetime.date(2026, 11, 30))
    assert 'Retired by Anthropic on 2026-11-30' in models.route_block_reason(entry, cfg)
    assert not models.catalog_routable(entry, cfg)


def test_codex_catalog_advertises_parallel_tool_calls_for_clients_that_still_require_the_field():
    native = models.codex_catalog()['models']
    assert native and all(item['supports_parallel_tool_calls'] is True for item in native)


def test_desktop_metadata_publishes_only_verified_route_input_limits():
    by_id = {entry['id']: entry for entry in models.catalog()}
    names = ('claude-opus-4-6', 'claude-sonnet-4-6', 'claude-opus-5-5', 'claude-sonnet-5-5',
             'claude-haiku-4-5-20251001')
    entries = [by_id[name] for name in names]
    meta = {entry['id']: models.desktop_metadata(entry, entries) for entry in entries}
    for name, limit in (('claude-opus-4-6', 872000), ('claude-sonnet-4-6', 936000), ('claude-opus-5-5', 872000)):
        assert meta[name]['max_input_tokens'] == limit and 'supports_1m' not in meta[name]
        assert by_id[name]['context'] == 1000000  # the public window stays under "context"
    for name in ('claude-sonnet-5-5', 'claude-haiku-4-5-20251001'):
        assert 'max_input_tokens' not in meta[name] and 'supports_1m' not in meta[name]


def test_supports_1m_follows_a_verified_route_limit_of_one_million():
    entry = dict(models.resolve('claude-opus-4-6')[0], route_input_tokens=1000000)
    assert models.desktop_metadata(entry, [entry])['supports_1m'] is True


@pytest.mark.parametrize('model', ['claude-haiku-4-5-20251001', 'zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'])
def test_effort_the_route_ignores_is_not_published_anywhere(model):
    entry = models.resolve(model)[0]
    assert entry['reasoning_effort_levels'] == [] and entry['default_reasoning_effort'] is None
    assert entry['reasoning_effort_control'] == 'accepted-ignored'
    assert entry['reasoning_effort_checked_at'] == '2026-11-05'
    native = models.codex_catalog([entry])['models'][0]
    assert native['supported_reasoning_levels'] == [] and native['default_reasoning_level'] is None


def test_haiku_is_listed_as_not_reasoning_because_the_route_never_returns_thinking():
    entry = models.resolve('claude-haiku-4-5-20251001')[0]
    assert entry['reasoning'] is False
    assert 'no thinking events' in entry['metadata_notes'] and entry['metadata_status'] == 'verified'


@pytest.mark.parametrize('model', ['gpt-5-mini', 'gpt-5-nano'])
def test_gpt5_mini_levels_that_measurably_change_the_answer_stay_selectable(model):
    entry = models.resolve(model)[0]
    assert entry['reasoning_effort_levels'] == ['low', 'medium', 'high']
    assert entry['reasoning_effort_control'] == 'selectable' and entry['default_reasoning_effort'] == 'low'
    assert entry['reasoning_effort_checked_at'] == '2026-11-05'
    assert 'measurably changes' in entry['metadata_notes'] and entry['metadata_status'] == 'partial'


@pytest.mark.parametrize('model', ['claude-haiku-4-5-20251001', 'zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'])
@pytest.mark.parametrize('effort', ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'HIGH'])
def test_ignored_effort_is_accepted_but_never_forwarded(model, effort):
    entry, forwarded = models.resolve(model, effort)
    assert forwarded is None
    assert models.resolve_tolerant(model, effort)[1] is None
    assert models.resolve_tolerant(model + '#' + effort.lower())[1] is None
    assert models.effective_effort(entry, configured='high') is None
    assert models.effective_effort(entry, configured='auto') is None


@pytest.mark.parametrize('model', ['claude-haiku-4-5-20251001', 'zai-org/GLM-5.3'])
@pytest.mark.parametrize('bogus', ['extra', 'ultra', '', 5])
def test_ignored_effort_still_rejects_values_no_provider_defines(model, bogus):
    with pytest.raises(models.ApiError) as caught:
        models.resolve(model, bogus)
    assert caught.value.status == 400 and 'known level' in caught.value.message
