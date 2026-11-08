"""During-turn keepwarm: anchored ping refreshes the 5-minute KV TTL inside
long (310s+) opus MAX turns, which otherwise expired mid-generation and forced
a full 190k-token rewrite on the next turn (30+ credits per turn)."""
import asyncio
import time

import pytest

import direct
import journal
import client_context
import apierr


class _Session:
    def __init__(self):
        self._cancelled = False
        self.thread_id = "t"
        self._busy = False
        self._turn_started_at = 0
        self._turn_pinged = False
        self._warm_body = None
        self._warm_at = 0
        self._last_request_at = time.time()
        self._warm_failures = 0
        self._keepwarm_cost = 0.0
        self._last_prefix_tokens = 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("busy", [False, True])
async def test_reused_keepwarm_labels_the_body_owner_not_the_first_request(monkeypatch, failure, busy):
    session = _Session()
    # The cache was last touched a little over the ping interval ago: a ping is due and the entry is still alive.
    session._warm_at = time.time() - 250
    cfg = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800}
    request_a = {"client": "opencode", "request_id": "req-A", "chat_title": "Первый чат"}
    request_b = {"client": "opencode", "request_id": "req-B", "chat_title": "Второй чат"}
    captured = []
    finished = asyncio.Event()
    real_emit = journal.emit

    def observe(kind, message="", level="info", **fields):
        event = real_emit(kind, message, level, **fields)
        if kind == "cache.keepwarm":
            captured.append(event)
            finished.set()
        return event

    observed = []

    def ping(_session, body):
        # Runs in to_thread and _keepwarm swallows exceptions: asserts here would
        # never fail the test, so only record and check after the run.
        observed.append((body["model"], client_context.current()))
        # A request that starts while this blocking ping runs must not change
        # the identity of the result/error already being produced.
        session._warm_identity = {"client": "opencode", "request_id": "req-C", "chat_title": "Третий чат"}
        if failure:
            raise RuntimeError("synthetic ping failure")
        return 0.01

    monkeypatch.setattr(journal, "emit", observe)
    monkeypatch.setattr(direct, "_ping", ping)
    token = client_context.bind(request_a)
    try:
        direct._ensure_keepwarm(session, cfg)
    finally:
        client_context.reset(token)
    first_task = session._warm_task
    # A later turn updates the cached body; the long-lived scheduler is reused.
    session._warm_body = {"model": "model-B"}
    session._warm_identity = dict(request_b)
    if busy:
        session._busy = True
        session._turn_started_at = time.time() - 201
        session._turn_body = {"model": "model-B"}
        session._turn_identity = dict(request_b)
        session._warm_identity = dict(request_a)
    token = client_context.bind(request_b)
    try:
        direct._ensure_keepwarm(session, cfg)
    finally:
        client_context.reset(token)
    assert session._warm_task is first_task
    try:
        await asyncio.wait_for(finished.wait(), timeout=2)
        assert observed and all(item == ("model-B", request_b) for item in observed)
        assert captured[0]["request_id"] == "req-B"
        assert captured[0]["data"]["chat_title"] == "Второй чат"
        assert "Первый чат" not in captured[0]["message"]
    finally:
        first_task.cancel()
        await first_task


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_cache_body_keeps_its_request_identity_when_a_later_turn_is_rejected(monkeypatch, rejected):
    session = _Session()
    body_a = {"model": "model-A", "openAICacheKey": "key-A"}
    body_b = {"model": "model-B", "openAICacheKey": "key-B"}
    request_a = {"client": "opencode", "request_id": "req-A", "chat_title": "Первый чат"}
    request_b = {"client": "opencode", "request_id": "req-B", "chat_title": "Второй чат"}
    session._warm_body, session._warm_identity = body_a, request_a
    monkeypatch.setattr(direct, "build_body", lambda *_args, **_kw: body_b)
    monkeypatch.setattr(direct, "_ensure_keepwarm", lambda *_args: None)

    async def stream(*_args):
        assert session._turn_identity == request_b
        if rejected:
            raise apierr.bad_request("rejected body")
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(direct, "_stream_body", stream)
    token = client_context.bind(request_b)
    try:
        async def consume():
            return [event async for event in direct.iter_events({}, session, {"model": "model-B"}, "", "", None, "auto", 32)]
        if rejected:
            with pytest.raises(apierr.ApiError):
                await consume()
        else:
            await consume()
    finally:
        client_context.reset(token)
    assert session._warm_body == (body_a if rejected else body_b)
    assert session._warm_identity == (request_a if rejected else request_b)
    assert session._turn_identity is None


