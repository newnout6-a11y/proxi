"""Keepwarm pays only for a cache it can still read: the TTL window and the retries inside it."""
import asyncio

import pytest

import direct
import journal
from keepwarm_harness import CFG, START, drive, install, session as warm_session, stops

BODY = {"model": "claude-opus-5-5"}


def cycle(monkeypatch, target, cfg=None, *, seconds=3000, ping=None):
    """Like drive(), for a ping that needs the simulated clock: ping(clock, owner, body)."""
    clock, pings = install(monkeypatch, seconds=seconds,
                           ping=None if ping is None else (lambda owner, body: ping(clock, owner, body)))
    asyncio.run(direct._keepwarm(target, dict(CFG, **(cfg or {}))))
    return clock, pings


def events(mark, kind):
    return [dict(e["data"], level=e["level"]) for e in journal.JOURNAL.query(since=mark, limit=1000)
            if e["kind"] == kind]


def failing(*_args):
    raise OSError("synthetic failure")


def test_the_window_is_the_ttl_less_a_margin():
    assert (direct.PROMPT_CACHE_TTL, direct.PROMPT_CACHE_MARGIN) == (300, 20)
    assert direct.PING_DEADLINE == 280
    assert direct.PING_LATEST < direct.PING_DEADLINE, "a ping is never scheduled for the moment it would be refused"


# -- the window ----------------------------------------------------------------------------------
@pytest.mark.parametrize("age", [281, 290, 600, 3600])
def test_an_idle_ping_is_never_sent_for_a_cache_older_than_the_window(monkeypatch, age):
    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, warm_session(_warm_at=START - age), seconds=1800)
    assert pings == [] and clock.now - START < 2, "the cycle ends at once instead of polling until the idle limit"
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"], stop["credits"], stop["limit"], stop["level"]) == (
        "cache_expired", 0, 0, direct.PING_DEADLINE, "info")
    assert stop["touch_age_seconds"] == pytest.approx(age, abs=2)
    assert stop["during_turn"] is False and "after_failure" not in stop
    assert stop["account"] == journal.tag("acc-keepwarm")


@pytest.mark.parametrize("age", [240, 270, 279])
def test_a_ping_is_still_sent_while_the_touch_is_inside_the_window(monkeypatch, age):
    mark = journal.JOURNAL.last_seq
    _, pings = drive(monkeypatch, warm_session(_warm_at=START - age), seconds=30)
    assert len(pings) == 1 and pings[0] - (START - age) <= direct.PING_DEADLINE
    assert [s["reason"] for s in stops(mark)] == ["closed"], "only the end of the simulation stopped it"


def test_a_session_without_any_recorded_touch_is_treated_as_expired_not_as_fresh(monkeypatch):
    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, warm_session(_warm_at=0), seconds=600)
    assert pings == [] and [s["reason"] for s in stops(mark)] == ["cache_expired"]


def test_regular_pings_keep_every_touch_inside_the_window_until_the_idle_limit(monkeypatch):
    mark = journal.JOURNAL.last_seq
    touch = START - 100
    clock, pings = drive(monkeypatch, warm_session(_warm_at=touch), seconds=5000)
    assert len(pings) == 4
    assert all(later - earlier <= direct.PING_DEADLINE for earlier, later in zip([touch] + pings, pings))
    assert [s["reason"] for s in stops(mark)] == ["idle_limit"], "never cache_expired for a healthy cycle"


@pytest.mark.parametrize("setting", ["cache_keepwarm_seconds", "cache_keepwarm_turn_offset"])
def test_a_setting_that_would_miss_the_window_is_pulled_back_inside_it(monkeypatch, setting):
    """3600 s between pings (the admin allows it) or a first in-turn ping at 285 s could never read the cache."""
    if setting == "cache_keepwarm_seconds":
        target = warm_session(_warm_at=START)
        clock, pings = drive(monkeypatch, target, {setting: 3600, "cache_keepwarm_idle": 5000}, seconds=700)
        assert [pytest.approx(t - START, abs=2) for t in pings] == [direct.PING_LATEST, 2 * direct.PING_LATEST]
    else:
        target = warm_session(_busy=True, _turn_started_at=START, _turn_seq=1, _turn_body=dict(BODY), _warm_at=0)
        clock, pings = drive(monkeypatch, target, {setting: 285}, seconds=300)
        assert [pytest.approx(t - START, abs=2) for t in pings] == [direct.PING_LATEST]


# -- a failed ping --------------------------------------------------------------------------------
def test_a_failed_ping_is_retried_only_while_the_retry_starts_inside_the_window(monkeypatch):
    mark = journal.JOURNAL.last_seq
    touch = START - 100
    clock, pings = drive(monkeypatch, warm_session(_warm_at=touch), seconds=3000, ping=failing)
    # Due at touch + 240; the retries come 10 and 20 s after the failures; the next one, 40 s later, would start
    # 310 s after the touch, so the cycle ends instead of paying for a cache that is gone.
    assert len(pings) == 3 and all(t - touch <= direct.PING_DEADLINE for t in pings)
    assert pings[1] - pings[0] == pytest.approx(direct.PING_RETRY, abs=1.2)
    assert pings[2] - pings[1] == pytest.approx(2 * direct.PING_RETRY, abs=1.2)
    assert clock.now - START < 200
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"], stop["failures"], stop["after_failure"], stop["limit"], stop["level"]) == (
        "cache_expired", 3, 3, True, direct.PING_DEADLINE, "info")
    assert stop["touch_age_seconds"] == pytest.approx(pings[-1] - touch, abs=2)
    failures = [e for e in events(mark, "cache.keepwarm") if e["level"] == "warn"]
    assert len(failures) == 3 and "повтор через 10 с" in " ".join(
        e["message"] if "message" in e else "" for e in journal.JOURNAL.query(since=mark, limit=1000)
        if e["kind"] == "cache.keepwarm")
    assert "не успевает до истечения кэша" in [e["message"] for e in journal.JOURNAL.query(since=mark, limit=1000)
                                                if e["kind"] == "cache.keepwarm"][-1]


