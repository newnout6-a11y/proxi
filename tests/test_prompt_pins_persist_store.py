"""Pins that survive a restart (review R3-02): the store keeps them next to the stored conversations and gives them back.

A "relay" here is a PromptStore on a prompts folder and a state directory; a restart is a new PromptStore on the same disk,
with a clock that starts from somewhere else (the monotonic clock is meaningless across a restart) and the wall clock going
on. A conversation started on revision V1 whose prompts file became V2 meanwhile must still get the head of V1.
"""
import hashlib
import json
import os
import pathlib
import threading
import time

import pytest

import apierr
import journal
import prompt_policy as pp
from test_prompt_policy_common import OPENCODE_SYSTEM, Clock, Sources, document, inp, write_json

SECRET = "SECRET-PROMPT-TEXT-0451"
KEY = "claude-metadata:secret-session-id"


def doc(version="V1", extra_profile=None, **extra):
    """A document whose head says which version it is: the client's system, a blank line and the rules text."""
    profiles = {"mine": {"head": "{{client_system}}\n\n{{text:rules}}"}}
    profiles.update(extra_profile or {})
    return document(default_profile="mine", texts={"rules": "RULES-" + version, "secret": SECRET}, profiles=profiles,
                    rules=[], **extra)


def conv(name="chat-1", **fields):
    return inp(conversation_key=name, **fields)


def head_of(plan):
    return plan.compose_head(OPENCODE_SYSTEM, Sources())[0]


def rules_of(plan):
    return head_of(plan).rsplit("\n", 1)[-1]


def digest(key, purpose="client-tools"):
    return hashlib.sha256(json.dumps([key, purpose], separators=(",", ":")).encode()).hexdigest()


class Relay:
    """A prompts folder and a state directory; start() is a relay that starts on them (the last one is left running)."""

    def __init__(self, tmp_path, **cfg):
        self.folder = tmp_path / "relay"
        self.folder.mkdir(exist_ok=True)
        self.state = tmp_path / "state" / "conversations"
        self.cfg = {"conversation_state_dir": str(self.state), **cfg}
        self.wall = Clock(1_800_000_000.0)
        self.mono = Clock(0.0)
        self.started = 0
        self.writes = 0

    @property
    def prompts(self):
        return self.folder / "prompts.json"

    @property
    def pins(self):
        return self.state / pp.PINS_DIR

    def start(self, **cfg):
        self.started += 1
        self.mono = Clock(5000.0 * self.started)
        return pp.PromptStore({"prompts_file": str(self.prompts), **self.cfg, **cfg}, clock=self.mono, wall=self.wall)

    def write(self, document_):
        """Put the prompts file in place; every write gets a stamp of its own (a file system stamps two quick writes alike)."""
        write_json(self.prompts, document_)
        self.writes += 1
        stamp = 1_700_000_000_000_000_000 + self.writes * 5_000_000_000
        os.utime(self.prompts, ns=(stamp, stamp))

    def advance(self, seconds):
        self.mono.advance(seconds)
        self.wall.advance(seconds)

    def tick(self, store, seconds=pp.PINS_WRITE_INTERVAL + 0.1):
        """Time passes, and the runtime asks the store to look at its file: that is when the pins are written."""
        self.advance(seconds)
        store.maybe_reload()

    def stored(self):
        return json.loads((self.pins / pp.PINS_FILE).read_text(encoding="utf-8"))

    def revisions(self):
        folder = self.pins / pp.REVISIONS_DIR
        return sorted(path.name[:-5] for path in folder.iterdir()) if folder.exists() else []

    def files(self):
        return sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*")) if self.state.exists() else []


@pytest.fixture
def relay(tmp_path):
    return Relay(tmp_path)


def mark():
    return journal.JOURNAL.last_seq


def events(since, *kinds):
    found = journal.JOURNAL.query(since=since, limit=5000, order="asc")
    return [event for event in found if not kinds or event["kind"] in kinds]


def spy_replace(monkeypatch):
    """The names of the files os.replace was asked to put in place, in order."""
    real, seen = os.replace, []
    monkeypatch.setattr(pp.os, "replace", lambda source, target: seen.append(pathlib.Path(target).name) or real(source, target))
    return seen


# -- nothing is kept unless there is somewhere to keep it ------------------------------------------------------------------


@pytest.mark.parametrize("cfg", [{"conversation_state_dir": ""}, {"conversation_state": False}, {"conversation_state_dir": None}])
def test_without_a_folder_or_with_the_stored_conversations_off_no_file_or_folder_appears(tmp_path, cfg):
    relay = Relay(tmp_path)
    relay.cfg = {"conversation_state_dir": str(relay.state), **cfg}
    relay.write(doc())
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    store = relay.start()
    for name in ("one", "two", "three"):
        store.plan(conv(name))
    relay.tick(store)
    store.unpin("one")
    relay.tick(store)
    store.save(doc("V2"), store.snapshot().rev)
    store.close()
    assert not relay.state.exists() and store._pins_path is None
    after = sorted(str(path) for path in tmp_path.rglob("*"))
    assert [name for name in after if name not in before and not name.startswith(str(relay.prompts))] == []


def test_a_store_without_a_folder_has_no_fingerprint_work_to_do(tmp_path, monkeypatch):
    calls = []
    real = pp.Snapshot._canonical
    monkeypatch.setattr(pp.Snapshot, "_canonical", lambda self: calls.append(1) or real(self))
    relay = Relay(tmp_path, conversation_state_dir="")
    relay.write(doc())
    store = relay.start()
    store.plan(conv())
    store.save(doc("V2"), 1)
    assert calls == []


def test_the_folder_is_not_made_while_there_is_no_pin(relay):
    relay.write(doc())
    store = relay.start()
    relay.tick(store)
    relay.tick(store)
    store.close()
    assert not relay.state.exists()


def test_a_store_that_only_looks_writes_nothing(relay):
    relay.write(doc())
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    before = {path.name: path.read_bytes() for path in relay.pins.rglob("*") if path.is_file()}
    stamp = (relay.pins / pp.PINS_FILE).stat().st_mtime_ns
    relay.advance(pp.PIN_IDLE_SECONDS + 100)             # every pin has expired by now: a writer would tidy up
    looker = relay.start()                               # a script that lists the models builds a store, reads, and goes
    assert looker.pins() == [] and looker.snapshot() is not None and looker.recent() == []
    assert {path.name: path.read_bytes() for path in relay.pins.rglob("*") if path.is_file()} == before
    assert (relay.pins / pp.PINS_FILE).stat().st_mtime_ns == stamp


def test_the_first_look_of_a_relay_that_found_only_expired_pins_tidies_the_folder(relay):
    relay.write(doc())
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    assert len(relay.revisions()) == 1
    relay.advance(pp.PIN_IDLE_SECONDS + 100)
    store = relay.start()
    assert store.pins() == []
    relay.tick(store)
    assert relay.stored()["pins"] == {} and relay.revisions() == []


# -- what is written -----------------------------------------------------------------------------------------------------


