import asyncio
import types

from session import SessionPool


class Backend:
    def __init__(self, mode):
        self.cfg = {"session_mode": mode, "thread_id": "t1", "page_uuid": "p", "max_workers": 2,
                    "base": "https://x", "upstream_timeout": 5}
        self.created = 0

    async def create_thread(self):
        self.created += 1
        return "thread-" + str(self.created)


def test_shared_mode_serialises_and_releases_lock():
    pool = SessionPool(Backend("shared"))

    async def scenario():
        first = await pool.acquire()
        busy = asyncio.ensure_future(pool.acquire())
        await asyncio.sleep(0.01)
        assert not busy.done(), "second acquire must wait while shared session is taken"
        await pool.release(first)
        second = await asyncio.wait_for(busy, timeout=1)
        await pool.release(second)
        # a third acquire still works => lock was released properly
        third = await asyncio.wait_for(pool.acquire(), timeout=1)
        await pool.release(third)

    asyncio.run(scenario())


def test_shared_reuses_one_session():
    backend = Backend("shared")
    pool = SessionPool(backend)

    async def scenario():
        for _ in range(3):
            session = await pool.acquire()
            await pool.release(session)
        return pool._shared

    first = asyncio.run(scenario())
    assert first is not None and backend.created == 0


def test_per_request_creates_a_thread_each_time():
    backend = Backend("per_request")
    pool = SessionPool(backend)

    async def scenario():
        sessions = [await pool.acquire() for _ in range(2)]
        for session in sessions:
            await pool.release(session)
        return [s.thread_id for s in sessions]

    assert asyncio.run(scenario()) == ["thread-1", "thread-2"]
