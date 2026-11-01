"""Review regressions for resource bounds and unsupported WebSocket frames."""
import asyncio
import json
import types

import pytest

import apierr
import config
from session import RelaySession


def load_config(tmp_path, monkeypatch, **settings):
    path = tmp_path / "config.json"
    cfg = {"base": "https://example.invalid", "page_uuid": "page", "ua": "ua",
           "cookie": "accessToken=fake-token", "xsrf": "fake-xsrf", **settings}
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")
    monkeypatch.delenv("RETOOL_COOKIE", raising=False)
    monkeypatch.delenv("RETOOL_XSRF", raising=False)
    return config.load(path)


@pytest.mark.parametrize("field", ["max_body_bytes", "max_frame_bytes"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_transport_bounds_are_rejected(tmp_path, monkeypatch, field, value):
    with pytest.raises(apierr.ApiError, match=f"{field} must be positive") as error:
        load_config(tmp_path, monkeypatch, **{field: value})
    assert error.value.status == 503


@pytest.mark.parametrize("field", ["max_body_bytes", "max_frame_bytes"])
@pytest.mark.parametrize("value", [1, 4096])
def test_positive_transport_bounds_load_without_rewriting(tmp_path, monkeypatch, field, value):
    assert load_config(tmp_path, monkeypatch, **{field: value})[field] == value


class Socket:
    def __init__(self, frames=()):
        self.frames = [json.dumps(frame) for frame in frames]
        self.sent = []

    async def recv(self):
        if not self.frames:
            await asyncio.Future()
        return self.frames.pop(0)

    async def send(self, frame):
        self.sent.append(json.loads(frame))

    async def close(self):
        pass


def make_session(frames=()):
    backend = types.SimpleNamespace(cfg={"turn_timeout": 2, "recv_timeout": 0.02,
                                         "show_thinking": False, "agent_mode": 2})
    session = RelaySession(backend, "thread")
    session.ws = Socket(frames)
    return session


def test_heartbeat_ignores_non_object_json_and_answers_a_real_ping():
    session = make_session()

    async def scenario():
        for frame in [None, [], [{"type": "ping"}], "ping", 1, True]:
            await session._maybe_pong(json.dumps(frame))
        assert not session.ws.sent
        await session._maybe_pong('{"type":"ping"}')

    asyncio.run(scenario())
    assert session.ws.sent == [{"type": "pong"}]


@pytest.mark.parametrize("invalid", [None, [], [{"type": "ping"}], "ping", 1, True])
@pytest.mark.parametrize("position", ["stream", "trailing"])
def test_non_object_json_does_not_interrupt_text_or_trailing_credits(invalid, position):
    text = {"type": "text", "messageId": "answer", "content": "OK"}
    credits = {"type": "run_credits_persisted", "creditsUsed": 0.5}
    done = {"type": "agent_loop_done"}
    unsupported_then_ping = [invalid, {"type": "ping"}]
    frames = (unsupported_then_ping + [text, credits, done] if position == "stream"
              else [text, done] + unsupported_then_ping + [credits])
    session = make_session(frames)

    async def scenario():
        return [event async for event in session.stream("prompt", {"model": "m"})]

    events = asyncio.run(scenario())
    assert events == [{"type": "text", "delta": "OK"},
                      {"type": "credits", "value": 0.5},
                      {"type": "final_text", "text": "OK"},
                      {"type": "end", "reason": "stop"}]
    assert session.ws.sent[-1] == {"type": "pong"}
