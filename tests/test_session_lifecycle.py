"""Lifecycle regressions with real asyncio scheduling and scripted sockets."""
import asyncio
import datetime
import json
import threading
import types

import pytest

import apierr
from session import RelaySession, SessionPool, StartResult


class Socket:
    def __init__(self, frames=(), *, ready=True):
        self.frames = list(frames)
        self.sent = []
        self.closed = False
        self.ready = ready

    async def recv(self):
        if not self.ready or not self.frames:
            await asyncio.sleep(10)
        item = self.frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def send(self, value):
        self.sent.append(json.loads(value))
        if self.sent[-1].get("type") == "user_prompt":
            self.ready = True

    async def close(self):
        self.closed = True


class Backend:
    def __init__(self, mode="per_conversation", sockets=(), **cfg):
        self.cfg = {"session_mode": mode, "max_workers": 4, "thread_id": "shared",
                    "turn_timeout": 2, "recv_timeout": 0.02, "agent_mode": 2,
                    "show_thinking": False, "base": "https://example.invalid", **cfg}
        self.created = []
        self.sockets = list(sockets)
        self.connects = 0

    async def create_thread(self):
        value = "thread-" + str(len(self.created) + 1)
        self.created.append(value)
        return value

    async def start_run(self, _thread):
        return StartResult("wss://example.invalid", "sandbox")

    async def connect(self, _url):
        self.connects += 1
        return self.sockets.pop(0)

    async def get_messages(self, _thread):
        return {"messages": []}


def run(awaitable):
    return asyncio.run(awaitable)


async def collect(session, **kwargs):
    return [event async for event in session.stream("original prompt", {"model": "m", "reasoningEffort": "high"}, **kwargs)]


def test_reconnect_replays_the_same_prompt_and_message_identity():
    first = Socket([RuntimeError("closed")])
    second = Socket([{"type": "text", "messageId": "a", "content": "OK"},
                     {"type": "agent_loop_done"}], ready=False)
    backend = Backend(sockets=[second])
    session = RelaySession(backend, "t")
    session.ws = first
    events = run(collect(session))
    assert first.sent[0] == second.sent[0]
    assert second.sent[0]["modelProvider"]["reasoningEffort"] == "high"
    assert [event["text"] for event in events if event["type"] == "final_text"] == ["OK"]


def test_reconnect_never_replays_after_content_has_been_delivered():
    socket = Socket([{"type": "text", "messageId": "a", "content": "partial"}, RuntimeError("closed")])
    backend = Backend()
    session = RelaySession(backend, "t")
    session.ws = socket
    with pytest.raises(apierr.ApiError):
        run(collect(session))
    assert backend.connects == 0
    assert socket.closed and session.ws is None


def test_timeout_closes_socket_and_joins_poller():
    backend = Backend(turn_timeout=0.01, show_thinking=True)
    session = RelaySession(backend, "t")
    socket = Socket()
    session.ws = socket
    events = run(collect(session))
    assert events[-1] == {"type": "end", "reason": "timeout"}
    assert socket.closed and session.ws is None
    assert session._needs_drain


def test_abandoned_stream_closes_socket_and_cancels_its_poller():
    backend = Backend(show_thinking=True)
    session = RelaySession(backend, "t")
    session.ws = socket = Socket([{"type": "text", "messageId": "a", "content": "partial"}])

    async def scenario():
        stream = session.stream("prompt", {})
        assert (await anext(stream))["type"] == "text"
        await stream.aclose()
        assert not any(task.get_coro().__name__ == "_poll_thinking" for task in asyncio.all_tasks())

    run(scenario())
    assert socket.closed


def test_cancellation_remains_sticky_for_a_tool_followup():
    backend = Backend()
    session = RelaySession(backend, "t")
    socket = Socket([{"type": "agent_loop_done"}])
    session.ws = socket

    async def scenario():
        await collect(session)
        session.cancel()  # e.g. client leaves while local exec is running
        return await collect(session)

    events = run(scenario())
    assert events == [{"type": "end", "reason": "cancelled"}]
    assert len([f for f in socket.sent if f["type"] == "user_prompt"]) == 1
    assert socket.closed


