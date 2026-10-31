"""Regression and verification tests for PR #2 fixes and direct transport latency hardening."""
import asyncio
import queue
from types import SimpleNamespace

import direct
import models
from orchestrator import TurnRunner, _ClientText
from session import RelaySession, SessionPool


def test_codex_catalog_vision_parity():
    """Verify Codex model catalog input_modalities matches each model's vision capability."""
    catalog = models.catalog()
    codex = models.codex_catalog(catalog)["models"]
    codex_by_slug = {m["slug"]: m for m in codex}

    for entry in catalog:
        m_id = entry["id"]
        assert m_id in codex_by_slug
        codex_entry = codex_by_slug[m_id]
        if entry.get("vision"):
            assert codex_entry["input_modalities"] == ["text", "image"], f"Expected vision modalities for {m_id}"
        else:
            assert codex_entry["input_modalities"] == ["text"], f"Expected text-only modality for {m_id}"


def test_direct_prompt_cache_persistence_across_turns():
    """Verify that direct.build_body preserves turn breakpoints across turns
    without any sliding window eviction so prompt caching hits across turns."""
    cfg = {"direct_cache": True}
    model = {"model": "claude-opus-5-5"}
    head = "You are a helpful assistant."
    sess = SimpleNamespace(_cached_turn_ends=[])

    # Turn 1: 1 chunk
    body1 = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=["Turn 1"], session=sess)
    # 1 system + 1 user message
    assert len(body1["messages"]) == 2
    assert body1["messages"][0]["content"][0]["cache"] is True
    assert body1["messages"][1]["content"][0]["cache"] is True

    # Turn 2: 2 chunks
    body2 = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=["Turn 1", "Turn 2"], session=sess)
    assert len(body2["messages"]) == 3
    assert body2["messages"][1]["content"][0]["cache"] is True
    assert body2["messages"][2]["content"][0]["cache"] is True

    # Turn 3: 3 chunks
    body3 = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=["Turn 1", "Turn 2", "Turn 3"], session=sess)
    assert len(body3["messages"]) == 4
    assert body3["messages"][1]["content"][0]["cache"] is True
    assert body3["messages"][2]["content"][0]["cache"] is True
    assert body3["messages"][3]["content"][0]["cache"] is True

    # Turn 4: 4 chunks (Anthropic strictly limits requests to at most 4 cache breakpoints)
    body4 = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=["Turn 1", "Turn 2", "Turn 3", "Turn 4"], session=sess)
    assert len(body4["messages"]) == 5
    # Turn 1 history chunk must NOT be dropped from cache breakpoints
    assert body4["messages"][1]["content"][0]["cache"] is True
    # Tail retains breakpoint
    assert body4["messages"][4]["content"][0]["cache"] is True
    # Total cached blocks across entire payload MUST be exactly 4 (never 5)
    cached4 = [b for m in body4["messages"] for b in m.get("content", []) if isinstance(b, dict) and b.get("cache")]
    assert len(cached4) == 4


def test_direct_tools_cache_budget_reserves_upstream_breakpoint_slot():
    """With client tools the upstream backend adds its own cache_control breakpoint
    on the tool definitions, so relay markers must stay within 3 — otherwise
    Anthropic rejects with "A maximum of 4 blocks with cache_control may be
    provided. Found 5." (regression: multi-turn request with 13-18 tools)."""
    cfg = {"direct_cache": True}
    model = {"model": "claude-opus-5-5"}
    head = "You are a helpful assistant."
    tools = [
        {"name": f"tool_{i}", "description": "t", "parameters": {"type": "object", "properties": {}}}
        for i in range(13)
    ]
    chunks = ["User: hi", "[tool result id=1] ok", "User: go on", "[tool result id=2] ok", "User: continue"]

    body = direct.build_body(cfg, model, head, "", tools, "auto", 100, chunks=chunks,
                             session=SimpleNamespace(_cached_turn_ends=[]))
    cached = [b for m in body["messages"] for b in m.get("content", []) if isinstance(b, dict) and b.get("cache")]
    # 3 relay markers + 1 upstream tool breakpoint = 4 (never 5)
    assert len(cached) <= 3
    assert body["messages"][0]["content"][0]["cache"] is True
    assert body["messages"][-1]["content"][-1].get("cache") is True

    # Without tools the previous budget of 4 relay markers still holds
    body_no_tools = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks,
                                      session=SimpleNamespace(_cached_turn_ends=[]))
    cached_no_tools = [b for m in body_no_tools["messages"] for b in m.get("content", [])
                       if isinstance(b, dict) and b.get("cache")]
    assert len(cached_no_tools) == 4


