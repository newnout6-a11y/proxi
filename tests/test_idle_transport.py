"""Persistent upstream control traffic and isolated, bounded application frames."""
import asyncio
import json

import pytest

import apierr
import session as transport
from session import RelaySession, StartResult


class Socket:
    def __init__(self, turns=()):
        self.incoming = asyncio.Queue()
        self.turns = list(turns)
        self.sent = []
        self.closed = False
        self.receivers = 0
        self.max_receivers = 0

    def feed(self, *frames):
        for frame in frames:
            self.incoming.put_nowait(frame if isinstance(frame, Exception) else json.dumps(frame))

    async def recv(self):
        self.receivers += 1
        self.max_receivers = max(self.max_receivers, self.receivers)
        try:
            value = await self.incoming.get()
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.receivers -= 1

    async def send(self, payload):
        event = json.loads(payload)
        self.sent.append(event)
        if event["type"] == "user_prompt" and self.turns:
            self.feed(*self.turns.pop(0))

    async def close(self):
        self.closed = True


class Backend:
    def __init__(self, *sockets, **cfg):
        self.cfg = {"turn_timeout": 2, "recv_timeout": 0.02, "show_thinking": False, **cfg}
        self.sockets = list(sockets)
        self.starts = self.connects = 0

    async def start_run(self, _thread):
        self.starts += 1
        return StartResult("wss://example.invalid", "sandbox")

    async def connect(self, _url):
        self.connects += 1
        return self.sockets.pop(0)

    async def get_messages(self, _thread):
        return {"messages": []}


def complete(text):
    return [{"type": "text", "messageId": text, "content": text},
            {"type": "run_credits_persisted", "creditsUsed": 0.25},
            {"type": "agent_loop_done"}]


async def collect(relay):
    return [event async for event in relay.stream("prompt", {"model": "m"})]


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


def test_idle_socket_answers_ping_and_ack_without_starting_a_turn():
    async def scenario():
        socket = Socket()
        relay = RelaySession(Backend(), "thread")
        relay.ws = socket  # Preserve direct socket injection used by callers/tests.
        await relay.open()
        reader = relay._reader
        try:
            socket.feed({"type": "ping"}, {"type": "sandbox_log", "ack": {"id": "idle-ack"}})
            await until(lambda: len(socket.sent) == 2)
            assert socket.sent == [{"type": "pong"}, {"type": "client_ack", "replyTo": "idle-ack"}]
            assert json.loads(await relay._recv())["type"] == "sandbox_log"
            assert not reader.frames  # Control replies are not application frames.
            assert socket.max_receivers == 1
        finally:
            await relay.aclose()
        assert reader.task.done() and socket.closed and socket.receivers == 0

    asyncio.run(scenario())


def test_application_order_survives_interleaved_control_frames():
    async def scenario():
        socket = Socket()
        relay = RelaySession(Backend(socket), "thread")
        await relay.open()
        expected = [{"type": "text", "content": "one", "ack": {"id": "a"}},
                    {"type": "thinking", "content": "two"},
                    {"type": "run_credits_persisted", "creditsUsed": 0.5, "ack": {"id": "b"}}]
        socket.feed(expected[0], {"type": "ping"}, expected[1], expected[2])
        try:
            received = [json.loads(await relay._recv()) for _ in expected]
            assert received == expected
            assert socket.sent == [{"type": "client_ack", "replyTo": "a"}, {"type": "pong"},
                                   {"type": "client_ack", "replyTo": "b"}]
            assert socket.max_receivers == 1
        finally:
            await relay.aclose()

    asyncio.run(scenario())


def test_warm_turns_reuse_one_reader_and_keep_responding_between_requests():
    async def scenario():
        socket = Socket([complete("first"), complete("second")])
        backend = Backend(socket)
        relay = RelaySession(backend, "thread")
        try:
            first = await collect(relay)
            reader = relay._reader
            socket.feed({"type": "ping"}, {"type": "sandbox_log", "ack": {"id": "between"}})
            await until(lambda: {"type": "client_ack", "replyTo": "between"} in socket.sent)
            second = await collect(relay)
            assert relay._reader is reader and not reader.task.done()
            assert backend.starts == backend.connects == 1
            assert [event["text"] for event in first if event["type"] == "final_text"] == ["first"]
            assert [event["text"] for event in second if event["type"] == "final_text"] == ["second"]
            assert socket.max_receivers == 1
        finally:
            await relay.aclose()

    asyncio.run(scenario())


def test_trailing_credits_use_the_same_reader_and_ack_before_delivery():
    async def scenario():
        frames = [{"type": "text", "messageId": "answer", "content": "OK"},
                  {"type": "agent_loop_done"}, {"type": "ping"},
                  {"type": "run_credits_persisted", "creditsUsed": 0.5, "ack": {"id": "credit"}}]
        socket = Socket([frames])
        relay = RelaySession(Backend(socket), "thread")
        try:
            events = await collect(relay)
            assert [event for event in events if event["type"] == "credits"] == [{"type": "credits", "value": 0.5}]
            assert {"type": "client_ack", "replyTo": "credit"} in socket.sent
            assert socket.max_receivers == 1
        finally:
            await relay.aclose()

    asyncio.run(scenario())


def test_reader_failure_after_content_propagates_without_replaying_the_prompt():
    async def scenario():
        socket = Socket([[{"type": "text", "content": "partial"}, RuntimeError("reader failed")]])
        backend = Backend(socket)
        relay = RelaySession(backend, "thread")
        with pytest.raises(apierr.ApiError, match="reader failed"):
            await collect(relay)
        assert backend.starts == backend.connects == 1
        assert socket.closed and socket.receivers == 0 and relay.ws is None

    asyncio.run(scenario())


