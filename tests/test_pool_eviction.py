"""The conversation pool: a new conversation never loses a slot to a race, the cheapest idle one is evicted, a full pool waits (item 2).

a4 F3 (2026-11-06): the pool sat at its cap of 128 under test load, and 183-206 conversations were evicted per 25-30 minutes.
1 or 2 of 5 new conversations met `503 conversation pool is full` in every dialect: _evict_idle popped an idle session and then
awaited its closing, a concurrent acquire took the freed slot during that await, and the first caller's capacity check failed
although other idle sessions existed. The oldest idle conversation went first, so a warm 345k-token session was evicted after
167 s for one-shot test conversations (regret on real sessions 752 credits in a4's replay, 0 with the order below)."""
import asyncio
import json
import random
import time
from pathlib import Path

import pytest

import apierr
import config
from session import SessionPool
from prompt_support import prompt_relay  # noqa: F401 - a fixture
from test_admin import admin_relay  # noqa: F401 - a fixture
from test_docs_consistency import load_with
from test_session_lifecycle import Backend

ROOT = Path(__file__).resolve().parents[1]


class Slow:
    """A socket whose closing takes `delay` seconds, as a session that is being closed does."""

    def __init__(self, delay=0.0):
        self.delay, self.closed = delay, False

    async def close(self):
        await asyncio.sleep(self.delay)
        self.closed = True


def pool_of(**cfg):
    return SessionPool(Backend(max_workers=64, **cfg))


async def fill(pool, keys, delay=0.0, prefix=None, served=None):
    """Open and release one conversation per key; returns {key: its socket}."""
    sockets = {}
    for key in keys:
        lease = await pool.acquire(key)
        sockets[key] = lease.session.ws = Slow(delay)
        if prefix and key in prefix:
            lease.session._last_prefix_tokens = prefix[key]
        await pool.release(lease)
        for _ in range((served or {}).get(key, 1) - 1):
            await pool.release(await pool.acquire(key))
        await asyncio.sleep(0.002)  # distinct last-used times
    return sockets


async def new_conversation(pool, key):
    try:
        lease = await pool.acquire(key)
        await pool.release(lease)
        return "ok"
    except apierr.ApiError as error:
        return str(error.status)


def test_new_conversations_at_capacity_with_slow_closing_victims_are_all_served():
    async def burst(delay=0.05, capacity=4):
        pool = pool_of(max_conversations=capacity)
        await fill(pool, ["p%d" % number for number in range(capacity)], delay)
        return await asyncio.gather(*(new_conversation(pool, "n%d" % number) for number in range(capacity)))

    assert asyncio.run(burst()) == ["ok"] * 4  # before: some of them 503 while idle conversations existed


def test_random_bursts_never_meet_a_503_while_conversations_are_idle():
    rng = random.Random(5)

    async def rounds():
        refused = served = 0
        for _ in range(40):
            capacity, delay = rng.choice([3, 4, 6, 8]), rng.choice([0.0, 0.005, 0.02])
            pool = pool_of(max_conversations=capacity)
            await fill(pool, ["p%d" % number for number in range(capacity)], delay)
            for result in await asyncio.gather(*(new_conversation(pool, "n%d" % number) for number in range(capacity))):
                refused += result == "503"
                served += result == "ok"
        return served, refused

    served, refused = asyncio.run(rounds())
    assert refused == 0 and served > 100


def test_the_conversation_that_is_dearest_to_rebuild_survives_whatever_its_age():
    async def story():
        pool = pool_of(max_conversations=3)
        sockets = await fill(pool, ["big", "small1", "small2"], prefix={"big": 300000, "small1": 900, "small2": 1200},
                             served={"big": 3})  # the big one is the oldest
        await pool.release(await pool.acquire("newcomer"))
        return sorted(pool._conversations), {key: sock.closed for key, sock in sockets.items()}

    members, closed = asyncio.run(story())
    assert "big" in members and "newcomer" in members and closed["big"] is False
    assert closed["small1"] is True and "small1" not in members  # the cheapest one went; one is enough to make room


def test_among_equals_the_oldest_goes_and_only_as_many_as_the_room_needs():
    async def story():
        pool = pool_of(max_conversations=3)
        await fill(pool, ["a", "b", "c"])
        await pool.release(await pool.acquire("d"))
        return sorted(pool._conversations), len(pool._conversation_locks)

    members, locks = asyncio.run(story())
    assert members == ["b", "c", "d"] and locks == 3


def test_the_rebuild_cost_is_the_prefix_times_the_requests_served_at_most_five():
    async def story():
        pool = pool_of(max_conversations=10)
        await fill(pool, ["one", "many", "unknown"], prefix={"one": 1000, "many": 1000}, served={"many": 9})
        return pool._rebuild_cost("one"), pool._rebuild_cost("many"), pool._rebuild_cost("unknown"), pool._rebuild_cost("gone")

    assert asyncio.run(story()) == (1000 * 1, 1000 * 5, 1 * 1, 1 * 1)


def test_a_conversation_past_its_idle_timeout_goes_whether_or_not_room_is_needed():
    async def story():
        pool = pool_of(conversation_idle_timeout=1)
        sockets = await fill(pool, ["a"])
        pool._last_used["a"] -= 2
        await pool.release(await pool.acquire("b"))
        return sockets["a"].closed, "a" in pool._conversations, "a" in pool._conversation_locks, "a" in pool._served

    assert asyncio.run(story()) == (True, False, False, False)


