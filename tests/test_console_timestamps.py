"""Relay console timestamps remain truthful in a terminal, redirected output and replay."""
import datetime
import asyncio
import io
import re
import sys
from types import SimpleNamespace

import console_log
import journal
import orchestrator


STAMP = "[2026-11-08 13:14:15.123 +03:00] "


def test_timestamp_has_local_date_milliseconds_and_explicit_offset():
    value = console_log.timestamp()
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}\] ", value)
    parsed = datetime.datetime.strptime(value, "[%Y-%m-%d %H:%M:%S.%f %z] ")
    assert parsed.tzinfo is not None


def test_write_stamps_each_physical_line_in_plain_output(monkeypatch):
    monkeypatch.setattr(console_log, "timestamp", lambda: STAMP)
    monkeypatch.setenv("NO_COLOR", "1")
    stream = io.StringIO()
    console_log.write("first\nsecond\r\nthird", file=stream)
    assert stream.getvalue().splitlines() == [STAMP + line for line in ("first", "second", "third")]


def test_replay_keeps_original_timestamp_and_does_not_date_legacy_lines(monkeypatch):
    monkeypatch.setattr(console_log, "timestamp", lambda: "[2026-11-09 00:00:00.000 +03:00] ")
    monkeypatch.setenv("NO_COLOR", "1")
    stream = io.StringIO()
    console_log.write(STAMP + "original", file=stream, with_time=False)
    console_log.write("legacy", file=stream, with_time=False)
    assert stream.getvalue().splitlines() == [STAMP + "original", "legacy"]


def test_new_message_cannot_forge_timestamp_on_any_payload_line(monkeypatch):
    monkeypatch.setattr(console_log, "timestamp", lambda: STAMP)
    monkeypatch.setenv("NO_COLOR", "1")
    stream = io.StringIO()
    old = "[2000-01-01 00:00:00.000 +00:00] "
    console_log.write(old + "first\n" + old + "second", file=stream)
    assert stream.getvalue().splitlines() == [STAMP + old + part for part in ("first", "second")]


def test_stamped_error_retains_error_coloring():
    rendered = console_log.render(STAMP + "[err] failed", color=True)
    assert rendered == STAMP + "\x1b[1;91m[err] failed\x1b[0m"


def test_write_still_redacts_multiline_output(monkeypatch):
    monkeypatch.setattr(console_log, "timestamp", lambda: STAMP)
    monkeypatch.setenv("NO_COLOR", "1")
    stream = io.StringIO()
    console_log.write("api_key=synthetic-secret-value\npassword=synthetic-password", file=stream)
    assert "synthetic-secret-value" not in stream.getvalue()
    assert "synthetic-password" not in stream.getvalue()
    assert all(line.startswith(STAMP) for line in stream.getvalue().splitlines())


def test_whole_multiline_literal_secret_is_masked_before_lines_are_stamped(monkeypatch):
    instance = journal.Journal()
    instance.configure({"relay_api_key": "first-half\nsecond-half"})
    monkeypatch.setattr(journal, "JOURNAL", instance)
    stream = io.StringIO()
    console_log.write("key: first-half\nsecond-half", file=stream)
    assert "first-half" not in stream.getvalue() and "second-half" not in stream.getvalue()


def test_rotation_error_is_timestamped_without_reentering_journal_or_leaking_exception(tmp_path, monkeypatch, capsys):
    instance = journal.Journal()
    instance.configure({}, str(tmp_path / "journal.jsonl"), load=False)
    monkeypatch.setattr(instance, "_limits", lambda: (1, 1))
    monkeypatch.setattr(journal.console_clock, "timestamp", lambda: STAMP)

    def fail_rotation(_keep):
        raise OSError("private-path-containing-synthetic-secret")

    def forbidden_scrub(_text):
        raise AssertionError("journal failure must not recurse through console scrub")

    monkeypatch.setattr(instance, "_rotate", fail_rotation)
    monkeypatch.setattr(journal, "scrub", forbidden_scrub)
    instance.emit("probe", "force rotation")
    output = capsys.readouterr()
    assert output.err == STAMP + "[journal] rotation failed: OSError\n"
    assert "synthetic-secret" not in output.out + output.err


def test_rotation_diagnostic_is_best_effort_on_broken_stderr(tmp_path, monkeypatch):
    instance = journal.Journal()
    instance.configure({}, str(tmp_path / "journal.jsonl"), load=False)
    monkeypatch.setattr(instance, "_limits", lambda: (1, 1))

    def fail_rotation(_keep):
        raise OSError("synthetic failure")

    class Broken(io.StringIO):
        def write(self, _text):
            raise OSError("broken stderr")

    monkeypatch.setattr(instance, "_rotate", fail_rotation)
    monkeypatch.setattr(sys, "stderr", Broken())
    assert instance.emit("probe", "force rotation") is not None


def test_explain_fetch_failure_gets_timestamp_and_masks_secrets(monkeypatch, capsys):
    instance = journal.Journal()
    instance.configure({"relay_api_key": "synthetic-explain-secret"})
    monkeypatch.setattr(journal, "JOURNAL", instance)
    monkeypatch.setattr(console_log, "timestamp", lambda: STAMP)
    monkeypatch.setenv("NO_COLOR", "1")

    async def fail_fetch(_thread):
        raise OSError("synthetic-explain-secret")

    runner = orchestrator.TurnRunner.__new__(orchestrator.TurnRunner)
    holder = {"session": SimpleNamespace(backend=SimpleNamespace(get_messages=fail_fetch), thread_id="fake")}
    assert asyncio.run(runner._explain_empty(holder)) is None
    output = capsys.readouterr().out
    assert output.startswith(STAMP + "[explain] fetch failed:")
    assert "synthetic-explain-secret" not in output
