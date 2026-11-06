"""Pins on disk (review R3-02): whatever goes wrong around the files never reaches a request, and says so once, without a text.

The store keeps its pins next to the stored conversations; a disk that is gone, a file that was deleted by hand, a bug in the
writer or in the code that takes a pin back must cost at worst what no pins cost: the conversation starts on the current
revision. A request is never refused, delayed beyond a disk access, or sent down another path for it.
"""
import json
import os
import pathlib
import shutil

import pytest

import journal
import prompt_policy as pp
from test_prompt_pins_persist_store import Relay, conv, digest, doc, events, mark, relay, rules_of  # noqa: F401


def started_on_v1_then_edited(relay, names=("old",)):
    """A relay that had conversations on V1 and stopped; the prompts file is V2 now. Returns the fingerprint of V1."""
    relay.write(doc("V1"))
    first = relay.start()
    for name in names:
        first.plan(conv(name))
    fingerprint = first.snapshot().fingerprint
    first.close()
    relay.write(doc("V2"))
    return fingerprint


# -- files that are gone ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("what", ["pins_file", "revisions_folder", "pins_folder", "state_directory"])
def test_files_deleted_by_hand_cost_the_conversation_its_pin_and_nothing_else(relay, what):
    started_on_v1_then_edited(relay)
    if what == "pins_file":
        (relay.pins / pp.PINS_FILE).unlink()
    elif what == "revisions_folder":
        shutil.rmtree(relay.pins / pp.REVISIONS_DIR)
    elif what == "pins_folder":
        shutil.rmtree(relay.pins)
    else:
        shutil.rmtree(relay.state)
    since = mark()
    store = relay.start()
    plan = store.plan(conv("old"))
    assert rules_of(plan) == "RULES-V2" and not plan.pinned and plan.rev == 1
    problems = [event["data"]["reason"] for event in events(since, "prompt.pins_error")]
    # A file that is not there is nothing to complain about; a pin whose document was taken away is said once.
    assert problems == (["revision_missing"] if what == "revisions_folder" else [])
    assert store.plan(conv("old")).pinned
    relay.tick(store)
    assert set(relay.stored()["pins"]) == {digest("old")} and relay.revisions() == [store.snapshot().fingerprint]
    store.close()
    restarted = relay.start()
    assert restarted.plan(conv("old")).pinned


def test_a_pins_file_that_is_a_directory_is_ignored_and_the_pins_still_work_in_memory(relay):
    relay.write(doc("V1"))
    (relay.pins / pp.PINS_FILE).mkdir(parents=True)
    since = mark()
    store = relay.start()
    assert [event["data"]["reason"] for event in events(since, "prompt.pins_error")] == ["unreadable"]
    assert store.plan(conv("old")).profile == "mine" and store.plan(conv("old")).pinned
    relay.tick(store)
    relay.tick(store)
    failures = [event["data"]["reason"] for event in events(since, "prompt.pins_error")]
    assert failures == ["unreadable", "write"]                          # the write that cannot happen is said once
    relay.write(doc("V2"))
    relay.tick(store)
    assert rules_of(store.plan(conv("old"))) == "RULES-V1" and rules_of(store.plan(conv("new"))) == "RULES-V2"


# -- bugs --------------------------------------------------------------------------------------------------------------------


def test_a_bug_in_the_writer_never_reaches_the_request_and_is_said_once_without_its_message(relay, monkeypatch):
    relay.write(doc("V1"))
    store = relay.start()

    def boom(now):
        raise RuntimeError("a secret text in the message of a bug")

    monkeypatch.setattr(store, "_write_pins", boom)
    since = mark()
    for number in range(4):
        assert store.plan(conv("chat-%d" % number)).profile == "mine"
        relay.tick(store)                                               # maybe_reload() does not raise
    store.unpin("chat-0")                                               # nor does an unpin
    store.close()
    found = events(since, "prompt.pins_error")
    assert len(found) == 1 and found[0]["data"]["reason"] == "write" and found[0]["data"]["error"] == "RuntimeError"
    assert "secret text" not in json.dumps(journal.JOURNAL.query(since=since, limit=500), ensure_ascii=False)


def test_a_bug_while_a_pin_is_taken_back_gives_the_conversation_the_current_revision(relay, monkeypatch):
    started_on_v1_then_edited(relay, names=("one", "two"))
    store = relay.start()

    def boom(print_):
        raise RuntimeError("a bug while the revision is made again")

    monkeypatch.setattr(store, "_read_revision", boom)
    since = mark()
    plan = store.plan(conv("one"))
    assert rules_of(plan) == "RULES-V2" and not plan.pinned
    assert store.plan(conv("one")).pinned                               # pinned afresh, on the current revision
    problems = events(since, "prompt.pins_error")
    assert [event["data"]["reason"] for event in problems] == ["restore"] and problems[0]["level"] == "error"
    assert problems[0]["data"]["error"] == "RuntimeError"
    monkeypatch.undo()
    other = store.plan(conv("two"))                                     # the bug did not poison the other stored pin
    assert rules_of(other) == "RULES-V1" and other.pinned


def test_a_bug_in_reading_the_pins_file_is_the_start_of_a_relay_without_pins(relay, monkeypatch):
    started_on_v1_then_edited(relay)
    monkeypatch.setattr(pp, "_read_limited", lambda path, limit: (_ for _ in ()).throw(OSError(5, "I/O error")))
    since = mark()
    store = relay.start()
    assert [event["data"]["reason"] for event in events(since, "prompt.pins_error")] == ["unreadable"]
    assert rules_of(store.plan(conv("old"))) == "RULES-V2"


# -- where the folder is -------------------------------------------------------------------------------------------------------


def test_a_relative_state_directory_is_taken_from_the_working_directory_as_the_conversation_store_takes_it(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relay = Relay(tmp_path)
    relay.cfg = {"conversation_state_dir": "relative-state"}
    relay.write(doc("V1"))
    store = relay.start()
    assert store._pins_path == pathlib.Path(os.path.abspath("relative-state")) / pp.PINS_DIR
    store.plan(conv("old"))
    relay.tick(store)
    assert (tmp_path / "relative-state" / pp.PINS_DIR / pp.PINS_FILE).is_file()


@pytest.mark.parametrize("directory", [7, 0, [], {}, b"", object()])
def test_a_state_directory_that_is_not_a_path_is_no_directory(relay, directory):
    relay.cfg = {"conversation_state_dir": directory}
    relay.write(doc("V1"))
    store = relay.start()
    store.plan(conv("old"))
    relay.tick(store)
    store.close()
    assert store._pins_path is None and not relay.state.exists()


def test_the_pins_folder_is_inside_the_conversation_state_directory_and_nowhere_else(relay):
    relay.write(doc("V1"))
    store = relay.start()
    store.plan(conv("old"))
    relay.tick(store)
    assert store._pins_path == relay.state / "prompt-pins" and pp.PINS_DIR == "prompt-pins"
    assert pp.PINS_FILE == "pins.json" and pp.REVISIONS_DIR == "revisions"
    assert sorted(path.relative_to(relay.state).as_posix() for path in relay.state.rglob("*")) == [
        "prompt-pins", "prompt-pins/pins.json", "prompt-pins/revisions",
        "prompt-pins/revisions/%s.json" % store.snapshot().fingerprint]
    assert [path.name for path in relay.folder.iterdir()] == ["prompts.json"]       # nothing beside the prompts file
