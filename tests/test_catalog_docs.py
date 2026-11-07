"""The audit documents describe the registry the code actually has; they drifted once."""
import datetime
from pathlib import Path
import re

import pytest

import models

DOCS = Path(__file__).resolve().parents[1] / 'docs'
ALL_MANAGED = {key: '__managed__' for key in models.PROVIDER_RESOURCE_KEY.values() if key != 'google_provider_resource_name'}

_EXCEPT = {'claude-fable-5', 'claude-haiku-4-5-20251001'}
GROUPS = {
    'GPT-5': ['gpt-5'],
    'GPT-5 mini/nano': ['gpt-5-mini', 'gpt-5-nano'],
    'GPT-5.1/5.2': ['gpt-5.1', 'gpt-5.2'],
    'GPT-5.3 Codex/5.4/5.5/5.6 variants': ['gpt-5.3-codex', 'gpt-5.4', 'gpt-5.5', 'gpt-5.6-sol', 'gpt-5.6-terra',
                                           'gpt-5.6-luna'],
    'GPT-6 Astra': ['gpt-6-astra'],
    'GPT-6.1 Sol': ['gpt-6.1-sol'],
    'GPT-6 Sol/Luna, GPT-6.1 Terra': ['gpt-6-sol', 'gpt-6-luna', 'gpt-6.1-terra'],
    'GPT-4.1': ['gpt-4.1'],
    'Claude except Fable/Haiku': [entry['id'] for entry in models.catalog()
                                  if entry['provider'] == 'anthropic' and entry['id'] not in _EXCEPT],
    'Claude Fable 5': ['claude-fable-5'],
    'Claude Haiku 4.5': ['claude-haiku-4-5-20251001'],
    'Gemini Flash and Flash-Lite (route never probed)': ['gemini-3-flash-preview', 'gemini-3.5-flash',
                                                         'gemini-3.1-flash-lite-preview', 'gemini-2.5-flash'],
    'Gemini Pro (route never probed)': ['gemini-3.1-pro-preview', 'gemini-2.5-pro'],
    'GLM-5.3 and GLM-5.3 Flash': ['zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'],
}


def cells(line):
    return [cell.strip() for cell in line.strip().strip('|').split('|')]


def effort_rows():
    lines = (DOCS / 'reports' / 'model-catalog-audit.md').read_text(encoding='utf-8').splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith('## Effort controls'))
    rows = []
    for line in lines[start:]:
        if line.startswith('| ') and not line.startswith('| Model group') and not line.startswith('| ---'):
            rows.append(cells(line))
        elif rows and not line.startswith('|'):
            break
    return rows


def test_effort_table_covers_every_model_exactly_once():
    assert {row[0] for row in effort_rows()} == set(GROUPS)
    listed = [name for names in GROUPS.values() for name in names]
    assert sorted(listed) == sorted(entry['id'] for entry in models.catalog())


@pytest.mark.parametrize('group', sorted(GROUPS))
def test_effort_table_row_matches_the_registry(group):
    row = next(row for row in effort_rows() if row[0] == group)
    levels = set() if row[1].startswith('—') else set(row[1].split(', '))
    default = None if row[2].startswith('—') else row[2].split(' ')[0]
    for name in GROUPS[group]:
        entry = models.resolve(name)[0]
        assert set(entry['reasoning_effort_levels']) == levels, name
        assert entry['default_reasoning_effort'] == default, name
        assert ('accepted, ignored' in row[1]) == (entry['reasoning_effort_control'] == 'accepted-ignored'), name


def route_rows():
    rows = {}
    for line in (DOCS / 'reports' / 'model-route-availability-2026-11-05.md').read_text(encoding='utf-8').splitlines():
        match = re.match(r'\| (\S+) \| (.*) \| (публикуется|скрыт)[^|]*\|$', line)
        if match:
            rows[match.group(1)] = match.group(3)
    return rows


def test_route_table_lists_the_registry_in_order_and_publishes_what_the_code_publishes(monkeypatch):
    monkeypatch.setattr(models, '_today', lambda: datetime.date(2026, 11, 5))
    rows = route_rows()
    catalog = models.catalog()
    assert list(rows) == [entry['id'] for entry in catalog]
    published = {entry['id'] for entry in catalog if models.catalog_routable(entry, ALL_MANAGED)}
    assert {name for name, state in rows.items() if state == 'публикуется'} == published
    assert len(published) == 27


def test_route_table_states_the_retirement_of_the_models_anthropic_retired():
    text = (DOCS / 'reports' / 'model-route-availability-2026-11-05.md').read_text(encoding='utf-8')
    for name, date in (('claude-opus-4-20250514', '2026-06-15'), ('claude-opus-4-1-20250805', '2026-08-05'),
                       ('claude-sonnet-4-20250514', '2026-06-15')):
        row = next(line for line in text.splitlines() if line.startswith('| %s |' % name))
        assert date in row and 'скрыт' in row
        assert models.resolve(name)[0]['retires_at'] == date
    assert '2026-11-30' in next(line for line in text.splitlines()
                                if line.startswith('| claude-sonnet-4-5-20250929 |'))


def test_catalog_counts_in_the_audit_header_match_the_registry():
    text = ' '.join((DOCS / 'reports' / 'model-catalog-audit.md').read_text(encoding='utf-8').split())
    entries = models.catalog()
    counts = {provider: sum(entry['provider'] == provider for entry in entries)
              for provider in ('openai', 'anthropic', 'google', 'retool-fast')}
    assert 'retains 40 Retool model IDs: 17 OpenAI, 15 Anthropic, 6 Google and 2 Retool-fast' in text
    assert (len(entries), counts) == (40, {'openai': 17, 'anthropic': 15, 'google': 6, 'retool-fast': 2})