def test_client_text_suppresses_incomplete_tool_envelopes():
    """Verify _ClientText suppresses incomplete tool envelopes on final flush
    while preserving ordinary markdown backtick markup."""
    # Case A: Prose followed by unclosed tool envelope
    ct = _ClientText()
    out = ct.feed('Thinking about the task.\n<tool_call>{"name": "bash", "arguments": {"command": "ls', final=True)
    assert out.strip() == "Thinking about the task."
    assert "<tool_call" not in out

    # Case B: Prose mentioning `<tool_call>` in backticks
    ct2 = _ClientText()
    out2 = ct2.feed("Use `<tool_call>` syntax for invocation.", final=True)
    assert "`<tool_call>`" in out2


def test_agent_mode_rejects_incomplete_tool_envelope_after_prose(tmp_path):
    """Verify TurnRunner reports upstream_error when prose is followed by an unclosed tool call."""
    class Session:
        async def stream(self, *args, **kwargs):
            yield {"type": "text", "delta": 'I am about to run a command:\n<tool_call>{"name": "bash", "arguments":'}
            yield {"type": "end", "reason": "stop"}

    tool = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
    parsed = SimpleNamespace(prompt="run bash", system="sys", model="claude-opus-5-5",
                             entry={"id": "claude-opus-5-5", "provider": "anthropic", "reasoning": True},
                             effort="low", tools=tool, tool_choice="auto", conversation_key=None,
                             stream=True)

    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "off",
           "transport": "agent", "context_max_bytes": 100000, "max_prompt_bytes": 0,
           "provider_resource_name": "provider"}

    class Pool:
        async def acquire(self, *args, **kwargs):
            return Session()
        async def release(self, *args, **kwargs):
            pass

    runner = TurnRunner(cfg, SimpleNamespace(record_turn=lambda *_: None), Pool())
    out = queue.Queue()

    asyncio.run(runner.run(parsed, out, {}))
    items = list(out.queue)
    err_event = next((e for e in items if isinstance(e, dict) and e.get("type") == "error"), None)
    assert err_event is not None
    assert err_event["error"].status == 502
    assert "malformed or disallowed" in err_event["error"].message


def test_tool_less_request_preserves_task_specific_system_prompt(tmp_path):
    """Verify tool-less requests (like title generation) preserve distinct system instructions."""
    target_captured = {}

    class FakeDirectSession:
        def __init__(self):
            self._cached_head = "INITIAL CACHED HEAD FROM MAIN TURN"
            self._registered_tools = {"bash": {"name": "bash"}}
            self.thread_id = "th1"

        def has_tools(self):
            return bool(self._registered_tools)

        def get_canonical_tools(self):
            return list(self._registered_tools.values())

        def set_direct(self, head, tools, tool_choice, max_tokens, chunks=None):
            target_captured["head"] = head
            target_captured["tools"] = tools
            target_captured["tool_choice"] = tool_choice
            target_captured["chunks"] = chunks

        async def stream(self, *args, **kwargs):
            yield {"type": "text", "delta": "Generated Title"}
            yield {"type": "end", "reason": "stop"}

    sess = FakeDirectSession()

    class Pool:
        async def acquire(self, *args, **kwargs):
            return sess
        async def release(self, *args, **kwargs):
            pass

    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "off",
           "transport": "direct", "context_max_bytes": 100000, "max_prompt_bytes": 0,
           "provider_resource_name": "provider"}

    runner = TurnRunner(cfg, SimpleNamespace(record_turn=lambda *_: None), Pool())
    out = queue.Queue()

    # Tool-less request with distinct task-specific system prompt
    parsed = SimpleNamespace(prompt="Generate a 3-word title", system="Generate a short title",
                             model="claude-opus-5-5",
                             entry={"id": "claude-opus-5-5", "provider": "anthropic", "reasoning": True},
                             effort="low", tools=[], tool_choice="none", conversation_key="conv-1",
                             stream=True)

    asyncio.run(runner.run(parsed, out, {}))

    assert target_captured["head"] == "INITIAL CACHED HEAD FROM MAIN TURN"
    assert target_captured["tool_choice"] == "none"
    # Distinct task-specific system instructions must reach set_direct via chunks
    chunks = target_captured["chunks"]
    assert any("[CLIENT INSTRUCTIONS]\nGenerate a short title" in str(c) for c in chunks)