def test_a_pin_is_written_by_the_next_look_and_not_more_often_than_once_per_interval(relay, monkeypatch):
    relay.write(doc())
    store = relay.start()
    seen = spy_replace(monkeypatch)
    store.plan(conv("one"))
    assert seen == [] and not relay.pins.exists()                     # plan() never writes
    store.maybe_reload()                                              # the first write is not made to wait
    assert seen.count(pp.PINS_FILE) == 1
    store.plan(conv("two"))
    relay.advance(pp.PINS_WRITE_INTERVAL / 2)
    store.maybe_reload()
    assert seen.count(pp.PINS_FILE) == 1
    relay.advance(pp.PINS_WRITE_INTERVAL / 2 + 0.01)
    store.maybe_reload()
    assert seen.count(pp.PINS_FILE) == 2 and len(relay.stored()["pins"]) == 2
    for _ in range(5):
        store.maybe_reload()
    assert seen.count(pp.PINS_FILE) == 2                              # nothing changed: nothing is written


def test_the_file_names_a_pin_by_a_digest_and_holds_no_key_and_no_text(relay):
    relay.write(doc())
    store = relay.start()
    snapshot = store.snapshot()
    store.plan(conv(KEY, purpose="client-tools"))
    relay.tick(store)
    raw = (relay.pins / pp.PINS_FILE).read_text(encoding="utf-8")
    assert KEY not in raw and "secret-session-id" not in raw and SECRET not in raw
    stored = json.loads(raw)
    assert stored["format"] == pp.PINS_FORMAT and abs(stored["saved_at"] - relay.wall()) < 5
    (name, row), = stored["pins"].items()
    assert name == digest(KEY) == pp._pin_digest((KEY, "client-tools"))
    assert row == {"tag": journal.tag(KEY), "purpose": "client-tools", "rev": snapshot.fingerprint, "profile": "mine",
                   "created": row["created"], "touched": row["touched"]}
    assert abs(row["created"] - relay.wall()) < 5 and row["touched"] >= row["created"]
    # the revision it names is stored whole, by that name, as the bytes the fingerprint is taken of
    assert relay.revisions() == [snapshot.fingerprint]
    blob = (relay.pins / pp.REVISIONS_DIR / (snapshot.fingerprint + ".json")).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == snapshot.fingerprint and blob == snapshot._canonical()
    assert json.loads(blob) == snapshot.literal_document


def test_every_purpose_of_a_conversation_is_stored_on_its_own(relay):
    relay.write(doc())
    store = relay.start()
    store.plan(conv("chat", purpose="client-tools"))
    store.plan(conv("chat", purpose="client-text"))
    relay.tick(store)
    assert set(relay.stored()["pins"]) == {digest("chat", "client-tools"), digest("chat", "client-text")}
    assert len(relay.revisions()) == 1                                # one revision, however many pins name it


def test_a_legacy_pin_is_stored_with_the_legacy_revision_and_no_document(relay):
    store = relay.start()
    assert store.plan(conv("early")).legacy
    relay.tick(store)
    (row,) = relay.stored()["pins"].values()
    assert row["rev"] == pp.LEGACY_REV == 0 and type(row["rev"]) is int and row["profile"] is None
    assert set(row) == {"tag", "purpose", "rev", "profile", "created", "touched"}
    assert relay.revisions() == []


def test_a_touch_reaches_the_file_only_when_the_stored_one_is_a_minute_old(relay, monkeypatch):
    relay.write(doc())
    store = relay.start()
    store.plan(conv("busy"))
    relay.tick(store)
    first = relay.stored()["pins"][digest("busy")]["touched"]
    seen = spy_replace(monkeypatch)
    for _ in range(5):
        relay.advance(10)
        store.plan(conv("busy"))
        store.maybe_reload()
    assert seen == [] and relay.stored()["pins"][digest("busy")]["touched"] == first
    relay.advance(pp.PIN_TOUCH_INTERVAL)
    store.plan(conv("busy"))
    store.maybe_reload()
    assert seen.count(pp.PINS_FILE) == 1
    assert relay.stored()["pins"][digest("busy")]["touched"] > first + pp.PIN_TOUCH_INTERVAL


def test_a_pin_that_moves_to_another_revision_or_profile_is_written(relay):
    relay.write(doc())
    store = relay.start()
    store.plan(conv("chat"))
    relay.tick(store)
    first = relay.stored()["pins"][digest("chat")]
    store.save(doc("V2", extra_profile={"extra": {"head": "X"}}), 1)
    plan = store.plan(conv("chat", virtual_profile="extra", requested_model="m"))   # only the current revision has it
    assert plan.profile == "extra" and plan.rev == 2
    relay.tick(store)
    moved = relay.stored()["pins"][digest("chat")]
    assert moved["rev"] == store.snapshot().fingerprint != first["rev"] and moved["profile"] == "extra"
    assert relay.revisions() == [store.snapshot().fingerprint]        # the revision nobody names any more is gone


def test_a_change_of_the_profile_alone_is_written(relay):
    document_ = doc(extra_profile={"other": {"head": "O"}})
    document_["rules"] = [{"name": "titles", "use": "other", "if": {"model": "title-*"}}]
    relay.write(document_)
    store = relay.start()
    store.plan(conv("chat", model="claude-opus-5-5"))
    relay.tick(store)
    assert relay.stored()["pins"][digest("chat")]["profile"] == "mine"
    store.plan(conv("chat", model="title-model"))
    relay.tick(store)
    assert relay.stored()["pins"][digest("chat")]["profile"] == "other"


def test_an_unpin_is_written_at_once_with_nothing_waiting_for_the_interval(relay):
    relay.write(doc())
    store = relay.start()
    store.plan(conv("one"))
    store.plan(conv("two"))
    relay.tick(store)
    assert len(relay.stored()["pins"]) == 2
    assert store.unpin("one") == 1                                   # no tick: the same instant
    assert list(relay.stored()["pins"]) == [digest("two")]
    assert store.unpin() == 1 and relay.stored()["pins"] == {} and relay.revisions() == []


def test_close_writes_what_is_waiting_and_a_second_close_does_nothing(relay, monkeypatch):
    relay.write(doc())
    store = relay.start()
    store.plan(conv("one"))
    relay.tick(store)
    store.plan(conv("two"))                                           # changed inside the interval: still waiting
    assert len(relay.stored()["pins"]) == 1
    seen = spy_replace(monkeypatch)
    store.close()
    assert len(relay.stored()["pins"]) == 2 and seen.count(pp.PINS_FILE) == 1
    store.plan(conv("three"))
    store.close()
    store.maybe_reload()
    assert seen.count(pp.PINS_FILE) == 1                              # after close nothing more is written
    assert store.plan(conv("three")).pinned                           # the store itself still serves


def test_a_save_puts_the_pins_on_disk_before_the_file_changes(relay, monkeypatch):
    relay.write(doc())
    store = relay.start()
    store.plan(conv("chat"))                                          # a pin that no look has written yet
    seen = spy_replace(monkeypatch)
    store.save(doc("V2"), 1)
    assert seen.index(pp.PINS_FILE) < seen.index("prompts.json")
    assert digest("chat") in relay.stored()["pins"]


def test_nothing_is_written_when_the_pins_are_what_they_were(relay, monkeypatch):
    relay.write(doc())
    store = relay.start()
    store.plan(conv("chat"))
    relay.tick(store)
    seen = spy_replace(monkeypatch)
    for _ in range(4):
        store.plan(conv("chat"))
        relay.tick(store, 2)
    assert seen == []


