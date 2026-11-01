"""Avoid relay-owned waits without weakening provider cleanup or retries."""
import asyncio
import io
import json
import time
import types
import urllib.error

import pytest

import apierr
import session as transport
from session import Backend, RelaySession, StartResult


def run(awaitable):
    return asyncio.run(awaitable)


def backend(post):
    cfg = {"upstream_timeout": 5, "base": "https://example.invalid"}
    refresher = types.SimpleNamespace(call=lambda request: request(), last_error=None)
    return Backend(cfg, http_post=post, refresher=refresher)


@pytest.mark.parametrize("error", [
    lambda: urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {}, io.BytesIO(b"busy")),
    lambda: urllib.error.HTTPError("https://example.invalid", 429, "limited", {"retry-after": "2"}, io.BytesIO()),
    lambda: TimeoutError(),
    lambda: urllib.error.URLError("offline"),
])
def test_exhausted_http_retry_does_not_sleep_before_returning_failure(monkeypatch, error):
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(transport.asyncio, "sleep", sleep)
    calls = []

    def post(*_):
        calls.append(1)
        raise error()

    with pytest.raises(apierr.ApiError):
        run(backend(post)._call("/test", {}, attempts=3))
    assert len(calls) == 3
    assert len(sleeps) == 2  # only the gaps between actual retries


def test_healthy_http_transport_never_adds_a_sleep(monkeypatch):
    async def unexpected_sleep(_delay):
        pytest.fail("healthy request inserted a retry delay")

    monkeypatch.setattr(transport.asyncio, "sleep", unexpected_sleep)
    assert run(backend(lambda *_: {"ok": True})._call("/test", {})) == {"ok": True}


def test_recovered_http_retry_preserves_between_attempt_backoff(monkeypatch):
    sleeps, calls = [], []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(transport.asyncio, "sleep", sleep)

    def post(*_):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("temporary network failure")
        return {"ok": True}

    assert run(backend(post)._call("/test", {})) == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [0.4]


class Socket:
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.sent = []
        self.reads = 0

    async def recv(self):
        self.reads += 1
        if not self.frames:
            await asyncio.sleep(10)
        return json.dumps(self.frames.pop(0))

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def close(self):
        pass


def make_session(socket, *, show_thinking=False):
    async def start(_thread):
        return StartResult("wss://example.invalid", "sandbox")

    async def connect(_url):
        return socket

    async def messages(_thread):
        return {"messages": []}

    cfg = {"base": "https://example.invalid", "turn_timeout": 3, "recv_timeout": 2,
           "show_thinking": show_thinking}
    upstream = types.SimpleNamespace(cfg=cfg, start_run=start, connect=connect, get_messages=messages)
    return RelaySession(upstream, "t")


def test_new_websocket_sends_prompt_without_waiting_for_quiet_period():
    socket = Socket()
    relay = make_session(socket)

    async def scenario():
        started = time.monotonic()
        await relay.open()
        assert time.monotonic() - started < 0.1
        assert socket.reads == 0  # no fixed 400 ms startup drain
        await relay.aclose()

    run(scenario())


def test_already_received_credits_do_not_wait_for_another_trailing_frame():
    socket = Socket([{"type": "run_credits_persisted", "creditsUsed": 0.5},
                     {"type": "agent_loop_done"}])
    relay = make_session(socket)

    async def unexpected_trailing():
        pytest.fail("already received credits must not start a trailing wait")

    relay._trailing = unexpected_trailing

    async def scenario():
        started = time.monotonic()
        events = [event async for event in relay.stream("prompt", {})]
        assert time.monotonic() - started < 0.1
        assert [event for event in events if event["type"] == "credits"] == [
            {"type": "credits", "value": 0.5}]
        assert events[-1] == {"type": "end", "reason": "stop"}

    run(scenario())


def test_empty_history_polls_back_off_but_new_content_restores_cadence(monkeypatch):
    relay = make_session(Socket(), show_thinking=True)
    intervals = []
    changes = iter([False, False, False, False, True, False])

    async def queue_history(*_):
        return next(changes)

    async def tick(awaitable, *, timeout):
        awaitable.close()
        intervals.append(timeout)
        if len(intervals) > 6:
            raise asyncio.CancelledError()
        raise asyncio.TimeoutError()

    relay._queue_history = queue_history
    monkeypatch.setattr(transport.asyncio, "wait_for", tick)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await relay._poll_thinking(0, asyncio.Queue())

    run(scenario())
    assert intervals == [2.0, 4.0, 8.0, 15.0, 15.0, 2.0, 4.0]


def test_ws_activity_resets_history_backoff_without_extra_rest_request(monkeypatch):
    relay = make_session(Socket(), show_thinking=True)
    intervals, polls = [], []

    async def queue_history(*_):
        polls.append(1)
        return False

    async def tick(awaitable, *, timeout):
        awaitable.close()
        intervals.append(timeout)
        if len(intervals) > 3:
            raise asyncio.CancelledError()
        if len(intervals) == 3:
            return True  # new visible content arrived over the WebSocket
        raise asyncio.TimeoutError()

    relay._queue_history = queue_history
    monkeypatch.setattr(transport.asyncio, "wait_for", tick)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await relay._poll_thinking(0, asyncio.Queue())

    run(scenario())
    assert intervals == [2.0, 4.0, 8.0, 2.0]
    assert len(polls) == 2


def test_failed_history_poll_backs_off_instead_of_hammering_provider(monkeypatch):
    relay = make_session(Socket(), show_thinking=True)
    intervals = []

    async def queue_history(*_):
        raise urllib.error.URLError("temporary failure")

    async def tick(awaitable, *, timeout):
        awaitable.close()
        intervals.append(timeout)
        if len(intervals) > 3:
            raise asyncio.CancelledError()
        raise asyncio.TimeoutError()

    relay._queue_history = queue_history
    monkeypatch.setattr(transport.asyncio, "wait_for", tick)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await relay._poll_thinking(0, asyncio.Queue())

    run(scenario())
    assert intervals == [2.0, 4.0, 8.0, 15.0]
