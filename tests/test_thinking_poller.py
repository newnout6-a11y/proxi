import asyncio
import json
import types

from session import RelaySession

THINK = {"senderRole": "sandpack", "createdAt": "2999-01-01T00:00:00.000Z",
         "content": {"type": "thinking", "thinking": "размышляю"}}


class SlowWS:
    """Idles like a socket so the poller gets a chance to tick."""

    def __init__(self, pings=8):
        self.pings = pings
        self.sent = []

    async def recv(self):
        if self.pings:
            self.pings -= 1
            await asyncio.sleep(0.5)
            return json.dumps({"type": "ping"})
        await asyncio.sleep(0.5)
        return json.dumps({"type": "agent_loop_done"})

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        return None


def test_poller_emits_persisted_thinking():
    backend = types.SimpleNamespace(
        cfg={"turn_timeout": 30, "recv_timeout": 5, "base": "https://x", "agent_mode": 2,
             "show_thinking": True},
        refresher=None,
        get_messages=lambda thread_id: _messages(),
    )
    session = RelaySession(backend, "t1")
    session.ws = SlowWS()

    async def scenario():
        return [e async for e in session.stream("p", {})]

    events = asyncio.run(scenario())
    thinking = [e["delta"] for e in events if e["type"] == "thinking"]
    assert "размышляю" in "".join(thinking)


async def _messages():
    return {"messages": [THINK]}
