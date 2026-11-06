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


def section():
    text = readme()
    heading = text.index("### Состояние диалогов после перезапуска")
    following = text.find("\n### ", heading + 10)
    return text[heading:following if following >= 0 else len(text)]


def test_the_readme_says_what_the_code_does_after_review_3():
    text = section()
    for promise in (
            "Python ≥ 3.13", "SYSTEM", "Администраторам", "3.11–3.12", "наследуются от родительской папки",  # R3-07 c
            "считаются символы самого ключа", "2048 символов",  # d
            "чужие `*.state`", "не читает, не меняет и не удаляет",  # e
            "медиана 2,2 КБ", "среднее 5,9 КБ", "98 КБ", "1–2,3 МБ",  # f
            "от последней записи файла", "не от последнего касания кэша",  # g
            "считаются только\n  файлы `.state`".replace("\n  ", " "),  # h
            "при первом перезапуске", "со второго перезапуска",  # i
            "`prompt-pins/`", "переживает перезапуск", "`restored: true`", "`prompt.pin_dropped`",  # R3-02, fixed later
            "две новейшие ветки", "штамп блоков", "`unusable`", "`format`", "`corrupt`",
            "независимо от возраста", "thinking, подпись", "«Invalid request format»",
            "ждёт сессию", "`cache.effort_kept`"):
        assert " ".join(promise.split()) in " ".join(text.split()), promise


def test_the_limits_the_readme_names_are_the_ones_the_code_has():
    assert cs.MAX_KEY_CHARS == 2048 and cs.STORED_BRANCHES == 2 and cs.RESTORE_TIMEOUT == 15.0
    assert cs.IO_PAUSE == 30.0 and cs.IO_REMINDER == 600.0
    text = " ".join(section().split())
    for number in ("2048 символов", "не дольше 15 с", "паузу в 30 с", "раз в 10 минут"):
        assert number in text, number


def test_the_example_config_and_the_readme_agree_on_where_the_expiry_counts_from():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))["conversation_state_note"]
    assert "от последней записи файла" in example and "от последнего хода" not in example
    lines = readme().splitlines()
    ttl = next(line for line in lines if line.startswith("| conversation_state_max_conversations,"))
    assert "от последней записи файла" in ttl and "целое положительное" in ttl
    sizes = next(line for line in lines if line.startswith("| conversation_state_max_file_bytes,"))
    assert "считаются только файлы `.state`" in sizes and "целые положительные" in sizes


def test_the_reasons_of_the_journal_no_longer_blame_a_restart_for_a_missing_branch():
    import orchestrator
    assert "перезапуск" not in orchestrator._RESTART_REASONS["no_branch"]
    text = readme()
    line = " ".join(text[text.index("`no_branch` (у диалога нет ветки"):][:400].split())
    assert "перезапуск relay" not in line and "сам перезапуск ветку не теряет" in line


def test_the_review_is_in_the_design_document_too():
    document = (ROOT / "docs" / "conversation-state-2026-11-06.md").read_text(encoding="utf-8")
    for promise in ("Исправления по ревью №3", "R3-01", "R3-03", "R3-04", "R3-06", "R3-08", "R3-09", "R3-02 исправлено",
                    "`prompt-pins/`", "от 1 до 98 КБ", "OWNER RIGHTS", "load_stored", "`unusable`", "StoredSession"):
        assert promise in document, promise
    cost = document.split("Цена самого сохранения.")[1].split("## Стыки")[0]
    assert "была завышена" in cost and "1–2,3 МБ" in cost


def test_the_pins_of_the_prompts_file_live_where_the_docs_say_and_the_scanner_leaves_them_alone(tmp_path):
    import prompt_policy as pp
    from test_prompt_policy_common import document, inp, write_json
    assert (pp.PINS_DIR, pp.PINS_FILE, pp.REVISIONS_DIR) == ("prompt-pins", "pins.json", "revisions")
    # the scanner of the stored conversations reads only its own names, so the folder of the pins is not its business
    assert not cs._STATE_NAME.fullmatch(pp.PINS_DIR) and not cs._TEMP_NAME.fullmatch(pp.PINS_DIR)
    design = (ROOT / "docs" / "conversation-state-2026-11-06.md").read_text(encoding="utf-8")
    assert "`prompt-pins/`" in design and "сканер состояния её не читает и не удаляет" in design
    assert "закрепления ревизий `prompts.json` лежат в подпапке" in design
    text = " ".join(section().split())
    assert "`prompt-pins/`" in text and "`pins.json`" in text and "`revisions/<sha256>.json`" in text
    assert "Подпапку сканер состояния не читает и не удаляет" in text
    assert "пока включено `conversation_state`" in text
    # The pins are written beside the stored conversations when those are on, and nowhere when they are off.
    write_json(tmp_path / "prompts.json", document(profiles={"p": {"head": "{{client_system}}"}}, default_profile="p"))
    base = {"prompts_file": str(tmp_path / "prompts.json")}
    state = tmp_path / "state"
    store = pp.PromptStore({**base, "conversation_state": True, "conversation_state_dir": str(state)})
    store.plan(inp(conversation_key="conversation-1"))
    store.close()
    assert (state / pp.PINS_DIR / pp.PINS_FILE).is_file()
    assert list((state / pp.PINS_DIR / pp.REVISIONS_DIR).glob("*.json"))          # the literal document of the revision
    off = pp.PromptStore({**base, "conversation_state": False, "conversation_state_dir": str(tmp_path / "off")})
    off.plan(inp(conversation_key="conversation-1"))
    off.close()
    assert not (tmp_path / "off").exists()


def test_the_pin_events_the_readme_names_are_events_of_the_code():
    source = (ROOT / "prompt_policy.py").read_text(encoding="utf-8")
    text = section()
    for event in ("prompt.pins_loaded", "prompt.pins_saved", "prompt.pins_error", "prompt.pin_restored",
                  "prompt.pin_dropped", "prompt.revision_restored"):
        assert '"%s"' % event in source, event
        assert "`%s`" % event in text, event


def test_the_readme_advice_on_where_the_state_goes_names_options_that_exist():
    # Run from the folder of the repository without --state-dir, the stored conversations and the pins land in
    # <repo>/conversations (which .gitignore keeps out of git); the README says how to keep them elsewhere.
    text = " ".join(readme().split())
    assert "<репозиторий>/conversations/" in text and "scripts\\run_relay.ps1 -StateDir" in text
    assert "[string]$StateDir" in (ROOT / "scripts" / "run_relay.ps1").read_text(encoding="utf-8")
    assert '"--state-dir"' in (ROOT / "proxy.py").read_text(encoding="utf-8")
    assert "/conversations/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
