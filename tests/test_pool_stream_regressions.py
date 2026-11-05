"""Regression checks for account routing, repeated keepwarm and stream timeouts."""
import asyncio
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace

import pytest

from apierr import ApiError
import config
import direct
import models
from session import Backend, SessionPool


def pool_config(**overrides):
    cfg = dict(config.DEFAULTS, base="https://tenant-a.invalid", ua="focused-audit",
               cookie="synthetic-only", xsrf="synthetic-only", cookie_values=[],
               page_uuid="page-a", session_refresh_interval=0,
               cache_keepwarm_seconds=0, max_workers=8,
               provider_resource_name=None,
               accounts=[{"page_uuid": "page-a", "base": "https://tenant-a.invalid"},
                         {"page_uuid": "page-b", "base": "https://tenant-b.invalid",
                          "provider_resource_name": "__managed__"}])
    cfg.update(overrides)
    return cfg


@pytest.mark.parametrize("model,requested,effective", [
    ("claude-opus-5-5", "xhigh", "high"),
    ("claude-opus-5-5", "max", "max"),
    ("gpt-6-sol", "xhigh", "high"),
    ("gpt-6.1-sol", "max", "max"),
    ("gpt-5.5", "xhigh", "high"),
])
def test_explicit_effort_compatibility_mapping(model, requested, effective):
    entry, got = models.resolve_tolerant(model, requested, allow_downgrade=True)
    assert got == effective
    print("EVIDENCE effort", model, requested, "->", got)


@pytest.mark.parametrize("requested", ["extra", "xhight", "ultra"])
def test_unknown_effort_is_rejected(requested):
    with pytest.raises(ApiError) as caught:
        models.resolve_tolerant("claude-opus-5-5", requested)
    assert caught.value.status == 400


def test_pool_selects_only_accounts_with_required_provider_resource():
    pool = SessionPool(Backend(pool_config()))
    entry, _ = models.resolve("claude-opus-5-5")
    chosen = pool._next_backend(entry)
    assert chosen is pool.backends[1]
    assert models.provider_resource(chosen.cfg, entry) is None


def test_account_cannot_inherit_a_resource_from_a_different_tenant():
    cfg = pool_config(provider_resource_name="synthetic-resource-from-tenant-a",
                      accounts=[{"page_uuid": "page-a"},
                                {"page_uuid": "page-b", "base": "https://tenant-b.invalid"}])
    pool = SessionPool(Backend(cfg))
    entry, _ = models.resolve("claude-opus-5-5")
    with pytest.raises(ApiError) as caught:
        models.provider_resource(pool.backends[1].cfg, entry)
    assert caught.value.status == 503


@pytest.mark.asyncio
async def test_parallel_same_conversation_keeps_the_same_account():
    pool = SessionPool(Backend(pool_config(provider_resource_name="__managed__")))
    entry, _ = models.resolve("claude-opus-5-5")
    parent = await pool.acquire("conversation", model_entry=entry)
    parent.session._cached_head = "synthetic canonical prefix"
    child = await pool.acquire("conversation", model_entry=entry)
    try:
        assert parent.backend is pool.backends[0]
        assert child.backend is pool.backends[0]
        assert child.session._cached_head == parent.session._cached_head
    finally:
        await pool.release(child)
        await pool.release(parent)
        for session in list(pool._conversations.values()):
            await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [900, 1800])
