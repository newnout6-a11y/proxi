"""Codex through the relay: namespaced tools get names upstream accepts, and an input past the context window is final.

Codex 0.166 sends its MCP servers as namespaces ("mcp__blender" holding "look"). The relay sent them upstream as
"mcp__blender.look", and OpenAI refused every turn of a GPT model: "Invalid 'tools[6].name': string does not match
pattern. Expected a string that matches the pattern '^[a-zA-Z0-9_-]+$'". A memory job of Codex sent 3 MB to
gpt-5.6-luna, the provider answered "Your input exceeds the context window of this model", and the relay passed that on
as a temporary 502, which Codex asks again up to five times."""
import json

import pytest

import adapters
import apierr
import direct
import tool_names
import validate
from test_adapter_protocols import Writer
from test_client_contracts import CFG, _body

PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"
CODEX_TOOLS = [
    {"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
    {"type": "namespace", "name": "mcp__blender", "tools": [
        {"type": "function", "name": "look", "parameters": {"type": "object"}},
        {"type": "function", "name": "disable_telemetry", "parameters": {"type": "object"}}]},
    {"type": "namespace", "name": "mcp__codex_tui", "tools": [
        {"type": "function", "name": "get_worktree_creation_status", "parameters": {"type": "object"}}]},
    {"type": "namespace", "name": "multi_agent_v1", "tools": [
        {"type": "function", "name": "spawn_agent", "parameters": {"type": "object"}}]},
]


def test_every_tool_codex_sends_gets_a_name_upstream_accepts():
    parsed = validate.parse_responses(_body("responses", tools=CODEX_TOOLS), CFG)
    names = [tool["function"]["name"] for tool in parsed.tools]
    assert names == ["exec_command", "mcp__blender__look", "mcp__blender__disable_telemetry",
                     "mcp__codex_tui__get_worktree_creation_status", "multi_agent_v1__spawn_agent"]
    import re
    assert all(re.fullmatch(PATTERN, name) for name in names)
    upstream = [tool["name"] for tool in direct.tools_to_retool(parsed.tools)]
    assert all(re.fullmatch(PATTERN, name) for name in upstream)


def test_a_long_or_odd_name_is_shortened_and_cleaned_but_stays_unique():
    import re
    long_a = tool_names.namespaced("mcp__a_server_with_a_really_long_name", "and_a_tool_with_an_even_longer_name_a")
    long_b = tool_names.namespaced("mcp__a_server_with_a_really_long_name", "and_a_tool_with_an_even_longer_name_b")
    assert len(long_a) == 64 and long_a != long_b and re.fullmatch(PATTERN, long_a)
    assert tool_names.namespaced("my.server", "do it") == "my_server__do_it"


def test_a_call_of_a_namespaced_tool_reaches_codex_with_its_namespace_and_short_name():
    parsed = validate.parse_responses(_body("responses", tools=CODEX_TOOLS), CFG)
    writer = Writer()
    adapter = adapters.ResponsesAdapter(parsed, writer)
    adapter.begin()
    adapter.feed({"type": "tool_call", "id": "c1", "name": "mcp__blender__look", "arguments": "{}"})
    final = adapter.end("tool_calls")
    call = next(item for item in final["output"] if item["type"] == "function_call")
    assert (call["namespace"], call["name"], call["call_id"]) == ("mcp__blender", "look", "c1")


def test_the_history_and_the_tool_choice_name_the_same_upstream_tool():
    parsed = validate.parse_responses(_body("responses", tools=CODEX_TOOLS,
                                            tool_choice={"type": "function", "namespace": "mcp__blender", "name": "look"},
                                            input=[{"type": "function_call", "namespace": "mcp__blender", "name": "look",
                                                    "call_id": "c1", "arguments": "{}"},
                                                   {"type": "function_call_output", "call_id": "c1", "output": "ok"}]),
                                     CFG)
    assert parsed.tool_choice["function"]["name"] == "mcp__blender__look"
    assert "mcp__blender__look" in parsed.prompt and "mcp__blender.look" not in parsed.prompt


@pytest.mark.parametrize("event", [
    {"event": "error", "error": "Your input exceeds the context window of this model. Please adjust your input and try again."},
    {"event": "error", "statusCode": 400, "error": {"message": "prompt is too long: 240000 tokens > 200000 maximum"}},
    {"event": "error", "statusCode": 400, "error": "This model's maximum context length is 400000 tokens."},
])
def test_an_input_past_the_context_window_is_a_final_context_length_exceeded(event):
    error = direct._error_from_event(event)
    assert (error.status, error.code) == (400, "context_length_exceeded")
    assert error.responses_error()["code"] == "context_length_exceeded"      # Codex stops, without five retries
    assert error.anthropic_envelope()["error"]["type"] == "invalid_request_error"
    assert error.openai_envelope()["error"]["code"] == "context_length_exceeded"


def test_other_upstream_errors_keep_their_codes():
    assert direct._error_from_event({"event": "error", "error": "boom"}).code == "upstream_error"
    assert direct._error_from_event({"event": "error", "statusCode": 400, "error": "bad tool"}).code == "invalid_request_error"
    # a 429 that happens to mention the context window is still a rate limit
    assert direct._error_from_event({"event": "error", "statusCode": 429,
                                     "error": "exceeds the context window quota"}).status == 429
