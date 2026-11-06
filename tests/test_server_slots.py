"""The relay's HTTP server: a request, not an open connection, holds one of the max_workers slots.

Claude Desktop starts its sessions by counting the tokens of their tools in a dozen parallel requests and keeps those
connections open. While idle keep-alive connections held the slots, the relay stopped accepting, the listen queue of 5
filled and Windows refused every new connection (ECONNREFUSED) until the idle ones timed out 75 s later."""
import http.client
import socket
import threading
import time
from urllib.parse import urlparse

import pytest

import proxy


def make_server(slots, connections=None):
    state = {"inside": 0, "most": 0, "lock": threading.Lock(), "gate": threading.Event()}

    class Handler(proxy.SlottedRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 75

        def do_GET(self):
            with state["lock"]:
                state["inside"] += 1
                state["most"] = max(state["most"], state["inside"])
            try:
                if self.path == "/slow":
                    state["gate"].wait(10)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            finally:
                with state["lock"]:
                    state["inside"] -= 1

        def log_message(self, *args):
            pass

    server = proxy.BoundedServer(("127.0.0.1", 0), Handler, threading.BoundedSemaphore(slots),
                                 None if connections is None else threading.BoundedSemaphore(connections))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


@pytest.fixture
def served():
    started = []

    def start(slots, connections=None):
        server, state = make_server(slots, connections)
        started.append((server, state))
        return server, state

    yield start
    for server, state in started:
        state["gate"].set()
        server.shutdown()
        server.server_close()


def wait_until(check, seconds=3):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def answer_of(sock):
    data = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except (ConnectionResetError, ConnectionAbortedError):
            break
        if not chunk:
            break
        data += chunk
    return data


def test_idle_keep_alive_connections_hold_no_worker_slot(served):
    server, _ = served(slots=2)
    host, port = server.server_address
    idle = []
    for _ in range(6):  # three times the slots: each made a request and stays open, as a client's pool keeps it
        connection = http.client.HTTPConnection(host, port, timeout=3)
        connection.request("GET", "/")
        assert connection.getresponse().read() == b"ok"
        idle.append(connection)
    fresh = http.client.HTTPConnection(host, port, timeout=3)
    started = time.monotonic()
    fresh.request("GET", "/")
    assert fresh.getresponse().read() == b"ok"
    assert time.monotonic() - started < 2
    idle[0].request("GET", "/")  # and an idle one still takes its next request
    assert idle[0].getresponse().read() == b"ok"
    for connection in idle + [fresh]:
        connection.close()


def test_a_burst_of_connections_while_every_slot_is_busy_waits_instead_of_being_refused(served):
    server, state = served(slots=1)
    host, port = server.server_address
    slow = http.client.HTTPConnection(host, port, timeout=10)
    slow.request("GET", "/slow")  # holds the only slot
    wait_until(lambda: state["inside"] == 1)
    burst = [socket.create_connection((host, port), timeout=5) for _ in range(40)]  # none is refused
    for sock in burst:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: relay\r\nConnection: close\r\n\r\n")
    state["gate"].set()
    assert slow.getresponse().read() == b"ok"
    for sock in burst:
        assert answer_of(sock).startswith(b"HTTP/1.1 200")
        sock.close()
    slow.close()
    assert state["most"] == 1  # the requests still ran one at a time


def test_requests_in_progress_are_still_capped_at_max_workers(served):
    server, state = served(slots=2)
    host, port = server.server_address
    results = []

    def call():
        connection = http.client.HTTPConnection(host, port, timeout=10)
        connection.request("GET", "/slow")
        results.append(connection.getresponse().read())
        connection.close()

    workers = [threading.Thread(target=call) for _ in range(5)]
    for worker in workers:
        worker.start()
    wait_until(lambda: state["inside"] == 2)
    time.sleep(0.3)
    assert state["inside"] == 2  # the other three wait for a slot
    state["gate"].set()
    for worker in workers:
        worker.join(10)
    assert results == [b"ok"] * 5 and state["most"] == 2


def test_shutdown_lets_a_request_waiting_for_a_slot_go(served):
    server, state = served(slots=1)
    host, port = server.server_address
    slow = http.client.HTTPConnection(host, port, timeout=10)
    slow.request("GET", "/slow")
    wait_until(lambda: state["inside"] == 1)
    waiting = socket.create_connection((host, port), timeout=3)
    waiting.sendall(b"GET / HTTP/1.1\r\nHost: relay\r\n\r\n")
    time.sleep(0.2)  # its thread now waits for the slot
    server.shutdown()
    assert answer_of(waiting) == b""  # dropped unanswered, not left hanging
    waiting.close()
    state["gate"].set()
    assert slow.getresponse().read() == b"ok"
    slow.close()


def test_the_relay_serves_a_new_client_while_others_keep_idle_connections(relay):
    url = urlparse(relay.url)
    idle = []
    for _ in range(8):  # twice the 4 slots of the relay fixture
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read()
        assert response.status == 200 and not response.will_close
        idle.append(connection)
    fresh = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    fresh.request("GET", "/health")
    response = fresh.getresponse()
    response.read()
    assert response.status == 200
    for connection in idle + [fresh]:
        connection.close()
