"""During-turn keepwarm: anchored ping refreshes the 5-minute KV TTL inside
long (310s+) opus MAX turns, which otherwise expired mid-generation and forced
a full 190k-token rewrite on the next turn (30+ credits per turn)."""
import asyncio
import time
from types import SimpleNamespace

import pytest

import direct
import journal


class _Session:
    def __init__(self):
        self._cancelled = False
        self.thread_id = "t"
        self._busy = False
        self._turn_started_at = 0
        self._turn_pinged = False
        self._warm_body = None
        self._warm_at = 0
        self._last_request_at = time.time()
        self._warm_failures = 0
        self._keepwarm_cost = 0.0
        self._last_prefix_tokens = 0


@pytest.mark.asyncio
async def test_during_turn_ping_fires_once_at_offset(monkeypatch):
    session = _Session()
    session._busy = True
    session._turn_started_at = time.time() - 201  # offset 200 already reached
    session._warm_body = {"model": "claude-opus-5-5"}
    session._last_prefix_tokens = 190000

    calls = []

    def fake_ping(_session, _body):
        calls.append(time.time())
        return 0.85

    monkeypatch.setattr(direct, "_ping", fake_ping)
    cfg = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800,
           "cache_keepwarm_during_turns": True, "cache_keepwarm_turn_offset": 200,
           "direct_debug": False}

    task = asyncio.create_task(direct._keepwarm(session, cfg))
    await asyncio.sleep(1.5)  # anchored ping fires within ~1s
    assert len(calls) == 1, "exactly one anchored ping during the turn"
    assert session._turn_pinged is True
    await asyncio.sleep(1.5)  # no second ping inside the same turn
    assert len(calls) == 1
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_during_turn_ping_skipped_when_disabled(monkeypatch):
    session = _Session()
    session._busy = True
    session._turn_started_at = time.time() - 201
    session._warm_body = {"model": "claude-opus-5-5"}

    calls = []
    monkeypatch.setattr(direct, "_ping", lambda *_a: calls.append(1))
    cfg = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800,
           "cache_keepwarm_during_turns": False, "cache_keepwarm_turn_offset": 200}

    task = asyncio.create_task(direct._keepwarm(session, cfg))
    await asyncio.sleep(1.2)
    assert not calls, "legacy behaviour: no ping while the turn is busy"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_during_turn_ping_failure_has_no_immediate_retry(monkeypatch):
    session = _Session()
    session._busy = True
    session._turn_started_at = time.time() - 201
    session._warm_body = {"model": "claude-opus-5-5"}

    calls = []

    def failing_ping(*_a):
        calls.append(1)
        raise RuntimeError("upstream busy")

    monkeypatch.setattr(direct, "_ping", failing_ping)
    cfg = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800,
           "cache_keepwarm_during_turns": True, "cache_keepwarm_turn_offset": 200}

    task = asyncio.create_task(direct._keepwarm(session, cfg))
    await asyncio.sleep(1.5)
    assert len(calls) == 1, "a failed during-turn ping must back off before retrying"
    assert session._turn_pinged is True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_idle_keepalive_still_pings_between_turns(monkeypatch):
    session = _Session()
    session._warm_body = {"model": "claude-opus-5-5"}
    session._warm_at = time.time() - 300  # interval long passed
    session._last_request_at = time.time()

    calls = []
    monkeypatch.setattr(direct, "_ping", lambda *_a: calls.append(time.time()) or 0.1)
    cfg = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800,
           "cache_keepwarm_during_turns": True, "cache_keepwarm_turn_offset": 200}

    task = asyncio.create_task(direct._keepwarm(session, cfg))
    await asyncio.sleep(1.2)
    assert len(calls) == 1, "between-turns keepalive must keep working"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
