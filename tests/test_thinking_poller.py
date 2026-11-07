import asyncio
import json
import types

import pytest

from session import RelaySession

THINK = {"senderRole": "sandpack", "createdAt": "2999-01-01T00:00:00.000Z",
         "content": {"type": "thinking", "thinking": "размышляю"}}

# The poller asks the history again after 2 s of silence (RelaySession._poll_thinking) and the socket below idles half a second
# between its frames, so a test of what the poller does between the frames took 4.9 s of real time. Every timeout of
# asyncio.wait_for (the poller's, the receive timeouts) and the idling of the socket are SPEED times shorter: the proportions
# between them, which is what the tests are about, stay.
SPEED = 10


@pytest.fixture(autouse=True)
def faster_time(monkeypatch):
    real = asyncio.wait_for

    async def wait_for(awaitable, timeout=None):
        return await real(awaitable, None if timeout is None else timeout / SPEED)

    monkeypatch.setattr(asyncio, "wait_for", wait_for)


class SlowWS:
    """Idles like a socket so the poller gets a chance to tick."""

    def __init__(self, pings=8):
        self.pings = pings
        self.sent = []

    async def recv(self):
        if self.pings:
            self.pings -= 1
            await asyncio.sleep(0.5 / SPEED)
            return json.dumps({"type": "ping"})
        await asyncio.sleep(0.5 / SPEED)
        return json.dumps({"type": "agent_loop_done"})

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        return None


def test_poller_emits_persisted_thinking():
    calls = 0

    async def messages(_thread_id):
        nonlocal calls
        calls += 1
        return {"messages": [] if calls == 1 else [THINK]}

    backend = types.SimpleNamespace(
        cfg={"turn_timeout": 30, "recv_timeout": 5, "base": "https://x", "agent_mode": 2,
             "show_thinking": True},
        refresher=None,
        get_messages=messages,
    )
    session = RelaySession(backend, "t1")
    session.ws = SlowWS()

    async def scenario():
        return [e async for e in session.stream("p", {})]

    events = asyncio.run(scenario())
    thinking = [e["delta"] for e in events if e["type"] == "thinking"]
    assert "размышляю" in "".join(thinking)


def test_poller_ignores_thinking_from_an_older_turn():
    calls = 0

    async def messages(_thread_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"messages": [{"createdAt": "2020-01-01T00:00:00.000Z",
                                  "content": {"type": "thinking", "thinking": "старый блок"}}]}
        return {"messages": [
            {"createdAt": "2020-01-01T00:00:00.000Z",
             "content": {"type": "thinking", "thinking": "старый блок"}},
            THINK,
        ]}

    backend = types.SimpleNamespace(
        cfg={"turn_timeout": 30, "recv_timeout": 5, "base": "https://x", "agent_mode": 2,
             "show_thinking": True},
        refresher=None,
        get_messages=messages,
    )
    session = RelaySession(backend, "t1")
    session.ws = SlowWS()

    async def scenario():
        return [e async for e in session.stream("p", {})]

    events = asyncio.run(scenario())
    thinking = "".join(e["delta"] for e in events if e["type"] == "thinking")
    assert "старый блок" not in thinking
    assert "размышляю" in thinking


async def _messages():
    return {"messages": [THINK]}
