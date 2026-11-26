"""Claude through Minara's /v1/messages: the request written from a chat body, Anthropic's stream read back as the relay's
events, the levels that take that path, and a level moved there (or back) while the relay runs."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import apierr
import client_context
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
    # Both results and the text after them in one user turn, the results first, each as its text; the block that closes
    # the results carries the mark build_body put on the last one (Minara drops a mark on or inside a tool_result).
    kinds = [block["type"] for block in results["content"]]
    assert kinds == ["tool_result", "tool_result", "text", "text"]
    assert results["content"][1] == {"type": "tool_result", "tool_use_id": "call_2", "content": "noon"}
    assert results["content"][2] == {"type": "text", "text": minara.TOOL_RESULTS_END, "cache_control": MARK}
    assert results["content"][3] == {"type": "text", "text": "Use the answer."}
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


def test_a_claude_head_has_a_distinct_cache_key_in_each_conversation_but_not_each_level():
    def body(conversation, level=None, head="HEAD"):
        provider = {"model": "minara/anthropic/claude-fable-5.2"}
        if level:
            provider["reasoning_effort"] = level
        session = SimpleNamespace(_native_conversation_key=conversation, _native_client_messages=None)
        return minara.build_body({}, provider, head, "hi", None, None, 100, session=session)
    first, second, other_level = body(("a",)), body(("b",)), body(("a",), "max")
    assert first["prompt_cache_key"].startswith("proxi-")
    assert first["prompt_cache_key"] == other_level["prompt_cache_key"]
    assert first["prompt_cache_key"] != second["prompt_cache_key"]
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


@pytest.mark.parametrize("channel, reported, provider", [
    ("openrouter", "Azure", "Azure"),  # OpenRouter names its provider on the chat path
    ("openrouter", None, None),  # ... and nothing names it behind /v1/messages
    ("anthropic", None, "Anthropic"),  # Minara's own channel is the provider itself
    ("bedrock", None, "Amazon Bedrock"),
    ("other", None, None), (None, None, None),
])
def test_the_provider_of_a_reply_is_the_reported_one_else_its_channel(channel, reported, provider):
    assert minara.provider_of(channel, reported) == provider


def test_a_reply_of_minaras_own_anthropic_channel_names_anthropic_as_its_provider():
    usage = minara.normalize_messages_usage({"input_tokens": 4, "cache_read_input_tokens": 690_000}, channel="anthropic")
    assert (usage["channel"], usage["provider"]) == ("anthropic", "Anthropic")
    # OpenRouter behind /v1/messages names no provider: none is made up
    assert "provider" not in minara.normalize_messages_usage({"input_tokens": 4}, channel="openrouter")


FABLE = {"model": "minara/claude-fable-5-2", "upstream_model": "anthropic/claude-fable-5.2", "reasoningEffort": "max"}
WEATHER = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}}}]
REMINDER = "<system-reminder>\nThe user hasn't heard from you in a while.\n</system-reminder>"


def tool_loop(rounds, reminders=()):
    """A canonical history of a tool loop: a question, then `rounds` calls each answered by a result; a reminder after the
    results of the rounds in `reminders` (Claude Code adds one to the user turn that carries the results)."""
    history = [{"role": "user", "content": [{"type": "text", "text": "What is the weather in every city?"}]}]
    for n in range(rounds):
        history.append({"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "call_%d" % n,
                                                          "toolName": "get_weather", "toolInput": {"city": "c%d" % n}}]})
        history.append({"role": "tool", "content": {"toolCallId": "call_%d" % n, "toolResult": 'c%d: "18 C"\nsunny' % n}})
        if n in reminders:
            history.append({"role": "user", "content": [{"type": "text", "text": REMINDER}]})
    return history


def loop_request(history):
    session = SimpleNamespace(_native_client_messages=history, _native_allow_cold_replay=True,
                              _native_conversation_key=None)
    return minara.messages_body(minara.build_body({}, FABLE, "HEAD " * 600, "", WEATHER, "auto", 1000, session=session))


def marks_in(payload):
    """Every cache mark of an Anthropic request: ("system", index) or (turn, block, type), and "inside" for a part of a
    tool_result's content."""
    found = [("system", index) for index, block in enumerate(payload.get("system") or []) if "cache_control" in block]
    for turn_index, turn in enumerate(payload["messages"]):
        for block_index, block in enumerate(turn["content"]):
            if "cache_control" in block:
                found.append((turn_index, block_index, block["type"]))
            if isinstance(block.get("content"), list):
                found.extend((turn_index, block_index, "inside") for part in block["content"] if "cache_control" in part)
    return found


def read_by_the_model(payload):
    """The blocks of a request in order, as the model reads them: without the marks."""
    blocks = [json.dumps({k: v for k, v in block.items() if k != "cache_control"}) for block in payload["system"]]
    for turn in payload["messages"]:
        blocks.extend(json.dumps([turn["role"], {k: v for k, v in block.items() if k != "cache_control"}])
                      for block in turn["content"])
    return blocks


