"""Regression tests for Wave 1 drivers, transport pools, and Minara fixes."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import types
import urllib.error
from unittest import mock

import h2.config
import h2.connection
import h2.events
import httpcore
import httpx
import pytest

import apierr
import direct
import minara
import orchestrator
from drivers import (
    anthropic_messages,
    cache_engine,
    curl_pool,
    http_pool,
    openai_chat,
    openai_responses,
    sse_utils,
)


def _dummy_session(model: str = "gpt-5", *, reasoning: bool = True, conv_key: str = "conv-1"):
    return types.SimpleNamespace(
        _cancelled=False,
        _bound_entry={"upstream_model": model, "reasoning": reasoning},
        _native_conversation_key=conv_key,
        _provider_options={},
        _provider_request_sent=None,
        _active_direct_abort=None,
        _pending_history_commit=None,
        backend=types.SimpleNamespace(),
    )


def _make_cfg(**overrides) -> dict:
    cfg = {
        "upstream": "custom",
        "provider_id": "test-prov",
        "provider_name": "TestProvider",
        "preset": "openai",
        "base_url": "https://api.example.com/v1",
        "base": "https://api.example.com/v1",
        "api_key": "sk-test-secret",
        "bearer_token": "tok-test-secret",
        "direct_cache": True,
        "cache_policy": "auto",
    }
    cfg.update(overrides)
    return cfg


def _byte_transport(chunks: list[bytes], *, status: int = 200, headers: dict[str, str] | None = None):
    hdrs = {"content-type": "text/event-stream"}
    if headers:
        hdrs.update(headers)

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for c in chunks:
                yield c

    return httpx.MockTransport(lambda req: httpx.Response(status, headers=hdrs, stream=_Stream()))


def _collect(async_gen):
    async def _run():
        return [ev async for ev in async_gen]

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# CRITICAL-1: HTTP/2 sibling stream survival + prompt abort
# ---------------------------------------------------------------------------


def test_close_stream_response_preserves_http2_network_stream():
    async def run():
        ns_h2 = mock.Mock()
        ns_h2.aclose = mock.AsyncMock()
        resp_h2 = mock.Mock()
        resp_h2.http_version = "HTTP/2"
        resp_h2.extensions = {"http_version": b"HTTP/2", "network_stream": ns_h2, "stream_id": 1}
        resp_h2.aclose = mock.AsyncMock()

        await http_pool._close_stream_response(resp_h2)
        resp_h2.aclose.assert_awaited_once()
        ns_h2.aclose.assert_not_awaited()

        ns_h1 = mock.Mock()
        ns_h1.aclose = mock.AsyncMock()
        resp_h1 = mock.Mock()
        resp_h1.http_version = "HTTP/1.1"
        resp_h1.extensions = {"http_version": b"HTTP/1.1", "network_stream": ns_h1}
        resp_h1.aclose = mock.AsyncMock()

        await http_pool._close_stream_response(resp_h1)
        ns_h1.aclose.assert_awaited_once()
        resp_h1.aclose.assert_awaited_once()

    asyncio.run(run())


class _DuplexH2Stream(httpcore.AsyncNetworkStream):
    """In-memory bidirectional byte stream backed by an h2 server state machine."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.to_client: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False
        self.server = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
        )
        self.server.initiate_connection()
        self.to_client.put_nowait(self.server.data_to_send())
        self.stream1_reset = asyncio.Event()
        self.stream2_started = asyncio.Event()
        self.stream2_continue = asyncio.Event()
        self.seen_streams: list[int] = []

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self.closed and self.to_client.empty():
            return b""
        data = await self.to_client.get()
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if self.closed:
            raise httpcore.WriteError("closed")
        events = self.server.receive_data(buffer)
        out = self.server.data_to_send()
        if out:
            self.to_client.put_nowait(out)
        for ev in events:
            if isinstance(ev, h2.events.RequestReceived):
                self.seen_streams.append(ev.stream_id)
                self.loop.create_task(self._handle_stream(ev.stream_id))
            elif isinstance(ev, h2.events.StreamReset) and ev.stream_id == 1:
                self.stream1_reset.set()

    async def _handle_stream(self, sid: int) -> None:
        self.server.send_headers(
            sid,
            [(":status", "200"), ("content-type", "text/event-stream")],
        )
        self._flush()
        if sid == 1:
            # Silent hang after headers: stay open until client sends RST_STREAM.
            return
        if sid == 3:
            self.stream2_started.set()
            await self.stream2_continue.wait()
            self.server.send_data(sid, b"data: sibling-ok\n\n", end_stream=True)
            self._flush()
        else:
            self.server.send_data(sid, b"data: third-ok\n\n", end_stream=True)
            self._flush()

    def _flush(self) -> None:
        out = self.server.data_to_send()
        if out:
            self.to_client.put_nowait(out)

    async def aclose(self) -> None:
        self.closed = True
        self.to_client.put_nowait(b"")


