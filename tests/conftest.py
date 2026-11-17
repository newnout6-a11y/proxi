"""Offline harness: a real HTTP server with a scripted upstream (no network)."""
import collections
import http.client
import json
import os
import re
import socket
import socketserver
import struct
import sys
import threading
import urllib.error
import urllib.request

import pytest

import apierr
import proxy
import config as config_module

KEY = "test-key-123"

# shutdown() returns when serve_forever's loop looks at the stop request, which it does only when its select() returns: after
# poll_interval (0.5 s) of silence, and a test that stops its server has just sent its last request. That wait was two thirds
# of the suite (about 1,400 teardowns of 0.5 s in 983 s). Every server of the tests, the relay's own and the stdlib fakes,
# polls at this rate instead; nothing a test checks depends on it.
socketserver.BaseServer.serve_forever.__defaults__ = (0.01,)

# Every request a test sends opens a connection of its own, and the end that closes first is held by Windows for 120 s
# (TIME_WAIT). A full run made about 5,300 of them, and the dynamic range has 16,384 ports: three runs within two minutes (the
# suites of several agents on one machine, or a parallel one beside another) met WinError 10048. A client of the tests closes
# with a reset instead (linger 0), which leaves nothing to hold. Only a connection opened by a test file is changed: the
# relay's own clients, which the tests run, are not.
_TESTS = os.path.dirname(os.path.abspath(__file__))
_STDLIB = os.path.dirname(os.path.dirname(os.path.abspath(http.client.__file__)))
_http_connect = http.client.HTTPConnection.connect


def _connect_that_resets_on_close(self):
    _http_connect(self)
    frame = sys._getframe(1)
    while frame is not None and os.path.abspath(frame.f_code.co_filename).startswith(_STDLIB):
        frame = frame.f_back
    if frame is not None and os.path.abspath(frame.f_code.co_filename).startswith(_TESTS):
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))


http.client.HTTPConnection.connect = _connect_that_resets_on_close

# The same for the self-pipe of every asyncio event loop (a Runtime has one, and so does every asyncio.run): on Windows
# socket.socketpair() is a TCP connection through the loopback, about 2,500 of them in a run, and it only carries wake-up bytes.
_socketpair = socket.socketpair


def _socketpair_that_resets_on_close(*args, **kwargs):
    pair = _socketpair(*args, **kwargs)
    for end in pair:
        try:
            end.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
        except OSError:
            pass
    return pair


socket.socketpair = _socketpair_that_resets_on_close

_made = collections.Counter()


@pytest.fixture
def tmp_path(request, tmp_path_factory):
    """A directory of the test's own, made without pytest's way of numbering it.

    pytest finds the number of a tmp_path by listing the whole base directory, which holds one directory for every test run so far,
    so the cost of each grows with them, and over 9,000 tests the total with their square. A counter per name numbers the
    directories instead, and they are named as pytest names them (the test's name cut at 30 characters, then the number), so the
    path is as long as it was: test_prompt_ownership.py sets a byte budget of 1500 that a path a few characters longer overruns."""
    name = re.sub(r"\W", "_", request.node.name)[:30]
    number = _made[name]
    _made[name] += 1
    path = tmp_path_factory.getbasetemp() / ("%s%d" % (name, number))
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def no_rate_limit_pauses(monkeypatch):
    """The transport waits seconds before it asks again after Retool's 429 (direct.RATE_LIMIT_WAITS); a test must not.

    test_rate_limit_retry.py records the pauses itself and has a test of the real wait."""
    import direct

    async def at_once(seconds, is_cancelled):
        return not is_cancelled()

    monkeypatch.setattr(direct, "_pause", at_once)


@pytest.fixture(autouse=True)
def no_retry_backoff(request, monkeypatch):
    """The transport waits 0.5, 1, 2 and 4 s before it asks the same account again after a failed attempt (AttemptBudget.wait).

    That is real time a test only sits through: the retry tests that did not silence it themselves took 1.5 to 3.5 s each (35 s
    in all, the ones of test_model_and_effort_untouched.py because they use the helpers of test_forced_choice.py without the
    fixture that file has). The tests that check the delays record them with a wait of their own, and the ones that are about
    the waiting itself (a clock they control) carry the marker real_backoff."""
    if request.node.get_closest_marker("real_backoff"):
        return
    import upstream_attempts

    def wait(self, delay, cancelled):
        # What the real one does before it sleeps: an attempt cancelled while it would have waited does not go on.
        if delay > 0 and (cancelled() or (self.cancel_event and self.cancel_event.is_set())):
            raise InterruptedError("upstream retry cancelled")

    monkeypatch.setattr(upstream_attempts.AttemptBudget, "wait", wait)


