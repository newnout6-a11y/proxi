"""Early display status must not expose thinking from a discarded attempt."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

import adapters
import direct
import journal
import orchestrator
import validate
from test_native_context_reuse import FIRST, request, run, setup
from test_review_fixes_pr19 import END, THINKING, response, signature, stream_direct, text


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
    p = validate.Parsed()
    p.model, p.show_thinking, p.thinking_placeholder = "claude-opus-5-5", show, label
    return p


def test_chat_displays_early_status_only_once_without_storing_it():
    writer = Writer()
    adapter = adapters.ChatAdapter(parsed(), writer)
    adapter.feed({"type": "usage_start"})
    adapter.feed({"type": "thinking_status", "source": "upstream"})
    assert len(writer.events) == 1
    adapter.feed({"type": "thinking_start"})
    adapter.feed({"type": "thinking_signature", "signature": "opaque"})
    assert [p["choices"][0]["delta"] for p in writer.events] == [{"reasoning_content": "Модель думает…\n"}]
    assert adapter.thinking == ""
    assert "reasoning_content" not in adapter.payload("stop")["choices"][0]["message"]


@pytest.mark.parametrize("p", [parsed(show=False), parsed(label="")])
def test_early_status_respects_display_settings(p):
    writer = Writer()
    adapter = adapters.ChatAdapter(p, writer)
    adapter.feed({"type": "thinking_status", "source": "upstream"})
    adapter.feed({"type": "thinking_start"})
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
    monkeypatch.setattr(orchestrator, "time", SimpleNamespace(time=lambda: clock[0]))
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
