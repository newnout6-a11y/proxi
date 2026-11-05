"""docs/client-setup.md describes what the client sync scripts actually do; it drifted once."""
from pathlib import Path
import re

import models

DOCS = Path(__file__).resolve().parents[1] / 'docs'
ALL_MANAGED = {key: '__managed__' for key in models.PROVIDER_RESOURCE_KEY.values() if key != 'google_provider_resource_name'}
TEXT_GROUP = {'gpt-4.1': 'GPT-4.1', 'claude-haiku-4-5-20251001': 'Haiku 4.5', 'zai-org/GLM-5.3': 'GLM-5.3',
              'zai-org/GLM-5.3-Flash': 'GLM-5.3 Flash'}


def flat(text):
    """Text as a single line, without blockquote markers, so a check does not depend on how the doc wraps."""
    return ' '.join(re.sub(r'(?m)^> ?', '', text).split())


def section(heading, name='client-setup.md'):
    """One `## ` section as a single line of text."""
    text = (DOCS / name).read_text(encoding='utf-8')
    start = text.index('\n## %s\n' % heading) + 1
    end = text.find('\n## ', start)
    return flat(text[start:end if end >= 0 else len(text)])


def harness_split():
    """(published routes, retool models, retool-text models, retool-text provider) after a Harness sync."""
    from scripts.sync_harness_catalog import update_document
    entries = [entry for entry in models.catalog() if models.catalog_routable(entry, ALL_MANAGED)]
    profile = [{'id': 'llm-pi-ai', 'config': {'providers': {'retool': {
        'displayName': 'Retool Relay', 'compat': {'thinkingFormat': 'deepseek'}, 'models': []}}}}]
    providers = update_document(profile, entries)[0]['config']['providers']
    return entries, providers['retool']['models'], providers['retool-text']['models'], providers['retool-text']


def test_the_desktop_section_names_the_row_fields_the_script_owns_and_where_overwrites_are_listed():
    from scripts.sync_claude_desktop_catalog import OWNED_FIELDS
    text = section('Claude Desktop on Windows')
    sentence = next(part for part in text.split('. ') if 'owns exactly' in part)
    assert all('`%s`' % field in sentence for field in OWNED_FIELDS)
    assert 'overwrites' in text and '`changed_fields`' in text


def test_the_opencode_section_says_compaction_follows_the_main_models_window():
    text = section('OpenCode')
    assert 'to a larger-window model yourself' not in text  # the old advice: the script now picks the window
    assert 'whose window is at least the main `model`' in text and 'issue #46164' in text
    assert "compacts with the session's model" in text and '`warnings`' in text


def test_the_harness_section_states_the_split_a_sync_produces_and_what_retool_text_means():
    entries, retool, text_group, provider = harness_split()
    assert {model['id'] for model in text_group} == set(TEXT_GROUP)
    text = section('DeepSeek Harness')
    assert 'the same %d verified IDs' % len(entries) in text
    assert '`retool` group holds %d models and `retool-text` holds %d' % (len(retool), len(text_group)) in text
    assert all(name in text for name in TEXT_GROUP.values())
    # The name says "no level selector", not "does not think": GLM reasons on every request and Harness shows it.
    assert provider['compat'] == {'thinkingFormat': 'deepseek'}
    assert 'no reasoning-level selector' in text and 'GLM-5.3 and GLM-5.3 Flash reason on every request' in text
    assert 'thinkingFormat: deepseek' in text and 'not renamed' in text


def test_the_audit_corrects_the_harness_split_it_recorded():
    entries, retool, text_group, provider = harness_split()
    audit = flat((DOCS / 'model-client-audit-2026-11-04.md').read_text(encoding='utf-8'))
    recorded = audit.index('26 reasoning models in `retool`')
    correction = audit.index('so a sync now puts %d models in `retool` and %d in `retool-text`' % (
        len(retool), len(text_group)))
    assert recorded < correction and audit.rfind('**Correction', recorded, correction) > recorded
