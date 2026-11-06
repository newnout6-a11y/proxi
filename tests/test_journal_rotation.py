"""The journal keeps its evidence for hours, not minutes: size and number of files are settings, bounded on disk."""
import json
import os
from pathlib import Path

import pytest

import apierr
import config
import journal
from journal import Journal
from test_admin import admin_relay  # noqa: F401 - a fixture
from test_docs_consistency import load_with

ROOT = Path(__file__).resolve().parents[1]


def written(path):
    """The seq numbers of a journal file, in file order; [] for a file that is not there."""
    try:
        return [json.loads(line)["seq"] for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
    except FileNotFoundError:
        return []


def log(tmp_path, size, keep, **extra):
    instance = Journal()
    instance.configure({"journal_max_file_bytes": size, "journal_keep_files": keep, **extra},
                       str(tmp_path / "journal.jsonl"), load=False)
    return instance


def numbered(tmp_path):
    return sorted(int(name.rsplit(".", 1)[1]) for name in os.listdir(tmp_path) if name.startswith("journal.jsonl."))


def test_the_defaults_keep_more_than_one_short_generation():
    # 10 MiB and one old file were gone in 13-15 minutes under four agents' load
    assert journal.MAX_FILE_BYTES > 10 * 1024 * 1024 and journal.KEEP_FILES > 1
    assert Journal()._limits() == (journal.MAX_FILE_BYTES, journal.KEEP_FILES)
    assert config.DEFAULTS["journal_max_file_bytes"] == journal.MAX_FILE_BYTES
    assert config.DEFAULTS["journal_keep_files"] == journal.KEEP_FILES


def test_the_configured_number_of_files_is_kept_and_the_oldest_falls_off(tmp_path):
    instance = log(tmp_path, 2000, 3)
    for number in range(400):
        instance.emit("probe", "event %d" % number)
    assert numbered(tmp_path) == [1, 2, 3]  # never a fourth
    files = [tmp_path / "journal.jsonl"] + [tmp_path / ("journal.jsonl.%d" % n) for n in (1, 2, 3)]
    seqs = [written(path) for path in files]
    # newest in the live file, then .1, .2, .3: every file holds older events than the one before it
    assert all(group for group in seqs)
    assert all(max(older) < min(newer) for newer, older in zip(seqs, seqs[1:]))
    # contiguous: nothing between the files was lost, only what is older than .3
    assert sum(reversed(seqs), []) == list(range(min(seqs[3]), 401))
    # a file is no bigger than the limit plus the event that crossed it
    assert all(path.stat().st_size < 2000 + 600 for path in files)


def test_the_disk_is_bounded_by_the_files_and_the_size(tmp_path):
    instance = log(tmp_path, 4096, 2)
    for number in range(2000):
        instance.emit("probe", "event %d with some text to fill the file" % number)
    total = sum((tmp_path / name).stat().st_size for name in os.listdir(tmp_path))
    assert numbered(tmp_path) == [1, 2] and total < 3 * (4096 + 700)


def test_zero_old_files_means_the_live_file_starts_over(tmp_path):
    instance = log(tmp_path, 2000, 0)
    for number in range(100):
        instance.emit("probe", "event %d" % number)
    assert numbered(tmp_path) == []
    seqs = written(tmp_path / "journal.jsonl")
    assert seqs and seqs[-1] == 100 and len(seqs) < 100


def test_files_past_a_lowered_limit_go_at_the_next_rotation(tmp_path):
    for number in range(1, 6):
        (tmp_path / ("journal.jsonl.%d" % number)).write_text(json.dumps({"seq": number}) + "\n", encoding="utf-8")
    instance = log(tmp_path, 2000, 2)
    for number in range(100):
        instance.emit("probe", "event %d" % number)
    assert numbered(tmp_path) == [1, 2]


def test_a_rotation_that_stops_halfway_loses_no_event(tmp_path, monkeypatch):
    instance = log(tmp_path, 2000, 3)
    for number in range(150):
        instance.emit("probe", "event %d" % number)
    real, calls = os.replace, []

    def flaky(source, target):
        calls.append(source)
        if len(calls) == 2:
            raise OSError("held by another program")
        return real(source, target)

    monkeypatch.setattr(os, "replace", flaky)
    for number in range(150, 400):
        instance.emit("probe", "event %d" % number)
    assert len(calls) >= 2  # the second move of a rotation failed
    files = [tmp_path / ("journal.jsonl.%d" % n) for n in reversed(numbered(tmp_path))] + [tmp_path / "journal.jsonl"]
    chronological = sum((written(path) for path in files), [])
    # a file may be missing from the numbering, but no event is missing from the files: the rest is one run to the end
    assert chronological[-1] == 400 and chronological == list(range(chronological[0], 401))
    assert len(numbered(tmp_path)) <= 3


def test_a_restart_reads_back_the_newest_files_within_the_restore_budget(tmp_path, monkeypatch):
    first = log(tmp_path, 3000, 4)
    for number in range(300):
        first.emit("probe", "event %d" % number)
    on_disk = numbered(tmp_path)
    assert len(on_disk) == 4
    monkeypatch.setattr(journal, "RESTORE_BYTES", 5000)
    second = Journal()
    second.configure({"journal_max_file_bytes": 3000, "journal_keep_files": 4}, str(tmp_path / "journal.jsonl"))
    seqs = [event["seq"] for event in second.query(limit=10000, order="asc")]
    assert seqs == sorted(seqs) and seqs[-1] == 300 and 5 < len(seqs) < 40  # the newest events only, a cut line not among them
    monkeypatch.setattr(journal, "RESTORE_BYTES", 10 * 1024 * 1024)
    third = Journal()
    third.configure({"journal_max_file_bytes": 3000, "journal_keep_files": 4}, str(tmp_path / "journal.jsonl"))
    everything = [event["seq"] for event in third.query(limit=10000, order="asc")]
    assert everything[-1] == 300 and len(everything) > len(seqs)


def test_a_file_cut_at_the_budget_drops_only_its_first_line(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_bytes(b"".join(b'{"seq": %d}\n' % number for number in range(1, 11)))
    lines, used = journal._tail_lines(str(path), 40)
    assert lines == ['{"seq": 8}', '{"seq": 9}', '{"seq": 10}'][-len(lines):] and used <= 40
    assert journal._tail_lines(str(path), 10_000)[0] == ['{"seq": %d}' % number for number in range(1, 11)]
    exact = journal._tail_lines(str(path), len(b'{"seq": 10}\n'))
    assert exact[0] == ['{"seq": 10}']  # the limit starts exactly on a line: it is kept
    assert journal._tail_lines(str(tmp_path / "missing"), 100) == ([], 0)


def test_reset_removes_every_rotated_file(tmp_path):
    instance = log(tmp_path, 2000, 4)
    for number in range(300):
        instance.emit("probe", "event %d" % number)
    assert len(numbered(tmp_path)) == 4
    instance.reset()
    assert numbered(tmp_path) == [] and written(tmp_path / "journal.jsonl") == []


def test_the_settings_are_validated_and_documented(tmp_path, monkeypatch):
    loaded = load_with(tmp_path, monkeypatch)
    assert loaded["journal_max_file_bytes"] == 50 * 1024 * 1024 and loaded["journal_keep_files"] == 5
    assert load_with(tmp_path, monkeypatch, journal_keep_files=0, journal_max_file_bytes="2097152")[
        "journal_max_file_bytes"] == 2097152
    for bad in ({"journal_keep_files": -1}, {"journal_keep_files": 51}, {"journal_keep_files": True},
                {"journal_keep_files": 1.5}, {"journal_max_file_bytes": 1000}, {"journal_max_file_bytes": 2 ** 31},
                {"journal_max_file_bytes": False}):
        with pytest.raises(apierr.ApiError, match="journal_"):
            load_with(tmp_path, monkeypatch, **bad)
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    for key in ("journal_max_file_bytes", "journal_keep_files"):
        assert example[key] == config.DEFAULTS[key], key
    assert example["journal_note"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "| journal_max_file_bytes, journal_keep_files |" in readme


def test_the_admin_changes_both_settings_without_a_restart(admin_relay):  # noqa: F811
    status, body, _ = admin_relay.request("/admin/api/settings")
    schema = body["schema"]
    assert schema["journal_max_file_bytes"]["restart_required"] is False
    assert schema["journal_keep_files"]["restart_required"] is False
    status, body, _ = admin_relay.patch({"journal_keep_files": 2, "journal_max_file_bytes": 2097152})
    assert status == 200 and set(body["applied"]) == {"journal_keep_files", "journal_max_file_bytes"}
    assert admin_relay.cfg["journal_keep_files"] == 2
    assert journal.JOURNAL._limits() == (2097152, 2)  # the journal reads the live settings at every write
    assert admin_relay.patch({"journal_keep_files": 99})[0] == 400
    assert admin_relay.patch({"journal_max_file_bytes": 10})[0] == 400
