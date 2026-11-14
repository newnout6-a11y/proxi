"""A request that waited for one of the max_workers slots says how long: the cap is no longer invisible.

Live (2026-11-07, 13:17-13:24, a 40 and a 68 request burst): the relay answered the first 28-31 requests at once and held the
others in SlottedRequestHandler.parse_request for 2.6 s (p90 of 40) and 6.4 s (p50 of 68, max 15.8 s). Nothing in the journal
told: duration_ms starts when a request is handled, and session.acquire's wait_ms starts inside the turn."""
import http.client
import threading
import time

import journal
import proxy


def recording_server(slots):
    seen, gate = [], threading.Event()

    class Handler(proxy.SlottedRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 75

        def do_GET(self):
            if self.path == "/slow":
                gate.wait(10)
            seen.append((self.path, self._slot_wait))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = proxy.BoundedServer(("127.0.0.1", 0), Handler, threading.BoundedSemaphore(slots))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen, gate


def get(server, path):
    host, port = server.server_address
    connection = http.client.HTTPConnection(host, port, timeout=10)
    connection.request("GET", path)
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def test_a_handler_knows_how_long_it_waited_for_its_slot():
    server, seen, gate = recording_server(slots=1)
    try:
        slow = threading.Thread(target=get, args=(server, "/slow"))
        slow.start()
        deadline = time.monotonic() + 3
        while not server.slots._value == 0:  # the only slot is taken
            assert time.monotonic() < deadline
            time.sleep(0.01)
        waiting = threading.Thread(target=get, args=(server, "/fast"))
        waiting.start()
        time.sleep(0.4)
        gate.set()
        slow.join(10)
        waiting.join(10)
        waits = dict(seen)
        assert waits["/slow"] < 0.2
        assert 0.3 <= waits["/fast"] < 5
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


def test_a_kept_alive_connection_starts_every_request_with_no_wait():
    server, seen, gate = recording_server(slots=1)
    try:
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=10)
        for _ in range(3):
            connection.request("GET", "/fast")
            connection.getresponse().read()
        connection.close()
        assert len(seen) == 3 and all(wait < 0.2 for _, wait in seen)
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


def slow_slot(monkeypatch, seconds):
    """Make every request wait `seconds` before it gets a slot; the returned dict changes the wait later."""
    original = proxy.BoundedServer.acquire_slot
    delay = {"seconds": seconds}

    def slow(self):
        time.sleep(delay["seconds"])
        return original(self)

    monkeypatch.setattr(proxy.BoundedServer, "acquire_slot", slow)
    return delay


def journaled_health(relay, before):
    status, _ = relay.json_any("/health")
    assert status == 200
    deadline = time.monotonic() + 3
    while True:
        found = [event for event in journal.JOURNAL.query(since=before, limit=500)
                 if event["kind"] == "http.request" and event["data"]["path"] == "/health"]
        if found:
            return found[-1]
        assert time.monotonic() < deadline, "HTTP completion was not journaled"
        time.sleep(0.01)


def test_the_journal_says_how_long_a_request_waited_for_a_slot(relay, monkeypatch):
    slow_slot(monkeypatch, 0.3)
    event = journaled_health(relay, journal.JOURNAL.last_seq)
    assert 250 <= event["data"]["slot_wait_ms"] < 5000
    assert event["data"]["duration_ms"] < event["data"]["slot_wait_ms"]  # the wait is not part of duration_ms


def test_a_request_that_found_a_free_slot_waited_no_time(relay):
    event = journaled_health(relay, journal.JOURNAL.last_seq)
    assert event["data"]["slot_wait_ms"] < 100


def test_the_console_names_a_long_wait_and_stays_quiet_about_a_short_one(relay, monkeypatch, capsys):
    delay = slow_slot(monkeypatch, 0.6)
    journaled_health(relay, journal.JOURNAL.last_seq)
    assert "for a max_workers slot" in capsys.readouterr().out
    delay["seconds"] = 0
    journaled_health(relay, journal.JOURNAL.last_seq)
    assert "max_workers slot" not in capsys.readouterr().out
