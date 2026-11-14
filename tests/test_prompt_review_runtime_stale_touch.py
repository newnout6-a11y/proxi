"""R4-05: the window of a retry after a failed ping counts from the newest touch of the cache, read after the failure."""
import asyncio

import direct
import journal
from keepwarm_harness import CFG, START, install, session as warm_session, stops

BODY = {"model": "claude-opus-5-5"}


def drive_with_hanging_ping(monkeypatch, during_the_hang, *, seconds=900, fail=True, **fields):
    """The first ping hangs for 100 s (the read timeout is 120 s) and fails (or succeeds); `during_the_hang(owner, clock)`
    does what happens to the session meanwhile, 40 s into it."""
    calls = []

    def ping(owner, body):
        calls.append(clock.now)
        if len(calls) == 1:
            clock.now += 40
            during_the_hang(owner, clock)
            clock.now += 60
            if fail:
                raise TimeoutError("synthetic read timeout")
        return 0.1

    target = warm_session(_warm_at=START - 100, **fields)  # the first ping is due at START + 140
    clock, pings = install(monkeypatch, seconds=seconds, ping=ping)
    mark = journal.JOURNAL.last_seq
    asyncio.run(direct._keepwarm(target, dict(CFG, turn_timeout=5000)))
    return target, pings, stops(mark)


def a_turn_begins(owner, clock):
    """The user's next request reads the cache at START + 180 and is still in flight."""
    owner._busy, owner._turn_started_at, owner._turn_seq = True, clock.now, owner._turn_seq + 1
    owner._turn_body, owner._turn_identity = dict(BODY, openAICacheKey="k"), {}


def a_turn_begins_and_ends(owner, clock):
    """The same request, answered before the ping fails: the turn's end moved the touch (as iter_events does)."""
    owner._warm_at = clock.now
    owner._turn_seq += 1
    owner._last_request_at = clock.now + 20


def nothing_happens(owner, clock):
    pass


def test_a_turn_that_begins_while_the_ping_hangs_is_a_touch_the_retry_is_judged_against(monkeypatch):
    target, pings, journaled = drive_with_hanging_ping(monkeypatch, a_turn_begins)
    # the turn began at START + 180: its own ping is due at 380 (offset 200) and the entry lives until START + 460
    assert not [stop for stop in journaled if stop["reason"] == "cache_expired"]
    assert any(t >= START + 300 for t in pings), "no ping for the live turn that touched the cache"
    assert round(pings[0] - START) == 140 and round(target._turn_started_at - START) == 180


def test_a_turn_that_began_and_ended_while_the_ping_hangs_is_counted_too(monkeypatch):
    target, pings, journaled = drive_with_hanging_ping(monkeypatch, a_turn_begins_and_ends, seconds=700)
    assert not [stop for stop in journaled if stop["reason"] == "cache_expired"]
    # the turn touched the cache at START + 180, so the next ping is due 240 s after that, not 10 s after the failure
    assert [round(t - START) for t in pings[:2]] == [140, 420]


def test_without_a_newer_touch_the_retry_is_still_judged_against_the_old_one(monkeypatch):
    target, pings, journaled = drive_with_hanging_ping(monkeypatch, nothing_happens)
    [stop] = journaled
    assert (stop["reason"], stop["after_failure"], stop["failures"], stop["pings"]) == ("cache_expired", True, 1, 1)
    assert stop["touch_age_seconds"] == 340 and [round(t - START) for t in pings] == [140]  # from START - 100


def test_a_touch_never_moves_back_when_a_ping_that_began_earlier_succeeds(monkeypatch):
    target, pings, _ = drive_with_hanging_ping(monkeypatch, a_turn_begins_and_ends, seconds=60 + 140 + 100, fail=False)
    # the turn touched at START + 180, the ping began at START + 140: the later one is the touch
    assert round(target._warm_at - START) == 180 and [round(t - START) for t in pings] == [140]


def test_a_touch_a_ping_makes_is_still_recorded_when_nothing_else_touched(monkeypatch):
    target, pings, _ = drive_with_hanging_ping(monkeypatch, nothing_happens, seconds=60 + 140 + 100, fail=False)
    assert round(target._warm_at - START) == 140
