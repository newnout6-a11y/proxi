"""The store itself, after review 3: what a file keeps, what the writer does on a bad day, which files it touches
(R3-07 and R3-08), and the settings it is given (R3-09)."""
import errno
import os
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import apierr
import conversation_state as cs
import journal
import native_history
from test_conversation_state import capture_of, files, snapshot, state_file, stored
from test_docs_consistency import load_with


class Mono:
    """A clock for the pause after a failed write: it moves only when the test says so."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def encodes(monkeypatch):
    """One entry for every state the writer encodes."""
    calls, real = [], cs.encode

    def spy(*args, **kwargs):
        calls.append(args[0].key)
        return real(*args, **kwargs)

    monkeypatch.setattr(cs, "encode", spy)
    return calls


def broken_disk(monkeypatch, state):
    """os.replace fails with EIO while state['broken']."""
    real = os.replace

    def replace(source, target):
        if state["broken"]:
            raise OSError(errno.EIO, "the disk is gone")
        return real(source, target)

    monkeypatch.setattr(cs.os, "replace", replace)


# -- R3-08: a file keeps the two newest branches -------------------------------------------------------------------------
def session_with(branches, pad=0, key="k"):
    history = native_history.History()
    for number in range(branches):
        item = snapshot("%s-%d" % (key, number), pad)
        history.record(item.scope, item.client, item.messages, item.assistant, item.echo)
    return SimpleNamespace(backend=SimpleNamespace(cfg={"account_id": "acc-one"}), _native_history=history,
                           _cached_turn_ends=[0], _last_cache_key="k")


def test_a_file_keeps_the_two_newest_branches_and_the_dead_ones_are_not_rewritten_every_turn(tmp_path):
    session = session_with(6)
    captured = cs.capture("k", session)
    assert [branch.assistant[0]["signature"] for branch in captured.branches] == ["S-k-4", "S-k-5"]
    assert len(session._native_history.branches) == 6  # the live history is not touched, only what is stored of it
    store = cs.ConversationStore(tmp_path / "state")
    store.submit(captured)
    assert store.flush(5)
    header, state = cs.decode(state_file(tmp_path / "state", "k").read_bytes(), 10 ** 9)
    assert header["branches"] == 2 and [b.assistant[0]["signature"] for b in state.branches] == ["S-k-4", "S-k-5"]
    store.close()
    # What it saves: eight dead branches are eight times the work of one (the reviewer measured 8.3 MB, 241 ms
    # against 1.0 MB, 35 ms for his live-shaped dialogs).
    heavy = session_with(8, pad=20_000)
    everything = tuple(heavy._native_history.branches)
    whole = cs.encode(cs.Capture("k", cs.key_digest("k"), "acc-one", everything, (0,), "k", 0, 0.0, ("m",)),
                      1.0, 10 ** 9, 10 ** 10)[0]
    stored_part = cs.encode(cs.capture("k", heavy), 1.0, 10 ** 9, 10 ** 10)[0]
    assert len(stored_part) * 3 < len(whole)


# -- R3-08: a state that does not fit is not encoded again every turn ----------------------------------------------------
def test_a_state_that_did_not_fit_is_not_encoded_again_until_it_is_lighter(tmp_path, encodes):
    state = tmp_path / "state"
    store = cs.ConversationStore(state, max_file_bytes=3000, max_total_bytes=100_000)
    seq = journal.JOURNAL.last_seq
    for turn in range(5):  # a conversation that only grows: every turn is heavier than the one that did not fit
        store.submit(capture_of("huge", pad=6000 + 300 * turn))
        assert store.flush(5)
    assert len(encodes) == 1 and files(state) == []
    assert [e["data"]["reason"] for e in stored(seq, "conversation_state.save_failed")] == ["too_large"]
    store.submit(capture_of("huge", pad=10))  # a compaction: it fits, and is stored
    assert store.flush(5) and len(encodes) == 2 and files(state) == [cs.key_digest("huge") + cs.SUFFIX]
    store.submit(capture_of("huge", pad=6000))  # it outgrew the limit again: tried once, said once more
    assert store.flush(5) and len(encodes) == 3 and files(state) == []
    assert len(stored(seq, "conversation_state.save_failed")) == 2
    store.submit(capture_of("huge", pad=5000))  # lighter than that, still too heavy: tried, and remembered at the new weight
    assert store.flush(5) and len(encodes) == 4
    store.submit(capture_of("huge", pad=5200))
    assert store.flush(5) and len(encodes) == 4
    store.close()


def test_the_memory_of_a_state_that_did_not_fit_belongs_to_its_conversation(tmp_path, encodes):
    store = cs.ConversationStore(tmp_path / "state", max_file_bytes=3000, max_total_bytes=100_000)
    store.submit(capture_of("one", pad=6000))
    assert store.flush(5)
    store.submit(capture_of("two", pad=10))
    assert store.flush(5) and encodes == ["one", "two"]  # another conversation is judged on its own
    assert files(tmp_path / "state") == [cs.key_digest("two") + cs.SUFFIX]
    store.close()


# -- R3-08: a failing disk is said once and costs one encode a pause ---------------------------------------------------------
def test_a_failing_disk_is_said_once_and_costs_an_encode_once_a_pause_not_at_every_turn(tmp_path, monkeypatch, encodes):
    disk = {"broken": True}
    broken_disk(monkeypatch, disk)
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    clock = store._monotonic = Mono()
    key_id = cs.key_digest("flaky")
    seq = journal.JOURNAL.last_seq
    for turn in range(6):  # six turns of one conversation on a disk that refuses every write
        store.submit(capture_of("flaky", pad=turn))
        assert store.flush(5)
    said = stored(seq, "conversation_state.save_failed")
    assert len(encodes) == 1 and [e["data"]["reason"] for e in said] == ["io_error"]
    assert said[0]["data"]["pause_seconds"] == round(cs.IO_PAUSE) and said[0]["data"]["errno"] == errno.EIO
    assert store.failed(key_id)  # every turn marked it unsaved: the next change after the pause asks again
    clock.now += cs.IO_PAUSE + 1
    store.submit(capture_of("flaky"))
    assert store.flush(5) and len(encodes) == 2  # asked again after the pause ...
    assert len(stored(seq, "conversation_state.save_failed")) == 1  # ... and the same words are not said twice
    clock.now += cs.IO_REMINDER
    store.submit(capture_of("flaky"))
    assert store.flush(5) and len(encodes) == 3
    assert len(stored(seq, "conversation_state.save_failed")) == 2  # a disk that stays broken is mentioned now and then
    disk["broken"] = False  # the disk is back
    clock.now += cs.IO_PAUSE + 1
    store.submit(capture_of("flaky"))
    assert store.flush(5) and files(state) == [key_id + cs.SUFFIX] and not store.failed(key_id)
    assert store._paused_until == 0.0 and store._disk_said is None
    disk["broken"] = True  # a new outage is a new report
    store.submit(capture_of("flaky", pad=3))
    assert store.flush(5)
    assert len(stored(seq, "conversation_state.save_failed")) == 3
    store.close()


def test_a_stop_asks_the_disk_again_whatever_the_pause(tmp_path, monkeypatch, encodes):
    disk = {"broken": True}
    broken_disk(monkeypatch, disk)
    store = cs.ConversationStore(tmp_path / "state")
    store._monotonic = Mono()
    store.submit(capture_of("a"))
    assert store.flush(5) and store.failed(cs.key_digest("a"))
    disk["broken"] = False
    assert store.submit(capture_of("a", pad=1)) is True and store.flush(5) and len(encodes) == 1  # paused: not attempted
    store.submit(capture_of("a", pad=2), urgent=True)  # the relay is stopping: it is
    assert store.flush(5) and len(encodes) == 2 and files(tmp_path / "state") == [cs.key_digest("a") + cs.SUFFIX]
    store.close()


def test_what_was_queued_when_the_disk_failed_waits_for_the_next_change_instead_of_failing_one_by_one(
        tmp_path, monkeypatch, encodes):
    disk, release, real = {"broken": True}, threading.Event(), cs.ConversationStore._write
    broken_disk(monkeypatch, disk)

    def gated(self, key_id, blob):
        release.wait(5)
        return real(self, key_id, blob)

    monkeypatch.setattr(cs.ConversationStore, "_write", gated)
    store = cs.ConversationStore(tmp_path / "state")
    store._monotonic = Mono()
    store.submit(capture_of("first"))
    deadline = time.monotonic() + 5
    while not store._busy and time.monotonic() < deadline:
        time.sleep(0.01)
    store.submit(capture_of("second"))
    store.submit(capture_of("third"))
    release.set()
    assert store.flush(5)
    assert encodes == ["first"]  # the first write failed; the two behind it were not even encoded
    assert store.failed(cs.key_digest("second")) and store.failed(cs.key_digest("third"))
    store.close()


def test_closing_the_store_tries_what_is_queued_whatever_the_pause(tmp_path, monkeypatch):
    real_replace, failures, release, real_write = os.replace, [1], threading.Event(), cs.ConversationStore._write

    def replace(source, target):
        if failures:
            failures.pop()
            raise OSError(errno.EIO, "the disk is gone")
        return real_replace(source, target)

    def gated(self, key_id, blob):
        release.wait(5)
        return real_write(self, key_id, blob)

    monkeypatch.setattr(cs.os, "replace", replace)
    monkeypatch.setattr(cs.ConversationStore, "_write", gated)
    store = cs.ConversationStore(tmp_path / "state")
    store._monotonic = Mono()
    store.submit(capture_of("first"))
    deadline = time.monotonic() + 5
    while not store._busy and time.monotonic() < deadline:
        time.sleep(0.01)
    store.submit(capture_of("second"))
    closer = threading.Thread(target=store.close, args=(5,))
    closer.start()
    while not store._closed and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()  # the first write fails, which starts a pause; the stop is already under way
    closer.join(5)
    assert files(tmp_path / "state") == [cs.key_digest("second") + cs.SUFFIX]
    assert store.failed(cs.key_digest("first"))


# -- R3-07: what is the store's and what is not ---------------------------------------------------------------------------
def test_a_file_with_the_signature_of_the_store_is_judged_wherever_its_name_says_it_is_ours(tmp_path):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    store.submit(capture_of("mine"))
    assert store.flush(5)
    store.close()
    good = state_file(state, "mine").read_bytes()
    named = cs.key_digest("ours-too") + cs.SUFFIX
    (state / named).write_bytes(good)  # our signature and a name of ours, but another conversation's header
    (state / "copy of mine.state").write_bytes(good)  # our signature, not a name of ours: nobody's file to judge
    (state / (cs.key_digest("cut") + cs.SUFFIX)).write_bytes(cs.MAGIC[:9])  # ours, cut short
    seq = journal.JOURNAL.last_seq
    cs.ConversationStore(state).close()
    assert sorted(e["data"]["reason"] for e in stored(seq, "conversation_state.dropped")) == ["corrupt", "key_mismatch"]
    assert sorted(path.name for path in state.iterdir()) == sorted(["copy of mine.state", state_file(state, "mine").name])


# -- R3-09: the settings -----------------------------------------------------------------------------------------------------
STATE_NUMBERS = ("conversation_state_max_conversations", "conversation_state_ttl",
                 "conversation_state_max_file_bytes", "conversation_state_max_total_bytes")


@pytest.mark.parametrize("name", STATE_NUMBERS)
@pytest.mark.parametrize("value", [True, False, 1.9, 0.5, float("nan"), float("inf"), "1.5", "soon", "", None, [3], {"a": 1}])
def test_the_counts_and_sizes_of_stored_conversations_are_whole_numbers_never_true_or_a_fraction(
        tmp_path, monkeypatch, name, value):
    with pytest.raises(apierr.ApiError, match="must be an integer"):
        load_with(tmp_path, monkeypatch, **{name: value})


@pytest.mark.parametrize("value,expected", [(5, 5), ("7", 7), (" 9 ", 9), (60.0, 60), (10 ** 6, 10 ** 6)])
def test_whole_numbers_and_their_text_are_still_accepted(tmp_path, monkeypatch, value, expected):
    loaded = load_with(tmp_path, monkeypatch, conversation_state_ttl=value)
    assert loaded["conversation_state_ttl"] == expected and type(loaded["conversation_state_ttl"]) is int


def test_the_settings_stay_positive_and_the_file_limit_stays_inside_the_directory_limit(tmp_path, monkeypatch):
    for name in STATE_NUMBERS:
        for value in (0, -1, "0"):
            with pytest.raises(apierr.ApiError, match="positive"):
                load_with(tmp_path, monkeypatch, **{name: value})
    with pytest.raises(apierr.ApiError, match="must not exceed"):
        load_with(tmp_path, monkeypatch, conversation_state_max_file_bytes=2000, conversation_state_max_total_bytes=1000)


# -- R3-07 (c): what the README says of the rights on Windows ---------------------------------------------------------------
@pytest.mark.skipif(os.name != "nt" or sys.version_info < (3, 13) or shutil.which("powershell") is None,
                    reason="the rights of a directory made with mode 0o700 are Windows' and Python 3.13's")
def test_on_windows_with_python_3_13_the_directory_is_for_its_owner_system_and_administrators_only(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    # The parent lets every user in, the way the folder of a repository on a drive root does.
    subprocess.run(["icacls", str(parent), "/grant", "*S-1-5-32-545:(OI)(CI)RX", "/grant", "*S-1-5-11:(OI)(CI)M"],
                   capture_output=True, check=True)
    store = cs.ConversationStore(parent / "conversations")
    store.submit(capture_of("private"))
    assert store.flush(5)
    store.close()

    def sids(path):
        # .NET rather than Get-Acl: Windows PowerShell started from PowerShell 7, as a CI runner's shell starts it,
        # inherits a module path under which its own Microsoft.PowerShell.Security module does not load.
        script = ("foreach ($rule in [IO.%s]::GetAccessControl('%s').GetAccessRules($true, $true, "
                  "[Security.Principal.SecurityIdentifier])) { $rule.IdentityReference.Value }"
                  % ("Directory" if path.is_dir() else "File", path))
        done = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                              check=True)
        return set(done.stdout.split())

    allowed = {"S-1-3-4", "S-1-5-18", "S-1-5-32-544"}  # OWNER RIGHTS, SYSTEM, Administrators
    assert sids(parent / "conversations") == allowed
    assert sids(state_file(parent / "conversations", "private")) == allowed  # the files inherit it
    assert "S-1-5-32-545" in sids(parent) and "S-1-5-11" in sids(parent)  # and the parent really did let everyone in