def test_a_ping_that_times_out_is_not_followed_by_a_paid_rewrite(monkeypatch):
    """The live case: the ping hung for the 120 s read timeout, and the retry, 240 s later, wrote 330k tokens."""
    mark = journal.JOURNAL.last_seq

    def hang(clock, _owner, _body):
        clock.now += 120
        raise TimeoutError("synthetic read timeout")

    clock, pings = cycle(monkeypatch, warm_session(_warm_at=START - 100), ping=hang)
    assert len(pings) == 1, "no second ping: by the time the first one gave up, the cache was 360 s old"
    assert clock.now - START < 300, "and no retry is waited for"
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"], stop["failures"], stop["after_failure"]) == ("cache_expired", 1, 1, True)
    assert stop["touch_age_seconds"] == pytest.approx(360, abs=2)


def test_a_failed_ping_is_not_a_touch_and_the_retry_that_works_is(monkeypatch):
    seen = []

    def flaky(_clock, owner, _body):
        seen.append(owner._warm_at)
        if len(seen) == 1:
            raise OSError("synthetic failure")
        return 0.1

    target = warm_session(_warm_at=START - 100)
    clock, pings = cycle(monkeypatch, target, seconds=200, ping=flaky)
    assert len(pings) == 2 and seen == [START - 100] * 2, "the failed ping moved nothing"
    assert target._warm_at == pings[1] and target._warm_failures == 0, "the retry that worked touched the cache"


def test_the_retry_that_worked_resets_the_failure_count_and_the_rhythm(monkeypatch):
    results = iter([OSError("synthetic failure"), None, None, None])

    def flaky(_clock, _owner, _body):
        result = next(results)
        if result is not None:
            raise result
        return 0.1

    clock, pings = cycle(monkeypatch, warm_session(_warm_at=START - 100), seconds=700, ping=flaky)
    assert len(pings) == 4
    assert pings[2] - pings[1] == pytest.approx(240, abs=1.2), "regular pings again, counted from the touch"


# -- inside a turn --------------------------------------------------------------------------------
def test_a_slow_failure_inside_a_turn_follows_the_same_rule(monkeypatch):
    mark = journal.JOURNAL.last_seq

    def hang(clock, _owner, _body):
        clock.now += 100
        raise TimeoutError("synthetic read timeout")

    target = warm_session(_busy=True, _turn_started_at=START, _turn_seq=1, _turn_body=dict(BODY), _warm_at=0)
    clock, pings = cycle(monkeypatch, target, ping=hang)
    assert len(pings) == 1 and pings[0] - START == pytest.approx(200, abs=1.2)
    [stop] = stops(mark)
    assert (stop["reason"], stop["during_turn"], stop["after_failure"], stop["pings"]) == (
        "cache_expired", True, True, 1)
    assert stop["touch_age_seconds"] == pytest.approx(300, abs=2)


def test_a_fast_failure_inside_a_turn_is_retried_and_the_retry_is_a_touch(monkeypatch):
    seen = []

    def flaky(_clock, owner, _body):
        seen.append(owner._warm_at)
        if len(seen) == 1:
            raise OSError("synthetic failure")
        return 0.1

    target = warm_session(_busy=True, _turn_started_at=START, _turn_seq=1, _turn_body=dict(BODY), _warm_at=0)
    clock, pings = cycle(monkeypatch, target, {"turn_timeout": 3000}, seconds=700, ping=flaky)
    offsets = [round(t - START) for t in pings]
    assert offsets[:2] == [200, 210] and 449 <= offsets[2] <= 451, "200 s, a retry 10 s later, then 240 s after that"
    assert seen[0] == 0 and seen[1] == 0 and seen[2] == pings[1], "only the retry that worked moved the touch"
    assert all(later - earlier <= direct.PING_DEADLINE for earlier, later in zip([START] + pings[1:], pings[1:]))


def test_a_ping_inside_a_turn_is_refused_when_even_the_turns_own_start_is_too_old(monkeypatch):
    mark = journal.JOURNAL.last_seq
    target = warm_session(_busy=True, _turn_started_at=START - 300, _turn_seq=1, _turn_body=dict(BODY), _warm_at=0)
    clock, pings = drive(monkeypatch, target, {"turn_timeout": 3000}, seconds=300)
    assert pings == []
    [stop] = stops(mark)
    assert (stop["reason"], stop["during_turn"], stop["pings"]) == ("cache_expired", True, 0)
    assert stop["touch_age_seconds"] == pytest.approx(300, abs=2)


def test_a_turn_without_its_own_cached_body_pings_the_old_body_against_the_old_touch(monkeypatch):
    """No cache key, no turn body: the turn's start touched nothing, so the last successful touch rules."""
    target = warm_session(_busy=True, _turn_started_at=START - 201, _turn_seq=1, _turn_body=None,
                          _warm_body=dict(BODY), _warm_at=START - 600)
    clock, pings = drive(monkeypatch, target, {"turn_timeout": 3000}, seconds=30)
    assert pings == []
    target = warm_session(_busy=True, _turn_started_at=START - 201, _turn_seq=1, _turn_body=None,
                          _warm_body=dict(BODY), _warm_at=START - 250)
    clock, pings = drive(monkeypatch, target, {"turn_timeout": 3000}, seconds=30)
    assert len(pings) == 1
