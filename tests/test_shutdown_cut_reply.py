"""A reply the relay cuts because it is stopping reaches a client that is still there as an error, never as an answer."""
import json


def test_a_streamed_reply_cut_by_a_stop_ends_with_an_error_event(relay):
    relay.fake.script = [{"type": "text", "delta": "The first half of the answer, "}, {"type": "end", "reason": "cancelled"}]
    status, body = relay.sse("/v1/messages", {"model": "claude-opus-5-5", "max_tokens": 64, "stream": True,
                                              "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200 and "event: error" in body and "end_turn" not in body


def test_a_reply_cut_by_a_stop_before_it_is_sent_is_a_503(relay):
    relay.fake.script = [{"type": "text", "delta": "half"}, {"type": "end", "reason": "cancelled"}]
    status, body = relay.json_any("/v1/chat/completions", {"model": "claude-opus-5-5",
                                                           "messages": [{"role": "user", "content": "hi"}]})
    assert status == 503 and "shutting down" in json.dumps(body)


def test_an_ordinary_reply_is_untouched(relay):
    status, body = relay.json_any("/v1/chat/completions", {"model": "claude-opus-5-5",
                                                           "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200 and body["choices"][0]["finish_reason"] == "stop"
