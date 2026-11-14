"""R4-06: what the admin lets a keepwarm setting be is what the scheduler really applies, and the stop reasons say what
can happen."""
import asyncio

import pytest

import admin
import apierr
import direct
import journal
from keepwarm_harness import CFG, START, install, session as warm_session, stops
from test_admin import admin_relay  # noqa: F401 - the relay fixture


# -- the ceilings of the two settings -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("setting", ["cache_keepwarm_seconds", "cache_keepwarm_turn_offset"])
def test_the_admin_stops_where_the_scheduler_stops(setting):
    field = admin._schema()[setting]
    assert field["max"] == int(direct.PING_LATEST) == 275


@pytest.mark.parametrize("setting", ["cache_keepwarm_seconds", "cache_keepwarm_turn_offset"])
def test_a_value_above_the_ceiling_is_refused_and_the_ceiling_itself_is_accepted(admin_relay, setting):
    status, body, _ = admin_relay.patch({setting: 276})
    assert status == 400 and body["error"]["param"] == setting
    for value in (3600, 285):
        with pytest.raises(apierr.ApiError):
            admin_relay.runtime.admin._validate({setting: value})
    admin_relay.runtime.admin._validate({setting: 275})


def pings_of(monkeypatch, interval):
    mark = journal.JOURNAL.last_seq
    clock, pings = install(monkeypatch, seconds=600)
    asyncio.run(direct._keepwarm(warm_session(_warm_at=START), dict(CFG, cache_keepwarm_seconds=interval,
                                                                    cache_keepwarm_idle=5000)))
    return [round(t - START) for t in pings], stops(mark)


def test_the_largest_interval_the_admin_allows_is_applied_as_it_is_and_a_larger_one_would_not_be(monkeypatch):
    allowed, _ = pings_of(monkeypatch, 275)
    larger, _ = pings_of(monkeypatch, 3600)  # config.json may still say so
    assert allowed[0] == 275 and larger[0] == int(direct.PING_LATEST) == 275


# -- the stop that cannot happen with the default interval ----------------------------------------------------------------------
def failing(*_args):
    raise OSError("synthetic failure")


def failed_pings(monkeypatch, interval):
    mark = journal.JOURNAL.last_seq
    clock, pings = install(monkeypatch, ping=failing, seconds=20000)
    asyncio.run(direct._keepwarm(warm_session(_warm_at=START), dict(CFG, cache_keepwarm_seconds=interval,
                                                                    cache_keepwarm_idle=86400)))
    [stop] = stops(mark)
    return len(pings), stop


@pytest.mark.parametrize("interval,reason,count", [
    (30, "failures", 5), (100, "failures", 5),   # all five attempts fit in the window
    (140, "cache_expired", 4),                   # the fifth would start after it
    (240, "cache_expired", 3),                   # the default: the window closes after the third failure
])
def test_five_failures_in_a_row_are_reachable_only_with_a_short_interval(monkeypatch, interval, reason, count):
    attempts, stop = failed_pings(monkeypatch, interval)
    assert (stop["reason"], attempts, stop["failures"]) == (reason, count, count)
    if reason == "cache_expired":
        assert stop["after_failure"] is True


def test_the_default_interval_is_240_so_failures_is_not_what_ends_a_default_cycle():
    import config
    assert config.DEFAULTS["cache_keepwarm_seconds"] == 240


def test_the_failures_stop_says_pings_in_a_row_like_the_journal_event_that_goes_with_it(monkeypatch):
    attempts, stop = failed_pings(monkeypatch, 30)
    text = direct._STOP_TEXT["failures"]
    assert text.startswith("Прогрев остановлен") and "пять" in text and "подряд" in text and attempts == 5