def test_failed_initial_reader_reconnects_once_without_dual_receivers():
    async def scenario():
        first = Socket([[RuntimeError("closed before output")]])
        second = Socket([complete("fresh")])
        backend = Backend(first, second)
        relay = RelaySession(backend, "thread")
        try:
            events = await collect(relay)
            assert first.sent[0] == second.sent[0]
            assert first.closed and first.receivers == 0
            assert first.max_receivers == second.max_receivers == 1
            assert [event["text"] for event in events if event["type"] == "final_text"] == ["fresh"]
        finally:
            await relay.aclose()

    asyncio.run(scenario())


def test_idle_disconnect_reconnects_and_discards_only_the_old_socket_queue():
    async def scenario():
        old = Socket()
        new = Socket([complete("fresh")])
        backend = Backend(old, new)
        relay = RelaySession(backend, "thread")
        await relay.open()
        reader = relay._reader
        old.feed({"type": "text", "content": "stale"}, RuntimeError("idle disconnect"))
        await until(lambda: reader.task.done())
        try:
            events = await collect(relay)
            assert [event["text"] for event in events if event["type"] == "final_text"] == ["fresh"]
            assert reader.task.done() and not reader.frames and old.closed
            assert relay._reader is not reader
            assert backend.starts == backend.connects == 2
        finally:
            await relay.aclose()

    asyncio.run(scenario())


def test_explicit_close_clears_queued_frames_before_a_new_socket_is_injected():
    async def scenario():
        relay = RelaySession(Backend(), "thread")
        old = Socket()
        relay.ws = old
        await relay.open()
        reader = relay._reader
        old.feed({"type": "text", "content": "stale", "ack": {"id": "old"}})
        await until(lambda: len(reader.frames) == 1)
        await relay.aclose()
        relay.ws = Socket([complete("new")])
        try:
            events = await collect(relay)
            assert [event["text"] for event in events if event["type"] == "final_text"] == ["new"]
            assert not reader.frames and reader.task.done() and old.closed
        finally:
            await relay.aclose()

    asyncio.run(scenario())


def test_idle_cancel_joins_the_reader_and_stays_sticky_until_the_next_lease():
    async def scenario():
        socket = Socket()
        relay = RelaySession(Backend(socket), "thread")
        await relay.open()
        reader = relay._reader
        await until(lambda: socket.receivers == 1)
        relay.cancel()
        await asyncio.wrap_future(relay._drain_task)
        assert reader.task.done() and socket.receivers == 0 and socket.closed
        assert await collect(relay) == [{"type": "end", "reason": "cancelled"}]
        assert not socket.sent
        assert relay._cancelled
        relay.begin_request()
        assert not relay._cancelled

    asyncio.run(scenario())


def test_cancelling_a_consumer_does_not_cancel_the_idle_reader():
    async def scenario():
        socket = Socket()
        relay = RelaySession(Backend(socket), "thread")
        await relay.open()
        reader = relay._reader
        consumer = asyncio.create_task(relay._recv())
        await until(lambda: socket.receivers == 1)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        try:
            socket.feed({"type": "ping"})
            await until(lambda: socket.sent == [{"type": "pong"}])
            assert not reader.task.done() and socket.max_receivers == 1
        finally:
            await relay.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("bound", ["frames", "bytes"])
def test_idle_buffer_overflow_closes_the_socket_and_reports_explicit_failure(monkeypatch, bound):
    async def scenario():
        if bound == "frames":
            monkeypatch.setattr(transport, "MAX_PENDING_FRAMES", 2)
        socket = Socket()
        relay = RelaySession(Backend(socket, max_frame_bytes=100 if bound == "bytes" else 10000), "thread")
        await relay.open()
        reader = relay._reader
        socket.feed(*([{"type": "text", "content": "x"}] * 3 if bound == "frames" else
                      [{"type": "text", "content": "x" * 101}]))
        await until(lambda: reader.task.done())
        try:
            with pytest.raises(apierr.ApiError, match="buffer overflow"):
                await relay._recv()
            with pytest.raises(apierr.ApiError, match="buffer overflow"):
                await relay._trailing()
            assert socket.closed and socket.receivers == 0
            assert len(reader.frames) <= 2 and reader.bytes <= reader.byte_limit
        finally:
            await relay.aclose()

    asyncio.run(scenario())


def test_control_send_failure_is_not_hidden_as_an_idle_timeout():
    async def scenario():
        socket = Socket()

        async def fail(_payload):
            raise RuntimeError("control response failed")

        socket.send = fail
        relay = RelaySession(Backend(socket), "thread")
        await relay.open()
        socket.feed({"type": "ping"})
        try:
            with pytest.raises(RuntimeError, match="control response failed"):
                await asyncio.wait_for(relay._recv(), 1)
            assert socket.closed
        finally:
            await relay.aclose()

    asyncio.run(scenario())


def test_one_valid_frame_at_the_configured_byte_limit_is_preserved():
    async def scenario():
        socket = Socket()
        raw = json.dumps({"type": "text", "content": "Привет"}, ensure_ascii=False)
        relay = RelaySession(Backend(socket, max_frame_bytes=len(raw.encode("utf-8"))), "thread")
        await relay.open()
        socket.incoming.put_nowait(raw)
        try:
            assert await relay._recv() == raw
            assert relay._reader.error is None and not socket.closed
        finally:
            await relay.aclose()

    asyncio.run(scenario())
