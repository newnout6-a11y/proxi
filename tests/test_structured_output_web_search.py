"""A JSON reply format is checked after the relay's own web_search too.

An Anthropic request can declare the web_search server tool, which the relay runs itself, beside output_config.format.
The structured check releases a turn that calls a tool as it is and validates only a reply without calls: a call of a
client tool ends the response, and the answer comes in the reply to the client's next request. The search call ended the
check the same way, though the relay answers that call itself and the reply goes on in the same response: the prose the
model wrote after the search reached the client with 200 and x-proxi-output-format: validated-json-schema (a fenced answer
kept its fence), where a reply that breaks the format is a 502 upstream_invalid_output, or an SSE error once a stream has
sent its 200. The search is a step of the reply: what came before it goes out as it is, and what the model writes after
the last search is the answer that is validated."""
import json

import pytest

import adapters
from prompt_support import prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_structured_output import Writer, emitted_text, sse_events
from test_web_search import FakeSearch, parse
from test_web_search_loop import answers, asks

POPULATION = {"type": "object", "properties": {"city": {"type": "string"}, "population": {"type": "integer"}},
              "required": ["city", "population"], "additionalProperties": False}
VALID = '{"city":"Paris","population":2100000}'
PROSE = "Paris has about 2.1 million inhabitants."
FORMAT = {"output_config": {"format": {"type": "json_schema", "schema": POPULATION}}}
SERVER = {"type": "web_search_20250305", "name": "web_search"}
READ = {"name": "read_file", "description": "Read the client's file", "input_schema": {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}


def ask(prompt_relay, answer, stream):
    """One request that searches first: the model says it will search, calls web_search, then writes `answer`."""
    search = FakeSearch()
    relay = prompt_relay(_web_search=search)
    relay.scripts.extend([asks("population of Paris", words="Let me search."), answers(answer)])
    body = {"model": "claude-opus-5-5", "max_tokens": 8192, "stream": stream, "tools": [dict(SERVER)],
            "messages": [{"role": "user", "content": "How many people live in Paris?"}], **FORMAT}
    status, payload, headers = relay.post("anthropic", body)
    # the search ran and the reply went on after it in the same response
    assert [query for query, _ in search.calls] == ["population of Paris"] and len(relay.posts) == 2
    return status, payload, headers


def block_kinds(blocks):
    return [block["type"] for block in blocks if block["type"] != "thinking"]


def streamed(raw):
    """(the events of a stream, the types of the content blocks it opened, the text it sent)."""
    events = sse_events(raw)
    opened = [payload["content_block"] for name, payload in events if name == "content_block_start"]
    sent = "".join(payload["delta"]["text"] for name, payload in events
                   if name == "content_block_delta" and payload["delta"]["type"] == "text_delta")
    return events, block_kinds(opened), sent


@pytest.mark.parametrize("stream", [False, True])
def test_prose_after_the_relay_s_search_is_refused_like_any_reply_that_breaks_the_format(prompt_relay, stream):
    status, payload, headers = ask(prompt_relay, PROSE, stream)
    if stream:
        events, opened, sent = streamed(payload)
        names = [name for name, _ in events]
        # the 200 is out: the step that searched went out as it is, the answer that breaks the format never does
        assert status == 200 and names[-1] == "error" and "message_stop" not in names
        assert opened == ["text", "server_tool_use", "web_search_tool_result"] and sent == "Let me search."
        failure = events[-1][1]["error"]
    else:
        assert status == 502 and headers["x-should-retry"] == "false"
        failure = payload["error"]
    assert failure["type"] == "api_error" and failure["relay_code"] == "upstream_invalid_output"
    assert "upstream reply does not satisfy output_config.format (JSON Schema)" in failure["message"]
    assert "2.1 million" not in json.dumps(payload)


@pytest.mark.parametrize("stream", [False, True])
def test_a_json_answer_after_the_search_is_delivered_validated_and_without_its_fence(prompt_relay, stream):
    status, payload, headers = ask(prompt_relay, "```json\n" + VALID + "\n```", stream)
    assert status == 200 and headers["x-proxi-output-format"] == "validated-json-schema"
    if stream:
        events, opened, sent = streamed(payload)
        assert [name for name, _ in events][-1] == "message_stop" and "error" not in dict(events)
        assert opened == ["text", "server_tool_use", "web_search_tool_result", "text"]
        assert sent == "Let me search." + VALID
    else:
        assert block_kinds(payload["content"]) == ["text", "server_tool_use", "web_search_tool_result", "text"]
        assert payload["content"][-1]["text"] == VALID and payload["stop_reason"] == "end_turn"


def test_the_text_after_a_search_waits_for_the_check_and_a_call_of_a_client_tool_still_releases_it():
    writer = Writer()
    adapter = adapters.for_dialect("anthropic", parse(FakeSearch(), tools=[dict(SERVER), READ], **FORMAT), writer)
    adapter.begin()
    for event in ({"type": "text", "delta": "Let me search."},
                  {"type": "tool_call", "id": "s1", "name": "web_search", "arguments": '{"query": "q"}',
                   "tool_type": "function", "server": "web_search", "done": True},
                  {"type": "server_tool_result", "tool_use_id": "s1", "content": []},
                  {"type": "text", "delta": "Now the file."}):
        adapter.feed(event)
    assert emitted_text("anthropic", writer.events) == "Let me search."
    adapter.feed({"type": "tool_call", "id": "c1", "name": "read_file", "arguments": '{"path": "a"}', "done": True})
    # a turn the client continues is no final answer: it goes out as the model wrote it
    assert emitted_text("anthropic", writer.events) == "Let me search.Now the file."
    final = adapter.end("tool_calls")
    assert block_kinds(final["content"]) == ["text", "server_tool_use", "web_search_tool_result", "text", "tool_use"]
    assert final["stop_reason"] == "tool_use"