def test_waking_pins_costs_the_file_one_write_for_the_stale_touches_and_no_more(relay, monkeypatch):
    relay.write(doc("V1"))
    first = relay.start()
    for name in ("a", "b", "c"):
        first.plan(conv(name))
    first.close()
    old = {name: relay.stored()["pins"][digest(name)]["touched"] for name in ("a", "b", "c")}
    relay.write(doc("V2"))
    second = relay.start()
    relay.advance(2 * pp.PIN_TOUCH_INTERVAL)                          # the stored touches are old by now
    seen = spy_replace(monkeypatch)
    woke = relay.wall()
    for name in ("a", "b", "c"):
        assert second.plan(conv(name)).pinned
    assert seen == []                                                 # waking writes nothing by itself
    relay.tick(second)
    assert seen.count(pp.PINS_FILE) == 1                              # the touches of the three, together, in one write
    rows = relay.stored()["pins"]
    for name in ("a", "b", "c"):
        assert rows[digest(name)]["touched"] == pytest.approx(woke, abs=0.01)
        assert rows[digest(name)]["touched"] > old[name] + pp.PIN_TOUCH_INTERVAL
    for _ in range(6):                                                # half a minute of use: nothing more to write
        for name in ("a", "b", "c"):
            second.plan(conv(name))
        relay.tick(second, 5)
    assert seen.count(pp.PINS_FILE) == 1


# -- a restart: the conversation that was started on V1 stays on V1 --------------------------------------------------------


def test_a_conversation_keeps_the_head_it_started_with_through_an_edit_and_a_restart(relay):
    relay.write(doc("V1"))
    first = relay.start()
    started = first.plan(conv("old"))
    assert rules_of(started) == "RULES-V1" and not started.pinned
    first.close()
    relay.write(doc("V2"))                                            # the operator edits while the relay is down
    second = relay.start()
    assert second.snapshot().rev == 1 and not second.snapshot().restored
    old = second.plan(conv("old"))
    assert rules_of(old) == "RULES-V1" and old.pinned and old.snapshot.restored
    new = second.plan(conv("new"))
    assert rules_of(new) == "RULES-V2" and not new.pinned and new.snapshot is second.snapshot()
    again = second.plan(conv("old"))
    assert again.snapshot is old.snapshot and again.pinned            # the revision was made once
    assert head_of(old) == head_of(started)                           # the very bytes of the head it had before the restart


def test_the_same_holds_when_the_edit_happens_while_the_relay_runs_and_it_is_stopped_and_started_after(relay):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("old"))
    relay.tick(first)
    relay.write(doc("V2"))
    relay.tick(first)
    assert first.snapshot().rev == 2 and rules_of(first.plan(conv("old"))) == "RULES-V1"
    first.close()
    second = relay.start()
    assert rules_of(second.plan(conv("old"))) == "RULES-V1" and rules_of(second.plan(conv("new"))) == "RULES-V2"


def test_after_a_restart_a_pin_on_the_current_revision_needs_no_revision_to_be_made(relay, monkeypatch):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("chat"))
    first.close()
    second = relay.start()
    compiled = []
    real = pp._Compiler.compile
    monkeypatch.setattr(pp._Compiler, "compile", lambda self: compiled.append(self.literal) or real(self))
    plan = second.plan(conv("chat"))
    assert plan.pinned and plan.snapshot is second.snapshot() and plan.rev == 1 and compiled == []
    assert second._history.keys() == {1}


def test_a_revision_is_made_once_for_all_the_pins_that_name_it(relay, monkeypatch):
    relay.write(doc("V1"))
    first = relay.start()
    for name in ("a", "b", "c"):
        first.plan(conv(name))
    first.plan(conv("a", purpose="client-text"))
    first.close()
    relay.write(doc("V2"))
    second = relay.start()
    compiled = []
    real = pp._Compiler.compile
    monkeypatch.setattr(pp._Compiler, "compile", lambda self: compiled.append(self.literal) or real(self))
    plans = [second.plan(conv(name)) for name in ("a", "b", "c")] + [second.plan(conv("a", purpose="client-text"))]
    assert compiled == [True]                                         # one literal compile, however many conversations
    assert len({id(plan.snapshot) for plan in plans}) == 1 and all(plan.pinned for plan in plans)
    assert {rules_of(plan) for plan in plans} == {"RULES-V1"}
    assert second.snapshot().rev == 1 and plans[0].rev == 2 and second.snapshot().fingerprint != plans[0].snapshot.fingerprint


def test_the_restored_revision_is_history_only_and_the_numbers_go_on(relay):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    relay.write(doc("V2"))
    second = relay.start()
    old = second.plan(conv("old"))
    assert (old.rev, second.snapshot().rev) == (2, 1) and old.snapshot.restored and not second.snapshot().restored
    assert sorted(second._history) == [1, 2] and second.snapshot() is second._history[1]
    saved = second.save(doc("V3"), 1)
    assert saved.rev == 3 and second.snapshot() is saved               # the next real revision takes the next number
    assert second.plan(conv("old")).rev == 2 and second.plan(conv("fresh")).rev == 3


def test_a_legacy_pin_survives_a_restart_and_the_file_that_appeared_meanwhile_does_not_reach_it(relay):
    early = relay.start()
    assert early.snapshot() is None and early.plan(conv("early")).legacy
    early.close()
    relay.write(doc("V1"))
    late = relay.start()
    plan = late.plan(conv("early"))
    assert plan.legacy and plan.pinned and plan.snapshot is None
    fresh = late.plan(conv("fresh"))
    assert fresh.profile == "mine" and not fresh.pinned
    assert late.unpin("early") == 1 and late.plan(conv("early")).profile == "mine"


def test_a_pin_survives_the_removal_of_the_prompts_file_while_new_conversations_go_legacy(relay):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    relay.prompts.unlink()
    second = relay.start()
    assert second.snapshot() is None
    old = second.plan(conv("old"))
    assert old.profile == "mine" and old.pinned and rules_of(old) == "RULES-V1" and old.rev == 1
    assert second.plan(conv("new")).legacy
    relay.write(doc("V2"))
    relay.tick(second)
    relay.tick(second)
    assert second.snapshot().rev == 2 and rules_of(second.plan(conv("old"))) == "RULES-V1"


def test_texts_that_came_from_files_are_the_ones_read_when_the_pin_was_made(relay):
    (relay.folder / "prompts").mkdir()
    (relay.folder / "prompts" / "rules.md").write_text("FROM-FILE-ONE", encoding="utf-8")
    document_ = document(default_profile="mine", texts={"rules": {"file": "prompts/rules.md"}},
                         profiles={"mine": {"head": "{{client_system}}\n\n{{text:rules}}"}})
    relay.write(document_)
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    (relay.folder / "prompts" / "rules.md").write_text("FROM-FILE-TWO", encoding="utf-8")
    second = relay.start()
    assert rules_of(second.plan(conv("old"))) == "FROM-FILE-ONE" and rules_of(second.plan(conv("new"))) == "FROM-FILE-TWO"
    (relay.folder / "prompts" / "rules.md").unlink()
    third = relay.start()
    assert third.snapshot() is None and rules_of(third.plan(conv("old"))) == "FROM-FILE-ONE"


