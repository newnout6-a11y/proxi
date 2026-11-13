"""Journal hardening: key redaction, restore ordering/re-clean, rotation tail."""
import json

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


def test_restore_with_many_accounts_masks_their_secrets_and_builds_the_cookie_pattern_once(tmp_path, monkeypatch):
    # 501 accounts (1,027 cookie values and Minara tokens) made the restore at startup clean a million strings one by one,
    # each against an alternation of every secret: 90 s before the relay listened (2026-11-13). The same strings are now
    # cleaned once per restore, the cookie pattern is looked up once per event, and the secrets are still masked.
    import secrets as random_secrets

    import redact
    tokens = ["eyJ" + random_secrets.token_urlsafe(700) for _ in range(400)]
    cookies = ["c%03d%s" % (n, random_secrets.token_hex(20)) for n in range(600)]
    cfg = {"cookie_values": cookies + tokens,
           "accounts": [{"cookie": "session=" + value, "thread_id": "thread-%04d-abcdef" % n} for n, value in
                        enumerate(cookies[:78])]}
    path = tmp_path / "journal.jsonl"
    lines = []
    for seq in range(1, 2001):
        message = "turn %d" % (seq % 7)
        if seq == 1500:
            message = "pasted " + tokens[123] + " and " + cookies[456] + " and thread-0042-abcdef"
        lines.append(json.dumps({"seq": seq, "ts": float(seq), "kind": "upstream.usage", "category": "upstream",
                                 "level": "info", "message": message, "turn_id": "t%d" % (seq % 50),
                                 "data": {"client": "claude-desktop", "account": "a%d" % (seq % 9), "credits": 0.5}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    built = []
    real = redact._cookie_pattern
    monkeypatch.setattr(redact, "_cookie_pattern", lambda values: built.append(1) or real(values))
    instance = Journal()
    instance.configure(cfg, str(path), load=True)
    assert len(built) <= 2  # once for the restore (and for an event emitted alongside it), not once per string
    events = {event["seq"]: event for event in instance.query(limit=5000)}
    assert len(events) == 2000 and events[1499]["message"] == "turn 1" and events[1499]["data"]["credits"] == 0.5
    leaked = events[1500]["message"]
    assert tokens[123] not in leaked and cookies[456] not in leaked and "thread-0042-abcdef" not in leaked
    assert leaked.startswith("pasted ") and leaked.count("[redacted]") == 3
