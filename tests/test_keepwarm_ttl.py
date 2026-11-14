"""Keepwarm pays only for a cache it can still read: the TTL window, the retries inside it, one rewrite at most."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

import direct
import journal
from keepwarm_harness import CFG, DUE, START, drive, install, session as warm_session, stops

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


def messages(mark, kind):
    return [e["message"] for e in journal.JOURNAL.query(since=mark, limit=1000) if e["kind"] == kind]


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
    """3600 s between pings (config.json takes it; the admin stops at PING_LATEST) or a first in-turn ping at 285 s could
    never read the cache."""
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
    texts = messages(mark, "cache.keepwarm")
    assert len(texts) == 3, "one warning for every failed ping"
    assert "повтор через 10 с" in texts[0] and "повтор через 20 с" in texts[1]
    assert "повтор не успевает до истечения кэша" in texts[2]


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


# -- a rewrite ------------------------------------------------------------------------------------
REWRITES = [
    (190_000, 0),        # the whole prefix written, nothing read: the entry was gone
    (60_000, 340_000),   # a sixth of a huge prefix, but 6 credits at the write price
    (50_000, 450_000),   # exactly the threshold
    (6_000, 4_000),      # a miss on a small prompt: more than half written
    (1_024, 0),          # a prompt of one cache block, missed completely
]
HEALTHY = [
    (0, 190_000),        # a read, as intended
    (40_000, 150_000),   # below both thresholds: a partial write the cycle survives
    (49_999, 450_000),   # one token under the absolute threshold, far under half
    (1_023, 0),          # below one cache block: noise
    (200, 100),          # more than half but tiny
    (0, 0),
]


@pytest.mark.parametrize("written,read", REWRITES)
def test_a_ping_that_wrote_the_prefix_instead_of_reading_it_is_a_rewrite(written, read):
    usage = {"promptTokensWrittenToCache": written, "promptTokensReadFromCache": read, "inputTokens": 12}
    assert direct._rewrite_of(usage) == {"written_tokens": written, "read_tokens": read, "prefix_tokens": written + read}


@pytest.mark.parametrize("written,read", HEALTHY)
def test_a_ping_that_mostly_read_the_prefix_is_not(written, read):
    assert direct._rewrite_of({"promptTokensWrittenToCache": written, "promptTokensReadFromCache": read}) is None


@pytest.mark.parametrize("usage", [None, [], "usage", {}, {"promptTokensReadFromCache": 190_000},
                                   {"promptTokensWrittenToCache": None}, {"promptTokensWrittenToCache": "lots"},
                                   {"promptTokensWrittenToCache": True}, {"promptTokensWrittenToCache": -190_000},
                                   {"promptTokensWrittenToCache": float("nan")},
                                   {"promptTokensWrittenToCache": float("inf")}])
def test_missing_or_invalid_usage_never_stops_a_cycle_as_a_rewrite(usage):
    assert direct._rewrite_of(usage) is None


def test_usage_is_read_like_everywhere_else_a_numeric_string_is_a_number():
    assert direct._rewrite_of({"promptTokensWrittenToCache": "190000"})["written_tokens"] == 190_000


def usage_ping(usage, credits=0.1):
    def ping(_clock, owner, _body):
        owner._last_warm_usage = dict(usage)
        return credits
    return ping


def test_one_rewrite_is_the_most_a_cycle_ever_pays_for(monkeypatch):
    mark = journal.JOURNAL.last_seq
    usage = {"promptTokensWrittenToCache": 330_599, "promptTokensReadFromCache": 0, "creditsUsed": 34.46}
    target = warm_session(_warm_at=START - 100)
    clock, pings = cycle(monkeypatch, target, {"cache_keepwarm_idle": 86400}, seconds=20000,
                         ping=usage_ping(usage, 34.46))
    assert len(pings) == 1 and clock.now - START < 200, "the cycle ended with the first rewrite"
    [stop] = stops(mark)
    assert (stop["reason"], stop["level"], stop["pings"], stop["idle_pings"]) == ("cache_rewritten", "warn", 1, 1)
    assert (stop["written_tokens"], stop["read_tokens"], stop["prefix_tokens"]) == (330_599, 0, 330_599)
    assert stop["ping_credits"] == 34.46 and stop["credits"] == pytest.approx(34.46)
    assert stop["touch_age_seconds"] == pytest.approx(240, abs=2)
    [ping_event] = events(mark, "cache.keepwarm")
    assert ping_event["level"] == "warn" and ping_event["cache_verified"] is True
    assert ping_event["touch_age_seconds"] == pytest.approx(240, abs=2)
    assert direct._STOP_TEXT["cache_rewritten"] in messages(mark, "cache.keepwarm_stop")


def test_a_healthy_cycle_is_not_cut_by_pings_that_read(monkeypatch):
    mark = journal.JOURNAL.last_seq
    usage = {"promptTokensWrittenToCache": 120, "promptTokensReadFromCache": 190_000}
    clock, pings = cycle(monkeypatch, warm_session(_warm_at=START - 100), seconds=5000, ping=usage_ping(usage))
    assert len(pings) == 4 and [s["reason"] for s in stops(mark)] == ["idle_limit"]
    assert all(e["level"] == "debug" for e in events(mark, "cache.keepwarm"))


def test_a_rewrite_inside_a_turn_ends_the_cycle_too(monkeypatch):
    """The turn's own entry may not exist yet (the first response has not begun): one miss, not one per ping."""
    mark = journal.JOURNAL.last_seq
    usage = {"promptTokensWrittenToCache": 190_000, "promptTokensReadFromCache": 0}
    target = warm_session(_busy=True, _turn_started_at=START, _turn_seq=1, _turn_body=dict(BODY), _warm_at=0)
    clock, pings = cycle(monkeypatch, target, {"turn_timeout": 5000}, seconds=3000, ping=usage_ping(usage))
    assert len(pings) == 1
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"], stop["idle_pings"]) == ("cache_rewritten", 1, 0)


