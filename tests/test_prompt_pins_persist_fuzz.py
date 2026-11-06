"""A seeded model of the pins (review R3-02): random requests, edits of the prompts file, restarts, unpins and idle time.

The oracle is a dozen lines: a conversation that has a pin gets the version it was pinned to, one that has none gets the version
the file has now (or the legacy path when there is no file), a pin that has been idle for hours is gone, an unpin removes it.
A restart, clean or killed, must change nothing the oracle says, and the folder on disk must hold exactly the documents the
pins file names. Time moves in steps that stay clear of the idle limit (the stored touch is rough on purpose).
"""
import random

import pytest

import journal
import prompt_policy as pp
from test_prompt_pins_persist_store import Relay, conv, doc, mark, rules_of

VERSIONS = ["V%d" % number for number in range(6)]
NAMES = ["c%d" % number for number in range(6)]
PURPOSES = ["client-tools", "client-text"]
# Idle jumps: well under the limit (3300 s, up to six of them in a row stay below it) or well over it, plus steps of seconds.
SHORT, LONG = 3300, 7 * 3600
ACTIONS = (["plan"] * 8 + ["edit"] * 2 + ["restart", "kill", "unpin", "unpin_all", "idle", "remove", "tiny", "tiny"])


class Oracle:
    def __init__(self, version):
        self.current = version                  # the version the file has now, None without a file
        self.pins = {}                          # (name, purpose) -> [version or None for legacy, wall time of the last use]

    def expected(self, key, now):
        """(version the request must get, whether it is pinned); the pin it leaves behind is kept."""
        pin = self.pins.get(key)
        if pin is not None and now - pin[1] > pp.PIN_IDLE_SECONDS:
            del self.pins[key]
            pin = None
        pinned = pin is not None
        if pin is None:
            pin = self.pins[key] = [self.current, now]
        pin[1] = now
        return pin[0], pinned


def check_disk(relay):
    """The pins file parses, and the revisions folder holds exactly the documents it names."""
    if not (relay.pins / pp.PINS_FILE).exists():
        return
    stored = relay.stored()
    assert stored["format"] == pp.PINS_FORMAT and len(stored["pins"]) <= pp.MAX_PINS
    named = {row["rev"] for row in stored["pins"].values() if row["rev"] != pp.LEGACY_REV}
    assert set(relay.revisions()) == named, "the documents on disk are not the ones the pins name"


def run(seed, tmp_path):
    """One seeded story of a relay and the files it keeps; returns the kinds of step it took."""
    rng = random.Random(seed)
    relay = Relay(tmp_path)
    start = rng.choice([None] + VERSIONS)
    if start is not None:
        relay.write(doc(start))
    oracle = Oracle(start)
    store = relay.start()
    taken = []
    for step in range(90):
        action = rng.choice(ACTIONS)
        taken.append(action)
        where = (seed, step, action, taken[-12:])
        if action == "plan":
            name, purpose = rng.choice(NAMES), rng.choice(PURPOSES)
            version, pinned = oracle.expected((name, purpose), relay.wall())
            plan = store.plan(conv(name, purpose=purpose))
            if version is None:
                assert plan.legacy, where
            else:
                assert not plan.legacy, where
                assert rules_of(plan) == "RULES-" + version, where
            assert plan.pinned is pinned, where
        elif action == "edit":
            version = rng.choice(VERSIONS)
            relay.write(doc(version))
            oracle.current = version
            relay.tick(store)
            check_disk(relay)
        elif action == "remove":
            if relay.prompts.exists():
                relay.prompts.unlink()
                relay.tick(store)
                relay.tick(store)               # a removal is believed at the second look
                oracle.current = None
        elif action in ("restart", "kill"):
            if action == "restart":
                store.close()
            else:
                relay.tick(store)               # whatever was going to be written has been: the process dies now
            store = relay.start()
            check_disk(relay)
        elif action == "unpin":
            name = rng.choice(NAMES)
            store.unpin(name)
            for purpose in PURPOSES:
                oracle.pins.pop((name, purpose), None)
            check_disk(relay)
        elif action == "unpin_all":
            store.unpin(None)
            oracle.pins.clear()
            check_disk(relay)
        elif action == "idle":
            relay.advance(rng.choice([SHORT, SHORT, LONG]))
            store.maybe_reload()
        else:
            relay.advance(rng.randint(1, 4))
            store.maybe_reload()
    store.close()
    check_disk(relay)
    return taken


@pytest.fixture(autouse=True)
def every_revision_is_kept(monkeypatch):
    monkeypatch.setattr(pp, "SNAPSHOT_HISTORY", 10_000)                # no revision is pushed out in these stories


@pytest.mark.parametrize("seed", range(60))
def test_the_pins_behave_like_the_oracle_through_edits_restarts_kills_unpins_and_idle_time(tmp_path, seed):
    assert "plan" in run(seed, tmp_path)


def test_the_stories_reach_every_kind_of_step_and_restarts_that_make_a_revision_again(tmp_path):
    since, kinds = mark(), set()
    for seed in range(30):
        folder = tmp_path / ("seed-%d" % seed)
        folder.mkdir()
        kinds |= set(run(seed, folder))
    assert kinds == set(ACTIONS)
    found = journal.JOURNAL.query(since=since, limit=20000, order="asc")
    counts = {kind: sum(1 for event in found if event["kind"] == kind)
              for kind in ("prompt.revision_restored", "prompt.pin_restored", "prompt.pin_dropped", "prompt.pins_saved")}
    assert counts["prompt.revision_restored"] > 10 and counts["prompt.pin_restored"] > 20, counts
    assert counts["prompt.pins_saved"] > 50, counts
