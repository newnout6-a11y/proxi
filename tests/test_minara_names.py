"""A Minara name is served by Minara or refused: it never becomes a Retool model, and client catalog syncs keep it."""
import pytest

import apierr
import minara_catalog
import models
from scripts import sync_claude_desktop_catalog as desktop
from scripts import sync_opencode_catalog as opencode

MEASURED = {
    'openai/gpt-6-luna': {'id': 'minara/gpt-6-luna', 'upstream_model': 'openai/gpt-6-luna', 'label': 'GPT 6 Luna',
                          'provider': 'openai', 'status': 'ok', 'tools': True, 'vision': True, 'reasoning': True,
                          'reasoning_effort_levels': ['low', 'high'], 'context': 400000, 'max_output': 128000},
    'openai/gpt-6.1-sol': {'id': 'minara/gpt-6.1-sol', 'upstream_model': 'openai/gpt-6.1-sol', 'label': 'GPT 6.1 Sol',
                           'provider': 'openai', 'status': 'ok', 'tools': True, 'vision': True, 'reasoning': True,
                           'reasoning_effort_levels': ['low', 'high'], 'context': 400000, 'max_output': 128000},
    'anthropic/claude-opus-5.5': {'id': 'minara/claude-opus-5-5', 'upstream_model': 'anthropic/claude-opus-5.5',
                                  'label': 'Claude Opus 5.5', 'provider': 'anthropic', 'status': 'ok', 'tools': True,
                                  'vision': True, 'reasoning': True, 'reasoning_effort_levels': ['max'],
                                  'context': 1000000, 'max_output': 128000},
    'anthropic/claude-haiku-5.5': {'id': 'minara/claude-haiku-5-5', 'upstream_model': 'anthropic/claude-haiku-5.5',
                                   'label': 'Claude Haiku 5.5', 'provider': 'anthropic', 'status': 'ok', 'tools': True,
                                   'vision': True, 'reasoning': True, 'reasoning_effort_levels': [],
                                   'context': 200000, 'max_output': 64000},
}


@pytest.fixture
def routes(monkeypatch):
    monkeypatch.setattr(minara_catalog, 'MEASURED', MEASURED)
    built = models.build_minara_catalog([{'upstream': 'minara', 'bearer_token': 't'}])
    return {'_minara_models': built}


def test_a_minara_model_whose_retool_namesake_has_a_substitute_stays_on_minara(routes):
    assert models.substitute_for('gpt-6-luna') == 'gpt-6.1-terra'  # Retool's own model still has its substitute
    assert models.substitute_for('minara/gpt-6-luna') is None
    entry, level = models.resolve_tolerant('minara/gpt-6-luna#high', cfg=routes)
    assert entry['id'] == 'minara/gpt-6-luna' and entry['upstream'] == 'minara' and level == 'high'


def test_a_mis_cased_minara_name_is_minaras_or_refused_never_retools(routes):
    assert models.resolve('Minara/gpt-6.1-sol', cfg=routes)[0]['upstream'] == 'minara'
    with pytest.raises(apierr.ApiError) as error:
        models.resolve('MINARA/gpt-6.1-sol', cfg={'_minara_models': {}})
    assert error.value.status == 404


def opencode_entries(routes):
    return [entry for entry in models.catalog(routes) if models.catalog_routable(entry, routes)
            or entry.get('upstream') == 'minara']


def test_the_opencode_sync_keeps_every_reference_to_a_minara_model(routes):
    document = {'provider': {'retool-relay': {'models': {}}}, 'model': 'retool-relay/minara/claude-opus-5-5',
                'small_model': 'retool-relay/minara/gpt-6.1-sol',
                'agent': {'plan': {'model': 'retool-relay/minara/gpt-6.1-sol#high'},
                          'compaction': {'model': 'retool-relay/minara/claude-haiku-5-5'}}}
    warnings = []
    result = opencode.update_document(document, list(routes['_minara_models'].values())
                                      + [models._BY_ID['claude-opus-5-5']], warnings=warnings)
    assert result['model'] == 'retool-relay/minara/claude-opus-5-5'
    assert result['small_model'] == 'retool-relay/minara/gpt-6.1-sol'
    assert result['agent']['plan']['model'] == 'retool-relay/minara/gpt-6.1-sol#high'
    assert result['agent']['compaction']['model'] == 'retool-relay/minara/claude-haiku-5-5'
    assert not [warning for warning in warnings if 'no longer published' in warning]


def test_claude_desktop_takes_a_minara_default_and_gives_minara_claude_its_family(routes):
    entries = [models._BY_ID['claude-opus-5-5'], *routes['_minara_models'].values()]
    rows = desktop.build_catalog(entries, [{'name': 'claude-opus-5-5'}], default_model='minara/claude-opus-5-5',
                                 include_unlisted=True)
    names = [row['name'] if isinstance(row, dict) else row for row in rows]
    assert names[0] == 'minara/claude-opus-5-5' and 'claude-opus-5-5' in names
    meta = models.desktop_metadata(routes['_minara_models']['minara/claude-opus-5-5'], entries)
    assert meta['anthropic_family_tier'] == 'opus' and meta['is_family_default'] is False  # Retool's row is the default
    assert models.desktop_metadata(models._BY_ID['claude-opus-5-5'], entries)['is_family_default'] is True
    alone = models.desktop_metadata(routes['_minara_models']['minara/claude-opus-5-5'], entries[1:])
    assert alone['is_family_default'] is True  # a Minara-only relay: its own row is the family's default
