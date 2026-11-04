"""Tests for stream-safety hardening: SSRF guard, ping billing, bounded queues."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

import direct
from apierr import ApiError
from validate import _assert_public_http_url, _assert_public_peer


def test_ssrf_guard_rejects_loopback_and_link_local():
    for url in ("http://127.0.0.1/x.png", "http://localhost/x.png",
                "http://169.254.169.254/latest/meta-data", "http://[::1]/x.png"):
        with pytest.raises(Exception):
            _assert_public_http_url(url)


def test_ssrf_guard_rejects_non_http_schemes():
    with pytest.raises(Exception):
        _assert_public_http_url("ftp://example.com/x.png")
    with pytest.raises(Exception):
        _assert_public_http_url("file:///etc/passwd")


def test_redirect_guard_rejects_private_target():
    handler = validate_guard()
    with pytest.raises(ApiError):
        handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/x.png")


def validate_guard():
    from validate import _GuardedRedirectHandler
    return _GuardedRedirectHandler()


def test_force_close_closes_through_owner_and_never_twice():
    """Blocked-in-readline scenario: the worker holds its own reference to the
    buffered reader, so defusing resp.fp alone cannot stop a second close. The
    close must go through the OWNING socket object, which then remembers -1 —
    an unrelated connection that later reuses the number stays alive."""
    import socket
    import threading
    import time

    s1, s2 = socket.socketpair()
    try:
        reader = s1.makefile("rb")  # what http.client wraps in resp.fp.raw._sock

        outcome = []
        def worker():
            try:
                reader.readline()
                outcome.append("eof")
            except OSError:
                outcome.append("oserror")

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.05)

        class FakeResp:
            def fileno(self):
                return s1.fileno()
            def close(self):
                pass

        resp = FakeResp()
        resp.fp = SimpleNamespace(raw=SimpleNamespace(_sock=s1))
        direct._force_close_response(resp)

        t.join(timeout=2.0)
        assert not t.is_alive(), "blocked reader must be unbroken by the force close"
        assert outcome and outcome[0] in ("eof", "oserror")
        assert s1.fileno() == -1, "owning socket must forget the descriptor"

        # The worker's cleanup path closes the socket object again after exiting:
        # that must be a harmless no-op, not a second closesocket on the number.
        s1.close()

        # A fresh connection that may have received the reused number stays alive.
        s3, s4 = socket.socketpair()
        try:
            s3.send(b"ping")
            s4.settimeout(1.0)
            assert s4.recv(4) == b"ping", "unrelated socket killed by a second close"
        finally:
            s3.close()
            s4.close()
    finally:
        for sock in (s1, s2):
            try:
                sock.close()
            except OSError:
                pass


def test_force_close_defuses_response_reader():
    """After the forced close the response must not own the fd anymore, so its
    later close() cannot hit a reused descriptor (Windows fd reuse)."""
    import socket

    s1, s2 = socket.socketpair()
    try:
        class FakeResp:
            def __init__(self):
                self.fp = SimpleNamespace(close=lambda: closed.append(1))
            def fileno(self):
                return s1.fileno()
            def close(self):
                self.fp.close() if self.fp else None

        closed = []
        resp = FakeResp()
        direct._force_close_response(resp)
        assert resp.fp is None, "reader must be defused so resp.close() is a no-op"
    finally:
        # The force close may already have closed the socketpair endpoint.
        for sock in (s1, s2):
            try:
                sock.close()
            except OSError:
                pass


class _FakePeer:
    def __init__(self, ip):
        self._ip = ip

    def getpeername(self):
        return (self._ip, 443)


def test_peer_check_rejects_rebound_connection():
    resp = SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=_FakePeer("127.0.0.1"))))
    with pytest.raises(ApiError):
        _assert_public_peer(resp)


def test_peer_check_allows_public_connection():
    resp = SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=_FakePeer("93.184.216.34"))))
    assert _assert_public_peer(resp) is None


def test_bounded_put_never_duplicates():
    # The queue lives on exactly ONE loop: the producer's put is scheduled onto it
    # (as _bounded_put does in the relay) and the consumer runs on the same loop.
    loop = asyncio.new_event_loop()
    queue = asyncio.Queue(maxsize=1)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        async def fill():
            await queue.put("resident")

        asyncio.run_coroutine_threadsafe(fill(), loop).result(5)

        async def drain():
            # The queue stays FULL during the put: the worker must block on it.
            await asyncio.sleep(0.4)
            first = await queue.get()
            second = await queue.get()
            return first, second

        consumer = asyncio.run_coroutine_threadsafe(drain(), loop)
        time.sleep(0.15)  # drain() is now sleeping with a full queue
        direct._bounded_put(loop, queue, "payload", lambda: False)
        first, second = consumer.result(10)
        assert (first, second) == ("resident", "payload")
        assert queue.qsize() == 0, "bounded put enqueued the item more than once"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_bounded_put_gives_up_when_cancelled():
    loop = asyncio.new_event_loop()
    queue = asyncio.Queue(maxsize=1)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        async def fill():
            await queue.put("resident")

        asyncio.run_coroutine_threadsafe(fill(), loop).result(5)

        started = time.time()
        # The put future targets the loop; is_cancelled fires after the 5s timeout.
        direct._bounded_put(loop, queue, "payload", lambda: True)
        assert time.time() - started < 8, "cancelled put must not block forever"
        assert queue.qsize() == 1, "cancelled put must not enqueue"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def _stream_events(lines, monkeypatch):
    monkeypatch.setattr(direct.urllib.request, "urlopen",
                        lambda request, timeout: _FakeResponse([line.encode() + b"\n" for line in lines]))

    async def collect():
        session = SimpleNamespace(_cancelled=False, thread_id="t",
                                  backend=SimpleNamespace(cfg={"base": "http://upstream"}, headers=lambda: {}))
        out = []
        async for event in direct._stream_body({"direct_debug": False}, session, {"model": "unknown-model"}):
            out.append(event)
        return out

    return asyncio.run(collect())


def test_truncated_stream_ends_in_error_not_clean_stop(monkeypatch):
    lines = [json.dumps({"event": "content_block_start", "type": "text"}),
             json.dumps({"event": "content_block_delta", "type": "text", "partialText": "half"}),
             ]  # no end_of_stream: the connection just dies
    events = _stream_events(lines, monkeypatch)
    assert any(e.get("type") == "text" for e in events), "partial text must still stream"
    assert events[-1]["type"] == "error", "truncated stream must not end as clean stop"


def test_clean_stream_still_ends_in_stop(monkeypatch):
    lines = [json.dumps({"event": "content_block_start", "type": "text"}),
             json.dumps({"event": "content_block_delta", "type": "text", "partialText": "ok"}),
             json.dumps({"event": "end_of_stream"})]
    events = _stream_events(lines, monkeypatch)
    assert events[-1] == {"type": "end", "reason": "stop"}


class _FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


def test_ping_extracts_actual_credits(monkeypatch):
    events = [
        json.dumps({"event": "content_block_start", "type": "text"}),
        json.dumps({"event": "metadata", "usage": {"creditsUsed": 0.0123,
                                                   "promptTokensReadFromCache": 5256}}),
        json.dumps({"event": "end_of_stream"}),
    ]
    monkeypatch.setattr(direct.urllib.request, "urlopen",
                        lambda request, timeout: _FakeResponse([line.encode() + b"\n" for line in events]))
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"base": "http://upstream"}, headers=lambda: {}))
    assert direct._ping(session, {"model": "unknown-model"}) == 0.0123


def test_ping_returns_none_without_usage_event(monkeypatch):
    events = [json.dumps({"event": "content_block_start", "type": "text"}),
              json.dumps({"event": "end_of_stream"})]
    monkeypatch.setattr(direct.urllib.request, "urlopen",
                        lambda request, timeout: _FakeResponse([line.encode() + b"\n" for line in events]))
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"base": "http://upstream"}, headers=lambda: {}))
    assert direct._ping(session, {"model": "unknown-model"}) is None


def test_rejected_body_dump_lands_files_and_never_raises(monkeypatch, tmp_path):
    """The upstream 400 capture must write the exact payload plus the error
    detail, and a failing dump must not break the turn."""
    monkeypatch.chdir(tmp_path)
    direct._dump_rejected_body(b'{"messages": []}', '{"statusCode":400,"message":"Invalid request format"}')
    files = sorted(p.name for p in (tmp_path / "logs" / "upstream-400").iterdir())
    assert len(files) == 3  # exact body, upstream detail, correlated client metadata
    body_file = tmp_path / "logs" / "upstream-400" / files[0]
    assert body_file.read_bytes() == b'{"messages": []}'

    # A hostile/unwritable target must be swallowed: diagnostics never break a turn.
    import shutil
    shutil.rmtree(tmp_path / "logs" / "upstream-400")
    (tmp_path / "logs" / "upstream-400").write_text("not a dir", encoding="utf-8")
    direct._dump_rejected_body(b"x", "y")  # must not raise