def test_several_restarts_in_a_row_keep_every_conversation_on_its_own_revision(relay):
    relay.write(doc("V1"))
    store = relay.start()
    store.plan(conv("one"))
    store.close()
    for version in ("V2", "V3", "V4"):
        relay.write(doc(version))
        store = relay.start()
        store.plan(conv("later-" + version))
        assert rules_of(store.plan(conv("one"))) == "RULES-V1"
        store.close()
    store = relay.start()
    got = {name: rules_of(store.plan(conv(name))) for name in ("one", "later-V2", "later-V3", "later-V4")}
    assert got == {"one": "RULES-V1", "later-V2": "RULES-V2", "later-V3": "RULES-V3", "later-V4": "RULES-V4"}
    assert len(relay.revisions()) <= 4


def test_a_pin_that_asks_for_a_profile_only_the_current_revision_has_moves_to_it_and_stays_moved(relay):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("chat"))
    first.close()
    relay.write(doc("V2", extra_profile={"extra": {"head": "X"}}))
    second = relay.start()
    plan = second.plan(conv("chat", virtual_profile="extra", requested_model="m"))
    assert plan.profile == "extra" and plan.rev == 1 and not plan.pinned
    relay.tick(second)
    third = relay.start()
    assert third.plan(conv("chat")).snapshot is third.snapshot()      # the pin was written as moved


def test_a_restored_pin_obeys_the_rules_of_its_own_revision(relay):
    first_doc = doc("V1")
    first_doc["rules"] = [{"name": "first", "use": "mine"}]
    relay.write(first_doc)
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    second_doc = doc("V2", extra_profile={"b": {"head": "B"}})
    second_doc["rules"] = [{"name": "second", "use": "b"}]
    relay.write(second_doc)
    second = relay.start()
    old, new = second.plan(conv("old")), second.plan(conv("new"))
    assert (old.profile, old.rule) == ("mine", "first") and (new.profile, new.rule) == ("b", "second")


def test_a_pin_that_names_a_revision_nobody_can_make_gets_the_current_one_and_is_pinned_afresh(relay):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    relay.write(doc("V2"))
    for path in (relay.pins / pp.REVISIONS_DIR).iterdir():
        path.unlink()
    second = relay.start()
    since = mark()
    plan = second.plan(conv("old"))
    assert rules_of(plan) == "RULES-V2" and not plan.pinned and plan.rev == 1
    assert second.plan(conv("old")).pinned                            # the pin it has now is on the current revision
    reasons = [event["data"]["reason"] for event in events(since, "prompt.pin_dropped", "prompt.pins_error")]
    assert reasons == ["revision_missing", "revision_missing"]


# -- what the admin sees ---------------------------------------------------------------------------------------------------


def test_the_pins_that_have_not_been_woken_are_listed_with_what_is_known_about_them(relay):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("same-revision"))
    first.plan(conv("same-revision", purpose="client-text"))
    first.close()
    relay.write(doc("V2"))
    second = relay.start()
    second.plan(conv("fresh"))
    relay.advance(100)
    listed = second.pins()
    assert [row["conversation"] for row in listed][0] == journal.tag("fresh")
    fresh, *asleep = listed
    assert "restored" not in fresh and set(fresh) == {"conversation", "purpose", "rev", "profile", "age_s"}
    assert {row["conversation"] for row in asleep} == {journal.tag("same-revision")}
    assert {row["purpose"] for row in asleep} == {"client-tools", "client-text"}
    assert all(row["restored"] is True and row["rev"] is None and row["profile"] == "mine" for row in asleep)
    assert all(100 <= row["age_s"] < 200 for row in asleep)           # the age is the wall-clock age across the restart
    woken = second.plan(conv("same-revision"))
    row = next(row for row in second.pins() if row["purpose"] == "client-tools" and row["rev"] == woken.rev)
    assert row["restored"] is True and row["rev"] == 2 and row["conversation"] == journal.tag("same-revision")
    assert len(second.pins()) == 3                                    # woken, not doubled


def test_a_dormant_pin_on_the_current_revision_shows_its_number_and_a_legacy_one_zero(relay):
    early = relay.start()
    early.plan(conv("early"))
    early.close()
    relay.write(doc("V1"))
    middle = relay.start()
    middle.plan(conv("middle"))
    middle.close()
    last = relay.start()
    rows = {row["conversation"]: row for row in last.pins()}
    assert rows[journal.tag("early")]["rev"] == pp.LEGACY_REV and rows[journal.tag("early")]["profile"] is None
    assert rows[journal.tag("middle")]["rev"] == last.snapshot().rev == 1


def test_unpinning_by_tag_by_key_or_all_reaches_the_pins_that_were_not_woken_and_a_restart_does_not_bring_them_back(relay):
    relay.write(doc("V1"))
    first = relay.start()
    for name in ("one", "two", "three"):
        first.plan(conv(name))
    first.plan(conv("one", purpose="client-text"))
    first.close()
    relay.write(doc("V2"))
    second = relay.start()
    assert second.unpin(journal.tag("one")) == 2                      # both purposes, by the tag the admin shows
    assert second.unpin("two") == 1                                   # by the raw key
    assert second.unpin("unknown") == 0 and second.unpin("") == 0
    assert [row["conversation"] for row in second.pins()] == [journal.tag("three")]
    third = relay.start()
    assert [row["conversation"] for row in third.pins()] == [journal.tag("three")]
    assert rules_of(third.plan(conv("one"))) == "RULES-V2" and rules_of(third.plan(conv("three"))) == "RULES-V1"
    assert third.unpin() == 2                                         # "apply to all": the live one and the sleeping one
    assert third.pins() == [] and relay.stored()["pins"] == {} and relay.revisions() == []


def test_applying_to_all_after_a_restart_moves_every_conversation_exactly_once(relay):
    relay.write(doc("V1"))
    first = relay.start()
    for name in ("a", "b"):
        first.plan(conv(name))
    first.close()
    relay.write(doc("V2"))
    second = relay.start()
    assert [rules_of(second.plan(conv(name))) for name in ("a", "b")] == ["RULES-V1"] * 2
    assert second.unpin(None) == 2
    one = [second.plan(conv(name)) for name in ("a", "b")]
    two = [second.plan(conv(name)) for name in ("a", "b")]
    assert [plan.pinned for plan in one] == [False, False] and [plan.pinned for plan in two] == [True, True]
    assert {rules_of(plan) for plan in one + two} == {"RULES-V2"}


def test_an_unpin_while_a_pin_is_being_woken_wins(relay, monkeypatch):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("chat"))
    first.close()
    relay.write(doc("V2"))
    second = relay.start()
    real = second._read_revision

    def unpinned_meanwhile(print_):
        second.unpin("chat")                                          # what an admin does while the revision is made
        return real(print_)

    monkeypatch.setattr(second, "_read_revision", unpinned_meanwhile)
    plan = second.plan(conv("chat"))
    assert rules_of(plan) == "RULES-V2" and not plan.pinned           # the unpin was not undone by the pin that came back


# -- a file that is not what it should be ----------------------------------------------------------------------------------


def broken_pins(relay, content):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    (relay.pins / pp.PINS_FILE).write_bytes(content)
    relay.write(doc("V2"))


