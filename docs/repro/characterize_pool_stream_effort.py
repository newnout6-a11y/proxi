"""Focused characterization checks for proxi 188fd76.

These checks record current behavior, including limitations. They are not fixes.
Only synthetic credentials and localhost HTTP are used; no paid model calls.
"""
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
    ("claude-haiku-4-5-20251001", "xhigh", "high"),
    ("gpt-6-sol", "xhigh", "xhigh"),
    ("gpt-6.1-sol", "max", "max"),
    ("gpt-5.5", "xhigh", "high"),
])
def test_observed_effective_effort(model, requested, effective):
    entry, got = models.resolve_tolerant(model, requested)
    assert got == effective
    print("EVIDENCE effort", model, requested, "->", got)


@pytest.mark.parametrize("requested", ["extra", "xhight", "ultra"])
def test_unknown_effort_is_rejected(requested):
    with pytest.raises(ApiError) as caught:
        models.resolve_tolerant("claude-opus-5-5", requested)
    assert caught.value.status == 400


def test_pool_can_select_account_without_required_provider_resource():
    pool = SessionPool(Backend(pool_config()))
    chosen = pool._next_backend()
    assert chosen is pool.backends[0]
    entry, _ = models.resolve("claude-opus-5-5")
    with pytest.raises(ApiError) as caught:
        models.provider_resource(chosen.cfg, entry)
    assert caught.value.status == 503
    assert models.provider_resource(pool.backends[1].cfg, entry) is None
    print("EVIDENCE pool chose unroutable account while another is routable")


def test_account_inherits_global_resource_even_for_different_tenant():
    cfg = pool_config(provider_resource_name="synthetic-resource-from-tenant-a",
                      accounts=[{"page_uuid": "page-a"},
                                {"page_uuid": "page-b", "base": "https://tenant-b.invalid"}])
    pool = SessionPool(Backend(cfg))
    entry, _ = models.resolve("claude-opus-5-5")
    assert models.provider_resource(pool.backends[1].cfg, entry) == "synthetic-resource-from-tenant-a"
    print("EVIDENCE tenant B inherits tenant A's resource config without validation")


@pytest.mark.asyncio
async def test_parallel_same_conversation_moves_child_to_other_account():
    pool = SessionPool(Backend(pool_config(provider_resource_name="__managed__")))
    parent = await pool.acquire("conversation")
    parent.session._cached_head = "synthetic canonical prefix"
    child = await pool.acquire("conversation")
    try:
        assert parent.backend is pool.backends[0]
        assert child.backend is pool.backends[1]
        assert child.session._cached_head == parent.session._cached_head
        print("EVIDENCE same conversation parent/account A, parallel child/account B")
    finally:
        await pool.release(child)
        await pool.release(parent)
        for session in list(pool._conversations.values()):
            await session.aclose()


@pytest.mark.asyncio
async def test_virtual_900_second_turn_only_pings_once(monkeypatch):
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
        if clock.now - start >= 901:
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
    assert calls == [200.0]
    assert clock.now - start >= 900
    print("EVIDENCE virtual turn >=900s, ping ages:", calls)


def test_ping_replaces_effort_with_low(monkeypatch):
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
    assert captured[0]["reasoningEffort"] == "low"
    print("EVIDENCE warm ping uses low while main request uses max")


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
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
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

    httpd = ThreadingHTTPServer(("127.0.0.1",0), Handler)
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
            with pytest.raises(TimeoutError):
                asyncio.run(run())
        print("EVIDENCE local HTTP thinking gap=0.5s, socket timeout=0.15s, upstream bytes=",
              upstream_blank_heartbeats, "result=", "stop" if upstream_blank_heartbeats else "TimeoutError")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)
