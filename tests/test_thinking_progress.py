"""Thinking progress the client sees comes from the upstream only: a real start of reasoning, real reasoning text.

The relay invents nothing: no timer status, no "Модель думает…" label, no "⏳ N с…" ticks, no empty summary part. Early
display status must not expose thinking from a discarded attempt either."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

import adapters
import direct
import journal
import minara
import orchestrator
import validate
from test_native_context_reuse import FIRST, request, run, setup
from test_review_fixes_pr19 import END, THINKING, signature, stream_direct, text


class SilentMinaraResponse:
    """Minara sends its first byte only after a silence longer than the old relay timer's 1.5 s, as a channel that
    reasons without showing it does (GPT through resp_, Gemini through chatcmpl-: 19-23 s in the probes)."""
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, silence):
        self.silence = silence

    def iter_content(self, chunk_size):
        time.sleep(self.silence)
        frame = {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]}
        yield ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode()

    def close(self):
        pass


def test_a_silent_minara_upstream_gets_no_thinking_status_from_a_relay_timer():
    # 54b1023 sent {"type": "thinking_status", "source": "relay"} after 1.5 s of silence, and a heartbeat every 5 s,
    # to every Minara turn whose level was not "none" (a turn without any level as well): the client printed
    # "Модель думает…" with no signal from the backend, and the orchestrator took the turn for started (no failover).
    session = SimpleNamespace(backend=SimpleNamespace(_minara_post=lambda *a, **kw: SilentMinaraResponse(1.8)),
                              _cancelled=False)

    async def collect():
        return [event async for event in minara.iter_events(
            {"bearer_token": "secret"}, session, {"model": "minara/openai/x"}, "hi", "", None, None, 10)]

    assert [event["type"] for event in asyncio.run(collect())] == ["usage_start", "text", "end"]


def test_thinking_status_arrives_while_the_answer_is_still_blocked(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()

    class PausedResponse:
        status = 200
        headers = {}

        def __iter__(self):
            yield json.dumps(THINKING[0]).encode() + b"\n"
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release the answer")
            for event in (signature("opaque-final"), text("ok"), *END):
                yield json.dumps(event).encode() + b"\n"

        def close(self):
            release.set()

    calls, _ = stream_direct(tmp_path, monkeypatch, [PausedResponse])

    async def collect():
        cfg = {"base": "https://fixture.invalid", "upstream_error_dir": str(tmp_path / "diag")}
        session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {}), _cancelled=False)
        stream = direct._stream_body(cfg, session, {"model": "claude-opus-5-5"})
        assert await anext(stream) == {"type": "usage_start"}
        pending = asyncio.create_task(anext(stream))
        early = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            ready, _ = await asyncio.wait({pending}, timeout=1)
            if ready:
                early = pending.result()
        finally:
            release.set()
            first = await pending
            rest = [event async for event in stream]
            await stream.aclose()
        return early, [first, *rest]

    early, events = asyncio.run(collect())
    assert len(calls) == 1
    assert early == {"type": "thinking_status", "source": "upstream"}
    assert [e["signature"] for e in events if e["type"] == "thinking_signature"] == ["opaque-final"]


def test_status_survives_retry_but_discarded_thinking_does_not(tmp_path, monkeypatch):
    calls, go = stream_direct(tmp_path, monkeypatch, [
        [*THINKING, signature("discarded"), *END],
        [*THINKING, signature("accepted"), text("ok"), *END],
    ])
    events = asyncio.run(go())
    assert len(calls) == 2
    assert sum(e["type"] == "thinking_status" for e in events) == 2
    assert [e["signature"] for e in events if e["type"] == "thinking_signature"] == ["accepted"]
    assert sum(e["type"] == "thinking_start" for e in events) == 1
    assert sum(e["type"] == "credits" for e in events) == 2


def test_no_thinking_status_without_an_upstream_start(tmp_path, monkeypatch):
    _, go = stream_direct(tmp_path, monkeypatch, [[text("ok"), *END]])
    assert not any(e["type"] == "thinking_status" for e in asyncio.run(go()))


class Writer:
    def __init__(self):
        self.events = []

    def data(self, payload):
        self.events.append(payload)


def parsed(show=True, label="Модель думает…\n"):
    # thinking_placeholder is what a config of before 2026-11-19 put on every request; the adapters never read it now.
    p = validate.Parsed()
    p.model, p.show_thinking, p.thinking_placeholder = "claude-opus-5-5", show, label
    return p


SILENT = [{"type": "usage_start"}, {"type": "thinking_status", "source": "upstream"},
          {"type": "thinking_heartbeat", "elapsed": 6.5}, {"type": "thinking_status", "source": "relay"},
          {"type": "thinking_heartbeat", "elapsed": 11.5}]


def test_chat_writes_nothing_until_the_upstream_writes_reasoning():
    # Chat has no empty reasoning container: pi-ai and ai-sdk show reasoning only as text. The label of 54b1023 went
    # into DeepSeek Harness's history (134 of 827 replies began with it) and back to the model as reasoning_content.
    writer = Writer()
    adapter = adapters.ChatAdapter(parsed(), writer)
    for event in SILENT + [{"type": "thinking_start"}, {"type": "thinking_signature", "signature": "opaque"}]:
        adapter.feed(event)
    assert writer.events == []
    adapter.feed({"type": "thinking", "delta": "real reasoning"})
    adapter.feed({"type": "text", "delta": "42"})
    assert [p["choices"][0]["delta"] for p in writer.events] == [{"reasoning_content": "real reasoning"},
                                                                 {"content": "42"}]
    assert adapter.payload("stop")["choices"][0]["message"]["reasoning_content"] == "real reasoning"


class EventWriter:
    def __init__(self):
        self.events = []

    def event(self, name, payload):
        self.events.append((name, payload))

    def done(self):
        self.events.append(("done", None))


def test_anthropic_opens_an_empty_thinking_block_on_the_upstreams_status_and_only_then():
    # Claude Code shows "✻ Thinking…" and counts the seconds itself once a thinking block starts: the honest progress is
    # the block, opened when Retool's content_block_start says the model thinks, not when the answer is released.
    writer = EventWriter()
    adapter = adapters.AnthropicAdapter(parsed(), writer)
    adapter.begin()
    adapter.feed({"type": "usage_start"})
    adapter.feed({"type": "thinking_status", "source": "relay"})
    adapter.feed({"type": "thinking_heartbeat", "elapsed": 6.5})
    assert [name for name, _ in writer.events] == ["message_start", "ping"]
    adapter.feed({"type": "thinking_status", "source": "upstream"})
    assert writer.events[-1] == ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
        "type": "thinking", "thinking": "", "signature": ""}})
    adapter.feed({"type": "thinking_heartbeat", "elapsed": 11.5})
    adapter.feed({"type": "thinking_start"})  # the held start of the same block, released with its signature
    adapter.feed({"type": "thinking_signature", "signature": "opaque"})
    adapter.feed({"type": "text", "delta": "42"})
    final = adapter.end("stop")
    starts = [p["index"] for name, p in writer.events if name == "content_block_start"]
    assert starts == [0, 1] and adapter.thinking == ""
    assert final["content"] == [{"type": "thinking", "thinking": "", "signature": "opaque"}, {"type": "text", "text": "42"}]


@pytest.mark.parametrize("cls", [adapters.ChatAdapter, adapters.AnthropicAdapter, adapters.ResponsesAdapter])
def test_a_status_shows_nothing_when_the_client_hides_thinking(cls):
    writer = Writer() if cls is adapters.ChatAdapter else EventWriter()
    adapter = cls(parsed(show=False), writer)
    for event in SILENT + [{"type": "thinking_start"}]:
        adapter.feed(event)
    assert writer.events == []


@pytest.mark.parametrize("cls", [adapters.AnthropicAdapter, adapters.ResponsesAdapter])
def test_status_does_not_create_native_thinking_blocks(cls):
    adapter = cls(parsed(), None)
    adapter.feed({"type": "thinking_status", "source": "upstream"})
    assert adapter.thinking == ""
    payload = adapter.payload("stop")
    blocks = payload.get("content", payload.get("output"))
    assert not any(b["type"] in ("thinking", "reasoning") for b in blocks)


def test_runner_measures_from_live_status_instead_of_buffer_flush(tmp_path, monkeypatch):
    backend, pool, runner = setup(tmp_path, transport="direct", cache_keepwarm_seconds=0)
    clock = [0.0]
    logs = []
    monkeypatch.setattr(orchestrator, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    monkeypatch.setattr(journal, "emit", lambda kind, message, level="info", **fields: logs.append((kind, fields)))

    async def script(*args, **kwargs):
        clock[0] = 6.0
        yield {"type": "thinking_status", "source": "upstream"}
        clock[0] = 286.0
        yield {"type": "thinking_start"}
        yield {"type": "thinking_signature", "signature": "opaque"}
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(direct, "iter_events", script)

    async def collect():
        try:
            return await run(runner, request(FIRST))
        finally:
            for session in pool._conversations.values():
                await session.aclose()

    events = asyncio.run(collect())
    assert any(e["type"] == "thinking_status" for e in events)
    assert [f["ms"] for k, f in logs if k == "upstream.thinking_start"] == [6000]
    assert [f["thinking_ms"] for k, f in logs if k == "upstream.thinking_end"] == [280000]


def test_a_status_of_the_relays_own_is_not_journaled_as_an_upstream_thinking_start(tmp_path, monkeypatch):
    # "Upstream сообщил начало thinking" was written for the Minara timer's status too (59 of 63 such events in the
    # journal came 1450-1600 ms after the request): the journal said the upstream spoke when it had not.
    backend, pool, runner = setup(tmp_path, transport="direct", cache_keepwarm_seconds=0)
    logs = []
    monkeypatch.setattr(journal, "emit", lambda kind, message, level="info", **fields: logs.append((kind, fields)))

    async def script(*args, **kwargs):
        yield {"type": "thinking_status", "source": "relay"}
        yield {"type": "thinking_heartbeat", "elapsed": 6.5}
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(direct, "iter_events", script)

    async def collect():
        try:
            return await run(runner, request(FIRST))
        finally:
            for session in pool._conversations.values():
                await session.aclose()

    asyncio.run(collect())
    assert not [kind for kind, _ in logs if kind in ("upstream.thinking_start", "upstream.thinking_end")]


def names(writer):
    return [name for name, _ in writer.events if name != "done"]


def test_responses_opens_an_empty_reasoning_item_on_the_upstreams_status_and_writes_no_text():
    # Codex Desktop shows "Thinking" for a reasoning item that has begun and "Thought for N s" once it is done, and the
    # TUI keeps its own "Working (Ns)": an item without a summary is the whole of the honest progress. 54b1023 wrote
    # "Модель думает…" and "⏳ N с…" into the summary instead (the TUI made a status header of them, Desktop a summary).
    writer = EventWriter()
    adapter = adapters.ResponsesAdapter(parsed(), writer)
    adapter.begin()
    for event in SILENT:
        adapter.feed(event)
    adapter.feed({"type": "thinking_start"})  # the same reasoning, no second item
    adapter.feed({"type": "text", "delta": "done"})
    final = adapter.end("stop")
    assert names(writer) == [
        "response.created", "response.output_item.added",
        # the reasoning item is done before the message begins, as OpenAI streams it: "Thought for N s" ends here
        "response.output_item.done",
        "response.output_item.added", "response.content_part.added", "response.output_text.delta",
        "response.output_text.done", "response.content_part.done", "response.output_item.done", "response.completed"]
    added = writer.events[1][1]["item"]
    assert added["type"] == "reasoning" and added["summary"] == [] and added["status"] == "in_progress"
    assert writer.events[2][1]["item"] == {**added, "status": "completed"}
    assert [item["type"] for item in final["output"]] == ["reasoning", "message"]
    assert final["output"][0]["summary"] == [] and adapter.thinking == ""
    assert final["output_text"] == "done"


def test_responses_shows_nothing_for_a_status_of_the_relays_own():
    writer = EventWriter()
    adapter = adapters.ResponsesAdapter(parsed(), writer)
    adapter.begin()
    adapter.feed({"type": "thinking_status", "source": "relay"})
    adapter.feed({"type": "thinking_heartbeat", "elapsed": 6.5})
    adapter.feed({"type": "text", "delta": "done"})
    final = adapter.end("stop")
    assert [item["type"] for item in final["output"]] == ["message"]
    assert "response.reasoning_summary_text.delta" not in names(writer)


def test_responses_real_reasoning_goes_into_the_item_the_status_opened():
    writer = EventWriter()
    adapter = adapters.ResponsesAdapter(parsed(), writer)
    adapter.begin()
    adapter.feed({"type": "thinking_status", "source": "upstream"})
    adapter.feed({"type": "thinking_start"})
    adapter.feed({"type": "thinking", "delta": "**Plan**"})
    adapter.feed({"type": "thinking", "delta": " count"})
    adapter.feed({"type": "text", "delta": "4050"})
    final = adapter.end("stop")
    reasoning_events = [(name, payload.get("output_index")) for name, payload in writer.events
                        if name.startswith("response.reasoning") or (name.startswith("response.output_item")
                                                                      and payload["item"]["type"] == "reasoning")]
    assert reasoning_events == [
        ("response.output_item.added", 0), ("response.reasoning_summary_part.added", 0),
        ("response.reasoning_summary_text.delta", 0), ("response.reasoning_summary_text.delta", 0),
        ("response.reasoning_summary_text.done", 0), ("response.reasoning_summary_part.done", 0),
        ("response.output_item.done", 0)]
    assert final["output"][0]["summary"] == [{"type": "summary_text", "text": "**Plan** count"}]
    assert [item["type"] for item in final["output"]] == ["reasoning", "message"]


def test_responses_reasoning_after_the_answer_began_is_an_item_of_its_own():
    # Codex files every delta into the item added last: reasoning sent into an item before the message went into the
    # message, and the TUI dropped it (research S4 §2.4).
    writer = EventWriter()
    adapter = adapters.ResponsesAdapter(parsed(), writer)
    adapter.begin()
    adapter.feed({"type": "thinking", "delta": "first"})
    adapter.feed({"type": "text", "delta": "Looking."})
    adapter.feed({"type": "thinking", "delta": "second"})
    final = adapter.end("stop")
    deltas = [(name, payload["output_index"]) for name, payload in writer.events if name.endswith(".delta")]
    assert deltas == [("response.reasoning_summary_text.delta", 0), ("response.output_text.delta", 1),
                      ("response.reasoning_summary_text.delta", 2)]
    assert [(item["type"], (item.get("summary") or item.get("content"))[0]["text"]) for item in final["output"]] == [
        ("reasoning", "first"), ("message", "Looking."), ("reasoning", "second")]
