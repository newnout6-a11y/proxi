"""Tests for OpenCode cache reporting, subagent concurrency, thinking heartbeats, and multi-account pool."""
import asyncio
import json
import queue
import time
import pytest

import adapters
import config
import direct
import journal
import session as session_mod
import validate
from orchestrator import TurnRunner
import upstream_refresh


# --- 1. OpenCode Cache & Reasoning Reporting Tests ---

def test_chat_adapter_cache_and_reasoning_details():
    parsed = type("Parsed", (), {
        "model": "claude-opus-5-5",
        "show_thinking": True,
        "wants_usage": True,
        "system": "System instructions",
        "prompt": "Hello",
        "tools": [],
    })()
    adapter = adapters.ChatAdapter(parsed)
    adapter.begin()
    adapter.feed({"type": "thinking", "delta": "Deep thought analyzing the problem..."})
    adapter.feed({"type": "text", "delta": "Here is the response."})
    adapter.feed({
        "type": "credits",
        "value": 0.015,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 50,
            "reasoning_tokens": 7,
            "cache_read": 1000,
            "cache_write": 200,
        }
    })
    payload = adapter.end("stop")
    usage = payload["usage"]
    assert usage["prompt_tokens_details"]["cached_tokens"] == 1000
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 7
    assert usage["credits_used"] == 0.015


def test_responses_adapter_cache_and_reasoning_details():
    parsed = type("Parsed", (), {
        "model": "claude-opus-5-5",
        "show_thinking": True,
        "tools": [],
        "system": "",
        "prompt": "Hello",
    })()
    adapter = adapters.ResponsesAdapter(parsed)
    adapter.begin()
    adapter.feed({"type": "thinking", "delta": "Thinking..."})
    adapter.feed({"type": "text", "delta": "Response"})
    adapter.feed({
        "type": "credits",
        "value": 0.02,
        "usage": {
            "input_tokens": 500,
            "output_tokens": 80,
            "reasoning_tokens": 13,
            "cache_read": 450,
            "cache_write": 50,
        }
    })
    payload = adapter.end("stop")
    usage = payload["usage"]
    assert usage["input_token_details"]["cached_tokens"] == 450
    assert usage["output_token_details"]["reasoning_tokens"] == 13
    assert usage["credits_used"] == 0.02


def test_anthropic_adapter_cache_details():
    parsed = type("Parsed", (), {
        "model": "claude-opus-5-5",
        "show_thinking": True,
        "tools": [],
        "system": "",
        "prompt": "Hello",
    })()
    adapter = adapters.AnthropicAdapter(parsed)
    adapter.begin()
    adapter.feed({"type": "thinking", "delta": "Thinking..."})
    adapter.feed({"type": "text", "delta": "Response"})
    adapter.feed({
        "type": "credits",
        "value": 0.03,
        "usage": {
            "input_tokens": 800,
            "output_tokens": 100,
            "cache_read": 750,
            "cache_write": 80,
        }
    })
    payload = adapter.end("stop")
    usage = payload["usage"]
    assert usage["cache_read_input_tokens"] == 750
    assert usage["cache_creation_input_tokens"] == 80


# --- 2. Subagent Concurrency & Session Pool Unblocking ---

class DummyBackend:
    def __init__(self, cfg=None):
        self.cfg = cfg or {
            "session_mode": "per_conversation",
            "transport": "direct",
            "max_workers": 8,
            "max_conversations": 16,
            "conversation_idle_timeout": 900,
            "thread_id": "direct-shared",
            "base": "https://example.invalid",
            "page_uuid": "p1",
            "cookie": "accessToken=c1",
            "xsrf": "x1",
        }
        self.created = []

    async def create_thread(self):
        tid = f"direct-t{len(self.created) + 1}"
        self.created.append(tid)
        return tid

    async def new_thread_id(self):
        return await self.create_thread()

    async def get_messages(self, thread):
        return {"messages": []}


