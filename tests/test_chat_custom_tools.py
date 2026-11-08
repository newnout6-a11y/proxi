"""Chat Completions custom tools: accepted as a request, answered in the shape Chat gives them, read back from the history.

A custom tool of Chat is {"type": "custom", "custom": {"name", "description", "format"}} and its call is
{"id", "type": "custom", "custom": {"name", "input"}} (OpenAI's ChatCompletionMessageCustomToolCall). The relay refused the
request with a 422 ("tools[0].function.name must be a non-empty string"), and so a tool_choice that names one (ultracode audit
of the relay, C14; offline reproduction: 422 for both). Accepting the tool alone would have been worse than the refusal:
the model's call came back as a function call with the input wrapped in {"input": ...}, which a client that declared a
custom tool does not read, and the call in the next request's history was read as a call of a tool named "tool"."""
import asyncio
import copy
import json

import pytest

import adapters
import apierr
import direct
import validate
from test_adapter_protocols import Writer
from test_client_contracts import CFG, _body
from test_native_context_reuse import FIRST, request, run
from test_native_history import close, harness
from test_review_fixes_pr19 import END, THINKING, signature, text

SQL = {"type": "custom", "custom": {"name": "run_sql", "description": "Run SQL", "format": {"type": "text"}}}
READ = {"type": "function", "function": {"name": "read_file", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}}}}}
GRAMMAR = {"type": "grammar", "grammar": {"syntax": "lark", "definition": "start: /.+/"}}


def parse(**extra):
    return validate.parse_chat(_body("chat", **extra), CFG)


def test_a_custom_tool_of_chat_is_a_tool_with_the_one_string_input_the_model_is_offered():
    function = parse(tools=[SQL]).tools[0]["function"]
    assert function["name"] == "run_sql" and function["description"] == "Run SQL"
    assert function["x-client-type"] == "custom" and function["x-format"] == {"type": "text"}
    assert function["parameters"] == {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}
    assert direct.tools_to_retool([{"type": "function", "function": function}])[0]["params"] == {
        "input": {"type": "string", "required": True}}


def test_the_grammar_of_a_custom_tool_is_kept_and_it_sits_beside_function_tools():
    grammar = {"type": "custom", "custom": {"name": "calc", "format": GRAMMAR}}
    tools = parse(tools=[READ, grammar, SQL]).tools
    assert [tool["function"]["name"] for tool in tools] == ["read_file", "calc", "run_sql"]
    assert tools[1]["function"]["x-format"] == GRAMMAR and "x-client-type" not in tools[0]["function"]


@pytest.mark.parametrize("choice", [{"type": "custom", "custom": {"name": "run_sql"}},
                                    {"type": "function", "function": {"name": "run_sql"}}])
def test_a_tool_choice_may_name_a_custom_tool(choice):
    assert parse(tools=[READ, SQL], tool_choice=choice).tool_choice == {"type": "function", "function": {"name": "run_sql"}}


@pytest.mark.parametrize("bad,param", [
    ({"type": "custom", "custom": {}}, "tools[0].function.name"), ({"type": "custom", "custom": "run_sql"}, "tools[0].function.name"),
    ({"type": "custom"}, "tools[0].function.name"),
    ({"type": "custom", "custom": {"name": "x", "format": "text"}}, "tools[0].format")])
def test_a_custom_tool_that_names_nothing_or_has_no_format_object_is_still_refused(bad, param):
    with pytest.raises(apierr.ApiError) as error:
        parse(tools=[bad])
    assert error.value.status == 422 and error.value.param == param


@pytest.mark.parametrize("choice", [{"type": "custom", "custom": {"name": "missing"}}, {"type": "custom", "custom": {}},
                                    {"type": "custom", "custom": "run_sql"}])
def test_a_tool_choice_that_names_no_declared_tool_is_still_refused(choice):
    with pytest.raises(apierr.ApiError) as error:
        parse(tools=[READ, SQL], tool_choice=choice)
    assert error.value.status == 422 and error.value.param == "tool_choice"


def chat_adapter(stream=False, tools=(SQL, READ)):
    writer = Writer() if stream else None
    adapter = adapters.ChatAdapter(parse(tools=list(tools)), writer)
    adapter.begin()
    return writer, adapter


