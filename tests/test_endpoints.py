import json

import apierr

CHAT = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}
ANTH = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


def test_health_rejects_stale_balance(relay):
    relay.credits.balance.update(stale=True, stale_error="HTTP Error 401: Unauthorized")
    status, body = relay.json_any("/health", key=None)
    assert status == 503
    assert body["ok"] is False
    assert body["upstream"] == "degraded"
    assert "401" in body["error"]
    assert body["session"]["keepalive"] is False


def test_chat_non_stream_shape(relay):
    status, body = relay.json("/v1/chat/completions", CHAT)
    assert status == 200
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["estimated"] is True
    assert "credits_used" in body["usage"]


def test_chat_stream_chunks_and_done(relay):
    relay.fake.script = [{"type": "text", "delta": "he"}, {"type": "text", "delta": "llo"},
                         {"type": "end", "reason": "stop"}]
    status, body = relay.sse("/v1/chat/completions", dict(CHAT, stream=True))
    assert status == 200
    assert body.count("data: ") >= 3
    assert '"delta": {"content": "he"}' in body
    assert '"finish_reason": "stop"' in body
    assert body.rstrip().endswith("data: [DONE]")


def test_stream_usage_chunk_when_requested(relay):
    status, body = relay.sse("/v1/chat/completions",
                             dict(CHAT, stream=True, stream_options={"include_usage": True}))
    assert status == 200
    usage_lines = [line for line in body.splitlines() if '"usage"' in line]
    assert usage_lines and '"choices": []' in usage_lines[0]


def test_timeout_maps_to_length(relay):
    relay.fake.script = [{"type": "text", "delta": "partial"}, {"type": "end", "reason": "timeout"}]
    status, body = relay.json("/v1/chat/completions", CHAT)
    assert status == 200 and body["choices"][0]["finish_reason"] == "length"


def test_upstream_error_is_not_200(relay):
    relay.fake.script = [apierr.upstream_auth_failed("expired")]
    assert relay.status_of("/v1/chat/completions", CHAT) == 502


def test_upstream_error_envelope(relay):
    relay.fake.script = [apierr.upstream_timeout()]
    status, body = relay.json_any("/v1/chat/completions", CHAT)
    assert status == 504 and body["error"]["type"] == "upstream_timeout"


def test_anthropic_requires_max_tokens(relay):
    status, body = relay.json_any("/v1/messages", {"model": "claude-opus-5-5",
                                                  "messages": [{"role": "user", "content": "hi"}]})
    assert status == 400 and body["type"] == "error"


def test_anthropic_non_stream_shape(relay):
    status, body = relay.json("/v1/messages", ANTH, key=None,
                              headers={"x-api-key": relay.key, "anthropic-version": "2023-06-01"})
    assert status == 200
    assert body["type"] == "message" and body["content"][0]["type"] == "text"
    assert body["stop_reason"] == "end_turn"
    assert "credits_used" in body["usage"]


def test_anthropic_stream_events(relay):
    status, body = relay.sse("/v1/messages", dict(ANTH, stream=True))
    assert status == 200
    for name in ("message_start", "content_block_delta", "message_delta", "message_stop"):
        assert "event: " + name in body


def test_anthropic_error_envelope(relay):
    relay.fake.script = [apierr.upstream_auth_failed()]
    status, body = relay.json_any("/v1/messages", ANTH)
    assert status == 502 and body["type"] == "error"


def test_responses_non_stream(relay):
    status, body = relay.json("/v1/responses", {"model": "gpt-5.2", "input": "hi"})
    assert status == 200 and body["output_text"] == "ok"
    assert body["status"] == "completed"


def test_responses_stream_events(relay):
    status, body = relay.sse("/v1/responses", {"model": "gpt-5.2", "input": "hi", "stream": True})
    assert status == 200 and "response.output_text.delta" in body


def test_effort_and_budget_are_forwarded(relay):
    relay.json("/v1/messages", dict(ANTH, thinking={"type": "enabled", "budget_tokens": 2048}))
    assert relay.fake.seen[-1].effort == "medium"


def test_models_lists_capabilities(relay):
    status, body = relay.json("/v1/models")
    assert status == 200
    first = body["data"][0]
    assert {"reasoning", "tools", "vision"} <= set(first["capabilities"])


def test_unknown_route_404_and_wrong_method_405(relay):
    assert relay.status_of("/v1/nope", CHAT) == 404
    assert relay.status_of("/v1/chat/completions", key=None, method="GET") == 405


def test_options_preflight(relay):
    status = relay.status_of("/v1/chat/completions", key=None, method="OPTIONS")
    assert status == 204


def test_health_degraded_is_503(relay):
    relay.credits.balance = {"available": None, "error": "boom"}
    assert relay.status_of("/health", key=None) == 503


def test_usage_endpoint_reports_turns(relay):
    relay.fake.script = [{"type": "credits", "value": 0.25}, {"type": "text", "delta": "x"},
                         {"type": "end", "reason": "stop"}]
    relay.json("/v1/chat/completions", CHAT)
    status, body = relay.json("/v1/usage")
    assert status == 200 and body["turns"] == 1
    assert json.dumps(body).find("0.25") != -1


def test_structure_preserved_for_tool_and_blocks(relay):
    payload = {"model": "claude-opus-5-5", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "read file"}]},
        {"role": "assistant", "content": "calling", "tool_calls": [
            {"id": "1", "function": {"name": "read_file", "arguments": "{\"path\":\"a\"}"}}]},
        {"role": "tool", "name": "read_file", "content": "DATA"},
    ]}
    relay.json("/v1/chat/completions", payload)
    prompt = relay.fake.seen[-1].prompt
    assert "read file" in prompt and "tool_call read_file" in prompt and "tool result" in prompt


def test_chat_native_tool_call_round_trip(relay):
    relay.fake.script = [{"type": "tool_call", "id": "call_1", "name": "bash",
                          "arguments": '{"command":"dir"}'},
                         {"type": "end", "reason": "tool_calls"}]
    payload = dict(CHAT, tools=[{"type": "function", "function": {
        "name": "bash", "parameters": {"type": "object"}}}], stream=False)
    status, body = relay.json("/v1/chat/completions", payload)
    assert status == 200
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    call = body["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_1" and body["choices"][0]["message"]["content"] is None