@pytest.mark.asyncio
async def test_subagent_concurrent_branched_session_lease():
    backend = DummyBackend()
    pool = session_mod.SessionPool(backend)
    
    # 1. Main agent acquires session for conversation "session-alpha"
    parent_lease = await pool.acquire("session-alpha")
    parent_session = parent_lease.session
    # Set parent session direct cache
    parent_session.set_direct("CACHED_HEAD_CONTENT", [{"name": "read_file"}], "auto", 4000)
    
    # 2. While parent holds the lock, subagent arrives with the SAME conversation key
    subagent_lease = await pool.acquire("session-alpha")
    subagent_session = subagent_lease.session
    
    # Assert subagent was NOT blocked and got an independent branched session
    assert subagent_session is not parent_session
    assert subagent_session.thread_id != parent_session.thread_id
    
    # Assert subagent inherited cached head and tools from parent
    assert subagent_session._cached_head == "CACHED_HEAD_CONTENT"
    assert "read_file" in subagent_session._registered_tools
    assert subagent_session._direct["head"] == "CACHED_HEAD_CONTENT"
    
    # 3. Release subagent lease: must NOT release parent session or corrupt conversation
    await pool.release(subagent_lease)
    assert subagent_lease.released
    assert not parent_lease.released
    assert pool._conversations["session-alpha"] is parent_session
    
    # 4. Release parent lease
    await pool.release(parent_lease)
    assert parent_lease.released


# --- 3. Thinking Heartbeat in Direct Transport ---

@pytest.mark.asyncio
async def test_thinking_heartbeat_emitted_during_long_reasoning(monkeypatch, capsys):
    q = asyncio.Queue()
    # Feed start of thinking
    q.put_nowait('{"event":"content_block_start","type":"thinking"}')
    
    events = []
    session_obj = type("S", (), {"thread_id": "req-think-1", "_cancelled": False})()

    # Accelerate loop_now and timeout for the test
    async def run_drain():
        async for event in direct._drain({}, session_obj, q, [], []):
            events.append(event)
            if event.get("type") == "thinking_heartbeat":
                # Stop after first heartbeat
                session_obj._cancelled = True
                break

    # Patch wait_for timeout to 0.05s so test runs in 50ms instead of 5s
    orig_wait_for = asyncio.wait_for
    async def short_wait_for(coro, timeout):
        return await orig_wait_for(coro, timeout=0.05)
    
    monkeypatch.setattr(asyncio, "wait_for", short_wait_for)
    await run_drain()
    
    types = [e.get("type") for e in events]
    assert "thinking_start" in types
    assert "thinking_heartbeat" in types
    output = capsys.readouterr().out
    assert "Waiting for upstream reasoning output" in output
    assert "[thinking] started source=upstream event=content_block_start" in output
    assert "actively reasoning" not in output


@pytest.mark.asyncio
async def test_no_thinking_indicator_before_an_upstream_thinking_signal(monkeypatch, capsys):
    queue = asyncio.Queue()
    queue.put_nowait('{"event":"ping"}')
    session = type("S", (), {"thread_id": "not-thinking", "_cancelled": False})()
    original_wait_for = asyncio.wait_for

    async def short_wait_for(coroutine, timeout):
        return await original_wait_for(coroutine, timeout=0.02)

    async def finish():
        await asyncio.sleep(0.08)
        queue.put_nowait('{"event":"end_of_stream"}')

    monkeypatch.setattr(asyncio, "wait_for", short_wait_for)
    producer = asyncio.create_task(finish())
    events = [event async for event in direct._drain({}, session, queue, [], [])]
    await producer
    assert not any(event.get("type") in ("thinking_start", "thinking_heartbeat") for event in events)
    output = capsys.readouterr().out
    assert "[thinking]" not in output and "reasoning output" not in output


# --- 4. Multi-Account Pool & Balancing ---

def test_config_multi_account_validation(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "ua": "test-ua",
        "accounts": [
            {"base": "https://acc1.retool.com", "page_uuid": "p1", "cookie": "accessToken=c1; foo=bar", "xsrf": "x1"},
            {"base": "https://acc2.retool.com", "page_uuid": "p2", "cookie": "accessToken=c2; baz=qux", "xsrf": "x2"},
        ]
    }), encoding="utf-8")
    
    loaded = config.load(str(cfg_file))
    assert loaded["base"] == "https://acc1.retool.com"
    assert loaded["page_uuid"] == "p1"
    assert "c1" in loaded["cookie_values"]
    assert "c2" in loaded["cookie_values"]