CORRUPT_PINS = {
    "empty": (b"", "corrupt"), "not-json": (b"{not json", "corrupt"), "a-list": (b"[1, 2]", "corrupt"),
    "null": (b"null", "corrupt"), "binary": (b"\xff\xfe\x00garbage", "corrupt"),
    "future-format": (b'{"format": 2, "pins": {}}', "corrupt"), "no-pins": (b'{"format": 1}', "corrupt"),
    "pins-a-list": (b'{"format": 1, "pins": [1]}', "corrupt"), "format-true": (b'{"format": true, "pins": {}}', "corrupt"),
    "no-format": (b'{"pins": {}}', "corrupt"), "too-large": (b" " * (pp.MAX_PINS_BYTES + 1), "too_large")}


@pytest.mark.parametrize("name", sorted(CORRUPT_PINS))
def test_a_pins_file_that_is_not_one_is_ignored_and_the_conversation_gets_the_current_revision(relay, name):
    content, reason = CORRUPT_PINS[name]
    broken_pins(relay, content)
    since = mark()
    store = relay.start()
    plan = store.plan(conv("old"))
    assert rules_of(plan) == "RULES-V2" and not plan.pinned and store.pins()[0]["rev"] == 1
    problems = [event["data"]["reason"] for event in events(since, "prompt.pins_error")]
    assert problems == [reason]
    relay.tick(store)
    assert relay.stored()["pins"].keys() == {digest("old")}           # the next write replaces it with a good one


def test_a_record_that_is_not_a_pin_is_dropped_and_the_others_are_kept(relay):
    early = relay.start()                                             # a conversation that began before any prompts file
    early.plan(conv("early"))
    early.close()
    relay.write(doc("V1"))
    first = relay.start()
    for name in ("good", "other"):
        first.plan(conv(name))
    first.close()
    stored = relay.stored()
    good, legacy = stored["pins"][digest("good")], stored["pins"][digest("early")]
    assert legacy["rev"] == pp.LEGACY_REV
    bad = {digest("x1"): dict(good, tag="nothex"), digest("x2"): dict(good, rev="abc"),
           digest("x3"): dict(good, rev=1), digest("x4"): dict(good, touched="soon"),
           digest("x5"): dict(good, touched=True), digest("x6"): dict(good, created=float("nan")),
           digest("x7"): dict(good, purpose=7), digest("x8"): dict(good, profile=3),
           "short": good, digest("x9"): "not a dict", digest("xa"): {k: v for k, v in good.items() if k != "tag"},
           digest("xb"): dict(good, rev=None), digest("xc"): dict(legacy, rev=False), digest("xd"): dict(legacy, rev=0.0),
           digest("xe"): dict(legacy, rev=-1), digest("xf"): dict(good, rev=good["rev"].upper()),
           digest("y0"): {k: v for k, v in good.items() if k != "rev"}}
    stored["pins"].update(bad)
    (relay.pins / pp.PINS_FILE).write_text(json.dumps(stored), encoding="utf-8")
    since = mark()
    store = relay.start()
    kept = sorted(journal.tag(name) for name in ("good", "other", "early"))
    assert sorted(row["conversation"] for row in store.pins()) == kept
    (event,) = events(since, "prompt.pins_loaded")
    assert event["data"]["pins"] == 3 and event["data"]["invalid"] == len(bad) and event["data"]["legacy"] == 1
    relay.tick(store)
    assert relay.stored()["pins"].keys() == {digest("good"), digest("other"), digest("early")}


def forged_revision(relay, text):
    """Replace the stored revision by a file that passes the integrity check (it is named by the hash of its bytes) but is not
    what a revision is; the pins name it now. Returns its fingerprint."""
    (old,) = list((relay.pins / pp.REVISIONS_DIR).iterdir())
    blob = text.encode("utf-8")
    name = hashlib.sha256(blob).hexdigest()
    old.unlink()
    old.with_name(name + ".json").write_bytes(blob)
    stored = relay.stored()
    for row in stored["pins"].values():
        row["rev"] = name
    (relay.pins / pp.PINS_FILE).write_text(json.dumps(stored), encoding="utf-8")
    return name


DAMAGE = {
    "missing": (lambda relay, target: target.unlink(), "revision_missing"),
    "empty": (lambda relay, target: target.write_bytes(b""), "revision_corrupt"),
    "garbage": (lambda relay, target: target.write_bytes(b"\x00\x01 not json"), "revision_corrupt"),
    "another-document": (lambda relay, target: target.write_text(json.dumps(doc("TAMPERED")), encoding="utf-8"),
                         "revision_corrupt"),
    "too-large": (lambda relay, target: target.write_bytes(b" " * (pp.MAX_REVISION_BYTES + 1)), "revision_corrupt"),
    "invalid-document": (lambda relay, target: forged_revision(
        relay, json.dumps({"version": 1, "profiles": {"p": {"head": "{{nope}}"}}})), "revision_invalid"),
    "file-reference": (lambda relay, target: forged_revision(
        relay, json.dumps({"version": 1, "texts": {"a": {"file": "prompts/a.md"}}})), "revision_invalid"),
    "duplicate-keys": (lambda relay, target: forged_revision(relay, '{"version": 1, "version": 1}'), "revision_invalid"),
    "not-an-object": (lambda relay, target: forged_revision(relay, "[1, 2]"), "revision_invalid")}


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_a_revision_that_cannot_be_made_again_costs_the_conversation_one_reset_and_says_why(relay, damage):
    damaging, reason = DAMAGE[damage]
    relay.write(doc("V1"))
    first = relay.start()
    for name in ("a", "b"):
        first.plan(conv(name))
    fingerprint = first.snapshot().fingerprint
    first.close()
    result = damaging(relay, relay.pins / pp.REVISIONS_DIR / (fingerprint + ".json"))
    named = result if isinstance(result, str) else fingerprint
    relay.write(doc("V2"))
    since = mark()
    store = relay.start()
    one, two = store.plan(conv("a")), store.plan(conv("b"))
    assert rules_of(one) == rules_of(two) == "RULES-V2" and not one.pinned and not two.pinned
    assert store.plan(conv("a")).pinned and store.plan(conv("a")).rev == 1
    found = events(since, "prompt.pins_error")
    assert [event["data"]["reason"] for event in found] == [reason]    # said once for the revision, not once per pin
    assert found[0]["level"] == "warn" and found[0]["data"]["revision"] == named[:8]
    dropped = events(since, "prompt.pin_dropped")
    assert len(dropped) == 2 and {event["data"]["reason"] for event in dropped} == {reason}
    assert sorted(event["level"] for event in dropped) == ["info", "info"]
    assert store.snapshot().rev == 1 and sorted(store._history) == [1]


