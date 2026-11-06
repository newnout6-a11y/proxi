"""R1-08: a prompts file that is missing at one look is not "removed" (an editor that saves by delete + create).

Before, the first look that missed the file forgot the revision, a conversation that started in that moment was pinned to the
legacy path for good (LEGACY_REV) and the next look found the file again. Now a removal needs two looks that both miss the
file, at least RELOAD_INTERVAL apart. The tests move a fake clock by hand.
"""
import json
import os

import journal
import prompt_policy as pp
from test_prompt_policy_common import Clock, document, inp, make_store, write_json


def doc(head="MINE"):
    return document(profiles={"p": {"head": head}}, default_profile="p")


def tick(store, clock, seconds=None):
    clock.advance(pp.RELOAD_INTERVAL + 0.01 if seconds is None else seconds)
    store.maybe_reload()


def mark():
    return journal.JOURNAL.last_seq


def events(since, *kinds):
    found = journal.JOURNAL.query(since=since, limit=5000, order="asc")
    return [event for event in found if event["kind"] in (kinds or ("prompt.reload", "prompt.config_error"))]


def test_the_reviewers_sequence_a_conversation_that_starts_while_the_file_is_missing(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    path = tmp_path / "prompts.json"
    saved = path.read_bytes()
    assert store.snapshot().rev == 1
    os.remove(path)                      # an editor that saves by delete + create: the file is missing for a moment
    tick(store, clock)
    plan = store.plan(inp(conversation_key="new-conversation"))
    assert not plan.legacy and plan.profile == "p" and plan.rev == 1
    path.write_bytes(saved)              # the file is back a millisecond later
    tick(store, clock)
    again = store.plan(inp(conversation_key="new-conversation"))
    assert not again.legacy and again.profile == "p" and again.pinned
    assert not store.plan(inp(conversation_key="another-new-one")).legacy


def test_a_file_that_stays_missing_is_removed_on_the_second_look(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    since = mark()
    (tmp_path / "prompts.json").unlink()
    tick(store, clock)
    assert store.snapshot().rev == 1 and store.errors() == [] and events(since) == []
    tick(store, clock)
    assert store.snapshot() is None and store.plan(inp(conversation_key="late")).legacy
    (event,) = events(since)
    assert event["kind"] == "prompt.reload" and event["data"] == {**event["data"], "source": "removed", "rev": None}
    tick(store, clock)
    assert len(events(since)) == 1       # and it is said once


def test_two_looks_closer_than_the_reload_interval_do_not_remove_the_file(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    (tmp_path / "prompts.json").unlink()
    for _ in range(5):                   # save() looks at once, without the once-per-interval throttle of maybe_reload()
        with store._reload_lock:
            store._reload_if_changed()
    assert store.snapshot().rev == 1
    clock.advance(pp.RELOAD_INTERVAL / 2)
    with store._reload_lock:
        store._reload_if_changed()
    assert store.snapshot().rev == 1
    clock.advance(pp.RELOAD_INTERVAL / 2)
    with store._reload_lock:
        store._reload_if_changed()
    assert store.snapshot() is None


def test_the_interval_between_the_two_looks_is_the_reload_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "RELOAD_INTERVAL", 10.0)
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    (tmp_path / "prompts.json").unlink()
    tick(store, clock, 10.5)
    tick(store, clock, 10.5)             # the second look is 10.5 s after the first: the removal is believed
    assert store.snapshot() is None


def test_a_file_that_comes_back_unchanged_cancels_the_pending_removal(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    path = tmp_path / "prompts.json"
    saved, info = path.read_bytes(), path.stat()
    snapshot = store.snapshot()
    since = mark()
    path.unlink()
    tick(store, clock)
    path.write_bytes(saved)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))      # a restore that keeps the stamp: nothing to reload
    tick(store, clock)
    assert store.snapshot() is snapshot and events(since) == []
    # a later disappearance is a new episode: it needs its own two looks, the old first look does not count
    path.unlink()
    tick(store, clock)
    assert store.snapshot() is snapshot
    tick(store, clock)
    assert store.snapshot() is None


def test_a_file_that_comes_back_changed_is_a_new_revision_and_the_removal_never_happens(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    since = mark()
    (tmp_path / "prompts.json").unlink()
    tick(store, clock)
    write_json(tmp_path / "prompts.json", doc("EDITED"))
    tick(store, clock)
    assert store.snapshot().rev == 2 and store.plan(inp()).profile == "p"
    assert [event["data"]["source"] for event in events(since)] == ["file"]
    tick(store, clock)
    tick(store, clock)
    assert store.snapshot().rev == 2 and len(events(since)) == 1


def test_a_save_while_the_file_is_missing_for_a_moment_puts_it_back(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    since = mark()
    (tmp_path / "prompts.json").unlink()
    saved = store.save(doc("SAVED"), 1)                # the revision is still current: no conflict
    assert saved.rev == 2 and (tmp_path / "prompts.json").exists()
    for _ in range(3):
        tick(store, clock)
    assert store.snapshot() is saved
    assert [event["data"]["source"] for event in events(since)] == ["admin"]
    assert json.loads((tmp_path / "prompts.json").read_text(encoding="utf-8"))["profiles"]["p"]["head"] == "SAVED"


def test_a_pinned_conversation_keeps_its_revision_whatever_the_file_does(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    pinned = store.plan(inp(conversation_key="alive"))
    (tmp_path / "prompts.json").unlink()
    tick(store, clock)
    tick(store, clock)
    assert store.snapshot() is None
    assert store.plan(inp(conversation_key="fresh")).legacy
    kept = store.plan(inp(conversation_key="alive"))
    assert kept.profile == pinned.profile == "p" and kept.rev == 1 and kept.pinned


def test_a_missing_file_at_start_is_not_waited_for(tmp_path):
    clock = Clock()
    since = mark()
    store = make_store(tmp_path, clock=clock)
    assert store.snapshot() is None and store.plan(inp(conversation_key="c")).legacy
    tick(store, clock)
    assert store.snapshot() is None and events(since) == []


def test_an_invalid_file_that_then_vanishes_is_forgotten_at_once(tmp_path):
    clock = Clock()
    (tmp_path / "prompts.json").write_text("not json", encoding="utf-8")
    store = make_store(tmp_path, clock=clock)
    assert store.snapshot() is None and store.errors()
    (tmp_path / "prompts.json").unlink()
    tick(store, clock)
    assert store.snapshot() is None and store.errors() == []


def test_an_error_kept_for_a_good_revision_is_cleared_when_the_removal_is_confirmed(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    (tmp_path / "prompts.json").write_text("not json", encoding="utf-8")
    info = (tmp_path / "prompts.json").stat()
    os.utime(tmp_path / "prompts.json", ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
    tick(store, clock)
    assert store.snapshot().rev == 1 and store.errors()
    (tmp_path / "prompts.json").unlink()
    tick(store, clock)
    assert store.snapshot().rev == 1 and store.errors()      # the first look that misses it
    tick(store, clock)
    assert store.snapshot() is None and store.errors() == []


def test_a_file_that_vanishes_twice_with_a_good_look_between_needs_two_new_looks(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    path = tmp_path / "prompts.json"
    saved = path.read_bytes()
    for _ in range(3):
        path.unlink()
        tick(store, clock)
        assert store.snapshot() is not None
        path.write_bytes(saved)
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 7_000_000_000))
        tick(store, clock)
        assert store.snapshot() is not None and store.errors() == []
    assert not store.plan(inp(conversation_key="steady")).legacy