def test_config_multi_account_invalid_schema(tmp_path):
    cfg_file = tmp_path / "config.json"
    # Missing xsrf in account 1
    cfg_file.write_text(json.dumps({
        "ua": "test-ua",
        "accounts": [
            {"base": "https://acc1.retool.com", "page_uuid": "p1", "cookie": "c1"},
        ]
    }), encoding="utf-8")
    
    with pytest.raises(Exception) as exc:
        config.load(str(cfg_file))
    assert "missing required credentials" in str(exc.value)


@pytest.mark.asyncio
async def test_session_pool_balances_across_multiple_accounts():
    cfg = {
        "session_mode": "per_conversation",
        "transport": "direct",
        "max_workers": 8,
        "max_conversations": 16,
        "conversation_idle_timeout": 900,
        "thread_id": "direct-shared",
        "accounts": [
            {"base": "https://acc1.retool.com", "page_uuid": "p1", "cookie": "accessToken=c1", "xsrf": "x1"},
            {"base": "https://acc2.retool.com", "page_uuid": "p2", "cookie": "accessToken=c2", "xsrf": "x2"},
        ]
    }
    base_backend = DummyBackend(cfg)
    pool = session_mod.SessionPool(base_backend)
    assert len(pool.backends) == 2
    
    # Acquire two concurrent sessions: they must be balanced across backends
    lease1 = await pool.acquire("conv-1")
    lease2 = await pool.acquire("conv-2")
    
    backend1 = lease1.session.backend
    backend2 = lease2.session.backend
    assert backend1 is not backend2
    assert {backend1.cfg["page_uuid"], backend2.cfg["page_uuid"]} == {"p1", "p2"}
    
    await pool.release(lease1)
    await pool.release(lease2)


def test_upstream_refresh_updates_account_entry():
    cfg = {
        "base": "https://acc1.retool.com",
        "page_uuid": "p1",
        "cookie": "accessToken=old1",
        "xsrf": "old_xsrf",
        "upstream_timeout": 10,
        "accounts": [
            {"base": "https://acc1.retool.com", "page_uuid": "p1", "cookie": "accessToken=old1", "xsrf": "old_xsrf"},
        ]
    }
    refresher = upstream_refresh.Refresher(cfg, http_refresh=lambda: ["accessToken=new_token; Path=/"])
    refresher.refresh(force=True)
    
    assert "accessToken=new_token" in cfg["accounts"][0]["cookie"]


# --- 5. Responses Input Fallback in conversation_key ---

def test_conversation_key_responses_input_fallback():
    # String input
    key_str = validate.conversation_key({"input": "What is the capital of France?"}, allow_content_fallback=True)
    assert key_str is not None and key_str.startswith("content:")
    
    # List input
    key_list = validate.conversation_key({"input": [{"role": "user", "content": "What is the capital of France?"}]}, allow_content_fallback=True)
    assert key_list == key_str

    # Dict input
    key_dict = validate.conversation_key({"input": {"role": "user", "content": "What is the capital of France?"}}, allow_content_fallback=True)
    assert key_dict == key_str


# --- 6. Tool Call Variants & Resilient Execution ---

def test_tool_call_with_markdown_fences_and_attributes():
    import executor
    # 1. Tool call with markdown ```json ... ``` code fence inside
    text1 = '<tool_call>\n```json\n{"name": "read_file", "arguments": {"path": "test.txt"}}\n```\n</tool_call>'
    call1 = executor.extract_tool_call(text1)
    assert call1 is not None
    assert call1["name"] == "read_file"
    assert json.loads(call1["arguments"]) == {"path": "test.txt"}

    # 2. Tool call with attributes (id and name) and spaces
    text2 = '<tool_call id="call_custom_99" name="search_code" >\n{"query": "def main"}\n</tool_call>'
    call2 = executor.extract_tool_call(text2)
    assert call2 is not None
    assert call2["id"] == "call_custom_99"
    assert call2["name"] == "search_code"
    assert json.loads(call2["arguments"]) == {"query": "def main"}


