"""A subagent that waits for a long tool keeps its cache: it is warmed for 30 minutes, and the pool does not close it meanwhile.

In the live journal of 2026-11-06/07 every subagent whose keepwarm ended at the former limit of 600 s while it waited for a
tool came back (4 of 4). Two came back after a 15-minute test run: the pool had closed them at its idle timeout of 900 s and
their cache had expired, so they wrote 665,294 and 260,183 tokens again, 46 credits, where two more pings each (7.7 credits)
would have kept it."""
import asyncio
from types import SimpleNamespace

import config
import direct
import journal
from keepwarm_harness import CFG, START, drive, install, session as warm_session, stops
from test_keepwarm_limits import dependent
from test_pool_eviction import fill, pool_of

DEFAULT = config.DEFAULTS["cache_keepwarm_subagent_idle"]
TOOL_RUN = 15.2 * 60  # the longer of the two test runs that cost a rewrite


def waiting_subagent(**fields):
    """A subagent whose last answer asked for a tool; its request began 100 s before it ended."""
    return warm_session(_parent_key="main", _turn_final=False, _warm_at=START - 100, **fields)


def test_a_subagent_waiting_for_a_tool_is_warmed_for_half_an_hour_and_reads_its_cache_after_a_long_test_run(monkeypatch):
    assert DEFAULT == 1800
    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, waiting_subagent(), {"cache_keepwarm_subagent_idle": DEFAULT}, seconds=9000)
    assert [round(t - START) for t in pings] == [140, 380, 620, 860, 1100, 1340, 1580]
    assert 1800 < clock.now - START < 1803
    [stop] = stops(mark)
    assert (stop["reason"], stop["limit"], stop["subagent"], stop["pings"]) == ("idle_limit", 1800, True, 7)
    # The tool result comes back 15.2 minutes after the request that asked for it: the last ping touched the cache inside
    # its 5 minutes. With 600 s the last ping was at 380 s and the entry was gone by then.
    last_touch = max(t for t in pings if t - START <= TOOL_RUN)
    assert START + TOOL_RUN - last_touch < direct.PROMPT_CACHE_TTL


def test_the_scheduler_has_the_same_default_when_the_setting_is_not_given(monkeypatch):
    clock, pings = install(monkeypatch, seconds=9000)
    cfg = {key: value for key, value in CFG.items() if key != "cache_keepwarm_subagent_idle"}
    asyncio.run(direct._keepwarm(waiting_subagent(), cfg))
    assert len(pings) == 7 and 1800 < clock.now - START < 1803


def test_a_parent_stays_pinned_while_its_child_waits_for_a_long_tool(monkeypatch):
    child = SimpleNamespace(_busy=False, _turn_started_at=0, _last_request_at=START)  # its tool runs since START
    parent = warm_session(_last_request_at=START - 5000, _warm_at=START - 100, _cache_dependents=1,
                          _cache_dependent_records={dependent(child)})
    mark = journal.JOURNAL.last_seq
    clock, _ = drive(monkeypatch, parent, {"cache_keepwarm_idle": 60, "cache_keepwarm_subagent_idle": DEFAULT},
                     seconds=9000)
    assert 1800 <= clock.now - START < 1803, "the child's own limit ends the pin, not 600 s"
    assert [s["reason"] for s in stops(mark)] == ["idle_limit"]


def warming(session):
    """Give `session` a keepwarm cycle that is still running."""
    session._warm_task = asyncio.get_running_loop().create_future()
    return session._warm_task


def test_the_pool_keeps_a_conversation_past_its_idle_timeout_while_its_cache_is_kept_warm():
    async def story():
        pool = pool_of(conversation_idle_timeout=1)
        sockets = await fill(pool, ["subagent"])
        cycle = warming(pool._conversations["subagent"])
        pool._last_used["subagent"] -= 2
        await pool.release(await pool.acquire("other"))
        kept = "subagent" in pool._conversations and not sockets["subagent"].closed
        cycle.set_result(None)  # the cycle ended by its own limit
        await pool.release(await pool.acquire("third"))
        return kept, "subagent" in pool._conversations, sockets["subagent"].closed

    assert asyncio.run(story()) == (True, False, True)  # before: closed at the timeout, which ended its keepwarm


def test_to_make_room_a_conversation_whose_cache_is_not_kept_warm_goes_before_a_warm_one():
    async def story():
        pool = pool_of(max_conversations=2)
        sockets = await fill(pool, ["warm", "cold"], prefix={"warm": 900, "cold": 300_000}, served={"cold": 3})
        warming(pool._conversations["warm"])
        await pool.release(await pool.acquire("newcomer"))
        return sorted(pool._conversations), sockets["warm"].closed, sockets["cold"].closed

    # Before: the cheapest to rebuild went, the small warm one, whose keepwarm ended with it; the big one's cache was not kept
    # warm, so closing it costs nothing more (its history is stored).
    assert asyncio.run(story()) == (["newcomer", "warm"], False, True)
