"""Simulated clock for the keepwarm scheduler: no real sleeps, no network."""
import asyncio
from types import SimpleNamespace

import direct
import journal

START = 1000.0
REAL_SLEEP = asyncio.sleep
CFG = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 900, "cache_keepwarm_subagent_idle": 600,
       "cache_keepwarm_during_turns": True, "cache_keepwarm_turn_offset": 200, "turn_timeout": 900}


def backend(name="acc-keepwarm", **fields):
    return SimpleNamespace(cfg={"account_id": name}, unavailable=None, **fields)


def session(**fields):
    """A bare session as the scheduler sees it: a warm body, an idle conversation, a healthy account."""
    state = dict(_busy=False, _turn_started_at=0, _turn_seq=0, _turn_body=None, _warm_body={"model": "claude-opus-5-5"},
                 _warm_at=START, _last_request_at=START, _warm_failures=0, _keepwarm_cost=0.0,
                 _last_prefix_tokens=0, thread_id="thread", backend=backend())
    state.update(fields)
    return SimpleNamespace(**state)


def install(monkeypatch, *, seconds=None, ping=None, on_tick=None):
    """Run asyncio sleeps, time.time and pings on a simulated clock; no real waiting.

    Returns (clock, pings): the clock and the time of every ping sent. With `seconds`, the
    scheduler is cancelled when that much simulated time has passed."""
    clock = SimpleNamespace(now=START, frozen=False)
    pings = []

    async def sleep(delay):
        if not clock.frozen:
            clock.now += delay
            if on_tick is not None:
                on_tick(clock.now)
            if seconds is not None and clock.now >= START + seconds:
                raise asyncio.CancelledError()
        await REAL_SLEEP(0)

    async def to_thread(fn, *args):
        return fn(*args)

    def send(owner, body):
        pings.append(clock.now)
        value = 0.1 if ping is None else ping(owner, body)
        if value is not None:
            # What the real _ping books for a billed request.
            owner._keepwarm_cost = getattr(owner, "_keepwarm_cost", 0.0) + value
        return value

    monkeypatch.setattr(direct.time, "time", lambda: clock.now)
    monkeypatch.setattr(direct.asyncio, "sleep", sleep)
    monkeypatch.setattr(direct.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(direct, "_ping", send)
    return clock, pings


def drive(monkeypatch, target, cfg=None, *, seconds=3600, **options):
    """Run direct._keepwarm on a simulated clock until it stops itself or `seconds` have passed."""
    clock, pings = install(monkeypatch, seconds=seconds, **options)
    asyncio.run(direct._keepwarm(target, dict(CFG, **(cfg or {}))))
    return clock, pings


def stops(mark):
    """The cache.keepwarm_stop events journaled after `mark`: each one's data and its level."""
    return [dict(event["data"], level=event["level"]) for event in journal.JOURNAL.query(since=mark, limit=1000)
            if event["kind"] == "cache.keepwarm_stop"]
