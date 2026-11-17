"""Half of a surrogate pair in a message (a JavaScript client cut a string inside an emoji) is carried, not a 500."""
import pytest

HALF = "\\" + "ud83d"  # the JSON escape a client writes; json.loads gives the lone surrogate


@pytest.mark.parametrize("route,body", [
    ("/v1/chat/completions", '{"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "smile %s"}]}'),
    ("/v1/messages", '{"model": "claude-opus-5-5", "max_tokens": 8, "messages": [{"role": "user", "content": "smile %s"}]}'),
    ("/v1/responses", '{"model": "claude-opus-5-5", "input": "smile %s"}'),
])
def test_a_message_with_half_a_surrogate_pair_is_answered(relay, route, body):
    status, payload = relay.json_any(route, raw=(body % HALF).encode("ascii"))
    assert status == 200, payload