def test_the_rewriting_ping_still_counts_as_a_touch_of_the_entry_it_wrote(monkeypatch):
    usage = {"promptTokensWrittenToCache": 190_000, "promptTokensReadFromCache": 0}
    target = warm_session(_warm_at=START - 100)
    clock, pings = cycle(monkeypatch, target, seconds=3000, ping=usage_ping(usage))
    assert target._warm_at == pings[0], "the entry it created is as young as the ping"


# -- the real ping --------------------------------------------------------------------------------
def response(*events_):
    return io.BytesIO(b"\n".join(json.dumps(event).encode() for event in events_) + b"\n")


def test_a_real_ping_whose_usage_shows_a_rewrite_ends_the_cycle_with_its_measured_size(monkeypatch):
    real_ping = direct._ping
    mark = journal.JOURNAL.last_seq
    backend = SimpleNamespace(cfg={"account_id": "acc-real", "base": "https://upstream.invalid"}, unavailable=None,
                              headers=lambda: {})
    target = warm_session(backend=backend, _warm_at=DUE, _cancelled=False, _credits_manager=None)
    clock, pings = install(monkeypatch, seconds=3000)
    monkeypatch.setattr(direct, "_ping", real_ping)
    sent = []

    def urlopen(request, **_kw):
        sent.append(clock.now)
        return response({"event": "metadata", "usage": {"creditsUsed": 19.2, "promptTokensWrittenToCache": 190_000,
                                                        "promptTokensReadFromCache": 0, "inputTokens": 9}},
                        {"event": "end_of_stream"})

    monkeypatch.setattr(direct.urllib.request, "urlopen", urlopen)
    asyncio.run(direct._keepwarm(target, dict(CFG)))
    assert len(sent) == 1
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"], stop["written_tokens"], stop["prefix_tokens"]) == (
        "cache_rewritten", 1, 190_000, 190_000)
    assert stop["credits"] == pytest.approx(19.2) and stop["ping_credits"] == 19.2