def test_native_turns_replace_tools_and_omit_removed_tools(tmp_path):
    """Verify Turn 2 omits a tool declared in Turn 1 and does not accumulate old tools."""
    target_captured = {}

    class FakeDirectSession:
        def __init__(self):
            self._cached_head = None
            self._registered_tools = {}
            self.thread_id = "th1"

        def register_tools(self, tools):
            self._registered_tools = {t["name"]: t for t in tools}

        def has_tools(self):
            return bool(self._registered_tools)

        def get_canonical_tools(self):
            return list(self._registered_tools.values())

        def set_direct(self, head, tools, tool_choice, max_tokens, chunks=None):
            target_captured["tools"] = tools

        async def stream(self, *args, **kwargs):
            yield {"type": "text", "delta": "ok"}
            yield {"type": "end", "reason": "stop"}

    sess = FakeDirectSession()

    class Pool:
        async def acquire(self, *args, **kwargs):
            return sess
        async def release(self, *args, **kwargs):
            pass

    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "off",
           "transport": "direct", "context_max_bytes": 100000, "max_prompt_bytes": 0,
           "provider_resource_name": "provider"}
    runner = TurnRunner(cfg, SimpleNamespace(record_turn=lambda *_: None), Pool())

    tool_a = {"type": "function", "function": {"name": "tool_a", "parameters": {"type": "object"}}}
    tool_b = {"type": "function", "function": {"name": "tool_b", "parameters": {"type": "object"}}}

    # Turn 1: tool_a and tool_b
    parsed1 = SimpleNamespace(prompt="t1", system="", model="claude-opus-5-5",
                              entry={"id": "claude-opus-5-5", "provider": "anthropic", "reasoning": True},
                              effort="low", tools=[tool_a, tool_b], tool_choice="auto", conversation_key="c1",
                              stream=True)
    asyncio.run(runner.run(parsed1, queue.Queue(), {}))
    tools1 = [t["name"] for t in target_captured["tools"]]
    assert set(tools1) == {"tool_a", "tool_b"}

    # Turn 2: tool_a only (tool_b removed)
    parsed2 = SimpleNamespace(prompt="t2", system="", model="claude-opus-5-5",
                              entry={"id": "claude-opus-5-5", "provider": "anthropic", "reasoning": True},
                              effort="low", tools=[tool_a], tool_choice="auto", conversation_key="c1",
                              stream=True)
    asyncio.run(runner.run(parsed2, queue.Queue(), {}))
    tools2 = [t["name"] for t in target_captured["tools"]]
    assert tools2 == ["tool_a"]
    assert "tool_b" not in tools2
    assert "tool_b" not in sess._registered_tools


def test_isolated_lease_does_not_inherit_cached_head_or_tools():
    """Verify SessionPool does not copy cached head or tools into isolated sessions."""
    class FakeBackend:
        cfg = {"session_mode": "per_conversation", "max_conversations": 10,
               "thread_id": "root-t", "max_workers": 4, "base": "https://x",
               "upstream_timeout": 5}
        refresher = None
        async def create_thread(self):
            return "thread-1"

    async def scenario():
        pool = SessionPool(FakeBackend())
        lease1 = await pool.acquire("conv-1")
        # Populate session state on conv-1
        lease1.session._cached_head = "SECRET HEAD"
        lease1.session._registered_tools = {"secret_tool": {"name": "secret_tool"}}

        # Acquire an isolated session (e.g. for side request or concurrent request)
        isolated_lease = await pool.acquire(isolated=True)
        assert isolated_lease.session._cached_head is None
        assert isolated_lease.session._registered_tools == {}

        await pool.release(lease1)
        await pool.release(isolated_lease)

    asyncio.run(scenario())


