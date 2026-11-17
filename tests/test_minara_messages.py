"""Claude through Minara's /v1/messages: the request written from a chat body, Anthropic's stream read back as the relay's
events, the levels that take that path, and a level moved there (or back) while the relay runs."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import apierr
import minara


@pytest.fixture(autouse=True)
def no_switches():
    saved = {key: set(value) for key, value in minara.SWITCHED.items()}
    minara.SWITCHED.clear()
    listeners = list(minara._switch_listeners)
    yield
    minara.SWITCHED.clear()
    minara.SWITCHED.update(saved)
    minara._switch_listeners[:] = listeners


MARK = {"type": "ephemeral", "ttl": "1h"}


def chat_body(level=None, model="anthropic/claude-opus-4.7"):
    body = {"model": model, "stream": True, "stream_options": {"include_usage": True}, "max_tokens": 900,
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": "HEAD", "cache_control": MARK}]},
                {"role": "user", "content": [{"type": "text", "text": "weather?"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
                {"role": "assistant", "content": "checking", "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}},
                    {"id": "call_2", "type": "function", "function": {"name": "get_time", "arguments": ""}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "18 C"},
                {"role": "tool", "tool_call_id": "call_2", "content": [{"type": "text", "text": "noon",
                                                                        "cache_control": MARK}]},
                {"role": "user", "content": "Use the answer."}],
            "tools": [{"type": "function", "function": {"name": "get_weather", "description": "Weather",
                                                        "parameters": {"type": "object", "properties": {}}}},
                      {"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"},
                                                        "strict": True}}],
            "tool_choice": "auto"}
    if level:
        body["reasoning_effort"] = level
    return body


def test_a_chat_body_becomes_the_same_request_in_anthropic_shape():
    body = minara.messages_body(chat_body("max"))
    assert body["model"] == "anthropic/claude-opus-4.7" and body["max_tokens"] == 900 and body["stream"] is True
    assert body["system"] == [{"type": "text", "text": "HEAD", "cache_control": MARK}]
    user, assistant, results = body["messages"]
    assert user["role"] == "user" and user["content"][1] == {
        "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    assert assistant == {"role": "assistant", "content": [
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}},
        {"type": "tool_use", "id": "call_2", "name": "get_time", "input": {}}]}
    # Both results and the text after them in one user turn, the results first; the mark stays where build_body put it.
    kinds = [block["type"] for block in results["content"]]
    assert kinds == ["tool_result", "tool_result", "text"]
    assert results["content"][1]["content"] == [{"type": "text", "text": "noon", "cache_control": MARK}]
    assert body["tools"][0] == {"name": "get_weather", "description": "Weather",
                                "input_schema": {"type": "object", "properties": {}}}
    assert body["tools"][1]["strict"] is True and body["tool_choice"] == {"type": "auto"}
    assert body["output_config"] == {"effort": "max"}
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "stream_options" not in body and "reasoning_effort" not in body


@pytest.mark.parametrize("model,level,effort,thinking", [
    ("anthropic/claude-opus-4.7", None, None, None),  # Opus 4.7 runs without thinking unless asked: its own default
    ("anthropic/claude-opus-4.7", "none", None, {"type": "disabled"}),
    ("anthropic/claude-opus-4.7", "minimal", "low", "adaptive"),
    ("anthropic/claude-sonnet-4.6", "xhigh", "max", "adaptive"),  # 4.6 knows no xhigh: the level above
    ("anthropic/claude-opus-5", None, None, "adaptive"),  # thinks by default: asked as summaries
    ("anthropic/claude-opus-5", "none", None, {"type": "disabled"}),
    ("anthropic/claude-fable-5", "none", "low", "adaptive"),  # Fable cannot switch thinking off
    ("anthropic/claude-fable-5.2", "xhigh", "xhigh", "adaptive"),
    ("anthropic/claude-opus-5.5", "none", "low", "adaptive"),
])
def test_each_family_gets_the_level_and_thinking_the_messages_api_takes(model, level, effort, thinking):
    got_effort, got_thinking = minara.messages_reasoning(model, level)
    assert got_effort == effort
    assert got_thinking == ({"type": "adaptive", "display": "summarized"} if thinking == "adaptive" else thinking)


def test_only_known_claude_models_take_the_messages_path():
    assert minara.messages_supported("anthropic/claude-sonnet-4.6") and minara.messages_supported("anthropic/claude-fable-5.2")
    assert not minara.messages_supported("anthropic/claude-haiku-4.5")  # effort is an error there
    assert not minara.messages_supported("anthropic/claude-opus-4.1")
    assert not minara.messages_supported("openai/gpt-6.1-sol")


def test_the_catalog_and_a_switch_decide_the_path_per_level():
    provider = {"messages_levels": ["max", "xhigh"]}
    assert minara.use_messages(provider, chat_body("max"))
    assert not minara.use_messages(provider, chat_body("high"))
    assert not minara.use_messages({}, chat_body("high"))
    seen = []
    minara.on_switch(lambda *args: seen.append(args))
    assert minara.switch_to_messages("anthropic/claude-opus-4.7", "high", reason="test")
    assert not minara.switch_to_messages("anthropic/claude-opus-4.7", "high")  # already there
    assert minara.use_messages({}, chat_body("high"))
    assert minara.switch_to_messages("anthropic/claude-opus-4.7", "high", switched=False, reason="test")
    assert not minara.use_messages({}, chat_body("high"))
    assert seen == [("anthropic/claude-opus-4.7", "high", True), ("anthropic/claude-opus-4.7", "high", False)]
    assert not minara.switch_to_messages("openai/gpt-6.1-sol", "high")  # no Messages request for it


def sse(*items):
    return ["%s" % json.dumps(item) for item in items]


def test_an_anthropic_stream_gives_the_relay_events_with_cache_counts_and_channel():
    pricing = {"prompt": 5, "completion": 25, "cache_read": 0.5, "cache_write": 6.25}
    parser = minara.MessagesParser(pricing=pricing)
    events = []
    for data in sse(
            {"type": "message_start", "message": {"id": "msg_bdrk_01abc", "usage": {
                "input_tokens": 3, "cache_creation_input_tokens": 120, "cache_read_input_tokens": 10000,
                "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Let me check."}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2, "content_block": {
                "type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"city":'}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": ' "Paris"}'}},
            {"type": "content_block_stop", "index": 2},
            {"type": "ping"},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}},
            {"type": "message_stop"}):
        events.extend(parser.feed(data))
    kinds = [event["type"] for event in events]
    assert kinds[:4] == ["thinking_start", "thinking", "thinking_signature", "text"]
    assert events[2]["signature"] == "SIG"
    usage = next(event["usage"] for event in events if event["type"] == "usage")
    assert usage["input_tokens"] == 3 and usage["cache_read"] == 10000 and usage["cache_write"] == 120
    assert usage["output_tokens"] == 40 and usage["channel"] == "bedrock" and usage["channel_path"] == "messages"
    assert usage["cache_write_reported"] is True and usage["provider_cost_estimated"] is True
    assert usage["provider_cost"] == pytest.approx((3 * 5 + 40 * 25 + 10000 * 0.5 + 120 * 6.25) / 1e6)
    final = [event for event in events if event["type"] == "tool_call" and not event.get("partial")]
    assert final == [{"type": "tool_call", "id": "toolu_1", "name": "get_weather", "arguments": '{"city": "Paris"}'}]
    assert events[-1] == {"type": "end", "reason": "tool_calls"} and parser.done
    assert parser.reply_key() == ("calls", "toolu_1")


@pytest.mark.parametrize("stop,reason", [("end_turn", "stop"), ("max_tokens", "length"), ("refusal", "content_filter")])
def test_stop_reasons_map_to_the_chat_finishes(stop, reason):
    parser = minara.MessagesParser()
    for data in sse({"type": "message_start", "message": {"id": "msg_011C", "usage": {"input_tokens": 1}}},
                    {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 1}}):
        parser.feed(data)
    assert parser.feed(json.dumps({"type": "message_stop"}))[-1] == {"type": "end", "reason": reason}


def test_a_stream_error_or_a_broken_stream_is_an_upstream_error():
    with pytest.raises(apierr.ApiError):
        minara.MessagesParser().feed(json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}))
    parser = minara.MessagesParser()
    parser.feed(json.dumps({"type": "message_start", "message": {"id": "msg_1"}}))
    with pytest.raises(apierr.ApiError):
        parser.feed(json.dumps({"type": "message_stop"}))  # no stop reason


class Response:
    def __init__(self, status, chunks, content_type="text/event-stream"):
        self.status_code, self._chunks = status, chunks
        self.headers = {"content-type": content_type}

    def iter_content(self, **_):
        yield from self._chunks

    def close(self):
        pass


def anthropic_stream(text="OK"):
    items = [{"type": "message_start", "message": {"id": "msg_bdrk_9", "usage": {
                 "input_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 9000}}},
             {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
             {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
             {"type": "content_block_stop", "index": 0},
             {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
             {"type": "message_stop"}]
    return [("event: %s\ndata: %s\n\n" % (item["type"], json.dumps(item))).encode() for item in items]


def chat_stream(text="CHAT"):
    data = {"id": "gen-1", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 0,
                                                                                            "cache_write_tokens": 0}}}
    return [("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode()]


def run(model_provider, post):
    session = SimpleNamespace(backend=SimpleNamespace(_minara_post=post), _cancelled=False,
                              _native_client_messages=None)
    cfg = {"bearer_token": "synthetic", "base": "https://api.minara.ai/v1", "turn_timeout": 30, "direct_cache": True,
           "minara_channel_retry_delays": []}

    async def collect():
        events = []
        async for event in minara._stream(cfg, session, model_provider, "Hello", "HEAD", None, None, 500):
            events.append(event)
        return events
    return asyncio.run(collect())


def test_a_messages_level_is_posted_to_v1_messages_with_the_anthropic_version():
    posts = []

    def post(url, **kwargs):
        posts.append((url, kwargs["headers"], kwargs["json"]))
        return Response(200, anthropic_stream("VIA_MESSAGES"))
    events = run({"model": "anthropic/claude-opus-4.7", "reasoningEffort": "max", "messages_levels": ["max"]}, post)
    url, headers, payload = posts[0]
    assert url == "https://api.minara.ai/v1/messages" and headers["anthropic-version"] == minara.ANTHROPIC_VERSION
    assert payload["output_config"] == {"effort": "max"} and payload["system"][0]["text"] == "HEAD"
    assert "".join(event.get("delta", "") for event in events if event["type"] == "text") == "VIA_MESSAGES"
    assert next(e for e in events if e["type"] == "usage")["usage"]["cache_read"] == 9000


def test_a_level_moved_by_the_relay_goes_back_to_chat_when_messages_refuses_it():
    minara.switch_to_messages("anthropic/claude-opus-4.7", "high", reason="test")
    posts = []

    def post(url, **kwargs):
        posts.append(url)
        if url.endswith("/messages"):
            return Response(400, [b'{"type":"error","error":{"type":"invalid_request_error","message":"no"}}'],
                            "application/json")
        return Response(200, chat_stream("CHAT_AGAIN"))
    events = run({"model": "anthropic/claude-opus-4.7", "reasoningEffort": "high"}, post)
    assert posts == ["https://api.minara.ai/v1/messages", "https://api.minara.ai/v1/chat/completions"]
    assert "".join(event.get("delta", "") for event in events if event["type"] == "text") == "CHAT_AGAIN"
    assert "high" not in minara.SWITCHED.get("anthropic/claude-opus-4.7", set())


def test_a_measured_messages_level_that_is_refused_stays_an_error():
    def post(url, **kwargs):
        return Response(400, [b'{"type":"error","error":{"type":"invalid_request_error","message":"bad"}}'],
                        "application/json")
    with pytest.raises(apierr.ApiError) as error:
        run({"model": "anthropic/claude-opus-4.7", "reasoningEffort": "max", "messages_levels": ["max"]}, post)
    assert "bad" in error.value.message


def test_a_chat_usage_names_its_channel():
    parser = minara.EventParser()
    events = parser.feed(json.dumps({"id": "gen-abc", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    assert events[0]["usage"]["channel"] == "openrouter" and events[0]["usage"]["channel_path"] == "chat"


def test_a_claude_head_gets_one_cache_key_for_every_conversation_and_level():
    """OpenRouter keeps the requests of one prompt_cache_key on one provider (Fable 5.2: 6 of 6, every follow-up read);
    the key is the head's, so conversations and subagents with the same head share the provider that holds it."""
    def body(conversation, level=None, head="HEAD"):
        provider = {"model": "minara/anthropic/claude-fable-5.2"}
        if level:
            provider["reasoning_effort"] = level
        session = SimpleNamespace(_native_conversation_key=conversation, _native_client_messages=None)
        return minara.build_body({}, provider, head, "hi", None, None, 100, session=session)
    first, second, other_level = body(("a",)), body(("b",)), body(("a",), "max")
    assert first["prompt_cache_key"].startswith("proxi-")
    assert first["prompt_cache_key"] == second["prompt_cache_key"] == other_level["prompt_cache_key"]
    assert body(("a",), head="OTHER")["prompt_cache_key"] != first["prompt_cache_key"]
    gpt = lambda conversation: minara.build_body(  # noqa: E731
        {}, {"model": "minara/openai/gpt-6.1-sol"}, "HEAD", "hi", None, None, 100,
        session=SimpleNamespace(_native_conversation_key=conversation, _native_client_messages=None))
    assert gpt(("a",))["prompt_cache_key"] != gpt(("b",))["prompt_cache_key"]  # GPT: one key per conversation
    assert "prompt_cache_key" not in minara.messages_body(first)


def test_a_chat_usage_names_openrouters_provider():
    parser = minara.EventParser()
    events = parser.feed(json.dumps({"id": "gen-abc", "provider": "Azure", "choices": [],
                                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    assert events[0]["usage"]["provider"] == "Azure"
