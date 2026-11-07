"""A request answered before its body was read does not poison the next request on the same kept-alive connection."""
import json
import socket

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