@pytest.mark.parametrize("mode", ["shared", "per_conversation"])
def test_old_lease_cannot_release_or_cancel_the_new_owner(mode):
    pool = SessionPool(Backend(mode))

    async def scenario():
        first = await pool.acquire("a")
        await pool.release(first)
        second = await pool.acquire("a")
        assert second is not first and second.session is first.session
        third_task = asyncio.create_task(pool.acquire("a"))
        await asyncio.sleep(0)
        await pool.release(first)
        first.cancel()
        await asyncio.sleep(0)
        assert not third_task.done()
        assert not second.session._cancelled
        await pool.release(second)
        third = await asyncio.wait_for(third_task, 1)
        await pool.release(third)
        assert pool._slots._value == 4

    run(scenario())


def test_anonymous_requests_never_reuse_an_upstream_thread():
    pool = SessionPool(Backend())

    async def scenario():
        first = await pool.acquire()
        await pool.release(first)
        second = await pool.acquire()
        await pool.release(second)
        assert first.thread_id != second.thread_id
        assert not pool._conversations and not pool._conversation_locks

    run(scenario())


def test_conversation_cap_evicts_idle_socket_but_never_active_owner():
    pool = SessionPool(Backend(max_conversations=1))

    async def scenario():
        first = await pool.acquire("a")
        socket = Socket()
        first.session.ws = socket
        with pytest.raises(apierr.ApiError) as error:
            await pool.acquire("b")
        assert error.value.status == 503
        assert not socket.closed
        await pool.release(first)
        second = await pool.acquire("b")
        assert socket.closed
        assert len(pool._conversations) == len(pool._conversation_locks) == 1
        assert "b" in pool._conversations
        await pool.release(second)

    run(scenario())


def test_idle_timeout_removes_session_and_lock():
    pool = SessionPool(Backend(conversation_idle_timeout=1))

    async def scenario():
        first = await pool.acquire("a")
        first.session.ws = socket = Socket()
        await pool.release(first)
        pool._last_used["a"] -= 2
        second = await pool.acquire("b")
        assert socket.closed
        assert "a" not in pool._conversations
        assert "a" not in pool._conversation_locks
        await pool.release(second)

    run(scenario())


def test_waiting_conversation_is_not_evicted_during_handoff():
    pool = SessionPool(Backend(max_conversations=1))

    async def scenario():
        first = await pool.acquire("a")
        pending = asyncio.create_task(pool.acquire("a"))
        await asyncio.sleep(0)
        await pool.release(first)
        with pytest.raises(apierr.ApiError):
            await pool.acquire("b")
        second = await asyncio.wait_for(pending, 1)
        assert second.session is first.session
        await pool.release(second)
        third = await pool.acquire("b")
        await pool.release(third)
        assert pool._slots._value == 4

    run(scenario())


def test_failed_conversation_creation_cleans_pending_lock_and_slot():
    backend = Backend()

    async def fail():
        raise asyncio.CancelledError()

    backend.create_thread = fail
    pool = SessionPool(backend)
    with pytest.raises(asyncio.CancelledError):
        run(pool.acquire("a"))
    assert pool._slots._value == 4
    assert not pool._conversation_locks and not pool._conversation_refs


def test_open_auth_refresh_runs_outside_the_event_loop():
    backend = Backend()
    heartbeat = threading.Event()
    connected = 0

    def refresh():
        assert heartbeat.wait(1), "WebSocket refresh blocked the event loop"
        return True

    async def connect(_url):
        nonlocal connected
        connected += 1
        if connected == 1:
            error = RuntimeError("401 unauthorized")
            error.status_code = 401
            raise error
        return Socket(ready=False)

    backend.refresher = types.SimpleNamespace(refresh=refresh)
    backend.connect = connect
    session = RelaySession(backend, "t")

    async def scenario():
        async def tick():
            await asyncio.sleep(0.01)
            heartbeat.set()
        await asyncio.gather(session.open(), tick())

    run(scenario())
    assert connected == 2