def test_command_tool_call_supports_extended_client_tool_names():
    import executor
    text = "Let me check:\n<exec_command>git status</exec_command>"
    # Supported by terminal/execute_command/command
    call_terminal = executor.command_tool_call(text, ["terminal"])
    assert call_terminal is not None
    assert call_terminal["name"] == "terminal"
    assert json.loads(call_terminal["arguments"]) == {"command": "git status"}

    call_exec = executor.command_tool_call(text, ["execute_command"])
    assert call_exec is not None
    assert call_exec["name"] == "execute_command"
    assert json.loads(call_exec["arguments"]) == {"command": "git status"}


def test_persist_auth_does_not_corrupt_sibling_account_with_same_base(tmp_path):
    cfg_file = tmp_path / "config.json"
    initial = {
        "base": "https://company.retool.com",
        "page_uuid": "user-uuid-1",
        "cookie": "accessToken=token1",
        "xsrf": "xsrf1",
        "accounts": [
            {"base": "https://company.retool.com", "page_uuid": "user-uuid-1", "cookie": "accessToken=token1", "xsrf": "xsrf1"},
            {"base": "https://company.retool.com", "page_uuid": "user-uuid-2", "cookie": "accessToken=token2", "xsrf": "xsrf2"},
        ]
    }
    cfg_file.write_text(json.dumps(initial), encoding="utf-8")
    
    # Refresh account 2
    cfg2 = {
        "_config_path": str(cfg_file),
        "_persist_auth": True,
        "base": "https://company.retool.com",
        "page_uuid": "user-uuid-2",
        "cookie": "accessToken=token2_rotated",
        "xsrf": "xsrf2_rotated",
        "_disk_auth": ("accessToken=token2", "xsrf2"),
    }
    
    import config as cfg_module
    cfg_module.CONFIG_PATH = str(cfg_file)
    cfg_module.persist_auth(cfg2)
    
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    # Account 1 MUST NOT be corrupted by account 2's refresh even though they share the same base!
    assert saved["accounts"][0]["cookie"] == "accessToken=token1"
    assert saved["accounts"][0]["xsrf"] == "xsrf1"
    assert saved["accounts"][1]["cookie"] == "accessToken=token2_rotated"
    assert saved["accounts"][1]["xsrf"] == "xsrf2_rotated"


@pytest.mark.asyncio
async def test_branched_session_syncs_registered_tools_to_parent():
    backend = DummyBackend()
    pool = session_mod.SessionPool(backend)
    
    parent_lease = await pool.acquire("conv-tools")
    subagent_lease = await pool.acquire("conv-tools")
    
    # Subagent registers a new tool dynamically
    subagent_lease.session.register_tools([{"name": "subagent_special_tool"}])
    assert "subagent_special_tool" in subagent_lease.session._registered_tools
    
    # Release subagent
    await pool.release(subagent_lease)
    
    # Parent should now have the registered tool
    assert "subagent_special_tool" in parent_lease.session._registered_tools
    await pool.release(parent_lease)


