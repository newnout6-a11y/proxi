"""A stream that waits for its upstream stays alive in the client's own terms, and the relay writes no content for it.

The keepalive of 54b1023's relay was the SSE comment ": keep-alive" every 15 s of an empty event queue. Codex (0.167,
0.169) resets its stream_idle_timeout_ms (300 s) only on an event with data, and drops comments in its SSE parser; Claude
Code resets its idle timer on events (a ping up to 30 times in a row) and shows "Waiting for API response" after 45 s
without bytes. The keepalive is now the dialect's own event (research S4 §2.1, §3.1; R3-01; A01-01): Responses
response.in_progress, Messages ping every 25 s, Chat the comment (OpenCode counts bytes; DeepSeek Harness counts only
content, which nothing honest can fake). It goes out once nothing was written to the socket for its interval, counted on
the monotonic clock from the last write: events that write nothing (a transport heartbeat, a held structured reply) no
longer keep it from going out (findings 3 №16), and a step back of the wall clock no longer delays it (A01-08)."""
import asyncio
import json
import time as real_time

import pytest

import adapters
import apierr
import proxy
import sse
import validate

CHAT = {"model": "claude-opus-5-5", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
ANTHROPIC = {"model": "claude-opus-5-5", "max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
RESPONSES = {"model": "claude-opus-5-5", "stream": True, "input": "hi"}
ROUTES = {"chat": ("/v1/chat/completions", CHAT), "anthropic": ("/v1/messages", ANTHROPIC),
          "responses": ("/v1/responses", RESPONSES)}


class Writer:
    def __init__(self):
        self.events = []

    def event(self, name, payload):
        self.events.append((name, payload))

    def data(self, payload):
        self.events.append(("data", payload))

    def comment(self, text="keep-alive"):
        self.events.append(("comment", text))

    def done(self):
        self.events.append(("done", None))


def parsed():
    p = validate.Parsed()
    p.model, p.show_thinking = "claude-opus-5-5", True
    return p


def test_the_responses_keepalive_is_an_event_with_data_and_its_own_sequence_number():
    writer = Writer()
    adapter = adapters.ResponsesAdapter(parsed(), writer)
    adapter.begin()
    adapter.keepalive()
    adapter.keepalive()
    (_, created), (name, first), (_, second) = writer.events
    assert name == "response.in_progress" and first["type"] == "response.in_progress"
    assert [created["sequence_number"], first["sequence_number"], second["sequence_number"]] == [1, 2, 3]
    assert first["response"] == {"id": adapter.id, "object": "response", "created_at": adapter.created,
                                 "model": "claude-opus-5-5", "status": "in_progress", "output": []}
    assert adapter.keepalive_every == sse.HEARTBEAT_EVERY


def test_the_messages_keepalive_is_a_ping_every_25_seconds():
    # Claude Code counts at most 30 pings in a row: 30 x 25 s + its 300 s is more than the relay's 900 s turn.
    writer = Writer()
    adapter = adapters.AnthropicAdapter(parsed(), writer)
    adapter.keepalive()
    assert writer.events == [("ping", {"type": "ping"})] and adapter.keepalive_every == 25.0


def test_the_chat_keepalive_is_a_comment():
    writer = Writer()
    adapter = adapters.ChatAdapter(parsed(), writer)
    adapter.keepalive()
    assert writer.events == [("comment", "keep-alive")] and adapter.keepalive_every == sse.HEARTBEAT_EVERY


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_the_wrappers_pass_the_keepalive_on(dialect):
    p = parsed()
    p.structured_output = {"kind": "json_object", "source": "response_format", "label": "json", "title": False}
    p.stop_sequences, p.output_cap = ["STOP"], 100
    writer = Writer()
    adapter = adapters.for_dialect(dialect, p, writer)
    assert isinstance(adapter, adapters.OutputCapAdapter)
    adapter.begin()
    before = len(writer.events)
    adapter.keepalive()
    assert len(writer.events) == before + 1
    assert adapter.keepalive_every == (25.0 if dialect == "anthropic" else sse.HEARTBEAT_EVERY)


def silent_upstream(seconds, heartbeat=None):
    """A turn whose upstream says nothing for `seconds` (or only sends the transport's heartbeats), then answers."""
    async def run(parsed, out, holder):
        try:
            waited = 0.0
            while waited < seconds:
                await asyncio.sleep(heartbeat or seconds)
                waited += heartbeat or seconds
                if heartbeat:
                    out.put({"type": "thinking_heartbeat", "elapsed": round(waited, 2)})
            out.put({"type": "text", "delta": "ok"})
            out.put({"type": "end", "reason": "stop"})
        finally:
            out.put(None)
    return run


def keepalives(dialect, body):
    if dialect == "responses":
        return [json.loads(line[6:]) for line in body.splitlines()
                if line.startswith("data: ") and '"response.in_progress"' in line]
    if dialect == "anthropic":
        return [line for line in body.splitlines() if line == "event: ping"][1:]  # the first comes with message_start
    return [line for line in body.splitlines() if line == ": keep-alive"]


@pytest.fixture
def fast_keepalive(monkeypatch):
    monkeypatch.setattr(sse, "HEARTBEAT_EVERY", 0.3)
    monkeypatch.setattr(adapters.AnthropicAdapter, "keepalive_every", 0.3, raising=False)


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_a_silent_upstream_gets_the_dialects_keepalive(relay, fast_keepalive, dialect):
    relay.fake.run = silent_upstream(1.2)
    path, payload = ROUTES[dialect]
    status, body = relay.sse(path, payload)
    assert status == 200 and len(keepalives(dialect, body)) >= 2, body
    if dialect != "chat":
        assert ": keep-alive" not in body  # Codex and Claude Code drop comments: the event replaces it
    if dialect == "responses":
        numbers = [json.loads(line[6:])["sequence_number"] for line in body.splitlines() if line.startswith("data: {")]
        assert numbers == list(range(1, len(numbers) + 1))


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_events_that_write_nothing_do_not_hold_the_keepalive_back(relay, fast_keepalive, dialect):
    # The transport's heartbeats come every 0.05 s and the adapters write nothing for them: the queue was never empty
    # for 0.25 s, so the keepalive of the empty-queue branch never went out.
    relay.fake.run = silent_upstream(1.2, heartbeat=0.05)
    path, payload = ROUTES[dialect]
    status, body = relay.sse(path, payload)
    assert status == 200 and len(keepalives(dialect, body)) >= 2, body


class SteppedBackClock:
    """The wall clock stepped back an hour after the stream began (w32time correcting a drift, a manual change)."""

    def __getattr__(self, name):
        return getattr(real_time, name)

    @staticmethod
    def time():
        return real_time.time() - 3600


def test_a_step_back_of_the_wall_clock_does_not_hold_the_keepalive_back(relay, fast_keepalive, monkeypatch):
    monkeypatch.setattr(proxy, "time", SteppedBackClock())
    relay.fake.run = silent_upstream(1.2)
    status, body = relay.sse("/v1/messages", ANTHROPIC)
    assert status == 200 and len(keepalives("anthropic", body)) >= 2, body


def test_an_error_after_a_keepalive_still_ends_the_stream_as_an_error(relay, fast_keepalive):
    async def run(parsed, out, holder):
        try:
            await asyncio.sleep(0.8)
            out.put({"type": "error", "error": apierr.upstream_error("cut")})
        finally:
            out.put(None)
    relay.fake.run = run
    status, body = relay.sse("/v1/messages", ANTHROPIC)
    assert status == 200 and len(keepalives("anthropic", body)) >= 1 and "event: error" in body