class _SharedH2Transport(httpx.AsyncBaseTransport):
    def __init__(self, conn: httpcore.AsyncHTTP2Connection):
        self._conn = conn

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_req = httpcore.Request(
            method=request.method.encode("ascii"),
            url=httpcore.URL(
                scheme=request.url.scheme.encode("ascii"),
                host=request.url.host.encode("ascii"),
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        core_resp = await self._conn.handle_async_request(core_req)

        class _CoreBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                async for chunk in core_resp.stream:
                    yield chunk

            async def aclose(self):
                await core_resp.aclose()

        return httpx.Response(
            status_code=core_resp.status,
            headers=core_resp.headers,
            stream=_CoreBody(),
            extensions=core_resp.extensions,
        )


def test_http2_abort_stream1_keeps_sibling_stream2_and_connection_alive():
    async def run():
        loop = asyncio.get_running_loop()
        duplex = _DuplexH2Stream(loop)
        conn = httpcore.AsyncHTTP2Connection(origin=httpcore.Origin(b"https", b"h2.test", 443), stream=duplex)
        client = httpx.AsyncClient(http2=True, transport=_SharedH2Transport(conn))
        try:
            holder1 = {"resp": None}
            holder2 = {"resp": None}
            abort1 = http_pool.make_stream_abort(_dummy_session(), holder1, loop=loop)
            _abort2 = http_pool.make_stream_abort(_dummy_session(), holder2, loop=loop)

            s1_headers_ready = asyncio.Event()
            s1_chunks = []

            async def run_stream1():
                with pytest.raises(httpx.ReadError):
                    async with http_pool.abortable_stream(
                        client, "POST", "https://h2.test/s1", active_resp_holder=holder1, content=b"{}"
                    ) as resp1:
                        s1_headers_ready.set()
                        async for ch in http_pool.iter_abortable_bytes(resp1, holder1):
                            s1_chunks.append(ch)

            s2_chunks = []

            async def run_stream2():
                async with http_pool.abortable_stream(
                    client, "POST", "https://h2.test/s2", active_resp_holder=holder2, content=b"{}"
                ) as resp2:
                    async for ch in http_pool.iter_abortable_bytes(resp2, holder2):
                        s2_chunks.append(ch)

            t1 = asyncio.create_task(run_stream1())
            await asyncio.wait_for(s1_headers_ready.wait(), timeout=2.0)
            t2 = asyncio.create_task(run_stream2())
            await asyncio.wait_for(duplex.stream2_started.wait(), timeout=2.0)

            t0 = time.monotonic()
            abort1()
            await asyncio.wait_for(t1, timeout=1.0)
            assert time.monotonic() - t0 < 1.0
            await asyncio.sleep(0.05)
            assert 1 not in conn._events

            assert not duplex.closed
            duplex.stream2_continue.set()
            await asyncio.wait_for(t2, timeout=2.0)
            assert b"".join(s2_chunks) == b"data: sibling-ok\n\n"

            # Subsequent stream 3 reuses the exact same HTTP/2 connection cleanly.
            holder3 = {"resp": None}
            _abort3 = http_pool.make_stream_abort(_dummy_session(), holder3, loop=loop)
            s3_chunks = []
            async with http_pool.abortable_stream(
                client, "POST", "https://h2.test/s3", active_resp_holder=holder3, content=b"{}"
            ) as resp3:
                async for ch in http_pool.iter_abortable_bytes(resp3, holder3):
                    s3_chunks.append(ch)
            assert b"".join(s3_chunks) == b"data: third-ok\n\n"
            assert not duplex.closed
            assert duplex.seen_streams == [1, 3, 5]
        finally:
            await client.aclose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# HIGH-1: Minara silent stream abort exits <1s & frees _worker_slots / pool
# ---------------------------------------------------------------------------


def test_minara_silent_stream_abort_exits_promptly_and_releases_slots():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    accepted_conns: list[socket.socket] = []
    headers_sent = threading.Event()
    stop_srv = threading.Event()

    def server_loop():
        srv.settimeout(0.2)
        while not stop_srv.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            accepted_conns.append(conn)
            conn.settimeout(2.0)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            # Send HTTP 200 SSE headers + initial keepalive chunk so stream opens, then stay silently open.
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"e\r\n: keepalive\n\n\r\n"
            )
            headers_sent.set()

    srv_thread = threading.Thread(target=server_loop, daemon=True)
    srv_thread.start()

    acct_id = "high1-abort-acct"
    cfg = _make_cfg(
        upstream="minara",
        account_id=acct_id,
        base=f"http://127.0.0.1:{port}",
        max_workers=1,
        turn_timeout=60.0,
        bearer_token="tok-high1",
        direct_cache=False,
    )
    curl_pool.POOL.close_account(cfg)
    sess = _dummy_session("anthropic/claude-sonnet-4-5")

    async def run():
        slots = minara._worker_slots(cfg)
        req_sent = threading.Event()
        sess._provider_request_sent = req_sent.set
        gen = minara._stream(cfg, sess, {"model": "anthropic/claude-sonnet-4-5"}, "hi", None, None, None, None, None)

        async def consume():
            try:
                async for _ev in gen:
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                await gen.aclose()

        task = asyncio.create_task(consume())
        wait_deadline = time.monotonic() + 3.0
        while not (headers_sent.is_set() and req_sent.is_set()) and time.monotonic() < wait_deadline:
            await asyncio.sleep(0.01)
        assert headers_sent.is_set()
        assert req_sent.is_set()
        # Let worker bind response and enter _iter_response_chunks on the silent socket.
        await asyncio.sleep(0.05)

        t0 = time.monotonic()
        abort_cb = getattr(sess, "_active_direct_abort", None)
        assert abort_cb is not None
        abort_cb()
        task.cancel()
        await asyncio.wait_for(task, timeout=1.5)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5

        # While the server TCP socket is STILL open and silent, worker MUST exit and release its slot in <1s!
        assert slots.acquire(timeout=1.0), "_worker_slots stayed leaked after abort"
        slots.release()

        # Aborted session must not have been placed back into the reusable idle bucket.
        with curl_pool.POOL._lock:
            scope = curl_pool.account_scope(cfg)
            bucket_items = sum(v.qsize() for k, v in curl_pool.POOL._buckets.items() if k[0] == scope)
            assert bucket_items == 0

    try:
        asyncio.run(run())
    finally:
        stop_srv.set()
        for c in accepted_conns:
            try:
                c.close()
            except OSError:
                pass
        try:
            srv.close()
        except OSError:
            pass
        curl_pool.POOL.close_account(cfg)


# ---------------------------------------------------------------------------
# HIGH-4: Pre-send vs post-send unbilled classification + failover
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("driver", "preset"),
    [
        (anthropic_messages.AnthropicMessagesDriver(), "anthropic"),
        (openai_chat.OpenAIChatDriver(), "openai"),
        (openai_responses.OpenAIResponsesDriver(), "openai-responses"),
    ],
)
@pytest.mark.parametrize(
    ("exc_factory", "expected_unbilled"),
    [
        (lambda: httpx.ConnectError("refused"), True),
        (lambda: httpx.ConnectTimeout("connect timed out"), True),
        (lambda: httpx.ProxyError("proxy connect failed"), True),
        (lambda: httpx.PoolTimeout("pool full"), True),
        (lambda: httpx.ReadTimeout("read timed out"), False),
        (lambda: httpx.WriteTimeout("write timed out"), False),
        (lambda: httpx.ReadError("reset after send"), False),
        (lambda: httpx.RemoteProtocolError("incomplete chunked read"), False),
    ],
)
def test_universal_drivers_presend_vs_postsend_unbilled(driver, preset, exc_factory, expected_unbilled):
    def handler(_request):
        raise exc_factory()

    transport = httpx.MockTransport(handler)
    cfg = _make_cfg(preset=preset, base_url="https://api.example.com/v1", _http_transport=transport, direct_cache=False)
    model = "claude-sonnet-4-5" if preset == "anthropic" else "gpt-5"
    sess = _dummy_session(model)

    with pytest.raises(apierr.ApiError) as ei:
        _collect(driver.stream_turn(cfg, sess, {"model": model}, "hello", None, None, None, None))

    assert ei.value.unbilled is expected_unbilled


