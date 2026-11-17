"""Offline checks for the direct transport helpers and cached-head body."""
import asyncio
import json

import pytest

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


@pytest.mark.parametrize(("length", "expected"), [
    (0, "00000000"),
    (1, "9e3779b1"),
    (2, "3c6ef362"),
    (3_393_262, "3df5088e"),
    (3_393_263, "dc2c823f"),
    (3_393_264, "7a63fbf0"),
    (3_393_265, "189b75a0"),
    (3_394_410, "bebabe4a"),
    (3_749_929, "91ea3558"),
    (3_750_726, "24a01166"),
    (3_754_257, "6bcb8dc0"),
    (32_000_001, "31d941b0"),
])
def test_hash32_matches_javascript_number_rounding(length, expected):
    # Golden values from ((length * 2654435761) >>> 0).toString(16).
    # Cover the Number precision boundary and lengths of rejected/successful turns.
    assert direct._hash32(length) == expected


@pytest.mark.parametrize(("length", "expected"), [
    (3_394_410, "bebabe4a3d1241f7"),
    (3_749_929, "91ea35583d1241f7"),
    (3_750_726, "24a011663d1241f7"),
    (3_754_257, "6bcb8dc03d1241f7"),
])
def test_large_body_rit_matches_browser_without_changing_the_prompt(length, expected):
    # Synthetic content with the same serialized lengths as incident requests;
    # no private conversation, image or tool definitions belong in a fixture.
    compact = lambda value: json.dumps(value, separators=(",", ":"))
    tools = [{"name": "inspect", "description": "", "params": {}, "strict": True}]
    tools[0]["description"] = "x" * (14_375 - len(compact(tools)))
    prompt = "x" * (length - len(compact([{"role": "user", "content": ""}])))
    body = direct.build_body({"direct_cache": False}, {"model": "claude-opus-5-5"},
                             "", prompt, tools, "auto", 16)
    assert len(compact(body["messages"])) == length
    assert body["messages"] == [{"role": "user", "content": prompt}]
    assert body["tools"] == tools
    assert body["rit"] == expected


def test_tools_are_converted_to_the_retool_param_shape():
    converted = direct.tools_to_retool(TOOLS)
    assert converted == [{"name": "read_file", "description": "Read a file",
                          "params": {"path": {"type": "string", "required": True}}, "strict": False}]


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
    assert body["openAICacheKey"] == direct.cache_key_for(head, "m", None)
    assert body["metadata"] == {"sandpackAgent": True}
    assert body["reasoningEffort"] == "low"
    assert body["maxCompletionTokens"] == 32


def test_short_head_is_cached_with_stable_key():
    body = direct.build_body({"direct_cache": True}, {"model": "m"}, "short", "tail", None, "auto", None)
    assert body["messages"][0] == {"role": "system", "content": [{"type": "text", "text": "short", "cache": True}]}
    assert body["openAICacheKey"] == direct.cache_key_for("short", "m", None)


def test_cache_disabled_omits_cache_markers_and_key():
    body = direct.build_body({"direct_cache": False}, {"model": "m"}, "short", "tail", None, "auto", None)
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


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("message", ["invalid x-api-key", "authentication_error: invalid key"])
@pytest.mark.parametrize("nested", [False, True])
def test_managed_provider_key_failure_is_not_a_retool_session_failure(status, message, nested):
    error = direct._error_from_event({"statusCode": status, "isUsingRetoolManagedKey": True,
                                      "error": {"message": message} if nested else message})
    assert (error.status, error.code, error.message) == (502, "upstream_error", message)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("managed", [False, None, True])
def test_session_failures_keep_auth_classification_unless_the_managed_provider_key_failed(status, managed):
    # A normal Retool session rejection stays actionable even when it used a managed model route; a user's own
    # provider key is not silently reclassified as Retool's managed-key outage either.
    message = "session expired" if managed else "invalid x-api-key"
    event = {"statusCode": status, "error": message}
    if managed is not None:
        event["isUsingRetoolManagedKey"] = managed
    error = direct._error_from_event(event)
    assert (error.status, error.code) == (502, "upstream_auth_error")

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


def test_drain_surfaces_silent_thinking_start():
    import asyncio as _asyncio

    class _S:
        _cancelled = False

    async def collect():
        q = _asyncio.Queue()
        for line in ('{"event":"content_block_start","type":"thinking"}',
                     '{"event":"content_block_delta","type":"thinking","partialThinking":""}',
                     '{"event":"content_block_delta","type":"signature","partialSignature":"sig"}',
                     '{"event":"content_block_end"}',
                     '{"event":"content_block_start","type":"text"}',
                     '{"event":"content_block_delta","type":"text","partialText":"42"}'):
            q.put_nowait(line)
        q.put_nowait(None)
        return [e async for e in direct._drain({}, _S(), q, [], [])]

    kinds = [e["type"] for e in _asyncio.run(collect())]
    assert kinds == ["thinking_start", "thinking_signature", "text"]


def test_anthropic_adapter_opens_thinking_block_before_text():
    import adapters

    class W:
        def __init__(self):
            self.events = []

        def event(self, name, payload):
            self.events.append((name, payload))

    parsed = type("P", (), {"show_thinking": True, "model": "claude-opus-5-5", "system": "", "prompt": "x",
                            "tools": []})()
    writer = W()
    adapter = adapters.AnthropicAdapter(parsed, writer)
    adapter.feed({"type": "thinking_start"})
    adapter.feed({"type": "thinking_signature", "signature": "sig"})
    adapter.feed({"type": "text", "delta": "42"})
    starts = [p["content_block"]["type"] for n, p in writer.events if n == "content_block_start"]
    assert starts == ["thinking", "text"]
    sig = [p for n, p in writer.events if n == "content_block_delta" and p["delta"]["type"] == "signature_delta"]
    assert sig and sig[0]["index"] == 0
    assert writer.events[0][0] == "content_block_start"


def test_thinking_start_is_hidden_when_thinking_disabled():
    import adapters

    class W:
        def __init__(self):
            self.events = []

        def event(self, name, payload):
            self.events.append((name, payload))

    parsed = type("P", (), {"show_thinking": False, "model": "m", "system": "", "prompt": "x", "tools": []})()
    writer = W()
    adapter = adapters.AnthropicAdapter(parsed, writer)
    adapter.feed({"type": "thinking_start"})
    adapter.feed({"type": "text", "delta": "ok"})
    assert [p["content_block"]["type"] for n, p in writer.events if n == "content_block_start"] == ["text"]


def test_direct_session_stream_tool_calls_marks_completed():
    import session as session_mod

    class FakeBackend:
        cfg = {"transport": "direct", "session_mode": "per_conversation", "thread_id": "t1"}

    sess = session_mod.RelaySession(FakeBackend(), "thread-1")

    async def fake_iter(*args, **kwargs):
        yield {"type": "tool_call", "id": "call_1", "name": "test", "arguments": "{}"}
        yield {"type": "end", "reason": "tool_calls"}

    sess._stream_turn = fake_iter

    async def run():
        events = []
        async for e in sess.stream("prompt", {"model": "claude-opus-5-5"}):
            events.append(e)
        return events

    evs = asyncio.run(run())
    assert len(evs) == 2
    assert not sess._needs_drain