def test_direct_transport_instant_cancellation():
    """Verify session.cancel() immediately invokes active direct abort and frees resources."""
    backend = SimpleNamespace(cfg={"turn_timeout": 30, "base": "https://x", "transport": "direct"}, refresher=None)
    sess = RelaySession(backend, "t-direct")

    closed = False

    class FakeResponse:
        def close(self):
            nonlocal closed
            closed = True

    # Register active direct abort hook
    fake_resp = FakeResponse()
    sess._active_direct_abort = fake_resp.close

    sess.cancel()

    assert sess._cancelled is True
    assert sess._needs_drain is True
    assert closed is True


def test_direct_multi_chunk_tool_turn_cache_preservation():
    """Verify that multi-chunk tool execution turns (e.g. 4 file reads producing 8 chunks)
    preserve the previous turn's cache breakpoint, preventing full 150k token cache invalidation."""
    cfg = {"direct_cache": True}
    model = {"model": "claude-opus-5-5"}
    head = "You are a helpful coding assistant."
    sess = SimpleNamespace(_cached_turn_ends=[])

    # Turn 1: 1 chunk (User prompt)
    body1 = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=["User: Read 4 files"], session=sess)
    # 1 system + 1 user message
    assert len(body1["messages"]) == 2
    assert body1["messages"][0]["content"][0]["cache"] is True
    assert body1["messages"][1]["content"][0]["cache"] is True
    assert sess._cached_turn_ends == [0]

    # Turn 2: 4 tool calls + 4 tool results added = 8 chunks added (total 9 chunks)
    turn2_chunks = [
        "User: Read 4 files",
        "Assistant: [tool_call read 1]",
        "Assistant: [tool_call read 2]",
        "Assistant: [tool_call read 3]",
        "Assistant: [tool_call read 4]",
        "[tool result read 1]: content 1",
        "[tool result read 2]: content 2",
        "[tool result read 3]: content 3",
        "[tool result read 4]: content 4",
    ]
    body2 = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=turn2_chunks, session=sess)
    assert len(body2["messages"]) == 10  # 1 system + 9 history messages

    # System head has cache
    assert body2["messages"][0]["content"][0]["cache"] is True
    # CRITICAL: Index 1 (Turn 1's user prompt at chunk index 0) MUST preserve its cache breakpoint!
    assert body2["messages"][1]["content"][0].get("cache") is True, "Turn 1 cache breakpoint must NOT be dropped on multi-tool turns!"
    # The tail of Turn 2 (chunk index 8) MUST have cache breakpoint for the next turn
    assert body2["messages"][9]["content"][0].get("cache") is True


