import asyncio
import json
import queue
import types

import models
import validate
from orchestrator import TurnRunner
from session import SessionPool


CFG = {"strict_models": True, "model": "claude-opus-5-5"}


def test_tools_and_tool_choice_are_retained():
    parsed = validate.parse_chat({
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "read"}],
        "tools": [{"type": "function", "function": {
            "name": "read_file", "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }}],
        "tool_choice": {"type": "function", "function": {"name": "read_file"}},
    }, CFG)
    assert parsed.tools[0]["function"]["name"] == "read_file"
    assert parsed.tool_choice["function"]["name"] == "read_file"


def test_anthropic_tool_choice_forms_are_normalized():
    for raw, expected in (({"type": "auto"}, "auto"),
                          ({"type": "any"}, "required"),
                          ({"type": "tool", "name": "read_file"}, "read_file")):
        parsed = validate.parse_anthropic({
            "model": "claude-opus-5-5", "max_tokens": 32,
            "messages": [{"role": "user", "content": "read"}],
            "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
            "tool_choice": raw,
        }, CFG)
        value = parsed.tool_choice
        actual = value if isinstance(value, str) else value["function"]["name"]
        assert actual == expected


class ToolSession:
    thread_id = "t1"

    async def stream(self, prompt, model_provider, tool_names=None):
        assert tool_names == ["bash"]
        yield {"type": "final_text", "text":
               '<tool_call>{"id":"call_1","name":"bash","arguments":{"command":"dir"}}</tool_call>'}
        yield {"type": "end", "reason": "stop"}


class Pool:
    async def acquire(self, conversation_key=None, *, isolated=False, model_entry=None):
        return ToolSession()

    async def release(self, session):
        return None


def test_tool_bridge_returns_native_call_without_local_execution(tmp_path):
    parsed = validate.parse_chat({
        "model": "gpt-5.5", "messages": [{"role": "user", "content": "list files"}],
        "tools": [{"type": "function", "function": {
            "name": "bash", "parameters": {"type": "object"}}}],
    }, CFG)
    parsed.conversation_key = "header:session-a"
    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "allowlist",
           "exec_allowlist": ["^dir\\b"], "max_exec_turns": 2, "command_timeout": 5,
           "command_output_limit": 500, "show_exec_output": True, "cookie_values": [],
           "context_max_bytes": 300, "max_prompt_bytes": 5000,
           "provider_resource_name": "a", "openai_provider_resource_name": "o"}
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *a: None})(), Pool())
    out = queue.Queue()
    asyncio.run(runner.run(parsed, out, {}))
    events = []
    while not out.empty():
        events.append(out.get())
    call = next(e for e in events if e and e.get("type") == "tool_call")
    assert call["name"] == "bash" and json.loads(call["arguments"])["command"] == "dir"
    assert any(e and e.get("type") == "end" and e.get("reason") == "tool_calls" for e in events)


class ThreadBackend:
    def __init__(self):
        self.cfg = {"session_mode": "per_conversation", "thread_id": "legacy",
                    "page_uuid": "p", "max_workers": 4, "base": "https://x",
                    "upstream_timeout": 5}
        self.created = []

    async def create_thread(self):
        value = "thread-" + str(len(self.created) + 1)
        self.created.append(value)
        return value


def test_conversation_pool_reuses_one_thread_per_affinity_key():
    backend = ThreadBackend()
    pool = SessionPool(backend)

    async def scenario():
        a1 = await pool.acquire("header:a")
        await pool.release(a1)
        b = await pool.acquire("header:b")
        await pool.release(b)
        a2 = await pool.acquire("header:a")
        await pool.release(a2)
        return a1, b, a2

    a1, b, a2 = asyncio.run(scenario())
    assert a1 is not a2
    assert a1.session is a2.session
    assert a1.thread_id == a2.thread_id
    assert a1.thread_id != b.thread_id
    assert backend.created == ["thread-1", "thread-2"]


def test_conversation_key_prefers_opencode_affinity_header():
    key_a = validate.conversation_key({"model": "gpt-5.5", "messages": []}, "session-a")
    key_b = validate.conversation_key({"model": "gpt-5.5", "messages": []}, "session-b")
    assert key_a != key_b and key_a.startswith("header:")