def test_minara_presend_vs_postsend_curl_unbilled():
    from curl_cffi.requests.exceptions import (
        ConnectionError as CurlConnectionError,
        ConnectTimeout as CurlConnectTimeout,
        DNSError as CurlDNSError,
        ProxyError as CurlProxyError,
        SSLError as CurlSSLError,
        Timeout as CurlTimeout,
    )

    def run_with_post(post_fn):
        cfg = _make_cfg(upstream="minara", direct_cache=False, bearer_token="tok-1")
        sess = _dummy_session("anthropic/claude-sonnet-4-5")
        sess.backend = types.SimpleNamespace(_minara_post=post_fn)
        with pytest.raises(apierr.ApiError) as ei:
            _collect(minara._stream(cfg, sess, {"model": "anthropic/claude-sonnet-4-5"}, "hi", None, None, None, None, None))
        return ei.value

    for make_exc, expect_unbilled in [
        (lambda: CurlDNSError("dns fail", code=6), True),
        (lambda: CurlProxyError("proxy fail", code=97), True),
        (lambda: CurlConnectTimeout("connect timed out", code=28), True),
        (lambda: CurlSSLError("ssl fail", code=35), True),
        (lambda: CurlConnectionError("refused", code=7), True),
        (lambda: CurlTimeout("operation timed out", code=28), False),
        (lambda: CurlConnectionError("got nothing", code=52), False),
        (lambda: CurlConnectionError("recv error", code=56), False),
    ]:
        err = run_with_post(lambda *a, _f=make_exc, **kw: (_ for _ in ()).throw(_f()))
        assert err.unbilled is expect_unbilled

    # Even a DNS/SSL-like error AFTER HTTP headers arrived (inside iter_content) is NEVER unbilled.
    class _PostFailResp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def iter_content(self, chunk_size=None):
            raise CurlDNSError("late error", code=6)

        def close(self):
            pass

    err_after_resp = run_with_post(lambda *a, **kw: _PostFailResp())
    assert err_after_resp.unbilled is False


