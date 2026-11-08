"""Odd request shapes are client errors in the client's envelope, never a 500."""
from conftest import KEY


def test_a_body_nested_past_the_decoders_depth_is_a_400(relay):
    status, body = relay.json_any("/v1/chat/completions", raw=b'{"x": ' + b"[" * 100000 + b"]" * 100000 + b"}")
    assert status == 400 and body["error"]["code"] == "invalid_request_error"


def test_a_tool_call_whose_function_is_not_an_object_is_a_422(relay):
    status, body = relay.json_any("/v1/chat/completions", {"model": "claude-opus-5-5", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": 42}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"}]})
    assert status == 422 and "function must be an object" in body["error"]["message"]


def test_count_tokens_answers_errors_in_anthropics_envelope(relay):
    status, body = relay.json_any("/v1/messages/count_tokens", {"model": "claude-opus-5-5",
                                                                "messages": [{"role": "user", "content": "hi"}]},
                                  key="wrong-key")
    assert status == 401 and body["type"] == "error" and body["error"]["type"] == "authentication_error"
    assert KEY not in str(body)
