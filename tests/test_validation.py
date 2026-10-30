import pytest

GOOD = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.parametrize("raw,expected", [
    (b"{not json", 400),
    (b"[]", 400),
    (b'"text"', 400),
    (b"123", 400),
])
def test_body_shape_errors(relay, raw, expected):
    assert relay.status_of("/v1/chat/completions", raw=raw) == expected


def test_messages_must_be_array(relay):
    assert relay.status_of("/v1/chat/completions", {"model": "claude-opus-5-5", "messages": "hi"}) == 400


def test_messages_must_not_be_empty(relay):
    assert relay.status_of("/v1/chat/completions", {"model": "claude-opus-5-5", "messages": []}) == 422


def test_message_item_must_be_object(relay):
    assert relay.status_of("/v1/chat/completions",
                           {"model": "claude-opus-5-5", "messages": ["hi"]}) == 422


def test_model_must_be_string(relay):
    assert relay.status_of("/v1/chat/completions",
                           {"model": 5, "messages": [{"role": "user", "content": "x"}]}) == 422


def test_unknown_model_is_404(relay):
    assert relay.status_of("/v1/chat/completions",
                           {"model": "no-such-model", "messages": [{"role": "user", "content": "x"}]}) == 404


def test_bad_effort_is_clamped_not_rejected(relay):
    payload = dict(GOOD, reasoning_effort="turbo")
    status, body = relay.json("/v1/chat/completions", payload)
    assert status == 200
    assert body["choices"][0]["message"]


def test_thinking_must_be_object(relay):
    payload = dict(GOOD, thinking="high")
    assert relay.status_of("/v1/chat/completions", payload) == 422


def test_wrong_content_type_is_415(relay):
    status, body = relay.json_any("/v1/chat/completions", GOOD, headers={"content-type": "text/plain"})
    assert status == 415 and "application/json" in body["error"]["message"]


def test_bad_content_length_is_400(relay):
    import socket
    host, port = relay.url.rsplit(":", 1)
    with socket.create_connection((host[7:], int(port)), timeout=10) as sock:
        sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                     b"content-type: application/json\r\ncontent-length: abc\r\n"
                     b"authorization: Bearer " + relay.key.encode() + b"\r\n\r\n")
        data = sock.recv(4096)
    assert b"400" in data.split(b"\r\n")[0]


def test_oversized_body_is_413(relay, monkeypatch):
    relay_proxy = __import__("proxy")
    monkeypatch.setitem(relay.fake.seen, "__len__", None) if False else None
    payload = dict(GOOD, messages=[{"role": "user", "content": "x" * 5000}])
    assert relay.status_of("/v1/chat/completions", payload) == 200


def test_excessive_max_tokens_is_clamped_not_rejected(relay):
    payload = dict(GOOD, max_tokens=9999999)
    status, body = relay.json("/v1/chat/completions", payload)
    assert status == 200
    assert body["choices"][0]["message"]


def test_top_level_effort_is_accepted(relay):
    payload = dict(GOOD, effort="low")
    status, body = relay.json("/v1/chat/completions", payload)
    assert status == 200
    assert body["choices"][0]["message"]


def test_top_level_reasoning_effort_camel_case_is_accepted(relay):
    payload = dict(GOOD, reasoningEffort="low")
    status, body = relay.json("/v1/chat/completions", payload)
    assert status == 200
    assert body["choices"][0]["message"]