def test_a_revision_file_that_is_busy_for_a_moment_costs_one_conversation_and_not_the_next(relay, monkeypatch):
    relay.write(doc("V1"))
    first = relay.start()
    for name in ("a", "b"):
        first.plan(conv(name))
    first.close()
    relay.write(doc("V2"))
    second = relay.start()
    real, refusals = open, []

    def busy(file, *args, **kwargs):
        if str(file).endswith(".json") and pp.REVISIONS_DIR in str(file) and len(refusals) < pp.REPLACE_ATTEMPTS:
            refusals.append(file)
            raise PermissionError(13, "a scanner has it")
        return real(file, *args, **kwargs)

    monkeypatch.setattr(pp, "open", busy, raising=False)
    monkeypatch.setattr(pp, "REPLACE_PAUSE", 0)
    since = mark()
    unlucky = second.plan(conv("a"))
    assert rules_of(unlucky) == "RULES-V2" and not unlucky.pinned
    lucky = second.plan(conv("b"))
    assert rules_of(lucky) == "RULES-V1" and lucky.pinned             # a busy disk is not remembered as a broken revision
    assert [event["data"]["reason"] for event in events(since, "prompt.pins_error")] == ["revision_unreadable"]


def test_a_stored_revision_whose_document_is_too_big_for_this_build_is_refused(relay, monkeypatch):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    monkeypatch.setattr(pp, "MAX_REVISION_BYTES", 100)                # this build takes smaller revisions than the one before
    second = relay.start()
    since = mark()
    plan = second.plan(conv("old"))
    assert not plan.pinned and [event["data"]["reason"] for event in events(since, "prompt.pins_error")] == [
        "revision_corrupt"]


def test_a_revision_that_is_too_large_to_name_is_not_pinned_past_a_restart_and_the_journal_says_so(relay):
    (relay.folder / "prompts").mkdir()
    (relay.folder / "prompts" / "big.md").write_text("x" * (200 * 1024), encoding="utf-8")
    relay.write(document(default_profile="mine", texts={"t%d" % n: {"file": "prompts/big.md"} for n in range(60)},
                         profiles={"mine": {"head": "{{client_system}}"}}))
    since = mark()
    store = relay.start()
    assert store.snapshot().fingerprint is None
    assert [event["data"]["reason"] for event in events(since, "prompt.pins_error")] == ["revision_too_large"]
    plan = store.plan(conv("old"))
    assert plan.profile == "mine"
    relay.tick(store)
    assert relay.stored()["pins"] == {} and relay.revisions() == []   # nothing can name that revision
    store.save(doc("V2"), 1)
    store.plan(conv("old"))                                           # its pin is on the old revision still
    relay.tick(store)
    assert relay.stored()["pins"] == {}


def test_files_that_are_not_ours_stay_and_a_revision_nobody_names_goes(relay):
    relay.write(doc("V1"))
    store = relay.start()
    store.plan(conv("chat"))
    relay.tick(store)
    folder = relay.pins / pp.REVISIONS_DIR
    orphan = folder / (hashlib.sha256(b"orphan").hexdigest() + ".json")
    orphan.write_bytes(b"{}")
    stranger = folder / "notes.txt"
    stranger.write_text("mine", encoding="utf-8")
    uppercase = folder / (hashlib.sha256(b"x").hexdigest().upper() + ".json")
    uppercase.write_bytes(b"{}")
    a_folder = folder / (hashlib.sha256(b"dir").hexdigest() + ".json")
    a_folder.mkdir()
    old_temp = folder / ".pins-abc.tmp"
    old_temp.write_bytes(b"half")
    recent_temp = folder / ".pins-def.tmp"
    recent_temp.write_bytes(b"half")
    # In the folder of the pins file itself only the leftovers of its own writes are tidied: a revision-looking name there is
    # nobody's revision and is left alone, as is everything else.
    beside = relay.pins / (hashlib.sha256(b"beside").hexdigest() + ".json")
    beside.write_bytes(b"{}")
    note = relay.pins / "README.txt"
    note.write_text("not ours", encoding="utf-8")
    old_beside = relay.pins / ".pins-xyz.tmp"
    old_beside.write_bytes(b"half")
    long_ago = time.time() - 2 * pp.STALE_TEMP_SECONDS
    os.utime(old_temp, (long_ago, long_ago))
    os.utime(old_beside, (long_ago, long_ago))
    store.plan(conv("second"))
    relay.tick(store)
    assert not orphan.exists() and not old_temp.exists() and not old_beside.exists()
    assert stranger.exists() and uppercase.exists() and a_folder.is_dir() and recent_temp.exists()
    assert beside.exists() and note.exists()
    assert (folder / (store.snapshot().fingerprint + ".json")).exists()          # the one the pins name is kept


def test_a_document_of_an_earlier_run_that_is_damaged_is_replaced_by_the_revision_the_relay_still_has(relay):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    fingerprint = first.snapshot().fingerprint
    target = relay.pins / pp.REVISIONS_DIR / (fingerprint + ".json")
    target.write_bytes(b"a document the disk damaged")
    second = relay.start()                                            # the file is still V1: the pin is on the current one
    assert second.plan(conv("old")).snapshot is second.snapshot()
    relay.tick(second)
    assert target.read_bytes() == b"a document the disk damaged"      # waking a pin is not a reason to write anything
    relay.advance(pp.PIN_TOUCH_INTERVAL + 1)                          # the first write there is, here the pin's touch...
    second.plan(conv("old"))
    relay.tick(second)
    assert target.read_bytes() == second.snapshot()._canonical()      # ...puts the document there again, whole
    second.close()
    relay.write(doc("V2"))
    third = relay.start()
    assert rules_of(third.plan(conv("old"))) == "RULES-V1"            # and now a restart after an edit can use it


def test_a_document_that_goes_missing_while_the_relay_runs_is_written_again_with_the_next_change(relay):
    relay.write(doc("V1"))
    store = relay.start()
    store.plan(conv("one"))
    relay.tick(store)
    (relay.pins / pp.REVISIONS_DIR / (store.snapshot().fingerprint + ".json")).unlink()
    store.plan(conv("two"))
    relay.tick(store)
    assert relay.revisions() == [store.snapshot().fingerprint]
    assert set(relay.stored()["pins"]) == {digest("one"), digest("two")}


# -- how long a pin lives --------------------------------------------------------------------------------------------------


def test_a_revision_goes_with_the_last_pin_that_names_it(relay):
    relay.write(doc("V1"))
    store = relay.start()
    store.plan(conv("one"))
    store.plan(conv("two"))
    first = store.snapshot().fingerprint
    relay.tick(store)
    store.save(doc("V2"), 1)
    store.plan(conv("three"))
    relay.tick(store)
    assert relay.revisions() == sorted([first, store.snapshot().fingerprint])
    store.unpin("one")
    assert first in relay.revisions()                                 # "two" still names it
    store.unpin("two")
    assert relay.revisions() == [store.snapshot().fingerprint]


def test_pins_idle_for_hours_go_from_the_file_and_so_does_their_revision(relay):
    relay.write(doc("V1"))
    store = relay.start()
    store.plan(conv("idle"))
    relay.tick(store)
    old = store.snapshot().fingerprint
    store.save(doc("V2"), 1)
    relay.advance(pp.PIN_IDLE_SECONDS + 10)
    store.plan(conv("busy"))
    relay.tick(store)
    assert list(relay.stored()["pins"]) == [digest("busy")]
    assert old not in relay.revisions()


