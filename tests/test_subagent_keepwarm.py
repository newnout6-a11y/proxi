"""A subagent that waits for a long tool keeps its cache: it is warmed for 30 minutes, and the pool does not close it meanwhile.
Its parent, which goes on from the subagent's result, stays warm with it.

In the live journal of 2026-11-06/07 every subagent whose keepwarm ended at the former limit of 600 s while it waited for a
tool came back (4 of 4). Two came back after a 15-minute test run: the pool had closed them at its idle timeout of 900 s and
their cache had expired, so they wrote 665,294 and 260,183 tokens again, 46 credits, where two more pings each (7.7 credits)
would have kept it."""
import asyncio

import pytest

import config
import direct
import journal
from keepwarm_harness import CFG, START, drive, install, session as warm_session, stops
from test_pool_eviction import fill, pool_of

DEFAULT = config.DEFAULTS["cache_keepwarm_subagent_idle"]
TOOL_RUN = 15.2 * 60  # the longer of the two test runs that cost a rewrite
TEST_SUITE = 20 * 60  # a subagent's tool that outlasts the parent's own 15 minutes of idle warming


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


async def warm_parent(pool):
    """A main conversation of `pool` whose last turn gave a usable answer: a body to keep warm, its cache touched 100 s ago."""
    lease = await pool.acquire("main")
    lease.session._warm_body, lease.session._warm_at = {"model": "claude-opus-5-5"}, START - 100
    await pool.release(lease)
    return lease.session


def test_a_parent_stays_warm_while_its_child_waits_for_a_long_tool_after_the_childs_request_ended(monkeypatch):
    """A subagent's request ends with the call of a 20-minute test suite; the subagent comes back with the result and
    the parent goes on from its whole prefix.

    The parent was pinned only while the child's lease was held. At the release it was left to its own 15 minutes of
    idle warming (pings at 140 to 860 s, stop at 900 s) while the child itself was warmed for 30: when the result came
    back, the parent's cache had expired and its whole prefix was written again, about 30 credits at 300k tokens on
    Opus 5.5. The former version of this test passed on that code: it gave the parent the record of a child lease that
    the pool never leaves behind after a release."""
    clock, pings = install(monkeypatch, seconds=9000)
    mark = journal.JOURNAL.last_seq

    async def story():
        pool = pool_of(**dict(CFG, cache_keepwarm_subagent_idle=DEFAULT))
        parent = await warm_parent(pool)
        child = await pool.acquire("child", parent_key="main")  # the parent's cycle starts with its child's request
        child.session._turn_final = False  # the subagent's answer asked for a tool
        await pool.release(child)
        assert parent._cache_dependents == 0 and not parent._cache_dependent_records  # no lease pins it any more
        await parent._warm_task

    asyncio.run(story())
    assert 1800 <= clock.now - START < 1803, "the child's own limit ends the pin, not the parent's 900 s"
    assert [round(t - START) for t in pings] == [140, 380, 620, 860, 1100, 1340, 1580]
    [stop] = stops(mark)
    assert (stop["reason"], stop["limit"], stop["subagent"], stop["pings"]) == ("idle_limit", 900, False, 7)
    # The result comes back 20 minutes after the request that asked for it: a ping touched the parent's cache in the last 5.
    returns = START + TEST_SUITE
    assert returns - max(t for t in pings if t <= returns) < direct.PROMPT_CACHE_TTL


@pytest.mark.parametrize("case", ["waits_for_a_tool", "final_answer", "cancelled", "evicted", "stuck_turn"])
def test_after_its_request_a_child_keeps_its_parent_warm_only_while_it_is_expected_back(monkeypatch, case):
    """What the pin of a child that has no request running may cost is bounded by what the child really does.

    A child that gave its final answer, was cancelled or was evicted does not come back to this parent, and a turn that
    never ended keeps nothing warm past turn_timeout (900 s here): the parent's own idle limit (60 s here) ends its
    warming, or the stuck turn's limit does."""
    clock, _ = install(monkeypatch, seconds=9000)
    mark = journal.JOURNAL.last_seq

    async def story():
        pool = pool_of(**dict(CFG, cache_keepwarm_idle=60, cache_keepwarm_subagent_idle=DEFAULT))
        parent = await warm_parent(pool)
        lease = await pool.acquire("child", parent_key="main")
        child = lease.session
        child._turn_final = case == "final_answer"
        if case == "stuck_turn":
            child._busy, child._turn_started_at = True, START  # a turn whose end was never recorded
        if case == "cancelled":
            lease.cancel()  # the user interrupted the subagent: the pool closes its session at the release
        await pool.release(lease)
        if case == "evicted":
            pool._last_used["child"] -= 10_000
            await pool._evict_idle()
            assert "child" not in pool._conversations
        await parent._warm_task

    asyncio.run(story())
    expected = {"waits_for_a_tool": 1800, "final_answer": 60, "cancelled": 60, "evicted": 60, "stuck_turn": 900}[case]
    assert expected <= clock.now - START < expected + 3, case
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
