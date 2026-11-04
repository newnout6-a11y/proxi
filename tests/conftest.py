"""Offline harness: a real HTTP server with a scripted upstream (no network)."""
import json
import threading
import urllib.error
import urllib.request

import pytest

import apierr
import proxy
import config as config_module

KEY = "test-key-123"


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