@pytest.mark.asyncio
async def test_during_turn_ping_fires_once_at_offset(monkeypatch):
    session = _Session()
    session._busy = True
    session._turn_started_at = time.time() - 201  # offset 200 already reached
    session._turn_body = {"model": "claude-opus-5-5"}
    session._warm_body = {"model": "claude-opus-5-5"}
    session._last_prefix_tokens = 190000

    calls = []

    def fake_ping(_session, _body):
        calls.append(time.time())
        return 0.85

    monkeypatch.setattr(direct, "_ping", fake_ping)
    cfg = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800,
           "cache_keepwarm_during_turns": True, "cache_keepwarm_turn_offset": 200,
           "direct_debug": False}

    task = asyncio.create_task(direct._keepwarm(session, cfg))
    await asyncio.sleep(1.5)  # anchored ping fires within ~1s
    assert len(calls) == 1, "exactly one anchored ping during the turn"
    assert session._turn_pinged is True
    await asyncio.sleep(1.5)  # no second ping inside the same turn
    assert len(calls) == 1
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_during_turn_ping_skipped_when_disabled(monkeypatch):
    session = _Session()
    session._busy = True
    session._turn_started_at = time.time() - 201
    session._warm_body = {"model": "claude-opus-5-5"}

    calls = []
    monkeypatch.setattr(direct, "_ping", lambda *_a: calls.append(1))
    cfg = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800,
           "cache_keepwarm_during_turns": False, "cache_keepwarm_turn_offset": 200}

    task = asyncio.create_task(direct._keepwarm(session, cfg))
    await asyncio.sleep(1.2)
    assert not calls, "legacy behaviour: no ping while the turn is busy"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_during_turn_ping_failure_has_no_immediate_retry(monkeypatch):
    session = _Session()
    session._busy = True
    session._turn_started_at = time.time() - 201
    session._turn_body = {"model": "claude-opus-5-5"}
    session._warm_body = {"model": "claude-opus-5-5"}

    calls = []

    def failing_ping(*_a):
        calls.append(1)
        raise RuntimeError("upstream busy")

    monkeypatch.setattr(direct, "_ping", failing_ping)
    cfg = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800,
           "cache_keepwarm_during_turns": True, "cache_keepwarm_turn_offset": 200}

    task = asyncio.create_task(direct._keepwarm(session, cfg))
    await asyncio.sleep(1.5)
    assert len(calls) == 1, "a failed during-turn ping must back off before retrying"
    assert session._turn_pinged is True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_idle_keepalive_still_pings_between_turns(monkeypatch):
    session = _Session()
    session._warm_body = {"model": "claude-opus-5-5"}
    session._warm_at = time.time() - 250  # the interval has passed, the 5-minute entry is still alive
    session._last_request_at = time.time()

    calls = []
    monkeypatch.setattr(direct, "_ping", lambda *_a: calls.append(time.time()) or 0.1)
    cfg = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800,
           "cache_keepwarm_during_turns": True, "cache_keepwarm_turn_offset": 200}

    task = asyncio.create_task(direct._keepwarm(session, cfg))
    await asyncio.sleep(1.2)
    assert len(calls) == 1, "between-turns keepalive must keep working"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