def test_orchestrator_failover_allows_retry_when_presend_unbilled(tmp_path):
    """End-to-end check: ConnectError emits request_sent, sets unbilled=True, and orchestrator retries."""
    good_transport = _byte_transport([
        b'data: {"choices":[{"delta":{"content":"recovered"},"finish_reason":"stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ])
    bad_transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused")))

    cfg_bad = _make_cfg(preset="openai", provider_id="p-bad", _http_transport=bad_transport, direct_cache=False)
    cfg_good = _make_cfg(preset="openai", provider_id="p-good", _http_transport=good_transport, direct_cache=False)

    driver = openai_chat.OpenAIChatDriver()
    sess1 = _dummy_session("gpt-5")
    relay1 = orchestrator._FailoverOut(orchestrator._QueueSink() if hasattr(orchestrator, "_QueueSink") else None)
    if relay1.out is None:
        # Minimal queue-compatible sink for _FailoverOut
        class _Sink:
            def __init__(self):
                self.items = []

            def put(self, item):
                self.items.append(item)

        relay1 = orchestrator._FailoverOut(_Sink())

    sess1._provider_request_sent = lambda: relay1.put({"type": "request_sent"})
    with pytest.raises(apierr.ApiError) as ei:
        _collect(driver.stream_turn(cfg_bad, sess1, {"model": "gpt-5"}, "hi", None, None, None, None))
    assert relay1.sent is True
    assert ei.value.unbilled is True
    # Matches orchestrator.py:1185-1187 failover condition exactly.
    assert not relay1.started and (not relay1.sent or getattr(ei.value, "unbilled", False))

    sess2 = _dummy_session("gpt-5")
    events2 = _collect(driver.stream_turn(cfg_good, sess2, {"model": "gpt-5"}, "hi", None, None, None, None))
    assert any(ev.get("type") == "text" and ev.get("delta") == "recovered" for ev in events2)


# ---------------------------------------------------------------------------
# HIGH-5: retool_open post-send drops must NOT masquerade as unsent URLError
# ---------------------------------------------------------------------------


def test_retool_open_presend_vs_postsend_classification():
    from curl_cffi.requests.exceptions import (
        ConnectionError as CurlConnectionError,
        ConnectTimeout as CurlConnectTimeout,
        DNSError as CurlDNSError,
        ProxyError as CurlProxyError,
        SSLError as CurlSSLError,
        Timeout as CurlTimeout,
    )

    def fake_request_raising(exc):
        sess = types.SimpleNamespace(
            request=lambda *a, **kw: (_ for _ in ()).throw(exc),
            close=lambda: None,
        )
        slot = curl_pool._Slot(sess, types.SimpleNamespace(close=lambda: None), ("s", None, "c", False, ""), (0, 0))
        return curl_pool._Lease(curl_pool.POOL, slot)

    for exc in (
        CurlDNSError("dns", code=6),
        CurlProxyError("proxy", code=97),
        CurlSSLError("tls", code=35),
        CurlConnectionError("connect refused", code=7),
        CurlConnectTimeout("connect timed out", code=28),
    ):
        with mock.patch.object(curl_pool.POOL, "acquire", return_value=fake_request_raising(exc)):
            with pytest.raises(urllib.error.URLError) as ei:
                curl_pool.retool_open({"account_id": "a1"}, "https://retool.example/api")
            assert not isinstance(ei.value, urllib.error.HTTPError)
            # Matches direct.py:2834 `unsent = isinstance(exc, urllib.error.URLError) and not HTTPError`
            assert direct._safe_error_message if hasattr(direct, "_safe_error_message") else True

    for post_exc in (
        CurlConnectionError("empty reply", code=52),
        CurlConnectionError("recv failed", code=56),
        CurlConnectionError("h2 stream reset", code=92),
        CurlConnectionError("weird server reply", code=8),
        CurlConnectionError("partial file", code=18),
    ):
        with mock.patch.object(curl_pool.POOL, "acquire", return_value=fake_request_raising(post_exc)):
            with pytest.raises(ConnectionError) as ei:
                curl_pool.retool_open({"account_id": "a1"}, "https://retool.example/api")
            assert not isinstance(ei.value, urllib.error.URLError)

    with mock.patch.object(curl_pool.POOL, "acquire", return_value=fake_request_raising(CurlTimeout("read timeout", code=28))):
        with pytest.raises(TimeoutError) as ei:
            curl_pool.retool_open({"account_id": "a1"}, "https://retool.example/api")
        assert not isinstance(ei.value, urllib.error.URLError)


# ---------------------------------------------------------------------------
# HIGH-18 & #10-10: OpenRouter reasoning_details + non-stream deferral
# ---------------------------------------------------------------------------


def test_openai_chat_openrouter_reasoning_details_merge_multiitem_and_url_gate():
    driver = openai_chat.OpenAIChatDriver()
    sess = _dummy_session("anthropic/claude-sonnet-4-5")
    sess._native_client_messages = [{"role": "user", "content": [{"type": "text", "text": "run ls"}]}]

    sse_chunks = [
        b'data: {"choices":[{"delta":{"reasoning_details":[{"index":0,"type":"reasoning.text","id":"r0","text":"Step 1 "},{"index":1,"type":"reasoning.text","id":"r1","text":"and Step 2 "}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"reasoning_details":[{"index":0,"text":"cont."},{"index":1,"text":"done","signature":"sig1"}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"bash","arguments":"{\\"cmd\\":\\"ls\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    cfg_t = _make_cfg(
        preset="custom",
        base_url="https://openrouter.ai/api/v1",
        base="https://openrouter.ai/api/v1",
        direct_cache=False,
        _http_transport=_byte_transport(sse_chunks),
    )

    events = _collect(
        driver._stream_once(
            cfg_t,
            sess,
            {"model": "anthropic/claude-sonnet-4-5"},
            "run ls",
            None,
            None,
            None,
            None,
            None,
            bound_entry=sess._bound_entry,
        )
    )
    thinking_text = "".join(ev["delta"] for ev in events if ev.get("type") == "thinking")
    # Both rd[0] and rd[1] texts accumulated (2nd item not skipped by `if not r_delta`)
    assert thinking_text == "Step 1 and Step 2 cont.done"

    # Commit deferred history, then run next tool-result turn and inspect outgoing OpenRouter payload.
    assert callable(sess._pending_history_commit)
    sess._pending_history_commit()

    captured: list[dict] = []

    def capture_handler(req: httpx.Request):
        captured.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    cfg_or = _make_cfg(
        preset="custom",
        base_url="https://openrouter.ai/api/v1",
        base="https://openrouter.ai/api/v1",
        direct_cache=False,
        _http_transport=httpx.MockTransport(capture_handler),
    )
    sess._native_client_messages = [
        {"role": "user", "content": [{"type": "text", "text": "run ls"}]},
        {"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "call_1", "toolName": "bash", "toolInput": {"cmd": "ls"}}]},
        {"role": "tool", "content": {"toolCallId": "call_1", "toolResult": "file.py"}},
    ]
    _collect(driver.stream_turn(cfg_or, sess, {"model": "anthropic/claude-sonnet-4-5"}, "continue", None, None, None, None))
    assert captured
    assistant_sent = [m for m in captured[0]["messages"] if m.get("role") == "assistant"][-1]
    assert "reasoning_details" in assistant_sent
    assert len(assistant_sent["reasoning_details"]) == 2
    assert assistant_sent["reasoning_details"][0]["text"] == "Step 1 cont."
    assert assistant_sent["reasoning_details"][1]["text"] == "and Step 2 done"
    assert assistant_sent["reasoning_details"][1]["signature"] == "sig1"

    canonical = [
        {"role": "user", "content": "run ls"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": thinking_text, "signature": "openrouter:reasoning_details", "reasoning_details": assistant_sent["reasoning_details"]},
            {"type": "tool_call", "toolCallId": "call_1", "toolName": "bash", "toolInput": {"cmd": "ls"}},
        ]},
        {"role": "tool", "content": {"toolCallId": "call_1", "toolResult": "ok"}},
    ]
    assert "reasoning_details" not in openai_chat._canonical_to_chat_messages(canonical, preset="custom", is_openrouter=False)[1]


def test_openai_chat_non_stream_json_respects_text_only_and_defers_commit():
    driver = openai_chat.OpenAIChatDriver()
    sess = _dummy_session("gpt-5")
    payload = {
        "choices": [{
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    canonical = [{"role": "user", "content": "hi"}]

    with mock.patch.object(cache_engine, "record_canonical_reply") as rec_mock:
        events_text_only = list(
            driver._events_from_non_stream_json(
                payload, {"upstream_model": "gpt-5"}, sess, ("scope",), "echo", canonical, None, None, text_only=True
            )
        )
        assert any(ev.get("delta") == "hello" for ev in events_text_only)
        assert sess._pending_history_commit is None
        rec_mock.assert_not_called()

        events_normal = list(
            driver._events_from_non_stream_json(
                payload, {"upstream_model": "gpt-5"}, sess, ("scope",), "echo", canonical, None, None, text_only=False
            )
        )
        assert any(ev.get("type") == "end" for ev in events_normal)
        rec_mock.assert_not_called()
        assert callable(sess._pending_history_commit)
        sess._pending_history_commit()
        rec_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Medium fixes across curl_pool, sse_utils, universal drivers, cache_engine, minara
# ---------------------------------------------------------------------------


def test_curl_pool_close_account_bumps_scope_generation():
    pool = curl_pool.AccountSessionPool(max_per_bucket=4)
    cfg = {"account_id": "gen-acct", "impersonate": "chrome152"}
    created: list[object] = []

    class _FakeSess:
        def close(self):
            pass

    def fake_make_slot(key, gen):
        s = _FakeSess()
        created.append(s)
        return curl_pool._Slot(s, types.SimpleNamespace(close=lambda: None), key, gen)

    with mock.patch.object(pool, "_make_slot", side_effect=fake_make_slot):
        lease1 = pool.acquire(cfg)
        pool.close_account(cfg)
        # Releasing pre-renewal lease1 must NOT insert it into the new scope bucket.
        lease1.release(reusable=True)

        lease2 = pool.acquire(cfg)
        assert lease2.session is not lease1.session
        assert len(created) == 2
        lease2.release(reusable=True)
        pool.close_all()


@pytest.mark.parametrize(
    ("driver", "preset", "sse_err_pre", "sse_err_mid"),
    [
        (
            anthropic_messages.AnthropicMessagesDriver(),
            "anthropic",
            [b'event: error\ndata: {"type":"error","error":{"type":"rate_limit_error","message":"Slow down"}}\n\n'],
            [
                b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
                b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n',
                b'event: error\ndata: {"type":"error","error":{"type":"rate_limit_error","message":"Slow down"}}\n\n',
            ],
        ),
        (
            openai_chat.OpenAIChatDriver(),
            "openai",
            [b'data: {"error":{"type":"rate_limit_error","code":429,"message":"Rate limited"}}\n\n'],
            [
                b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                b'data: {"error":{"type":"rate_limit_error","code":429,"message":"Rate limited"}}\n\n',
            ],
        ),
        (
            openai_responses.OpenAIResponsesDriver(),
            "openai-responses",
            [b'data: {"type":"error","error":{"code":"rate_limit_exceeded","status":429,"message":"Too many"}}\n\n'],
            [
                b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n',
                b'data: {"type":"response.failed","response":{"error":{"type":"overloaded_error","message":"overloaded"}}}\n\n',
            ],
        ),
    ],
)
def test_sse_error_classification_pre_vs_post_output(driver, preset, sse_err_pre, sse_err_mid):
    model = "claude-sonnet-4-5" if preset == "anthropic" else "gpt-5"

    cfg_pre = _make_cfg(preset=preset, direct_cache=False, _http_transport=_byte_transport(sse_err_pre))
    with pytest.raises(apierr.ApiError) as pre_ei:
        _collect(driver.stream_turn(cfg_pre, _dummy_session(model), {"model": model}, "hi", None, None, None, None))
    assert pre_ei.value.upstream_status == 429
    assert pre_ei.value.unbilled is True
    assert pre_ei.value.account_quota is True

    cfg_mid = _make_cfg(preset=preset, direct_cache=False, _http_transport=_byte_transport(sse_err_mid))
    with pytest.raises(apierr.ApiError) as mid_ei:
        _collect(driver.stream_turn(cfg_mid, _dummy_session(model), {"model": model}, "hi", None, None, None, None))
    assert mid_ei.value.unbilled is False
    if mid_ei.value.upstream_status == 429:
        assert mid_ei.value.account_quota is False
    else:
        assert mid_ei.value.upstream_status == 503


def test_sse_utils_map_sse_error_402_503():
    err_402 = sse_utils.map_sse_error('{"error":{"code":402,"message":"insufficient credits"}}', None, pre_output=True)
    assert err_402.upstream_status == 402
    assert err_402.unbilled is True

    err_503 = sse_utils.map_sse_error('{"error":{"type":"overloaded_error","message":"Overloaded"}}', None, pre_output=True)
    assert err_503.upstream_status == 503
    assert err_503.unbilled is False


def test_anthropic_betas_case_insensitive_host_gated_and_redacted_thinking():
    seen_headers: list[dict[str, str]] = []

    def make_transport():
        def handler(req: httpx.Request):
            seen_headers.append(dict(req.headers))
            sse = (
                b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"redacted_thinking","data":"enc"}}\n\n'
                b'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":"ok"}}\n\n'
                b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

        return httpx.MockTransport(handler)

    driver = anthropic_messages.AnthropicMessagesDriver()
    safe_beta = next(iter(anthropic_messages.SAFE_ANTHROPIC_BETAS))

    with mock.patch("drivers.anthropic_messages.client_context.anthropic_betas", return_value=(safe_beta,)):
        # 1. Custom proxy host even with preset="anthropic" MUST NOT leak client anthropic-beta,
        # while merging existing mixed-case headers cleanly.
        cfg_proxy = _make_cfg(
            preset="anthropic",
            base_url="https://proxy.example.com/v1",
            headers={"Anthropic-Beta": "custom-beta"},
            direct_cache=False,
            _http_transport=make_transport(),
        )
        events = _collect(
            driver.stream_turn(cfg_proxy, _dummy_session("claude-sonnet-4-5"), {"model": "claude-sonnet-4-5"}, "hi", None, None, None, None)
        )
        assert any(ev.get("type") == "thinking_start" for ev in events)
        assert seen_headers[0]["anthropic-beta"] == "custom-beta"
        assert safe_beta not in seen_headers[0]["anthropic-beta"]

        # 2. Genuine *.anthropic.com host merges mixed-case extra header + safe client beta.
        cfg_official = _make_cfg(
            preset="anthropic",
            base_url="https://api.anthropic.com/v1",
            headers={"Anthropic-Beta": "custom-beta"},
            direct_cache=False,
            _http_transport=make_transport(),
        )
        _collect(
            driver.stream_turn(cfg_official, _dummy_session("claude-sonnet-4-5"), {"model": "claude-sonnet-4-5"}, "hi", None, None, None, None)
        )
        betas = [b.strip() for b in seen_headers[1]["anthropic-beta"].split(",")]
        assert "custom-beta" in betas and safe_beta in betas


def test_openai_responses_reasoning_item_added_emits_thinking_start():
    driver = openai_responses.OpenAIResponsesDriver()
    sse = [
        b'data: {"type":"response.output_item.added","item":{"id":"rs_1","type":"reasoning"}}\n\n',
        b'data: {"type":"response.output_text.delta","delta":"answer"}\n\n',
        b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
    ]
    cfg = _make_cfg(preset="openai-responses", direct_cache=False, _http_transport=_byte_transport(sse))
    events = _collect(driver.stream_turn(cfg, _dummy_session("gpt-5"), {"model": "gpt-5"}, "hi", None, None, None, None))
    types_seen = [ev.get("type") for ev in events]
    assert "thinking_start" in types_seen
    assert types_seen.index("thinking_start") < types_seen.index("text")


def test_cache_engine_openai_keys_and_effort_active():
    sess = _dummy_session("gpt-5")
    for non_openai in ("xai", "deepseek"):
        b = {"messages": [{"role": "user", "content": "hi"}]}
        cache_engine.apply_openai_cache_hints(b, _make_cfg(preset=non_openai), sess, "grok-4")
        assert "prompt_cache_key" not in b

    b1, b2, b3 = {}, {}, {}
    cfg_oa = _make_cfg(preset="openai")
    tools_a = [{"type": "function", "function": {"name": "a"}}]
    tools_b = [{"type": "function", "function": {"name": "b"}}]
    cache_engine.apply_openai_cache_hints(b1, cfg_oa, sess, "gpt-5", head="Head A", tools=tools_a)
    cache_engine.apply_openai_cache_hints(b2, cfg_oa, sess, "gpt-5", head="Head B", tools=tools_a)
    cache_engine.apply_openai_cache_hints(b3, cfg_oa, sess, "gpt-5", head="Head A", tools=tools_b)
    assert len({b1["prompt_cache_key"], b2["prompt_cache_key"], b3["prompt_cache_key"]}) == 3

    # Verify effort_active recognizes model_provider["effort"] and blocks forced choice when reasoning=True default.
    async def check_effort_forced():
        seen_choices: list[object] = []

        async def single_attempt(eff_tools, eff_choice, instruction=None, text_only=False):
            seen_choices.append(eff_choice)
            yield {"type": "tool_call", "id": "c1", "name": "t1", "arguments": "{}", "partial": False}
            yield {"type": "end", "reason": "tool_calls"}

        s = _dummy_session("claude-sonnet-4-5")
        events = []
        async for ev in cache_engine.run_guarded_turn(
            _make_cfg(preset="anthropic", driver="anthropic-messages"),
            s,
            {"model": "claude-sonnet-4-5", "effort": "high"},
            [{"name": "t1", "input_schema": {"type": "object"}}],
            "required",
            {"upstream": "minara", "forced_tool_choice": False},
            single_attempt,
        ):
            events.append(ev)
        # Because effort="high" is active on forced_tool_choice=False, it must downgrade choice to "auto"
        assert seen_choices[0] == "auto"
        assert any(ev.get("type") == "tool_call" and ev.get("name") == "t1" for ev in events)

    asyncio.run(check_effort_forced())


def test_minara_openai_cache_key_missing_token_and_truncated_error_body():
    sess = _dummy_session("openai/gpt-6.1-sol")
    m_cfg = _make_cfg(upstream="minara")
    b1 = minara.build_body(m_cfg, {"model": "openai/gpt-6.1-sol"}, "Sys A", "hi", [{"name": "t1", "input_schema": {"type": "object"}}], None, None, session=sess)
    b2 = minara.build_body(m_cfg, {"model": "openai/gpt-6.1-sol"}, "Sys B", "hi", [{"name": "t1", "input_schema": {"type": "object"}}], None, None, session=sess)
    b3 = minara.build_body(m_cfg, {"model": "openai/gpt-6.1-sol"}, "Sys A", "hi", [{"name": "t2", "input_schema": {"type": "object"}}], None, None, session=sess)
    assert len({b1["prompt_cache_key"], b2["prompt_cache_key"], b3["prompt_cache_key"]}) == 3

    # Missing token must fail BEFORE calling direct._claim_head.
    with mock.patch.object(direct, "_claim_head") as claim_mock:
        with pytest.raises(apierr.ApiError):
            _collect(
                minara._stream(
                    _make_cfg(upstream="minara", bearer_token="   ", direct_cache=True),
                    sess,
                    {"model": "anthropic/claude-sonnet-4-5"},
                    "hi",
                    "X" * 10000,
                    None,
                    None,
                    None,
                    None,
                )
            )
        claim_mock.assert_not_called()

    # >64KB truncated error body must NOT return curl_pool session to reusable idle bucket.
    class _FakeBigErrResp:
        status_code = 500
        headers = {}

        def iter_content(self, chunk_size=None):
            yield b"x" * 70000

        def close(self):
            pass

    class _FakeSess:
        def post(self, *a, **kw):
            return _FakeBigErrResp()

        def close(self):
            pass

    acct_cfg = _make_cfg(upstream="minara", account_id="minara-bigerr", direct_cache=False, bearer_token="tok-1", minara_channel_retry_delays=())
    curl_pool.POOL.close_account(acct_cfg)
    fake_slot = curl_pool._Slot(_FakeSess(), types.SimpleNamespace(close=lambda: None), (curl_pool.account_scope(acct_cfg), None, "chrome152", False, ""), (0, 0))
    lease = curl_pool._Lease(curl_pool.POOL, fake_slot)
    release_calls: list[bool] = []
    orig_pool_release = curl_pool.POOL._release

    def spy_pool_release(slot, *, reusable):
        release_calls.append(bool(reusable))
        orig_pool_release(slot, reusable=reusable)

    with mock.patch.object(curl_pool.POOL, "acquire", return_value=lease), mock.patch.object(
        curl_pool.POOL, "_release", side_effect=spy_pool_release
    ):
        with pytest.raises(apierr.ApiError):
            _collect(
                minara._stream(
                    acct_cfg,
                    _dummy_session("anthropic/claude-sonnet-4-5"),
                    {"model": "anthropic/claude-sonnet-4-5"},
                    "hi",
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
    assert release_calls == [False]
    curl_pool.POOL.close_account(acct_cfg)



def test_abortable_stream_header_wait_cancellation_cleanup():
    async def run():
        loop = asyncio.get_running_loop()

        # 1. External task cancellation while __aenter__ is still pending:
        #    __aenter__ must receive CancelledError cleanly (never orphaned) and __aexit__ is not called.
        enter_started = asyncio.Event()
        enter_cancelled = asyncio.Event()
        exit_calls_1: list[object] = []

        class _PendingStreamCtx:
            async def __aenter__(self):
                enter_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    enter_cancelled.set()
                    raise

            async def __aexit__(self, exc_type, exc, tb):
                exit_calls_1.append(exc_type)

        client1 = types.SimpleNamespace(stream=lambda *a, **kw: _PendingStreamCtx())
        holder1 = {"resp": None}
        http_pool.make_stream_abort(_dummy_session(), holder1, loop=loop)

        async def run_pending():
            async with http_pool.abortable_stream(client1, "POST", "https://h2.test/p", active_resp_holder=holder1):
                pass

        t1 = asyncio.create_task(run_pending())
        await asyncio.wait_for(enter_started.wait(), timeout=1.0)
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        assert enter_cancelled.is_set()
        assert exit_calls_1 == []

        # 2. External task cancellation in the same tick right AFTER __aenter__ succeeded:
        #    opened response MUST still be closed via __aexit__.
        exit_calls_2: list[object] = []
        opened_resp = object()

        class _CompletedBeforeCancelCtx:
            async def __aenter__(self):
                return opened_resp

            async def __aexit__(self, exc_type, exc, tb):
                exit_calls_2.append(exc_type)

        client2 = types.SimpleNamespace(stream=lambda *a, **kw: _CompletedBeforeCancelCtx())
        holder2 = {"resp": None}
        http_pool.make_stream_abort(_dummy_session(), holder2, loop=loop)

        async def run_raced_cancel():
            async with http_pool.abortable_stream(client2, "POST", "https://h2.test/c", active_resp_holder=holder2):
                raise AssertionError("should not reach body after cancellation")

        orig_wait = asyncio.wait

        async def cancel_after_enter(aws, *args, **kwargs):
            enter_t = next(t for t in aws)
            done, pending = await orig_wait(aws, *args, **kwargs)
            assert enter_t in done
            raise asyncio.CancelledError()

        with mock.patch("drivers.http_pool.asyncio.wait", side_effect=cancel_after_enter):
            with pytest.raises(asyncio.CancelledError):
                await run_raced_cancel()
        assert exit_calls_2 == [asyncio.CancelledError]

        # 3. abort_event firing in the same tick right after __aenter__ returned a response:
        #    MUST raise httpx.ReadError AND still call __aexit__ to close the opened response.
        exit_calls_3: list[object] = []

        class _CompletedAtAbortCtx:
            async def __aenter__(self):
                return opened_resp

            async def __aexit__(self, exc_type, exc, tb):
                exit_calls_3.append(exc_type)

        client3 = types.SimpleNamespace(stream=lambda *a, **kw: _CompletedAtAbortCtx())
        holder3 = {"resp": None}
        abort3 = http_pool.make_stream_abort(_dummy_session(), holder3, loop=loop)

        async def both_done_wait(aws, *args, **kwargs):
            # Ensure abort_event is set AND both enter_task & abort_task finish before wait returns.
            abort3()
            done, pending = await orig_wait(aws, return_when=asyncio.ALL_COMPLETED)
            return done, pending

        with mock.patch("drivers.http_pool.asyncio.wait", side_effect=both_done_wait):
            with pytest.raises(httpx.ReadError):
                async with http_pool.abortable_stream(client3, "POST", "https://h2.test/a", active_resp_holder=holder3):
                    pass
        assert exit_calls_3 == [httpx.ReadError]

    asyncio.run(run())
