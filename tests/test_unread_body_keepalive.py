"""A request answered before its body was read does not poison the next request on the same kept-alive connection, and its
answer reaches a client that is still sending that body."""
import http.client
import json
import socket
import threading
import time

import pytest

import proxy
from conftest import KEY

BODY = json.dumps({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]})


def request(path, key):
    head = ["POST %s HTTP/1.1" % path, "Host: 127.0.0.1", "Content-Type: application/json",
            "Content-Length: %d" % len(BODY)]
    if key:
        head.append("Authorization: Bearer " + key)
    return ("\r\n".join(head) + "\r\n\r\n" + BODY).encode()


def read_response(client):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                   if line.lower().startswith(b"content-length:")), 0)
    while len(rest) < length:
        chunk = client.recv(65536)
        if not chunk:
            break
        rest += chunk
    return head


def second_answer(relay, first_path, first_key):
    """(the first response's head, what the same connection answers to a valid request sent after it)."""
    port = int(relay.url.rsplit(":", 1)[1])
    client = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        client.sendall(request(first_path, first_key))
        head = read_response(client)
        try:
            client.sendall(request("/v1/chat/completions", KEY))
            answer = client.recv(65536)
        except OSError:
            answer = b""  # the relay closed the connection, as it says it does
        return head, answer
    finally:
        client.close()


def test_a_wrong_key_closes_the_connection_instead_of_desyncing_it(relay):
    head, answer = second_answer(relay, "/v1/chat/completions", "wrong-key")
    assert head.startswith(b"HTTP/1.1 401") and b"connection: close" in head.lower()
    assert answer == b"" or answer.startswith(b"HTTP/1.1 200")  # never the body glued to the next request line


def test_an_unknown_route_closes_the_connection_instead_of_desyncing_it(relay):
    head, answer = second_answer(relay, "/v1/embeddings", KEY)
    assert head.startswith(b"HTTP/1.1 404") and b"connection: close" in head.lower()
    assert answer == b"" or answer.startswith(b"HTTP/1.1 200")


# Far more than the socket buffers of both ends hold: when the relay answers, most of the body is still to be sent.
LARGE = 20 * 1024 * 1024


def upload_then_read(relay, method, path, headers):
    """(status, connection header, error object) as a client sees them that sends the whole body before it reads the
    answer, as http.client, urllib and the SDKs built on them do."""
    port = int(relay.url.rsplit(":", 1)[1])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        body = b'{"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "' + b"x" * LARGE + b'"}]}'
        connection.request(method, path, body, {"content-type": "application/json", "anthropic-version": "2023-06-01",
                                                **headers})
        reply = connection.getresponse()
        return reply.status, (reply.getheader("connection") or "").lower(), json.loads(reply.read())["error"]
    finally:
        connection.close()


@pytest.mark.parametrize("case, status, code", [("over max_body_bytes", 413, "payload_too_large"),
                                                ("wrong key", 401, "unauthorized"),
                                                ("not JSON", 415, "unsupported_media_type"),
                                                ("admin save over its own limit", 413, "payload_too_large")])
def test_a_refusal_sent_before_the_body_is_read_reaches_a_client_that_is_still_sending_it(relay, case, status, code):
    """These requests are refused on their headers, before a byte of the body is read, and the relay closed the connection
    with megabytes of the body still unread. That close is a reset, and on Windows the reset threw away the answer the
    client had not read yet: Claude Code, Node and Python clients got ECONNRESET or WinError 10053 instead of the 413
    request_too_large that tells Claude Code to compact, took it for a network error and asked again, to the same end."""
    method, path, headers = "POST", "/v1/messages", {"authorization": "Bearer " + KEY}
    if case == "over max_body_bytes":
        relay.cfg["max_body_bytes"] = LARGE // 2
    elif case == "wrong key":
        headers["authorization"] = "Bearer wrong-key"
    elif case == "not JSON":
        headers["content-type"] = "text/plain"
    else:  # a prompts document has a limit of its own (admin.PROMPTS_BODY_LIMIT), far below max_body_bytes
        method, path = "PUT", "/admin/api/prompts"
        headers.update({"x-admin-request": "1", "origin": relay.url})
    seen, connection, error = upload_then_read(relay, method, path, headers)
    assert (seen, connection) == (status, "close") and error.get("relay_code", error.get("code")) == code
    assert relay.fake.seen == []


def test_a_client_that_never_closes_keeps_the_lingering_close_no_longer_than_its_time(monkeypatch):
    """After such an answer the relay reads until the client closes; a client that never does must not keep the thread."""
    monkeypatch.setattr(proxy, "LINGER_SECONDS", 0.3)
    server_end, client_end = socket.socketpair()
    try:
        started = time.monotonic()
        proxy.linger_close(server_end, 1 << 30)
        assert time.monotonic() - started < 2.5
        assert client_end.recv(16) == b""  # the relay's side is shut: the client reads the end of the answer
    finally:
        server_end.close()
        client_end.close()


def test_a_client_that_keeps_sending_is_read_for_about_the_byte_bound_only(monkeypatch):
    """Nor does a client that sends without end: the relay stops reading at about the byte bound and closes."""
    monkeypatch.setattr(proxy, "LINGER_SECONDS", 60.0)
    server_end, client_end = socket.socketpair()

    def endless():
        try:
            while True:
                client_end.sendall(b"x" * 65536)
        except OSError:
            pass  # the relay closed the connection

    sender = threading.Thread(target=endless, daemon=True)
    sender.start()
    try:
        started = time.monotonic()
        proxy.linger_close(server_end, 4 * 1024 * 1024)
        assert time.monotonic() - started < 20  # it stopped at the byte bound, long before its time ran out
    finally:
        server_end.close()
        client_end.close()
        sender.join(5)
