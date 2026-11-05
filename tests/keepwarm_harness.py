"""Simulated clock for the keepwarm scheduler: no real sleeps, no network."""
import asyncio
from types import SimpleNamespace

import direct

START = 1000.0
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


def drive(monkeypatch, target, cfg=None, *, seconds=3600, ping=None, on_tick=None):
    """Run direct._keepwarm on a simulated clock until it stops itself or `seconds` have passed.

    Returns (clock, pings): the simulated clock and the time of every ping that was sent."""
    clock = SimpleNamespace(now=START)
    real_sleep = asyncio.sleep
    pings = []

    async def sleep(delay):
        clock.now += delay
        if on_tick is not None:
            on_tick(clock.now)
        if clock.now >= START + seconds:
            raise asyncio.CancelledError()
        await real_sleep(0)

    async def to_thread(fn, *args):
        return fn(*args)

    def send(owner, body):
        pings.append(clock.now)
        return 0.1 if ping is None else ping(owner, body)

    monkeypatch.setattr(direct.time, "time", lambda: clock.now)
    monkeypatch.setattr(direct.asyncio, "sleep", sleep)
    monkeypatch.setattr(direct.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(direct, "_ping", send)
    asyncio.run(direct._keepwarm(target, dict(CFG, **(cfg or {}))))
    return clock, pings
