"""docs/client-setup.md describes what the client sync scripts actually do; it drifted once."""
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / 'docs'


def section(heading, name='client-setup.md'):
    """One `## ` section as a single line of text, so a check does not depend on how the doc wraps."""
    text = (DOCS / name).read_text(encoding='utf-8')
    start = text.index('\n## %s\n' % heading) + 1
    end = text.find('\n## ', start)
    return ' '.join(text[start:end if end >= 0 else len(text)].split())


def test_the_desktop_section_names_the_row_fields_the_script_owns_and_where_overwrites_are_listed():
    from scripts.sync_claude_desktop_catalog import OWNED_FIELDS
    text = section('Claude Desktop on Windows')
    sentence = next(part for part in text.split('. ') if 'owns exactly' in part)
    assert all('`%s`' % field in sentence for field in OWNED_FIELDS)
    assert 'overwrites' in text and '`changed_fields`' in text