def test_direct_100_tool_calls_iterative_execution_preserves_all_cache_breakpoints():
    """Verify that when 100 tool calls are executed incrementally across 100 turns/steps,
    earlier cache breakpoints (including Turn 1's user prompt and earlier tool results)
    are NEVER evicted by any sliding window limit."""
    cfg = {"direct_cache": True}
    model = {"model": "claude-opus-5-5"}
    head = "You are an expert coding assistant with 100 tools."
    sess = SimpleNamespace(_cached_turn_ends=[])

    # Turn 1: Initial User prompt
    chunks = ["User: Please inspect and fix 100 files in the workspace"]
    body1 = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks, session=sess)
    assert len(body1["messages"]) == 2
    assert body1["messages"][0]["content"][0]["cache"] is True
    assert body1["messages"][1]["content"][0]["cache"] is True
    assert sess._cached_turn_ends == [0]

    # Simulate 100 tool executions, one by one
    for tool_idx in range(1, 101):
        chunks.append(f"Assistant: [tool_call read_file id=call_{tool_idx}: {{\"path\": \"file_{tool_idx}.py\"}}]")
        chunks.append(f"[tool result read_file id=call_{tool_idx}]: file_{tool_idx} contents")
        body = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks, session=sess)

        # 1 system message + 1 user prompt + 2 * tool_idx chunks
        expected_msg_count = 1 + 1 + 2 * tool_idx
        assert len(body["messages"]) == expected_msg_count

        # System head MUST retain cache
        assert body["messages"][0]["content"][0]["cache"] is True

        # CRITICAL: Chunk 0 (Turn 1 User prompt at index 1) MUST ALWAYS preserve cache breakpoint!
        assert body["messages"][1]["content"][0].get("cache") is True, (
            f"Turn 1 prompt cache breakpoint was dropped after tool {tool_idx}!"
        )

        # The tail (the latest tool result) MUST have cache breakpoint
        assert body["messages"][-1]["content"][0].get("cache") is True

        # Index 0 must NEVER be evicted from sess._cached_turn_ends
        assert 0 in sess._cached_turn_ends

    # Verify that all 100 tool steps preserved their breakpoints without window truncation
    assert len(sess._cached_turn_ends) >= 101
    assert sess._cached_turn_ends[0] == 0

    # Now verify follow-up Turn 2: user asks a new question
    chunks.append("User: Excellent, now run tests on all 100 files")
    body_followup = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks, session=sess)
    # Turn 1 prompt still cached
    assert body_followup["messages"][1]["content"][0].get("cache") is True
    # End of Turn 1 (last tool result at index 201) still cached
    assert body_followup["messages"][201]["content"][0].get("cache") is True
    # Turn 2 user prompt (index 202) cached
    assert body_followup["messages"][-1]["content"][0].get("cache") is True


def test_direct_100_tool_calls_batch_preserves_prompt_and_all_tool_breakpoints():
    """Verify that a batch history containing 100 tool calls and results preserves
    the user prompt and tail breakpoints while strictly respecting Anthropic's 4-breakpoint limit."""
    cfg = {"direct_cache": True}
    model = {"model": "claude-opus-5-5"}
    head = "System head"

    chunks = ["User: Process 100 items"]
    for i in range(100):
        chunks.append(f"Assistant: [tool_call tool_{i} id={i}: {{}}]")
        chunks.append(f"[tool result tool_{i} id={i}]: done {i}")

    # Case A: Stateless (no session)
    body_stateless = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks, session=None)
    assert len(body_stateless["messages"]) == 202
    assert body_stateless["messages"][0]["content"][0]["cache"] is True
    # User prompt MUST be cached
    assert body_stateless["messages"][1]["content"][0].get("cache") is True
    # Tail MUST be cached
    assert body_stateless["messages"][-1]["content"][0].get("cache") is True
    # CRITICAL: Total blocks with cache across entire request MUST NEVER EXCEED 4!
    cached = [b for m in body_stateless["messages"] for b in m.get("content", []) if isinstance(b, dict) and b.get("cache")]
    assert len(cached) <= 4

    # Case B: With session initialized from Turn 1
    sess = SimpleNamespace(_cached_turn_ends=[0])
    body_session = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks, session=sess)
    assert body_session["messages"][1]["content"][0].get("cache") is True
    assert body_session["messages"][-1]["content"][0].get("cache") is True
    assert 0 in sess._cached_turn_ends
    assert len(sess._cached_turn_ends) >= 101
    cached_sess = [b for m in body_session["messages"] for b in m.get("content", []) if isinstance(b, dict) and b.get("cache")]
    assert len(cached_sess) <= 4