def test_a_conversation_that_is_busy_or_pinned_by_a_running_subagent_is_never_evicted():
    async def story():
        pool = pool_of(max_conversations=2)
        busy = await pool.acquire("busy")
        busy.session.ws = busy_socket = Slow()
        pinned = await pool.acquire("pinned")
        pinned.session.ws = pinned_socket = Slow()
        await pool.release(pinned)
        pool._conversations["pinned"]._cache_dependents = 1  # the parent of a running subagent
        result = await new_conversation(pool, "newcomer")
        closed = (busy_socket.closed, pinned_socket.closed)
        await pool.release(busy)
        return result, closed

    assert asyncio.run(story()) == ("503", (False, False))


def test_a_full_pool_answers_503_at_once_when_the_wait_is_zero():
    async def story():
        pool = pool_of(max_conversations=1, conversation_wait=0)
        busy = await pool.acquire("only")
        started = time.monotonic()
        result = await new_conversation(pool, "other")
        elapsed = time.monotonic() - started
        await pool.release(busy)
        return result, elapsed

    result, elapsed = asyncio.run(story())
    assert result == "503" and elapsed < 0.2


def test_a_full_pool_waits_for_a_slot_and_serves_the_new_conversation_when_one_frees():
    async def story():
        pool = pool_of(max_conversations=1, conversation_wait=5)
        busy = await pool.acquire("only")
        busy.session.ws = Slow()
        started = time.monotonic()
        waiting = asyncio.ensure_future(new_conversation(pool, "other"))
        await asyncio.sleep(0.15)
        assert not waiting.done()  # it waits; it is not refused
        await pool.release(busy)
        result = await asyncio.wait_for(waiting, 2)
        return result, time.monotonic() - started, sorted(pool._conversations)

    result, elapsed, members = asyncio.run(story())
    assert result == "ok" and 0.1 < elapsed < 1.0  # woken by the release, not at the end of its wait
    assert members == ["other"]  # the one that ended was idle and made the room


def test_a_wait_that_nothing_ends_is_a_503_after_the_wait():
    async def story():
        pool = pool_of(max_conversations=1, conversation_wait=1)
        busy = await pool.acquire("only")
        started = time.monotonic()
        result = await new_conversation(pool, "other")
        elapsed = time.monotonic() - started
        await pool.release(busy)
        return result, elapsed

    result, elapsed = asyncio.run(story())
    assert result == "503" and 0.9 <= elapsed < 2.5


def test_a_request_that_gives_up_while_it_waits_leaves_nothing_behind():
    async def story():
        pool = pool_of(max_conversations=1, conversation_wait=30)
        busy = await pool.acquire("only")
        waiting = asyncio.ensure_future(new_conversation(pool, "other"))
        await asyncio.sleep(0.1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        state = (sorted(pool._conversation_locks), pool._slots._value)
        await pool.release(busy)
        return state, pool._slots._value

    (locks, slots_while_busy), slots_after = asyncio.run(story())
    assert locks == ["only"] and slots_while_busy == 63 and slots_after == 64  # the worker slot came back as well


def test_the_wait_of_one_request_does_not_take_the_slot_of_another_that_arrived_later():
    async def story():
        pool = pool_of(max_conversations=2, conversation_wait=5)
        first, second = await pool.acquire("a"), await pool.acquire("b")
        first.session.ws = second.session.ws = Slow()
        waiters = [asyncio.ensure_future(new_conversation(pool, key)) for key in ("x", "y")]
        await asyncio.sleep(0.1)
        await pool.release(first)
        await asyncio.sleep(0.1)
        await pool.release(second)
        return await asyncio.wait_for(asyncio.gather(*waiters), 3), sorted(pool._conversations)

    results, members = asyncio.run(story())
    assert results == ["ok", "ok"]  # each waiter got a slot as one freed; none was refused, none took another's
    assert len(members) == 2 and "y" in members and "a" not in members  # never more than the capacity


# --- the setting -----------------------------------------------------------------------------------------------------

def test_the_setting_is_validated_documented_and_live_in_the_admin(tmp_path, monkeypatch, admin_relay):  # noqa: F811
    assert config.DEFAULTS["conversation_wait"] == 10
    assert load_with(tmp_path, monkeypatch)["conversation_wait"] == 10
    assert load_with(tmp_path, monkeypatch, conversation_wait=0)["conversation_wait"] == 0
    for bad in (-1, 301, True, 1.5, "soon"):
        with pytest.raises(apierr.ApiError, match="conversation_wait"):
            load_with(tmp_path, monkeypatch, conversation_wait=bad)
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["conversation_wait"] == config.DEFAULTS["conversation_wait"] and example["conversation_wait_note"]
    assert "| max_conversations, conversation_idle_timeout, conversation_wait |" in (
        ROOT / "README.md").read_text(encoding="utf-8")
    status, body, _ = admin_relay.request("/admin/api/settings")
    for key in ("max_conversations", "conversation_wait"):
        assert body["schema"][key]["restart_required"] is False
    status, body, _ = admin_relay.patch({"conversation_wait": 3, "max_conversations": 64})
    assert status == 200 and set(body["applied"]) == {"conversation_wait", "max_conversations"}
    assert admin_relay.cfg["conversation_wait"] == 3 and admin_relay.cfg["max_conversations"] == 64
    assert admin_relay.patch({"conversation_wait": 301})[0] == 400 and admin_relay.patch({"conversation_wait": -1})[0] == 400


def test_the_pool_reads_the_live_settings_of_the_runtime(prompt_relay):
    relay = prompt_relay()
    assert relay.runtime.pool.cfg is relay.runtime.cfg  # what the admin updates is what the pool reads at every acquire
