"""Session-pool hardening: tool merge semantics, lease-cancel race, bounded put."""
import asyncio
import threading
import time

import pytest

import direct
import session as session_mod
from test_fixes_opencode_concurrency import DummyBackend


@pytest.mark.asyncio
async def test_release_merges_only_child_added_tools():
    """A subagent that branched BEFORE the parent's client narrowed the tool set
    must not resurrect removed tools on release (the old union merge did)."""
    backend = DummyBackend()
    pool = session_mod.SessionPool(backend)

    parent_lease = await pool.acquire("conv-narrow")
    parent_lease.session.register_tools([{"name": "read_file"}, {"name": "edit_file"}])
    subagent_lease = await pool.acquire("conv-narrow")
    # The parent's client narrows the set while the subagent holds the old snapshot.
    parent_lease.session.register_tools([{"name": "read_file"}])
    await pool.release(subagent_lease)

    assert set(parent_lease.session._registered_tools) == {"read_file"}, \
        "release must not resurrect tools removed by the parent's client"
    await pool.release(parent_lease)


@pytest.mark.asyncio
async def test_release_still_transfers_child_added_tools():
    backend = DummyBackend()
    pool = session_mod.SessionPool(backend)

    parent_lease = await pool.acquire("conv-add")
    parent_lease.session.register_tools([{"name": "read_file"}])
    subagent_lease = await pool.acquire("conv-add")
    subagent_lease.session.register_tools([{"name": "read_file"}, {"name": "subagent_special_tool"}])
    await pool.release(subagent_lease)

    assert "subagent_special_tool" in parent_lease.session._registered_tools, \
        "tools the child actually added must still transfer to the parent"
    await pool.release(parent_lease)


@pytest.mark.asyncio
async def test_lease_cancel_after_release_is_noop():
    backend = DummyBackend()
    pool = session_mod.SessionPool(backend)
    lease = await pool.acquire("conv-cancel")
    released_session = lease.session
    await pool.release(lease)

    lease.cancel()
    await asyncio.sleep(0.05)  # let any loop-scheduled callback run
    assert released_session._cancelled is False, \
        "a cancel after release must not poison the released session"


@pytest.mark.asyncio
async def test_lease_cancel_before_release_still_cancels():
    backend = DummyBackend()
    pool = session_mod.SessionPool(backend)
    lease = await pool.acquire("conv-cancel2")
    lease.session._loop = asyncio.get_running_loop()
    lease.cancel()
    await asyncio.sleep(0.05)
    assert lease.session._cancelled is True
    await pool.release(lease)


def test_bounded_put_drops_after_max_wait():
    loop = asyncio.new_event_loop()
    queue = asyncio.Queue(maxsize=1)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        async def fill():
            await queue.put("resident")

        asyncio.run_coroutine_threadsafe(fill(), loop).result(5)
        started = time.time()
        direct._bounded_put(loop, queue, "payload", lambda: False, max_wait=0.1)
        assert time.time() - started < 2, "dropped put must not block past max_wait"
        assert queue.qsize() == 1, "dropped put must not enqueue"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