def test_fast_turn_fetches_persisted_thinking_once_before_completion():
    backend = Backend(show_thinking=True)

    async def messages(_thread):
        return {"messages": [{"createdAt": "2999-01-01T00:00:00Z",
                               "content": {"type": "thinking", "thinking": "provider thought"}}]}

    backend.get_messages = messages
    session = RelaySession(backend, "t")
    session.ws = Socket([{"type": "agent_loop_done"}])
    events = run(collect(session))
    assert "".join(e["delta"] for e in events if e["type"] == "thinking") == "provider thought"


def test_ws_and_history_thinking_are_not_duplicated():
    backend = Backend(show_thinking=True)

    async def messages(_thread):
        return {"messages": [{"createdAt": "2999-01-01T00:00:00Z",
                               "content": {"type": "thinking", "thinking": "provider thought"}}]}

    backend.get_messages = messages
    session = RelaySession(backend, "t")
    session.ws = Socket([{"type": "thinking", "messageId": "a", "content": "provider thought"},
                         {"type": "agent_loop_done"}])
    events = run(collect(session))
    assert "".join(e["delta"] for e in events if e["type"] == "thinking") == "provider thought"


def test_internal_tool_calls_are_not_returned_as_client_tools():
    session = RelaySession(Backend(), "t")
    session.ws = Socket([{"type": "tool_call", "toolCallId": "internal", "toolName": "internal_tool", "input": {}},
                         {"type": "tool_call", "toolCallId": "native", "toolName": "read_file", "input": {"path": "a.py"}},
                         {"type": "agent_loop_done"}])
    events = run(collect(session, tool_names=["read_file"]))
    assert [e["id"] for e in events if e["type"] == "tool_call"] == ["native"]


def test_provider_thinking_signature_is_preserved_without_fabrication():
    session = RelaySession(Backend(), "t")
    session.ws = Socket([{"type": "thinking", "content": "thought", "signature": "provider-signature"},
                         {"type": "agent_loop_done"}])
    events = run(collect(session))
    assert [e for e in events if e["type"] == "thinking_signature"] == [
        {"type": "thinking_signature", "signature": "provider-signature"}]


def test_adjacent_turn_thinking_does_not_leak_through_timestamp_tolerance():
    backend = Backend(show_thinking=True)
    start = 1790000000.0
    old_timestamp = datetime.datetime.fromtimestamp(start - 0.05, datetime.timezone.utc).isoformat()

    async def messages(_thread):
        return {"messages": [
            {"createdAt": old_timestamp, "content": {"type": "thinking", "thinking": "previous turn"}},
            {"createdAt": "2999-01-01T00:00:00Z", "content": {"type": "thinking", "thinking": "current turn"}},
        ]}

    backend.get_messages = messages
    session = RelaySession(backend, "t")

    async def scenario():
        queue = asyncio.Queue()
        state = {"thinking": "", "tools": {}, "signatures": set()}
        await session._queue_history(start, queue, (), state)
        return [queue.get_nowait() for _ in range(queue.qsize())]

    events = run(scenario())
    assert "".join(e["delta"] for e in events if e["type"] == "thinking") == "current turn"


def test_persisted_thinking_is_delivered_while_the_websocket_is_quiet():
    backend = Backend(show_thinking=True, recv_timeout=10)
    session = RelaySession(backend, "t")
    session.ws = socket = Socket()

    async def poll(_start, queue, _names, _state):
        await asyncio.sleep(0.01)
        queue.put_nowait({"type": "thinking", "delta": "provider thought"})
        await asyncio.sleep(10)

    session._poll_thinking = poll

    async def scenario():
        stream = session.stream("prompt", {})
        try:
            event = await asyncio.wait_for(anext(stream), 0.75)
            assert event == {"type": "thinking", "delta": "provider thought"}
        finally:
            await stream.aclose()

    run(scenario())
    assert socket.closed
