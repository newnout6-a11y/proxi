"""The documentation says what the code does for stored conversations."""
import json

import config
import conversation_state as cs
from test_docs_consistency import ROOT

SETTINGS = ("conversation_state", "conversation_state_dir", "conversation_state_max_conversations",
            "conversation_state_ttl", "conversation_state_max_file_bytes", "conversation_state_max_total_bytes")
EVENTS = ("saved", "save_failed", "restored", "skipped", "dropped", "scan", "disabled", "restore_failed", "load_failed")
SAVE_FAILURES = ("io_error", "too_large", "unserializable", "capture", "internal")
SKIPS = ("account_missing", "account_disabled", "account_unavailable", "no_route", "account_changed")
DROPS = (*cs._REASONS, "rejected")


def readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_every_setting_is_in_the_example_config_and_in_the_readme_table():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    text = readme()
    for key in SETTINGS:
        assert example[key] == config.DEFAULTS[key], key
        assert key in text, key
    assert config.DEFAULTS["conversation_state"] is True  # on unless the owner switches it off


def test_every_event_and_reason_the_store_reports_is_in_the_readme():
    text = readme()
    for name in EVENTS:
        assert "`.%s`" % name in text or "`conversation_state.%s`" % name in text, name
    for reason in (*SAVE_FAILURES, *SKIPS, *DROPS):
        assert "`%s`" % reason in text, reason


def test_the_stored_files_stay_out_of_git_and_the_privacy_note_is_next_to_the_traces():
    assert "/conversations/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    text = readme()
    heading = text.index("### Состояние диалогов после перезапуска")
    # Next to the journal and the rejected-body snapshots, and the traces paragraph points back at it.
    assert text.index("Журнал (`journal.py`)") < text.index("`upstream_error_max_dumps` (50") < heading
    assert "`<state-dir>/conversations/`" in text[text.index("Отключите оба поля"):]
    section = text[heading:]
    section = section[:section.index("\n### ", 10)] if "\n### " in section[10:] else section
    for promise in ("не шифруются", "не попадает", "0600", "docs/conversation-state-2026-11-06.md"):
        assert promise in section, promise
    assert (ROOT / "docs" / "conversation-state-2026-11-06.md").is_file()