@pytest.mark.asyncio
async def test_direct_retry_clears_client_context_pending(monkeypatch):
    from types import SimpleNamespace
    import io
    import direct
    cfg = {
        "transport": "direct",
        "session_mode": "per_conversation",
        "max_workers": 4,
        "max_conversations": 8,
        "conversation_idle_timeout": 900,
        "workspace_root": "C:\\specialinay_papka",
        "exec_policy": "off",
        "show_exec_output": False,
        "max_exec_turns": 0,
        "turn_timeout": 5,
        "recv_timeout": 5,
        "command_timeout": 5,
        "command_output_limit": 1000,
        "base": "https://example.invalid",
        "thread_id": "direct-shared",
        "provider_resource_name": "p1",
    }
    backend = DummyBackend(cfg)
    backend.headers = lambda: {}
    pool = session_mod.SessionPool(backend)
    runner = TurnRunner(cfg, SimpleNamespace(record_turn=lambda *_: None), pool)

    attempts = 0
    bodies = []
    def opener(req, **kw):
        nonlocal attempts
        attempts += 1
        bodies.append(req.data)
        events = ([] if attempts == 1 else [
            {"event": "content_block_delta", "type": "text", "partialText": "Successful recovery"}])
        events.append({"event": "end_of_stream"})
        result = io.BytesIO(b"\n".join(json.dumps(e).encode() for e in events) + b"\n")
        result.status = 200
        result.headers = {}
        return result
    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)

    backend.create_thread = lambda: asyncio.sleep(0, result="t1")
    backend.new_thread_id = lambda: asyncio.sleep(0, result="t1")
    parsed = validate.parse_chat({
        "model": "claude-opus-5-5",
        "messages": [{"role": "user", "content": "Hello"}],
        "tools": [{"type": "function", "function": {"name": "f1", "parameters": {"type": "object"}}}],
    }, {"strict_models": False, "model": "claude-opus-5-5"})
    parsed.conversation_key = "conv-direct-retry"

    try:
        out = queue.Queue()
        holder = {}
        await runner.run(parsed, out, holder)
        
        session = pool._conversations["conv-direct-retry"]
        assert getattr(session, "_client_context_pending", None) is False
        assert attempts == 2 and bodies[0] == bodies[1]
        assert any(e.get("delta") == "Successful recovery" for e in list(out.queue) if e)
    finally:
        for session in pool._conversations.values():
            await session.aclose()




def test_validate_stream_options_camel_case_support():
    cfg = {"strict_models": False, "model": "claude-opus-5-5"}
    data1 = {
        "model": "claude-opus-5-5",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "streamOptions": {"includeUsage": True},
    }
    parsed1 = validate.parse_chat(data1, cfg)
    assert parsed1.wants_usage is True

    data2 = {
        "model": "claude-opus-5-5",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"includeUsage": True},
    }
    parsed2 = validate.parse_chat(data2, cfg)
    assert parsed2.wants_usage is True

    # When stream_options is omitted, wants_usage defaults to True for coding clients (OpenCode, Cursor, Aider)
    data3 = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    assert validate.parse_chat(data3, cfg).wants_usage is True

    # Explicit opt-out is respected
    data4 = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}], "stream": True, "stream_options": {"include_usage": False}}
    assert validate.parse_chat(data4, cfg).wants_usage is False


def test_chat_adapter_streaming_full_cache_read_write_details():
    class Writer:
        def __init__(self):
            self.events = []
        def data(self, p):
            self.events.append(p)
        def done(self):
            pass

    parsed = type("Parsed", (), {
        "model": "claude-opus-5-5",
        "show_thinking": True,
        "wants_usage": True,
        "system": "System instructions",
        "prompt": "Hello",
        "tools": [],
    })()
    writer = Writer()
    adapter = adapters.ChatAdapter(parsed, writer)
    adapter.begin()
    adapter.feed({"type": "thinking", "delta": "Reasoning step"})
    adapter.feed({"type": "text", "delta": "Result"})
    adapter.feed({
        "type": "credits",
        "value": 0.05,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 100,
            "reasoning_tokens": 11,
            "cache_read": 15000,
            "cache_write": 2500,
        }
    })
    payload = adapter.end("stop")
    
    # Check streaming chunk
    usage_chunk = next(e for e in writer.events if "usage" in e)
    usage = usage_chunk["usage"]
    assert usage["cached_tokens"] == 15000
    assert usage["cache_read_input_tokens"] == 15000
    assert usage["cache_creation_input_tokens"] == 2500
    assert usage["prompt_tokens_details"]["cached_tokens"] == 15000
    assert usage["prompt_tokens_details"]["cache_write_tokens"] == 2500
    assert usage["prompt_tokens_details"]["cache_read_tokens"] == 15000
    assert usage["prompt_tokens_details"]["cache_creation_input_tokens"] == 2500
    assert usage["input_tokens_details"]["cached_tokens"] == 15000
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 11
    assert usage["output_tokens_details"]["reasoning_tokens"] == 11
    # prompt_tokens is total
    assert usage["prompt_tokens"] >= 15000 + 2500 + 10

    # Check non-streaming payload matches
    assert payload["usage"]["prompt_tokens_details"]["cached_tokens"] == 15000
    assert payload["usage"]["prompt_tokens_details"]["cache_write_tokens"] == 2500