def test_direct_multi_turn_with_massive_tool_calls_no_window_eviction():
    """Verify 10 turns with 10 tool calls each preserve Turn 1 prompt, tail, and all
    turn boundaries in session memory without exceeding Anthropic's 4-breakpoint limit."""
    cfg = {"direct_cache": True}
    model = {"model": "claude-opus-5-5"}
    head = "Multi-turn assistant"
    sess = SimpleNamespace(_cached_turn_ends=[])

    chunks = []
    user_prompt_indices = []

    for turn_num in range(10):
        user_chunk_idx = len(chunks)
        user_prompt_indices.append(user_chunk_idx)
        chunks.append(f"User: Turn {turn_num} question")
        for tool_num in range(10):
            chunks.append(f"Assistant: [tool_call t_{turn_num}_{tool_num} id={tool_num}: {{}}]")
            chunks.append(f"[tool result t_{turn_num}_{tool_num} id={tool_num}]: ok")

        body = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks, session=sess)

        # Head and Chunk 0 MUST ALWAYS be cached
        assert body["messages"][0]["content"][0]["cache"] is True
        assert body["messages"][1]["content"][0]["cache"] is True
        # Tail MUST ALWAYS be cached
        assert body["messages"][-1]["content"][0]["cache"] is True
        # Total cached blocks across entire payload MUST NEVER exceed 4
        cached = [b for m in body["messages"] for b in m.get("content", []) if isinstance(b, dict) and b.get("cache")]
        assert len(cached) <= 4

    # All 10 user prompt turn boundaries are recorded in session state
    for prev_idx in user_prompt_indices:
        assert prev_idx in sess._cached_turn_ends


def test_direct_100_anthropic_style_tool_results_preserve_all_cache_breakpoints():
    """Verify that 100 tool calls using Anthropic-style '[tool_result id=...: ...]'
    preserve Turn 1 prompt and tail while obeying Anthropic's 4-breakpoint limit."""
    cfg = {"direct_cache": True}
    model = {"model": "claude-opus-5-5"}
    head = "Anthropic tool caller"
    sess = SimpleNamespace(_cached_turn_ends=[])

    chunks = ["User: please execute 100 operations"]
    for i in range(1, 101):
        chunks.append(f"Assistant: [tool_call op_{i} id=call_{i}: {{}}]")
        chunks.append(f"[tool_result id=call_{i}: done_{i}]")

    # Stateless
    body_stateless = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks, session=None)
    # Turn 1 user prompt and tail
    assert body_stateless["messages"][1]["content"][0].get("cache") is True
    assert body_stateless["messages"][-1]["content"][0].get("cache") is True
    cached_stateless = [b for m in body_stateless["messages"] for b in m.get("content", []) if isinstance(b, dict) and b.get("cache")]
    assert len(cached_stateless) <= 4

    # With session
    body_session = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks, session=sess)
    assert body_session["messages"][1]["content"][0].get("cache") is True
    assert body_session["messages"][-1]["content"][0].get("cache") is True
    assert 0 in sess._cached_turn_ends
    assert len(sess._cached_turn_ends) >= 101
    cached_session = [b for m in body_session["messages"] for b in m.get("content", []) if isinstance(b, dict) and b.get("cache")]
    assert len(cached_session) <= 4


def test_direct_100_tool_calls_with_unadorned_prompt_and_dict_chunks():
    """Verify that an unadorned prompt and dict chunks preserve Chunk 0 and tail
    breakpoints across 100 tool calls while obeying the 4-breakpoint limit."""
    cfg = {"direct_cache": True}
    model = {"model": "claude-opus-5-5"}
    head = "Coding agent"
    sess = SimpleNamespace(_cached_turn_ends=[])

    # Unadorned prompt (raw text without 'User:' or 'Human:')
    chunks = [{"type": "text", "text": "Inspect all 100 files in the repo"}]
    for i in range(1, 101):
        chunks.append(f"Assistant: [tool_call read_{i} id={i}: {{}}]")
        chunks.append({"type": "text", "text": f"[tool result read_{i} id={i}]: content_{i}"})

    body = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks, session=sess)
    # Chunk 0 (raw prompt) MUST be cached
    assert body["messages"][1]["content"][0].get("cache") is True, "Unadorned prompt chunk 0 must be cached!"
    assert body["messages"][-1]["content"][0].get("cache") is True
    assert 0 in sess._cached_turn_ends
    cached = [b for m in body["messages"] for b in m.get("content", []) if isinstance(b, dict) and b.get("cache")]
    assert len(cached) <= 4