async def test_long_turn_repeatedly_refreshes_inside_the_ttl(monkeypatch, duration):
    clock = SimpleNamespace(now=1000.0)
    start = clock.now
    original_sleep = asyncio.sleep
    calls = []
    session = SimpleNamespace(_busy=True, _turn_started_at=start,
        _turn_pinged=False, _last_request_at=start, _warm_body=None,
        _turn_body={"model": "claude-opus-5-5"}, _turn_seq=1,
        _warm_at=0, _warm_failures=0, _last_prefix_tokens=0,
        thread_id="fixture", _keepwarm_cost=0)

    async def sleep(delay):
        clock.now += delay
        if clock.now - start >= duration + 1:
            raise asyncio.CancelledError()
        await original_sleep(0)

    async def to_thread(fn, *args):
        return fn(*args)

    def ping(*_):
        calls.append(clock.now - start)
        return 0.01

    monkeypatch.setattr(direct.time, "time", lambda: clock.now)
    monkeypatch.setattr(direct.asyncio, "sleep", sleep)
    monkeypatch.setattr(direct.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(direct, "_ping", ping)
    await direct._keepwarm(session, {"cache_keepwarm_seconds":240,
        "cache_keepwarm_idle":1800, "cache_keepwarm_during_turns":True,
        "cache_keepwarm_turn_offset":200})
    assert calls[0] == pytest.approx(200, abs=0.1)
    assert len(calls) == (3 if duration == 900 else 7)
    assert all(right - left < 241 for left, right in zip(calls, calls[1:]))
    assert duration - calls[-1] < 300
    assert clock.now - start >= duration


def test_ping_preserves_effort(monkeypatch):
    captured = []

    class Response:
        def __iter__(self):
            return iter([b'{"event":"end_of_stream"}\n'])
        def close(self):
            pass

    def open_(request, **_):
        captured.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(direct.urllib.request, "urlopen", open_)
    session = SimpleNamespace(backend=SimpleNamespace(
        cfg={"base":"https://tenant-a.invalid"}, headers=lambda:{}))
    body = {"model":"claude-opus-5-5", "reasoningEffort":"max", "cache":True}
    direct._ping(session, body)
    assert body["reasoningEffort"] == "max"
    assert captured[0]["reasoningEffort"] == "max"


@pytest.mark.asyncio
async def test_model_routing_is_applied_before_creating_a_session():
    pool = SessionPool(Backend(pool_config()))
    entry, _ = models.resolve("claude-opus-5-5")
    lease = await pool.acquire("conversation", model_entry=entry)
    try:
        assert lease.backend is pool.backends[1]
    finally:
        await pool.release(lease)
        for session in pool._conversations.values():
            await session.aclose()


@pytest.mark.asyncio
async def test_missing_route_releases_slots_and_conversation_locks():
    pool = SessionPool(Backend(pool_config(accounts=[{"page_uuid": "p", "base": "https://tenant-a.invalid"}])))
    entry, _ = models.resolve("claude-opus-5-5")
    with pytest.raises(ApiError) as caught:
        await pool.acquire("conversation", model_entry=entry)
    assert caught.value.status == 404 and "provider_resource_name" in caught.value.message
    assert pool._slots._value == 8
    assert not pool._conversation_locks
    assert not pool._conversation_refs


def test_truncated_ping_does_not_claim_a_refreshed_cache(monkeypatch):
    class Response:
        def __iter__(self):
            return iter([b'{"event":"metadata","usage":{"creditsUsed":0.1}}\n'])

        def close(self):
            pass

    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_k: Response())
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"base": "https://fixture.invalid"}, headers=lambda: {}))
    with pytest.raises(ApiError, match="end_of_stream"):
        direct._ping(session, {"model": "claude-opus-5-5", "reasoningEffort": "max"})


@pytest.mark.asyncio
async def test_failed_keepwarm_retries_with_backoff_without_claiming_cache_hits(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    original_sleep = asyncio.sleep
    calls = []
    session = SimpleNamespace(_busy=True, _turn_started_at=1000.0, _turn_seq=1,
                              _warm_body={"model": "fixture"}, _last_request_at=1000.0)

    async def sleep(delay):
        clock.now += delay
        if clock.now >= 1900:
            raise asyncio.CancelledError()
        await original_sleep(0)

    async def to_thread(fn, *args):
        return fn(*args)

    def fail(*_):
        calls.append(clock.now)
        raise OSError("synthetic failure")

    monkeypatch.setattr(direct.time, "time", lambda: clock.now)
    monkeypatch.setattr(direct.asyncio, "sleep", sleep)
    monkeypatch.setattr(direct.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(direct, "_ping", fail)
    await direct._keepwarm(session, {"cache_keepwarm_seconds": 240, "cache_keepwarm_turn_offset": 200})
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 240
    assert getattr(session, "_warm_at", 0) == 0


@pytest.mark.parametrize("upstream_blank_heartbeats", [False, True])
def test_real_local_http_thinking_gap_and_read_timeout(upstream_blank_heartbeats):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            with contextlib.suppress(ConnectionError, OSError):
                self.wfile.write(b'{"event":"content_block_start","type":"thinking"}\n')
                self.wfile.flush()
                for _ in range(10):
                    time.sleep(0.05)
                    if upstream_blank_heartbeats:
                        self.wfile.write(b'\n')
                        self.wfile.flush()
                self.wfile.write(b'{"event":"content_block_delta","type":"text","partialText":"done"}\n')
                self.wfile.write(b'{"event":"end_of_stream"}\n')
                self.wfile.flush()

    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass  # the client deliberately drops the connection; socketserver would print a traceback

    httpd = QuietServer(("127.0.0.1",0), Handler)
    thread = threading.Thread(target=httpd.serve_forever,daemon=True)
    thread.start()
    session = SimpleNamespace(_cancelled=False, thread_id="fixture",
        backend=SimpleNamespace(cfg={"base":f"http://127.0.0.1:{httpd.server_address[1]}"},
                                headers=lambda:{}))

    async def run():
        return [event async for event in direct._stream_body(
            {"turn_timeout":0.15}, session, {"model":"fixture"})]

    try:
        if upstream_blank_heartbeats:
            events = asyncio.run(run())
            assert events[-1] == {"type":"end", "reason":"stop"}
        else:
            with pytest.raises(ApiError) as caught:
                asyncio.run(run())
            assert caught.value.status == 504
            assert caught.value.code == "upstream_timeout"
            assert caught.value.diagnostic_id
        print("EVIDENCE local HTTP thinking gap=0.5s, socket timeout=0.15s, upstream bytes=",
              upstream_blank_heartbeats, "result=", "stop" if upstream_blank_heartbeats else "504 upstream_timeout")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)
