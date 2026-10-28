import pytest

CHAT = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}


def test_missing_key_is_401(relay):
    assert relay.status_of("/v1/chat/completions", CHAT, key=None) == 401


def test_wrong_key_is_401(relay):
    assert relay.status_of("/v1/chat/completions", CHAT, key="nope") == 401


def test_x_api_key_header_accepted(relay):
    status, body = relay.json("/v1/chat/completions", CHAT, key=None,
                              headers={"x-api-key": relay.key})
    assert status == 200 and body["choices"][0]["message"]["content"] == "ok"


def test_query_key_accepted(relay):
    status, _ = relay.json("/v1/models?key=" + relay.key, key=None)
    assert status == 200


def test_auth_error_envelope_shape(relay):
    with pytest.raises(Exception) as exc:
        relay.request("/v1/models", key=None)
    assert getattr(exc.value, "code", None) == 401


def test_health_needs_no_key(relay):
    assert relay.status_of("/health", key=None) == 200
