"""Journal hardening: key redaction, restore ordering/re-clean, rotation tail."""
import json

import journal as journal_module
from journal import Journal


def _fresh(tmp_path, **cfg):
    instance = Journal()
    instance.configure(cfg or None, str(tmp_path / "journal.jsonl"), load=False)
    return instance


def test_clean_redacts_secret_in_dict_keys(tmp_path):
    instance = _fresh(tmp_path, relay_api_key="relay-secret-key-123456")
    event = instance.emit("tool.call", "t", arguments={"accessToken=supersecretcookievalue123": "value"})
    data = event["data"]["arguments"]
    assert all("supersecretcookievalue123" not in key for key in data), "secret key leaked raw"


def test_load_tail_reads_rotated_generation_and_sorts(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = Journal()
    first.configure(None, str(path), load=False)
    for i in range(1, 4):
        first.emit("probe", f"gen1-{i}")
    first._write({"seq": 99, "kind": "probe", "category": "probe",
                  "level": "info", "message": "gen1-tail", "ts": 0.0})
    # Simulate a rotation: current file becomes .1, the live file restarts.
    (tmp_path / "journal.jsonl.1").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text("", encoding="utf-8")
    second = Journal()
    second.configure(None, str(path), load=True)
    seqs = [e["seq"] for e in second.query()]
    assert seqs == sorted(seqs), "restored buffer must be seq-ordered"
    assert 99 in seqs, "rotated generation (.1) must be restored"


def test_restore_recleans_old_events(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text(json.dumps({
        "seq": 1, "kind": "tool.call", "category": "tool", "level": "info",
        "message": "token sk-Abcdefghijklmnopqrst12 leaked", "ts": 0.0}) + "\n", encoding="utf-8")
    instance = Journal()
    instance.configure(None, str(path), load=True)
    restored = [e for e in instance.query() if e["seq"] == 1]
    assert restored, "valid event must restore"
    assert "sk-Abcdefghijklmnopqrst12" not in restored[0]["message"], \
        "restored events must be re-cleaned with current patterns"


def test_reconfigure_does_not_duplicate_events(tmp_path):
    path = tmp_path / "journal.jsonl"
    instance = Journal()
    instance.configure(None, str(path), load=False)
    instance.emit("probe", "one")
    instance.configure(None, str(path), load=True)  # re-configure on the same instance
    instance.emit("probe", "two")
    seqs = [e["seq"] for e in instance.query()]
    assert len(seqs) == len(set(seqs)), "re-configure must not duplicate restored events"


def test_rotation_failure_does_not_kill_the_journal(tmp_path, monkeypatch, capsys):
    import os
    path = tmp_path / "journal.jsonl"
    instance = Journal()
    instance.configure(None, str(path), load=False)
    real_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("file locked")

    monkeypatch.setattr(os, "replace", failing_replace)
    for _ in range(5):
        instance.emit("probe", "x")
    monkeypatch.setattr(os, "replace", real_replace)
    event = instance.emit("probe", "after-failure")
    assert event is not None and event["seq"] == 6, "journal must survive failed rotations"
