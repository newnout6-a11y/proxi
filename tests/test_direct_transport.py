"""Offline checks for the direct transport helpers and cached-head body."""
import asyncio
import json

import config
import direct
import validate
from orchestrator import TurnRunner  # noqa: F401  (imported for the integration-style check)


TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


def test_rit_matches_javascript_lengths():
    messages = [{"role": "user", "content": "Привет"}]
    payload = json.dumps(messages, separators=(",", ":"), ensure_ascii=False)
    length = len(payload.encode("utf-16-le")) // 2
    assert direct.make_rit(messages) == direct._hash32(length) + direct._hash32(len("[]"))
    assert direct.make_rit(messages) != direct.make_rit(messages, TOOLS)


def test_rit_counts_astral_characters_as_two_units():
    plain = [{"role": "user", "content": "x"}]
    astral = [{"role": "user", "content": "x\U0001F600"}]
    # A surrogate pair is two UTF-16 units in JavaScript, one code point in Python.
    assert direct._js_length(astral) - direct._js_length(plain) == 2


def test_tools_are_converted_to_the_retool_param_shape():
    converted = direct.tools_to_retool(TOOLS)
    assert converted == [{"name": "read_file", "description": "Read a file",
                          "params": {"path": {"type": "string", "required": True}}, "strict": True}]


VALID_TYPES = {"literal", "string", "number", "boolean", "null", "array", "object", "enum", "anyOf"}


def test_unsupported_schemas_degrade_to_a_valid_retool_type():
    converted = direct.tools_to_retool([{"type": "function", "function": {
        "name": "x", "parameters": {"type": "object", "properties": {
            "q": {"$ref": "#/x"},
            "union": {"type": ["string", "null"]},
            "freeform": {}}}}}])
    assert converted[0]["params"]["q"] == {"type": "string", "required": False}
    assert converted[0]["params"]["union"]["type"] == "string"
    assert {param["type"] for param in converted[0]["params"].values()} <= VALID_TYPES


def test_nested_object_and_array_keep_their_shape():
    converted = direct.tools_to_retool([{"type": "function", "function": {
        "name": "x", "parameters": {"type": "object", "properties": {
            "cfg": {"type": "object", "properties": {"deep": {"type": "number"}}, "required": ["deep"]},
            "list": {"type": "array", "items": {"type": "boolean"}}}}}}])
    assert converted[0]["params"]["cfg"]["properties"]["deep"] == {"type": "number", "required": True}
    assert converted[0]["params"]["list"]["items"] == {"type": "boolean", "required": False}


def test_head_becomes_a_cached_system_block_with_a_stable_key():
    cfg = {"direct_cache": True}
    head = "stable instructions " * 400
    body = direct.build_body(cfg, {"model": "m", "reasoningEffort": "low"}, head, "tail", None, "auto", 32)
    assert body["messages"][0]["content"][0]["cache"] is True
    assert body["messages"][1] == {"role": "user", "content": "tail"}
    assert body["openAICacheKey"] == direct.cache_key_for(head)
    assert body["metadata"] == {"sandpackAgent": True}
    assert body["reasoningEffort"] == "low"
    assert body["maxCompletionTokens"] == 32


def test_short_head_is_not_cached_but_still_a_system_message():
    body = direct.build_body({"direct_cache": True}, {"model": "m"}, "short", "tail", None, "auto", None)
    assert body["messages"][0] == {"role": "system", "content": [{"type": "text", "text": "short"}]}
    assert "openAICacheKey" not in body


def test_output_floor_and_tool_choice_passthrough():
    body = direct.build_body({}, {"model": "m"}, "", "tail", direct.tools_to_retool(TOOLS),
                             {"type": "function", "function": {"name": "read_file"}}, 4)
    assert body["maxCompletionTokens"] == direct.MIN_OUTPUT_TOKENS
    assert body["toolChoice"] == "read_file"


def test_auto_choice_is_not_sent_but_required_is():
    tools = direct.tools_to_retool(TOOLS)
    assert "toolChoice" not in direct.build_body({}, {"model": "m"}, "", "t", tools, "auto", None)
    assert direct.build_body({}, {"model": "m"}, "", "t", tools, "required", None)["toolChoice"] == "required"


def test_stream_errors_map_to_relay_statuses():
    assert direct._error_from_event({"error": {"message": "x"}, "statusCode": 429}).status == 429
    assert direct._error_from_event({"error": "x", "statusCode": 404}).status == 400
    assert direct._error_from_event({"error": "x"}).status == 502

class DirectSession:
    """Session double that records what the direct transport would receive."""

    thread_id = "direct-1"

    def __init__(self):
        self.heads = []
        self.chunks = []
        self.calls = []

    def set_direct(self, head, tools, tool_choice, max_tokens, chunks=None):
        self.heads.append(head)
        self.chunks.append(chunks or [])

    async def stream(self, prompt, provider, tool_names=None):
        self.calls.append(prompt)
        if len(self.calls) == 1:
            yield {"type": "tool_call", "id": "c1", "name": "read_file", "arguments": '{"path":"a"}'}
        else:
            yield {"type": "text", "delta": "done"}
        yield {"type": "end", "reason": "tool_calls" if len(self.calls) == 1 else "stop"}

    def cancel(self):
        pass


class DirectPool:
    def __init__(self, session):
        self.session = session

    async def acquire(self, *args, **kwargs):
        return self.session

    async def release(self, session):
        pass


def test_direct_transport_keeps_instructions_in_the_head_and_resends_full_history(tmp_path):
    import queue
    from orchestrator import TurnRunner

    cfg = dict(config.DEFAULTS, workspace_root=str(tmp_path), cookie_values=[],
               transport="direct", project_context=False, provider_resource_name="a",
               openai_provider_resource_name="o")
    history = [{"role": "user", "content": "FIRST_TASK_MARKER"}]
    parsed = validate.parse_chat({
        "model": "claude-opus-5-5", "messages": history,
        "tools": [{"type": "function", "function": {
            "name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}],
    }, cfg)
    parsed.conversation_key = "header:d"
    session = DirectSession()
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *_: None})(), DirectPool(session))
    out = queue.Queue()
    asyncio.run(runner.run(parsed, out, {}))

    # The stable head carries the instructions, the history travels as chunks.
    assert "[ROLE]" in session.heads[0]
    assert "FIRST_TASK_MARKER" not in session.heads[0]
    assert any("FIRST_TASK_MARKER" in chunk for chunk in session.chunks[0])
    assert len(session.chunks[0]) == 1

def test_stringly_typed_numbers_and_booleans_are_repaired():
    params = {"offset": {"type": "number"}, "limit": {"type": "number"},
              "flag": {"type": "boolean"}, "name": {"type": "string"}}
    fixed = direct.coerce_arguments('{"offset": "1", "limit": "30", "flag": "true", "name": "7"}', params)
    assert json.loads(fixed) == {"offset": 1, "limit": 30, "flag": True, "name": "7"}


def test_coercion_keeps_unparsable_arguments_verbatim():
    assert direct.coerce_arguments("not json", {"a": {"type": "number"}}) == "not json"
    assert direct.coerce_arguments("", {}) == "{}"