def custom_call(arguments, done=True, call_id="call_1", name="run_sql"):
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": arguments, "done": done}


SELECT = json.dumps({"input": "SELECT 1\nFROM t"})


def test_the_call_comes_back_in_the_custom_shape_beside_a_function_call():
    _, adapter = chat_adapter()
    adapter.feed({"type": "text", "delta": "Querying."})
    adapter.feed(custom_call(SELECT))
    adapter.feed({"type": "tool_call", "id": "call_2", "name": "read_file", "arguments": '{"path":"a"}', "done": True})
    choice = adapter.payload("tool_calls")["choices"][0]
    assert choice["message"]["tool_calls"] == [
        {"id": "call_1", "type": "custom", "custom": {"name": "run_sql", "input": "SELECT 1\nFROM t"}},
        {"id": "call_2", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a"}'}}]
    assert choice["message"]["content"] == "Querying." and choice["finish_reason"] == "tool_calls"


def streamed_calls(writer):
    return [item for kind, payload in writer.events if kind == "data" and isinstance(payload, dict)
            for choice in payload.get("choices") or [] for item in choice.get("delta", {}).get("tool_calls") or []]


def test_a_streamed_custom_call_goes_out_whole_once_its_input_is_whole():
    writer, adapter = chat_adapter(stream=True)
    adapter.feed(custom_call("", done=False))
    adapter.feed(custom_call('{"input": "SELECT 1\\nFR', done=False))
    assert streamed_calls(writer) == []
    adapter.feed(custom_call(SELECT, done=False))
    adapter.feed(custom_call(SELECT, done=True))
    assert streamed_calls(writer) == [
        {"index": 0, "id": "call_1", "type": "custom", "custom": {"name": "run_sql", "input": "SELECT 1\nFROM t"}}]
    final = adapter.end("tool_calls")
    assert len(streamed_calls(writer)) == 1  # not again at the end
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    finishes = [c["finish_reason"] for k, p in writer.events if k == "data" and isinstance(p, dict)
                for c in p.get("choices") or [] if c.get("finish_reason")]
    assert finishes == ["tool_calls"]


def test_a_function_call_of_the_same_turn_still_streams_by_pieces_and_keeps_its_index():
    writer, adapter = chat_adapter(stream=True)
    adapter.feed({"type": "tool_call", "id": "call_0", "name": "read_file", "arguments": '{"path":', "done": False})
    adapter.feed(custom_call(SELECT))
    adapter.feed({"type": "tool_call", "id": "call_0", "name": "read_file", "arguments": '{"path":"a"}', "done": True})
    items = streamed_calls(writer)
    assert [(item["index"], item.get("type")) for item in items] == [(0, "function"), (1, "custom"), (0, None)]
    assert items[1]["custom"]["input"] == "SELECT 1\nFROM t"
    assert items[0]["function"]["arguments"] + items[2]["function"]["arguments"] == '{"path":"a"}'


def test_a_custom_call_nobody_marked_done_is_sent_when_the_reply_ends():
    writer, adapter = chat_adapter(stream=True)
    adapter.feed(custom_call(SELECT, done=False))
    assert streamed_calls(writer) == []
    adapter.end("tool_calls")
    assert [item["custom"]["input"] for item in streamed_calls(writer)] == ["SELECT 1\nFROM t"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["timeout", "cancelled"])
def test_a_custom_call_cut_by_a_timeout_or_a_cancel_is_dropped_not_sent_half_written(reason, stream):
    writer, adapter = chat_adapter(stream=stream)
    adapter.feed(custom_call('{"input": "SELECT 1\\nFR', done=False))
    final = adapter.end(reason)
    assert "tool_calls" not in final["choices"][0]["message"]
    if stream:
        assert streamed_calls(writer) == []


@pytest.mark.parametrize("stream", [False, True])
def test_a_custom_call_the_output_stopped_inside_is_dropped_and_the_reply_ends_with_length(stream):
    # The output limit (GPT's max_completion_tokens, the relay's cap on Claude) or a stream that ended inside the call
    # ends the turn as "length". That raised 502 upstream_error "Retool returned incomplete custom tool input", in a
    # stream an error event with no finish_reason: an error for a reply that only reached its limit, which a function
    # call of the same cut, or Responses' custom call, ends with as "length" (incomplete).
    writer, adapter = chat_adapter(stream=stream)
    adapter.feed({"type": "text", "delta": "Querying."})
    adapter.feed(custom_call('{"input": "SELECT 1\\nFR', done=False))
    choice = adapter.end("length")["choices"][0]
    assert choice["finish_reason"] == "length" and "tool_calls" not in choice["message"]
    assert choice["message"]["content"] == "Querying."
    if stream:
        assert streamed_calls(writer) == []  # no half of the input reaches the client
        finishes = [c["finish_reason"] for k, p in writer.events if k == "data" and isinstance(p, dict)
                    for c in p.get("choices") or [] if c.get("finish_reason")]
        assert finishes == ["length"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["tool_calls", "stop"])
def test_a_cut_input_of_a_turn_upstream_completed_is_an_upstream_error(reason, stream):
    _, adapter = chat_adapter(stream=stream)
    adapter.feed(custom_call('{"input": "SELECT 1\\nFR'))
    with pytest.raises(apierr.ApiError) as error:
        adapter.end(reason)
    assert error.value.status == 502


def test_the_model_writing_the_input_without_the_envelope_is_passed_on_as_it_wrote_it():
    _, adapter = chat_adapter()
    adapter.feed(custom_call("SELECT 2"))
    assert adapter.payload("tool_calls")["choices"][0]["message"]["tool_calls"][0]["custom"]["input"] == "SELECT 2"


def echoed(call_input="SELECT 1\nFROM t"):
    return [{"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "custom", "custom": {"name": "run_sql", "input": call_input}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "1 row"}]


def test_a_custom_call_in_the_history_keeps_its_name_and_input_on_the_text_bridge_and_the_native_view():
    parsed = parse(messages=echoed(), tools=[SQL])
    assert parsed.message_chunks[1] == 'Assistant: \n[tool_call run_sql id=call_1: {"input": "SELECT 1\\nFROM t"}]'
    assert parsed.message_chunks[2] == "[tool result call_1 id=call_1]: 1 row"
    assert parsed.native_messages[1] == {"role": "assistant", "content": [
        {"type": "tool_call", "toolCallId": "call_1", "toolName": "run_sql", "toolInput": {"input": "SELECT 1\nFROM t"}}]}
    assert parsed.native_messages[2]["content"]["toolCallId"] == "call_1"


def test_the_next_request_goes_on_from_the_signed_reply_when_the_client_echoes_the_custom_call(tmp_path, monkeypatch):
    reply = [*THINKING, signature("sig-sql"), {"event": "content_block_end", "type": "thinking"},
             {"event": "content_block_start", "type": "tool_call", "toolCallId": "call_1", "toolName": "run_sql"},
             {"event": "content_block_delta", "type": "tool_call", "partialInputs": SELECT},
             {"event": "content_block_end", "type": "tool_call", "toolCallId": "call_1"}, *END]
    pool, runner, sent = harness(tmp_path, monkeypatch, [reply, [text("done"), *END]])
    seen = {}

    async def scenario():
        try:
            first = request(copy.deepcopy(FIRST), dialect="chat", tools=[SQL])
            events = await run(runner, first)
            adapter = adapters.ChatAdapter(first, None)
            for event in events:
                adapter.feed(event)
            message = adapter.payload("tool_calls")["choices"][0]["message"]
            seen["call"] = message["tool_calls"]
            messages = copy.deepcopy(FIRST) + [message, {"role": "tool", "tool_call_id": "call_1", "content": "1 row"}]
            await run(runner, request(messages, dialect="chat", tools=[SQL]))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert seen["call"] == [{"id": "call_1", "type": "custom", "custom": {"name": "run_sql", "input": "SELECT 1\nFROM t"}}]
    assert len(sent) == 2 and "sig-sql" in json.dumps(sent[1])  # resumed natively with the signed reply
    replayed = next(m for m in sent[1]["messages"] if m["role"] == "assistant")
    assert next(b for b in replayed["content"] if b["type"] == "tool_call")["toolInput"] == {"input": "SELECT 1\nFROM t"}