def test_a_pin_idle_for_hours_before_a_restart_is_not_brought_back(relay):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("idle"))
    first.plan(conv("recent"))
    first.close()
    relay.advance(pp.PIN_IDLE_SECONDS - 100)
    second = relay.start()                                            # a restart that takes the pins at their age then
    second.plan(conv("recent"))
    relay.tick(second)
    second.close()
    relay.write(doc("V2"))
    relay.advance(200)                                                # "idle" is now past the limit, "recent" is not
    third = relay.start()
    assert [row["conversation"] for row in third.pins()] == [journal.tag("recent")]
    assert rules_of(third.plan(conv("idle"))) == "RULES-V2" and rules_of(third.plan(conv("recent"))) == "RULES-V1"


def test_a_pin_that_ran_out_between_the_load_and_the_conversation_coming_back_is_not_woken(relay):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("old"))
    first.close()
    relay.write(doc("V2"))
    second = relay.start()
    relay.advance(pp.PIN_IDLE_SECONDS + 1)
    since = mark()
    plan = second.plan(conv("old"))
    assert rules_of(plan) == "RULES-V2" and not plan.pinned
    assert [event["data"]["reason"] for event in events(since, "prompt.pin_dropped")] == ["expired"]
    assert second.pins() and all("restored" not in row for row in second.pins())


def test_the_number_of_pins_on_disk_is_bounded_and_the_least_recently_used_go_first(relay, monkeypatch):
    monkeypatch.setattr(pp, "MAX_PINS", 4)
    relay.write(doc("V1"))
    first = relay.start()
    for name in ("c1", "c2", "c3", "c4"):
        relay.advance(1)
        first.plan(conv(name))
    first.close()
    second = relay.start()
    for name in ("n1", "n2"):
        relay.advance(1)
        second.plan(conv(name))
    relay.tick(second)
    assert set(relay.stored()["pins"]) == {digest(name) for name in ("c3", "c4", "n1", "n2")}
    assert len(second.pins()) == 4


def test_too_many_pins_on_disk_at_the_start_are_cut_to_the_limit_keeping_the_newest(relay, monkeypatch):
    relay.write(doc("V1"))
    first = relay.start()
    for name in ("c1", "c2", "c3", "c4", "c5"):
        relay.advance(1)
        first.plan(conv(name))
    first.close()
    monkeypatch.setattr(pp, "MAX_PINS", 3)
    since = mark()
    second = relay.start()
    assert sorted(row["conversation"] for row in second.pins()) == sorted(journal.tag(name) for name in ("c3", "c4", "c5"))
    assert events(since, "prompt.pins_loaded")[0]["data"]["over_limit"] == 2


# -- the disk misbehaves ---------------------------------------------------------------------------------------------------


def test_a_disk_that_fails_never_breaks_a_plan_says_so_once_and_the_next_look_tries_again(relay, monkeypatch):
    relay.write(doc("V1"))
    store = relay.start()
    broken = {"on": True}
    real = os.replace

    def replace(source, target):
        if broken["on"]:
            raise OSError(28, "no space left on device")
        return real(source, target)

    monkeypatch.setattr(pp.os, "replace", replace)
    since = mark()
    for index in range(4):
        plan = store.plan(conv("chat-%d" % index))
        assert plan.profile == "mine"
        relay.tick(store)                                             # a failed write, again and again
    failures = events(since, "prompt.pins_error")
    assert len(failures) == 1 and failures[0]["level"] == "warn" and failures[0]["data"]["reason"] == "write"
    assert failures[0]["data"]["errno"] == 28 and "Traceback" not in json.dumps(failures[0])
    assert not (relay.pins / pp.PINS_FILE).exists() and [name for name in relay.files() if name.endswith(".tmp")] == []
    broken["on"] = False
    relay.tick(store)
    assert len(relay.stored()["pins"]) == 4
    recovered = events(since, "prompt.pins_saved")
    assert [event["level"] for event in recovered if event["level"] == "info"] == ["info"]
    broken["on"] = True
    store.plan(conv("again"))
    relay.tick(store)
    assert len(events(since, "prompt.pins_error")) == 2               # a new failure after the recovery is news again


def test_a_failing_write_is_retried_no_sooner_than_one_interval(relay, monkeypatch):
    relay.write(doc("V1"))
    store = relay.start()
    attempts = []

    def replace(source, target):
        attempts.append(1)
        raise OSError(5, "I/O error")

    monkeypatch.setattr(pp.os, "replace", replace)
    store.plan(conv("chat"))
    store.maybe_reload()
    first = len(attempts)
    for _ in range(10):
        store.maybe_reload()
    assert len(attempts) == first
    relay.tick(store)
    assert len(attempts) == 2 * first


def test_a_folder_that_cannot_be_made_is_the_same_story(tmp_path):
    blocked = tmp_path / "state"
    blocked.write_text("a file where the folder should be", encoding="utf-8")
    relay = Relay(tmp_path)
    relay.cfg = {"conversation_state_dir": str(blocked / "conversations")}
    relay.write(doc("V1"))
    store = relay.start()
    since = mark()
    plan = store.plan(conv("chat"))
    relay.tick(store)
    store.close()
    assert plan.profile == "mine" and store.plan(conv("chat")).pinned
    assert [event["data"]["reason"] for event in events(since, "prompt.pins_error")] == ["write"]


def test_a_target_that_a_scanner_holds_for_a_moment_is_replaced_on_a_retry_like_the_prompts_file(relay, monkeypatch):
    relay.write(doc("V1"))
    store = relay.start()
    real, attempts = os.replace, []

    def flaky(source, target):
        attempts.append(pathlib.Path(target).name)
        if len(attempts) < 3:
            raise PermissionError(13, "in use")
        return real(source, target)

    monkeypatch.setattr(pp, "REPLACE_PAUSE", 0)
    monkeypatch.setattr(pp.os, "replace", flaky)
    since = mark()
    store.plan(conv("chat"))
    relay.tick(store)
    assert len(attempts) >= 3 and len(relay.stored()["pins"]) == 1
    assert [name for name in relay.files() if name.endswith(".tmp")] == []
    assert events(since, "prompt.pins_error") == []


def test_a_write_that_stops_halfway_leaves_the_last_complete_file(relay, monkeypatch):
    relay.write(doc("V1"))
    store = relay.start()
    store.plan(conv("one"))
    relay.tick(store)
    before = (relay.pins / pp.PINS_FILE).read_bytes()
    monkeypatch.setattr(pp.os, "replace", lambda source, target: (_ for _ in ()).throw(KeyboardInterrupt()))
    store.plan(conv("two"))
    with pytest.raises(KeyboardInterrupt):
        store._flush_pins(force=True)                                 # an interruption is not swallowed...
    monkeypatch.undo()
    assert (relay.pins / pp.PINS_FILE).read_bytes() == before         # ...and the file is the last complete one
    assert [name for name in relay.files() if name.endswith(".tmp")] == []


# -- privacy (I7) ------------------------------------------------------------------------------------------------------------


def test_the_journal_gets_tags_counts_and_reasons_never_a_key_or_a_text(relay):
    since = mark()
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv(KEY))
    lost = first.save(doc("V2"), 1).fingerprint
    first.plan(conv(KEY, purpose="client-text"))
    first.save(doc("V3"), 2)
    relay.tick(first)
    first.close()
    (relay.pins / pp.REVISIONS_DIR / (lost + ".json")).unlink()
    second = relay.start()
    second.plan(conv(KEY))
    second.plan(conv(KEY, purpose="client-text"))
    second.unpin(KEY)
    relay.tick(second)
    second.close()
    found = events(since)
    kinds = {event["kind"] for event in found}
    assert {"prompt.pins_saved", "prompt.pins_loaded", "prompt.pin_restored", "prompt.pin_dropped",
            "prompt.revision_restored", "prompt.pins_error"} <= kinds
    text = json.dumps(found, ensure_ascii=False)
    assert KEY not in text and "secret-session-id" not in text and SECRET not in text and "RULES-V" not in text
    assert lost[:8] in text and lost not in text and journal.tag(KEY) in text


