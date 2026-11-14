"""The pins and the stored conversations (C1) share one directory: neither touches what is the other's.

<conversation_state_dir> holds the *.state files of C1 and, in its own folder, prompt-pins/. The scan of C1 reads the directory
it is given and removes what it cannot use; it must not recurse into, read or remove anything of the pins. The pins' own
tidying works inside prompt-pins/ and never looks at the state files.
"""
import hashlib
import json
import os
import threading
import time

import conversation_state as cs
import journal
import prompt_policy as pp
from test_conversation_state import capture_of, files, stored
from test_prompt_pins_persist_store import Relay, conv, doc, rules_of


def tree(folder):
    """{relative path: bytes} of every file under the folder (directories as None): what a scan must leave as it was."""
    found = {}
    for path in sorted(folder.rglob("*")):
        found[str(path.relative_to(folder))] = path.read_bytes() if path.is_file() else None
    return found


def pins_with_a_state(tmp_path):
    """A relay that kept a pin, and a C1 store with one stored conversation, on one directory."""
    relay = Relay(tmp_path)
    relay.write(doc("V1"))
    store = relay.start()
    store.plan(conv("old"))
    store.save(doc("V2"), 1)
    store.plan(conv("newer"))
    relay.tick(store)
    conversations = cs.ConversationStore(relay.state)
    conversations.submit(capture_of("busy"))
    assert conversations.flush(5)
    return relay, store, conversations


def test_the_scan_of_the_conversation_store_leaves_the_pins_folder_as_it_was(tmp_path):
    relay, store, conversations = pins_with_a_state(tmp_path)
    conversations.close()
    folder = relay.pins
    (folder / pp.REVISIONS_DIR / ".pins-leftover.tmp").write_bytes(b"half a write")       # a dead write of the pins
    old = time.time() - 10 * 86400
    os.utime(folder / pp.REVISIONS_DIR / ".pins-leftover.tmp", (old, old))
    (folder / "notes.txt").write_text("somebody's note", encoding="utf-8")
    before = tree(relay.state)
    assert (folder / pp.PINS_FILE).exists() and len(relay.revisions()) >= 2
    seq = journal.JOURNAL.last_seq
    rescanned = cs.ConversationStore(relay.state)
    rescanned.scan()
    assert tree(relay.state) == before
    assert files(relay.state) == [cs.key_digest("busy") + cs.SUFFIX] and rescanned.peek("busy") is not None
    assert not stored(seq, "conversation_state.dropped")
    scans = stored(seq, "conversation_state.scan")
    assert len(scans) == 2 and all(item["data"]["files"] == 1 and item["data"]["found"] <= 1 for item in scans)
    assert scans[0]["data"]["found"] == 1
    rescanned.close()


def test_the_scan_with_every_limit_at_its_tightest_still_leaves_the_pins_alone(tmp_path):
    relay, store, conversations = pins_with_a_state(tmp_path)
    conversations.close()
    before = {name: content for name, content in tree(relay.state).items() if name.startswith(pp.PINS_DIR)}
    tight = cs.ConversationStore(relay.state, ttl=1, max_conversations=1, max_total_bytes=1, max_file_bytes=1)
    tight.close()
    assert not files(relay.state)                                  # the state file went by its own limits...
    assert {name: content for name, content in tree(relay.state).items() if name.startswith(pp.PINS_DIR)} == before


def test_the_pins_never_touch_the_state_files_the_leftovers_or_the_files_of_others(tmp_path):
    relay = Relay(tmp_path)
    relay.write(doc("V1"))
    relay.state.mkdir(parents=True)
    conversations = cs.ConversationStore(relay.state)
    conversations.submit(capture_of("busy"))
    assert conversations.flush(5)
    conversations.close()
    others = {"notes.txt": b"not ours", "game.state": b"somebody else's save game",
              cs.TEMP_PREFIX + cs.key_digest("dead") + "-00000000": b"a dead write of C1",
              hashlib.sha256(b"x").hexdigest() + ".json": b"{}"}
    for name, content in others.items():
        (relay.state / name).write_bytes(content)
    before = tree(relay.state)
    store = relay.start()
    store.plan(conv("chat"))
    relay.tick(store)
    store.plan(conv("second"))
    relay.tick(store)
    store.unpin("chat")
    store.unpin(None)
    store.close()
    after = tree(relay.state)
    assert {name: content for name, content in after.items() if not name.startswith(pp.PINS_DIR)} == before
    assert (relay.pins / pp.PINS_FILE).exists()


def run_together(*works):
    """Run the callables on threads; returns the failures they hit (reprs). Each has 60 seconds."""
    failures = []

    def guard(work):
        def run():
            try:
                work()
            except BaseException as exc:      # noqa: BLE001 - the test reports whatever a thread hit
                failures.append(repr(exc))
        return run

    workers = [threading.Thread(target=guard(work)) for work in works]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    assert all(not worker.is_alive() for worker in workers), "a thread did not finish"
    return failures


def churn_pins(relay, store, rounds=40):
    """What the runtime does to the store on a busy relay: plans, looks at the file (which writes the pins), unpins."""
    for number in range(rounds):
        store.plan(conv("chat-%d" % number))
        relay.advance(2)
        store.maybe_reload()
        if number % 7 == 0:
            store.unpin("chat-%d" % (number // 2))


def restarts_after_an_edit(relay):
    """A relay that starts after the prompts became V2: the first conversation it hears of from before is still on V1."""
    relay.write(doc("V2"))
    restarted = relay.start()
    assert json.loads((relay.pins / pp.PINS_FILE).read_text(encoding="utf-8"))["format"] == pp.PINS_FORMAT
    assert rules_of(restarted.plan(conv("chat-39"))) == "RULES-V1" and rules_of(restarted.plan(conv("new"))) == "RULES-V2"


def test_the_conversation_store_and_the_pins_write_the_same_directory_at_once_and_each_reads_back_whole(tmp_path):
    relay = Relay(tmp_path)
    relay.write(doc("V1"))
    relay.state.mkdir(parents=True)
    conversations = cs.ConversationStore(relay.state)
    store = relay.start()

    def writer():
        for number in range(40):
            conversations.submit(capture_of("busy", pad=number))
            if number % 5 == 0:
                assert conversations.flush(5)

    assert run_together(writer, lambda: churn_pins(relay, store)) == []
    assert conversations.flush(5)
    conversations.close()
    store.close()
    assert cs.ConversationStore(relay.state).load("busy") is not None
    restarts_after_an_edit(relay)


def test_a_scan_that_runs_while_the_pins_are_being_written_removes_none_of_their_files(tmp_path):
    # One relay owns a state directory, so no conversation writer runs beside a scan here: the scan of a starting relay
    # (it removes the leftovers of dead writes) meets only the pins of the relay that is still going down or coming up.
    relay = Relay(tmp_path)
    relay.write(doc("V1"))
    relay.state.mkdir(parents=True)
    store = relay.start()
    stop = threading.Event()

    def scanner():
        while not stop.is_set():
            cs.ConversationStore(relay.state).close()

    def pinner():
        try:
            churn_pins(relay, store)
        finally:
            stop.set()

    assert run_together(pinner, scanner) == []
    store.close()
    restarts_after_an_edit(relay)