def test_a_tool_result_goes_as_its_text_and_the_block_closing_its_turn_carries_the_mark():
    """Minara's /v1/messages drops a mark on a tool_result or a tool_use, and one inside a tool_result's content reaches the
    model as text: a turn that ended in tool results wrote nothing to the cache, and Minara billed its whole prompt at the
    input price (Fable 5.2, 2026-11-13). A text block after the results carries the mark there (each such request then
    wrote its tail and the next one read it)."""
    payload = loop_request(tool_loop(2))
    last = payload["messages"][-1]
    assert last == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_1", "content": 'c1: "18 C"\nsunny'},
        {"type": "text", "text": minara.TOOL_RESULTS_END, "cache_control": MARK}]}
    # The head, the tail and the end of the turn before: never on a tool_result, inside one or on a tool_use.
    assert marks_in(payload) == [("system", 0), (2, 1, "text"), (4, 1, "text")]
    assert "cache_control" not in json.dumps([turn for turn in payload["messages"] if turn["role"] == "assistant"])


def test_a_tool_turn_reads_the_same_in_every_request_marked_or_not():
    """A mark inside a tool result was text the model read: the same result was 25 tokens longer while it was the tail or
    the end of the turn before than after, so the next request could not read what this one wrote and wrote it again
    (36,864 tokens, then the same span less 25). Each request must read as the one before it did, up to its end."""
    requests = [loop_request(tool_loop(rounds, reminders={1})) for rounds in (1, 2, 3, 4)]
    for earlier, later in zip(requests, requests[1:]):
        before = read_by_the_model(earlier)
        assert read_by_the_model(later)[:len(before)] == before
    for payload in requests:
        assert all(where[0] == "system" or where[2] == "text" for where in marks_in(payload))
        assert len(marks_in(payload)) <= 4
        assert "cache_control" not in json.dumps([block for turn in payload["messages"] for block in turn["content"]
                                                  if block["type"] == "tool_result"])
    # Every run of tool results is closed, marked or not, so that a turn reads the same wherever the marks are.
    closing = [turn["content"] for turn in requests[-1]["messages"] if turn["content"][0]["type"] == "tool_result"]
    assert len(closing) == 4 and all(blocks[1]["text"] == minara.TOOL_RESULTS_END for blocks in closing)


def test_results_before_a_reminder_are_closed_before_it_and_the_reminder_keeps_the_mark():
    payload = loop_request(tool_loop(2, reminders={1}))
    assert [block["type"] for block in payload["messages"][-1]["content"]] == ["tool_result", "text", "text"]
    assert payload["messages"][-1]["content"][1] == {"type": "text", "text": minara.TOOL_RESULTS_END}
    assert payload["messages"][-1]["content"][2] == {"type": "text", "text": REMINDER, "cache_control": MARK}


def test_two_marked_tool_results_of_one_turn_leave_one_mark_on_the_block_that_closes_it():
    body = chat_body("max", model="anthropic/claude-fable-5.2")
    body["messages"][3]["content"] = [{"type": "text", "text": "18 C", "cache_control": MARK}]
    del body["messages"][-1]  # the turn ends with its results
    payload = minara.messages_body(body)
    results = payload["messages"][-1]["content"]
    assert [block.get("content") for block in results[:2]] == ["18 C", "noon"]
    assert results[2] == {"type": "text", "text": minara.TOOL_RESULTS_END, "cache_control": MARK}
    assert marks_in(payload) == [("system", 0), (2, 2, "text")]


def test_a_tool_result_with_an_image_keeps_its_list_and_its_mark_closes_the_turn():
    # build_body never makes one (an image of a tool result goes to a user message after the results), but a list with an
    # image stays a list; its mark goes where every tool result's goes, and nothing of it is left inside.
    body = chat_body("max", model="anthropic/claude-fable-5.2")
    body["messages"][4]["content"] = [{"type": "text", "text": "noon", "cache_control": MARK},
                                      {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]
    del body["messages"][-1]
    results = minara.messages_body(body)["messages"][-1]["content"]
    assert results[1]["content"] == [{"type": "text", "text": "noon"}, {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "QUJD"}}]
    assert "cache_control" not in results[1] and results[2]["cache_control"] == MARK


def test_a_side_request_writes_its_tail_on_v1_messages_and_keeps_the_side_rule_on_chat():
    """A compaction marks no tail of its own: on /v1/messages it then wrote nothing, and Minara bills such a reply at the
    input price for its whole prompt. There its tail is written; on chat the side rule stands."""
    posts = []

    def post(url, **kwargs):
        posts.append((url, kwargs["json"]))
        return Response(200, anthropic_stream() if url.endswith("/messages") else chat_stream())
    token = client_context.bind({"client": "codex", "client_task": client_context.COMPACTION})
    try:
        run({"model": "anthropic/claude-opus-4.7", "reasoningEffort": "max", "messages_levels": ["max"]}, post)
        run({"model": "anthropic/claude-opus-4.7", "reasoningEffort": "high"}, post)
    finally:
        client_context._CURRENT.reset(token)
    (messages_url, messages_payload), (chat_url, chat_payload) = posts
    assert messages_url.endswith("/v1/messages") and chat_url.endswith("/chat/completions")
    assert messages_payload["messages"][-1]["content"][-1] == {"type": "text", "text": "Hello", "cache_control": MARK}
    assert "cache_control" not in json.dumps(chat_payload["messages"][1:])
