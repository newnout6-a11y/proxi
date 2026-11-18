"""A keepwarm ping that Retool refuses for the account's hourly limit stops the cycle and parks the account (A03-07), and
a ping that the cancel of its turn cuts is no failure (A03-08).

The ping read neither the body nor the status of an HTTP error: the hourly limit (429 "You've reached your hourly limit for
Copilot") went unrecognized, the cycle asked again on the same account 10 and 20 s later with a red `[err]` line and a
journal warning each time until its cache window closed, the account was not parked, and the HTTPError kept its socket
until the garbage collector came. A client that pressed Esc while a ping was out (proxy._cancel_turn -> RelaySession.cancel,
which aborts the ping with the turn) got the same red line, a warning and one of the five failures that end the cycle."""
import asyncio
import http.server
import io
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

import apierr
import direct
import journal
import session as session_mod
from keepwarm_harness import DUE, backend as warm_backend, drive, session as warm_session, stops

HOURLY = (b'{"statusCode":429,"error":"Too Many Requests","message":"You\'ve reached your hourly limit for Copilot. Please '
          b'try again in an hour.\\nYou can still use other AI features like Ask AI and Agents.","data":null}')


class Refusal(urllib.error.HTTPError):
    """An HTTP error response as urlopen raises it; it remembers whether the relay closed it."""

    def __init__(self, code, body):
        super().__init__("https://fixture.invalid/api/gpt/copilotStreamGPT", code, "refused", {}, io.BytesIO(body))
        self.closed_by_relay = False

    def close(self):
        self.closed_by_relay = True
        super().close()


class Owner:
    """The account dispatcher, as the refresher of a pool account names it (AccountDispatcher sets keepalive_owner)."""

    def __init__(self):
        self.parked = []

    def clock(self):
        return 5000.0

    def mark_rate_limited(self, backend, retry_after=None, request_id=None, sent_at=None):
        self.parked.append((backend, retry_after, sent_at))
        backend.unavailable = {"reason": "rate_limited"}
        return time.time() + 1200


def test_the_ping_reads_and_closes_an_http_refusal_and_names_the_hourly_limit(monkeypatch):
    refusal = Refusal(429, HOURLY)

    def refuse(request, **kw):
        raise refusal

    monkeypatch.setattr(direct.urllib.request, "urlopen", refuse)
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"base": "https://fixture.invalid"}, headers=lambda: {}))
    with pytest.raises(apierr.ApiError) as caught:
        direct._ping(session, {"model": "claude-opus-5-5"})
    assert apierr.is_account_quota(caught.value) and caught.value.status == 429
    assert refusal.closed_by_relay, "the socket of the refusal is let go at once"


@pytest.mark.parametrize("before, unknown", [([], []), ([b'{"event":"content_block_start","type":"text"}'], [True])],
                         ids=["refused before anything", "after the answer began"])
def test_an_hourly_limit_inside_the_pings_200_costs_nothing_before_the_answer_begins(monkeypatch, before, unknown):
    lines = before + [b'{"event":"error","error":"You\'ve reached your hourly limit for Copilot.","isRetryable":false}',
                      b'{"event":"end_of_stream"}']
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda request, **kw: io.BytesIO(b"\n".join(lines) + b"\n"))
    marks = []
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"base": "https://fixture.invalid"}, headers=lambda: {}),
                              _credits_manager=SimpleNamespace(mark_usage_unknown=lambda: marks.append(True)))
    with pytest.raises(apierr.ApiError) as caught:
        direct._ping(session, {"model": "claude-opus-5-5"})
    assert apierr.is_account_quota(caught.value) and marks == unknown


def test_an_hourly_limit_stops_the_cycle_at_once_and_parks_the_account(monkeypatch, capsys):
    owner = Owner()
    target = warm_session(_warm_at=DUE, backend=warm_backend(refresher=SimpleNamespace(keepalive_owner=owner)))
    seq = journal.JOURNAL.last_seq

    def limited(_session, _body):
        raise apierr.account_rate_limited("You've reached your hourly limit for Copilot.")

    _, pings = drive(monkeypatch, target, seconds=1800, ping=limited)
    assert len(pings) == 1, "the same account is not asked again: every request on it is refused for a while"
    assert [(backend, sent_at) for backend, _, sent_at in owner.parked] == [(target.backend, 5000.0)]
    assert [(stop["reason"], stop["level"]) for stop in stops(seq)] == [("account_rate_limited", "info")]
    assert target._warm_failures == 0
    assert "[err]" not in capsys.readouterr().out
    assert not [event for event in journal.JOURNAL.query(since=seq, limit=1000)
                if event["kind"] == "cache.keepwarm" and event["level"] == "warn"]


def test_without_a_dispatcher_the_cycle_still_stops(monkeypatch):
    target = warm_session(_warm_at=DUE)
    seq = journal.JOURNAL.last_seq

    def limited(_session, _body):
        raise apierr.account_rate_limited("You've reached your hourly limit for Copilot.")

    _, pings = drive(monkeypatch, target, seconds=1800, ping=limited)
    assert len(pings) == 1 and [stop["reason"] for stop in stops(seq)] == ["account_rate_limited"]


def test_another_failure_is_still_asked_again(monkeypatch):
    target = warm_session(_warm_at=DUE)

    def failing(_session, _body):
        raise apierr.upstream_error("Retool connection failed: RemoteDisconnected")

    _, pings = drive(monkeypatch, target, seconds=1800, ping=failing)
    assert len(pings) > 1


# -- A03-08: the cancel of a turn cuts the ping it shares the session with ------------------------------------------------
class HeldStream(http.server.BaseHTTPRequestHandler):
    """A fake upstream on the loopback: it answers the ping's first line and holds the stream open."""
    protocol_version = "HTTP/1.0"
    arrived = threading.Event()
    release = threading.Event()

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"event":"content_block_start","type":"text"}\n')
        self.wfile.flush()
        HeldStream.arrived.set()
        HeldStream.release.wait(10)


def test_a_ping_cut_by_the_cancel_of_its_turn_is_no_failure_and_no_red_line(capsys):
    HeldStream.arrived.clear()
    HeldStream.release.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HeldStream)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = {"base": "http://127.0.0.1:%d" % server.server_address[1], "cache_keepwarm_seconds": 1}
    backend = SimpleNamespace(cfg=cfg, headers=lambda: {"content-type": "application/json"}, unavailable=None)
    relay = session_mod.RelaySession(backend, "thread-cancel")
    relay._warm_body = {"model": "claude-opus-5-5", "openAICacheKey": "k",
                        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi", "cache": True}]}]}
    relay._warm_at = relay._last_request_at = time.time()
    seq = journal.JOURNAL.last_seq

    async def story():
        task = asyncio.create_task(direct._keepwarm(relay, cfg))
        try:
            assert await asyncio.to_thread(HeldStream.arrived.wait, 10), "the ping never reached the fake upstream"
            await asyncio.sleep(0.2)
            relay.cancel()  # what proxy._cancel_turn does when the client goes away
            await asyncio.sleep(0.8)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(story())
    finally:
        HeldStream.release.set()
        server.shutdown()
        server.server_close()
    assert getattr(relay, "_warm_failures", 0) == 0
    assert "[err]" not in capsys.readouterr().out
    assert not [event for event in journal.JOURNAL.query(since=seq, limit=1000)
                if event["kind"] == "cache.keepwarm" and event["level"] in ("warn", "error")]
