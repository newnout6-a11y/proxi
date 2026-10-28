"""DeepSeek Harness llm-pi-ai Chat Completions request and stream regressions."""
import json

import validate

CFG = {"strict_models": True, "model": "gpt-6-sol", "show_thinking": True}
GOAL_TOOLS = [
    {"type": "function", "function": {
        "name": "get_goal", "description": "Read the current client-owned goal.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "create_goal", "description": "Create the client-owned completion goal.",
        "parameters": {"type": "object", "properties": {"objective": {"type": "string"}},
                       "required": ["objective"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "update_goal", "description": "Update the client-owned goal by revision.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}, "revision": {"type": "integer"},
            "action": {"type": "string", "enum": ["edit", "pause", "resume", "complete", "blocked"]}},
            "required": ["id", "revision", "action"], "additionalProperties": False},
    }},
]


def harness_request(**overrides):
    """Fields emitted by installed Harness 0.1.5-rc.2 through pi-ai."""
    return dict({
        "model": "gpt-6-sol", "stream": True, "stream_options": {"include_usage": True},
        "thinking": {"type": "enabled"}, "reasoning_effort": "low", "max_tokens": 512,
        "messages": [
            {"role": "system", "content": "The client owns the workspace and goal state."},
            {"role": "user", "content": "Read the current goal using get_goal."},
        ],
        "tools": GOAL_TOOLS,
    }, **overrides)


def _chunks(body):
    return [json.loads(line[6:]) for line in body.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


def test_harness_request_preserves_goal_schemas_context_and_actual_effort():
    parsed = validate.parse_chat(harness_request(), CFG)
    assert parsed.effort == "low"
    assert parsed.stream and parsed.wants_usage and parsed.show_thinking
    assert parsed.system == "The client owns the workspace and goal state."
    assert parsed.tools == GOAL_TOOLS
    assert parsed.tool_choice == "auto"


def test_harness_off_wire_toggle_maps_to_none_instead_of_default_effort():
    # Installed pi-ai emits thinking.disabled and omits reasoning_effort for
    # a model configured with selectable off: none.
    body = harness_request(thinking={"type": "disabled"})
    del body["reasoning_effort"]
    assert validate.parse_chat(body, CFG).effort == "none"


def test_same_relay_key_supports_harness_bearer_and_anthropic_clients(relay):
    chat = harness_request(stream=False)
    assert relay.status_of("/v1/chat/completions", chat) == 200
    anthropic = {"model": "claude-opus-5-5", "max_tokens": 512,
                 "messages": [{"role": "user", "content": "hello"}]}
    assert relay.status_of("/v1/messages", anthropic, key=None,
                           headers={"x-api-key": relay.key}) == 200


def test_harness_receives_reasoning_as_its_own_stream_before_goal_call(relay):
    relay.fake.script = [
        {"type": "thinking", "delta": "Provider-supplied reasoning summary."},
        {"type": "tool_call", "id": "goal_1", "name": "get_goal", "arguments": "{}"},
        {"type": "end", "reason": "tool_calls"},
    ]
    status, body = relay.sse("/v1/chat/completions", harness_request())
    assert status == 200
    chunks = _chunks(body)
    deltas = [item["choices"][0]["delta"] for item in chunks if item["choices"]]
    reasoning_index = next(i for i, delta in enumerate(deltas) if "reasoning_content" in delta)
    tool_index = next(i for i, delta in enumerate(deltas) if "tool_calls" in delta)
    assert reasoning_index < tool_index
    assert deltas[reasoning_index] == {"reasoning_content": "Provider-supplied reasoning summary."}
    call = deltas[tool_index]["tool_calls"][0]
    assert call["id"] == "goal_1" and call["function"]["name"] == "get_goal"
    assert call["function"]["arguments"] == "{}"
    assert any(item["choices"] and item["choices"][0]["finish_reason"] == "tool_calls" for item in chunks), body
    assert any(item["choices"] == [] and "usage" in item for item in chunks)
    assert body.rstrip().endswith("data: [DONE]")


def test_harness_goal_results_and_revision_survive_the_next_turn():
    output = json.dumps({"goal": {"id": "goal-client-1", "revision": 17,
                                 "objective": "x" * 2000 + "END_OF_OBJECTIVE", "phase": "active"}})
    body = harness_request(messages=[
        {"role": "user", "content": "Read the goal."},
        {"role": "assistant", "content": None, "reasoning_content": "previous private summary",
         "tool_calls": [{"id": "goal_1", "type": "function", "function": {
             "name": "get_goal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "goal_1", "content": output},
        {"role": "user", "content": "Continue work on this goal."},
    ])
    parsed = validate.parse_chat(body, CFG)
    assert "END_OF_OBJECTIVE" in parsed.prompt
    assert '"revision": 17' in parsed.prompt
    assert parsed.prompt.count("id=goal_1") == 2
    assert "previous private summary" not in parsed.prompt