def test_direct_50_command_exec_followups_preserve_all_cache_breakpoints():
    """Verify that server-side command execution followups preserve Chunk 0 and tail
    breakpoints across 50 command execution steps while obeying the 4-breakpoint limit."""
    cfg = {"direct_cache": True}
    model = {"model": "claude-opus-5-5"}
    head = "Bash executor"
    sess = SimpleNamespace(_cached_turn_ends=[])

    chunks = ["USER TASK:\nRun 50 command steps to diagnose system"]
    for i in range(1, 51):
        chunks.append(f"Assistant: <exec_command>step_{i}</exec_command>")
        chunks.append(f"[Command result for 'step_{i}']\nExit code: 0\nOutput:\nstep {i} ok")

    body = direct.build_body(cfg, model, head, "", None, "auto", 100, chunks=chunks, session=sess)
    # Chunk 0
    assert body["messages"][1]["content"][0].get("cache") is True
    assert body["messages"][-1]["content"][0].get("cache") is True
    assert 0 in sess._cached_turn_ends
    cached = [b for m in body["messages"] for b in m.get("content", []) if isinstance(b, dict) and b.get("cache")]
    assert len(cached) <= 4


def test_force_close_response_unblocks_winsock():
    """Verify _force_close_response closes underlying socket descriptor to interrupt blocked recv()."""
    import socket
    import threading
    import time

    s1, s2 = socket.socketpair()
    try:
        class FakeResp:
            def fileno(self):
                return s1.fileno()
            def close(self):
                pass

        unblocked = False
        def reader():
            nonlocal unblocked
            try:
                s1.recv(1024)
            except Exception:
                unblocked = True

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)
        direct._force_close_response(FakeResp())
        t.join(timeout=1.0)
        assert not t.is_alive(), "Socket thread must terminate upon force close"
        assert unblocked is True
    finally:
        s2.close()


def test_cancelled_turn_does_not_retry_empty_in_orchestrator(tmp_path):
    """Verify TurnRunner aborts immediately on cancel and does not issue a retry on an empty turn."""
    import queue
    import threading
    import time
    from orchestrator import TurnRunner
    from types import SimpleNamespace

    cfg = {"workspace_root": str(tmp_path), "transport": "direct", "strict_models": False,
           "exec_policy": "off", "show_exec_output": False, "instructions": "System Head",
           "project_context": False, "context_max_bytes": 100000, "max_prompt_bytes": 0,
           "provider_resource_name": "provider", "command_timeout": 5, "command_output_limit": 1024,
           "exec_allowlist": []}

    class FakeSession:
        _cancelled = False
        def set_direct(self, *args, **kwargs):
            pass
        def cancel(self):
            self._cancelled = True
        async def aclose(self):
            pass
        async def stream(self, *args, **kwargs):
            # Simulated empty turn on cancellation
            if False:
                yield None

    sess = FakeSession()

    class Pool:
        async def acquire(self, *args, **kwargs):
            return sess
        async def release(self, *args, **kwargs):
            pass

    runner = TurnRunner(cfg, SimpleNamespace(record_turn=lambda *_: None), Pool())
    out = queue.Queue()
    cancel_evt = threading.Event()
    cancel_evt.set()  # Cancelled before or during turn
    holder = {"cancel_event": cancel_evt, "upstream_started_at": time.time()}

    parsed = SimpleNamespace(prompt="hello", system="", model="claude-opus-5-5",
                              entry={"id": "claude-opus-5-5", "provider": "anthropic", "reasoning": True},
                              effort="high", tools=[], tool_choice="none", stream=True, conversation_key="c1")

    try:
        asyncio.run(runner.run(parsed, out, holder))
    except asyncio.CancelledError:
        pass

    events = []
    while not out.empty():
        events.append(out.get_nowait())

    # Must produce end event with reason 'cancelled', not an empty turn retry
    assert any(isinstance(e, dict) and e.get("type") == "end" and e.get("reason") == "cancelled" for e in events)
    assert not any(isinstance(e, dict) and e.get("type") == "error" for e in events)