@pytest.fixture(autouse=True)
def invalid_names_do_not_resolve(monkeypatch):
    """A name under .invalid never resolves (RFC 6761), and the tests use such names for hosts that must not be reached.

    A resolver that has to find that out by asking can take many seconds: one test spent 11 s on example.invalid, in
    validate's guard against private image hosts. They fail at once, as the real answer would."""
    import socket
    real = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if isinstance(host, str) and host.rstrip(".").lower().endswith(".invalid"):
            raise socket.gaierror(getattr(socket, "EAI_NONAME", -2), "Name or service not known")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def pinned_calendar(monkeypatch):
    """Anthropic's retirement dates hide models by the calendar (models._today).

    The suite describes the catalog as of 2026-11-05, so a model that retires later (Sonnet 4.5 on
    2026-11-30) does not change published counts on the day it retires. Tests of the dates themselves
    set their own date."""
    import datetime
    import models
    monkeypatch.setattr(models, "_today", lambda: datetime.date(2026, 11, 5))


@pytest.fixture(autouse=True)
def upstream_shapes_unknown(monkeypatch):
    """What upstream refuses is learned once per run (direct._TOOL_RESULT_HINTS: a cache hint on a tool result;
    direct._PRUNING: a history that lost a call): a test starts from not having learned it, whatever an earlier one
    made upstream refuse."""
    import direct
    monkeypatch.setattr(direct, "_TOOL_RESULT_HINTS", {"refused": False})
    monkeypatch.setattr(direct, "_PRUNING", {"refused": False})


class FakeCredits:
    def __init__(self):
        self.used = []
        self.balance = {"available": 100.0, "monthly_allocation": 500.0, "reset_date": "2026-11-01"}

    def get_balance(self, force_refresh=False):
        return dict(self.balance)

    def record_turn(self, turn_id, cost):
        self.used.append((turn_id, cost))

    def usage(self):
        return {"session_used": sum(c for _, c in self.used), "turns": len(self.used), "recent": []}


class FakeRunner:
    """Emits scripted events; default is a short answer."""

    def __init__(self):
        self.script = None
        self.seen = []
        self.credits = None

    async def run(self, parsed, out, holder):
        self.seen.append(parsed)
        script = self.script or [{"type": "text", "delta": "ok"}, {"type": "end", "reason": "stop"}]
        try:
            for event in script:
                if isinstance(event, Exception):
                    out.put({"type": "error", "error": event})
                    return
                if event.get("type") == "credits" and self.credits is not None:
                    self.credits.record_turn("turn", event["value"])
                out.put(event)
        finally:
            out.put(None)


@pytest.fixture
def relay(monkeypatch, tmp_path):
    cfg = dict(config_module.DEFAULTS)
    cfg.update({
        "base": "https://example.invalid", "page_uuid": "page-1", "thread_id": "thread-1",
        "ua": "test-agent", "cookie": "accessToken=abc123456789xyz; xsrfToken=q",
        "xsrf": "q", "relay_api_key": KEY, "workspace_root": str(tmp_path),
        "command_log": str(tmp_path / "commands.log"), "host": "127.0.0.1", "port": 0,
        "cookie_values": ["abc123456789xyz"],
    })
    runtime = proxy.Runtime(cfg)
    runtime.credits = FakeCredits()
    runner = FakeRunner()
    runner.credits = runtime.credits
    runtime.runner = runner
    handler = proxy.make_handler(runtime)
    slots = threading.BoundedSemaphore(4)
    server = proxy.BoundedServer(("127.0.0.1", 0), handler, slots)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_address[1])

    class Ctx:
        url = base
        key = KEY
        fake = runner
        credits = runtime.credits
        cfg = runtime.cfg
        workspace = tmp_path

        def request(self, path, payload=None, *, key=KEY, method=None, headers=None, raw=None):
            body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
            request = urllib.request.Request(self.url + path, data=body, method=method or ("POST" if body else "GET"))
            request.add_header("content-type", "application/json")
            if key:
                request.add_header("authorization", "Bearer " + key)
            for name, value in (headers or {}).items():
                request.add_header(name, value)
            return urllib.request.urlopen(request, timeout=20)

        def json(self, path, payload=None, **kw):
            with self.request(path, payload, **kw) as resp:
                return resp.status, json.loads(resp.read().decode())

        def json_any(self, path, payload=None, **kw):
            """Like json(), but returns (status, body) for error statuses too."""
            try:
                return self.json(path, payload, **kw)
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode() or "{}")

        def sse(self, path, payload):
            with self.request(path, payload) as resp:
                return resp.status, resp.read().decode()

        def status_of(self, path, payload=None, **kw):
            try:
                with self.request(path, payload, **kw) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code

    yield Ctx()
    server.shutdown()
    try:
        server.server_close()
    except OSError:
        # CI (Windows runners) has shown the listener socket already closed by
        # the OS at teardown; the fixture is done either way.
        pass
    finally:
        runtime.close()