# -- many threads ----------------------------------------------------------------------------------------------------------


def test_planning_unpinning_reloading_and_closing_together_never_leave_a_damaged_file(relay):
    relay.write(doc("V0"))
    first = relay.start()
    for number in range(20):
        first.plan(conv("seed-%d" % number))
    first.close()
    relay.write(doc("V1"))
    store = relay.start()
    failures, stop = [], threading.Event()

    class Moving(Clock):
        """A clock that moves a little at every look, as time does."""
        def __call__(self):
            self.now += 0.7
            return self.now

    store.clock = Moving(relay.mono.now)

    def guard(work):
        def run():
            try:
                work()
            except BaseException as exc:      # noqa: BLE001 - the test reports whatever a thread hit
                failures.append(repr(exc))
                stop.set()
        return run

    def planner(name):
        def work():
            number = 0
            while not stop.is_set():
                number += 1
                store.maybe_reload()
                plan = store.plan(conv("%s-%d" % (name, number % 12)))
                assert plan.snapshot is None or plan.rev == plan.snapshot.rev
                store.pins()
        return work

    def waker():
        for number in range(20):
            if stop.is_set():
                return
            assert store.plan(conv("seed-%d" % number)).profile == "mine"

    def unpinner():
        for number in range(30):
            if stop.is_set():
                return
            store.unpin("seed-%d" % (number % 20))
            store.unpin("p0-%d" % (number % 12))

    def saver():
        for index in range(6):
            if stop.is_set():
                return
            try:
                store.save(doc("S%d" % index), store.snapshot().rev)
            except apierr.ApiError as exc:
                assert exc.status in (409, 503)

    def reader():
        path = relay.pins / pp.PINS_FILE
        while not stop.is_set():
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            assert json.loads(blob)["format"] == pp.PINS_FORMAT, "a damaged pins file was seen"

    workers = [threading.Thread(target=guard(work)) for work in
               (planner("p0"), planner("p1"), waker, unpinner, saver, reader)]
    for worker in workers:
        worker.start()
    for worker in workers[2:5]:
        worker.join(timeout=60)
    stop.set()
    for worker in workers:
        worker.join(timeout=60)
    store.close()
    assert failures == [] and all(not worker.is_alive() for worker in workers)
    stored = relay.stored()
    assert stored["format"] == pp.PINS_FORMAT
    named = {row["rev"] for row in stored["pins"].values() if "rev" in row}
    assert named <= set(relay.revisions()) and set(relay.revisions()) <= named | {None}
    revs = sorted(store._history)
    assert revs == sorted(set(revs)) and len(revs) <= pp.SNAPSHOT_HISTORY


# -- the history and the numbers ---------------------------------------------------------------------------------------------


def test_restored_revisions_never_push_the_current_one_out_of_the_history(relay, monkeypatch):
    monkeypatch.setattr(pp, "SNAPSHOT_HISTORY", 4)
    relay.write(doc("V0"))
    first = relay.start()
    for number in range(1, 8):
        relay.write(doc("V%d" % number))
        relay.tick(first)
        first.plan(conv("c%d" % number))
    first.close()
    second = relay.start()
    current = second.snapshot()
    got = [rules_of(second.plan(conv("c%d" % number))) for number in range(1, 8)]
    assert got[-1] == "RULES-V7"                                      # the newest revision is the current one
    assert second.snapshot() is current and current.rev in second._history and len(second._history) <= 4
    assert second.plan(conv("brand-new")).snapshot is current


def test_a_pin_whose_restored_revision_was_pushed_out_gets_the_current_one_like_any_other(relay, monkeypatch):
    monkeypatch.setattr(pp, "SNAPSHOT_HISTORY", 2)
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("a"))
    relay.write(doc("V2"))
    relay.tick(first)
    first.plan(conv("b"))
    first.close()
    relay.write(doc("V3"))
    second = relay.start()
    assert rules_of(second.plan(conv("a"))) == "RULES-V1"
    assert rules_of(second.plan(conv("b"))) == "RULES-V2"             # makes room by pushing the first one out
    assert sorted(second._history) == [1, 3] and second.snapshot().rev == 1
    again = second.plan(conv("a"))
    assert rules_of(again) == "RULES-V3" and not again.pinned and again.rev == 1


def test_the_digest_of_a_pin_is_fixed_by_what_it_names():
    assert pp._pin_digest(("header:abc", "client-tools")) == hashlib.sha256(
        b'["header:abc","client-tools"]').hexdigest()
    assert pp._pin_digest(("Привет", "p")) == hashlib.sha256(b'["\\u041f\\u0440\\u0438\\u0432\\u0435\\u0442","p"]').hexdigest()
    assert pp._pin_digest(("a", "b")) != pp._pin_digest(("b", "a")) != pp._pin_digest(("ab", ""))
    assert pp._pin_digest((object(), "p")) is None


# -- cost ---------------------------------------------------------------------------------------------------------------------


def timed(function, runs=500):
    times = []
    for _ in range(runs):
        started = time.perf_counter()
        function()
        times.append((time.perf_counter() - started) * 1000)
    return min(times), sorted(times)[len(times) // 2]


def test_planning_with_the_pins_kept_on_disk_costs_what_it_cost(relay, monkeypatch):
    relay.write(doc("V1"))
    first = relay.start()
    first.plan(conv("steady"))
    first.close()
    store = relay.start()
    request = conv("steady")
    store.plan(request)
    relay.tick(store)
    seen = spy_replace(monkeypatch)
    monkeypatch.setattr(pp.tempfile, "NamedTemporaryFile", lambda *a, **k: pytest.fail("plan() touched the disk"))
    fastest, median = timed(lambda: store.plan(request))
    assert fastest < 0.5 and median < 1.5, (fastest, median)
    other = conv("elsewhere")
    fastest, median = timed(lambda: store.plan(other))
    assert fastest < 0.5 and median < 1.5, (fastest, median)
    assert seen == []


def test_planning_while_dormant_pins_wait_costs_a_hash_not_a_disk_read(relay, monkeypatch):
    relay.write(doc("V1"))
    first = relay.start()
    for number in range(200):
        first.plan(conv("asleep-%d" % number))
    first.close()
    store = relay.start()
    assert len(store._dormant) == 200
    live = conv("live")
    store.plan(live)
    monkeypatch.setattr(pp, "open", lambda *a, **k: pytest.fail("plan() read a file"), raising=False)
    fastest, median = timed(lambda: store.plan(live))
    assert fastest < 0.5 and median < 1.5, (fastest, median)
    fresh = [conv("nobody-%d" % number) for number in range(500)]
    started = time.perf_counter()
    for request in fresh:
        store.plan(request)
    assert (time.perf_counter() - started) / len(fresh) < 0.002
