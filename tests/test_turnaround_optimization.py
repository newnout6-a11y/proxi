"""Tests for turnaround optimization: stable prompt cache prefix, tool normalization,
isolated request context alignment, effort clamping, and full context preservation.
"""
import asyncio
import json
import queue
import pytest

import apierr
import config
import direct
import models
from orchestrator import TurnRunner
from session import Backend, RelaySession, SessionPool
import validate


def test_tool_hash_normalization_canonical_order():
    """Verify tools declared in different orders produce identical canonical Retool tools."""
    tools_order_a = [
        {"type": "function", "function": {"name": "write", "description": "Write", "parameters": {"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "number"}}}}},
        {"type": "function", "function": {"name": "read", "description": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    ]
    tools_order_b = [
        {"type": "function", "function": {"name": "read", "description": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "write", "description": "Write", "parameters": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "string"}}}}},
    ]
    res_a = direct.tools_to_retool(tools_order_a)
    res_b = direct.tools_to_retool(tools_order_b)
    # Both must be sorted alphabetically by name
    assert [t["name"] for t in res_a] == ["read", "write"]
    assert [t["name"] for t in res_b] == ["read", "write"]
    # Parameter properties must be sorted canonically
    assert list(res_a[1]["params"].keys()) == ["a", "b"]
    assert list(res_b[1]["params"].keys()) == ["a", "b"]
    # Retool representation must be 100% identical
    assert json.dumps(res_a, sort_keys=True) == json.dumps(res_b, sort_keys=True)


def test_varying_tool_declarations_preserve_normalized_superset():
    """Verify that a turn declaring fewer tools (e.g. 15 tools vs 18 tools) aligns the session's tool schema to the current turn."""
    session = RelaySession(type("B", (), {"cfg": {}})(), "t1")
    tools_18 = [{"name": f"tool_{i:02d}", "description": f"Tool {i}", "params": {}, "strict": True} for i in range(18)]
    tools_15 = tools_18[:15]

    # Turn 1 registers 18 tools
    session.set_direct("head_v1", tools_18, "auto", 100)
    assert len(session.get_canonical_tools()) == 18

    # Turn 2 passes only 15 tools: replaced with the current authorized set
    session.register_tools(tools_15)
    canonical = session.get_canonical_tools()
    assert len(canonical) == 15
    assert [t["name"] for t in canonical] == [t["name"] for t in sorted(tools_15, key=lambda x: x["name"])]


def test_isolated_tool_less_request_context_alignment(tmp_path):
    """Verify tool-less requests (/compact, /goal, title generation) inherit the cached head
    and tool schema with tool_choice='none', preventing divergent 30k byte project context."""
    cfg = dict(config.DEFAULTS, workspace_root=str(tmp_path), cookie_values=[],
               transport="direct", project_context=True, context_max_bytes=30000,
               provider_resource_name="a", openai_provider_resource_name="o")

    pool = SessionPool(Backend(cfg))
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *_: None})(), pool)

    # 1. Main turn with native tools and session affinity
    tools = [
        {"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "write", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    ]
    parsed_main = validate.parse_anthropic({
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "system": "Client custom system prompt",
        "messages": [{"role": "user", "content": "Turn 1 request"}],
        "tools": tools,
    }, cfg)
    parsed_main.conversation_key = "claude-metadata:session-abc-123"

    out = queue.Queue()
    holder_main = {}
    asyncio.run(runner.run(parsed_main, out, holder_main))
    main_session = holder_main["session"].session

    # Verify main session established direct cached head and registered tools
    cached_head = main_session._cached_head
    assert cached_head is not None
    assert "[ROLE]" in cached_head
    assert "Client custom system prompt" in cached_head
    # Must NOT have 30k project context in native client head
    assert "[PROJECT CONTEXT]" not in cached_head
    assert main_session.has_tools()
    assert len(main_session.get_canonical_tools()) == 2

    # 2. Tool-less auxiliary request (e.g. /compact, /goal Stop hook, title query) on the SAME conversation
    parsed_aux = validate.parse_anthropic({
        "model": "claude-opus-5-5",
        "max_tokens": 50,
        "messages": [
            {"role": "user", "content": "Turn 1 request"},
            {"role": "assistant", "content": "Turn 1 response"},
            {"role": "user", "content": "Please summarize the conversation"}
        ],
    }, cfg)
    parsed_aux.conversation_key = "claude-metadata:session-abc-123"

    holder_aux = {}
    asyncio.run(runner.run(parsed_aux, out, holder_aux))
    aux_session = holder_aux["session"].session

    # The auxiliary tool-less session must inherit the EXACT same cached head and tools
    assert aux_session._direct["head"] == cached_head
    assert "[PROJECT CONTEXT]" not in aux_session._direct["head"]
    assert "[LOCAL EXEC REMINDER]" not in aux_session._direct["head"]
    assert aux_session._direct["tool_choice"] == "none"
    assert len(aux_session._direct["tools"]) == 2


def test_short_head_prompt_caching_without_char_threshold():
    """Verify prompt caching is active even for short heads (e.g. 50 characters)."""
    cfg = {"direct_cache": True}
    short_head = "Brief system role for coding assistant"
    assert len(short_head) < 100
    body = direct.build_body(cfg, {"model": "claude-opus-5-5"}, short_head, "user prompt", None, "auto", 50)
    # System block has cache: True
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"][0]["cache"] is True
    # openAICacheKey is present and matches hash
    assert body.get("openAICacheKey") == direct.cache_key_for(short_head)


def test_reasoning_effort_clamping_and_preservation():
    """Verify effortLevel: high is strictly preserved on Opus/Sonnet, while
    unsupported effort (e.g. xhigh) clamps to high on Haiku and None on non-reasoning models."""
    # 1. Opus 5.5 strictly preserves 'high'
    entry_opus, effort_opus = models.resolve_tolerant("claude-opus-5-5", "high")
    assert effort_opus == "high"

    # 2. Sonnet 5 strictly preserves 'high'
    entry_sonnet, effort_sonnet = models.resolve_tolerant("claude-sonnet-5", "high")
    assert effort_sonnet == "high"

    # 3. Haiku only accepts low, medium, high, max: 'xhigh' clamps to 'high'
    entry_haiku, effort_haiku = models.resolve_tolerant("claude-haiku-4-5-20251001", "xhigh")
    assert effort_haiku == "high"

    # 4. Non-reasoning model safely clamps effort to None without 400 error
    entry_glm, effort_glm = models.resolve_tolerant("zai-org/GLM-5.3-Flash", "high")
    assert effort_glm is None


def test_full_context_preservation_no_truncation():
    """Verify all conversation chunks are preserved in order without truncation or dropped messages."""
    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"Turn {i} content text"}
                for i in range(120)]
    system, chunks = validate._render_chunks(messages)
    assert len(chunks) == 120
    assert chunks[0] == "User: Turn 0 content text"
    assert chunks[119] == "Assistant: Turn 119 content text"

    # In direct build_body, all 120 chunks become user messages with cache on the last one
    body = direct.build_body({"direct_cache": True}, {"model": "claude-opus-5-5"}, "system head", "", None, "auto", 100, chunks=chunks)
    # 1 system message + 120 chunk messages = 121 messages total
    assert len(body["messages"]) == 121
    assert body["messages"][-1]["content"][0]["cache"] is True
    assert "Turn 119" in body["messages"][-1]["content"][0]["text"]


def test_universal_conversation_key_fallback_for_opencode():
    """Verify that multi-turn requests without client affinity headers (e.g. OpenCode)
    get a stable deterministic conversation key based on root message hash."""
    turn_1 = {
        "model": "claude-opus-5-5",
        "messages": [
            {"role": "system", "content": "You are OpenCode assistant"},
            {"role": "user", "content": "Root user task: optimize performance"}
        ]
    }
    turn_2 = {
        "model": "claude-opus-5-5",
        "messages": [
            {"role": "system", "content": "You are OpenCode assistant"},
            {"role": "user", "content": "Root user task: optimize performance"},
            {"role": "assistant", "content": "Running git diff", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "diff output"},
            {"role": "user", "content": "Next turn: now run tests"}
        ]
    }
    key_1 = validate.conversation_key(turn_1, allow_content_fallback=True)
    key_2 = validate.conversation_key(turn_2, allow_content_fallback=True)
    assert key_1 is not None and key_1.startswith("content:")
    assert key_1 == key_2, f"Conversation keys must match across turns: {key_1} vs {key_2}"

    # Without allow_content_fallback, returns None (standard unit contract)
    assert validate.conversation_key(turn_1) is None

    # Different conversation root gets a different key
    other_conv = {
        "model": "claude-opus-5-5",
        "messages": [
            {"role": "system", "content": "You are OpenCode assistant"},
            {"role": "user", "content": "Completely different user task"}
        ]
    }
    assert validate.conversation_key(other_conv, allow_content_fallback=True) != key_1


def test_session_aclose_cancels_warm_task():
    """Verify that closing a session immediately cancels any active _warm_task,
    preventing zombie background ping loops from polluting upstream cache."""
    session = RelaySession(type("B", (), {"cfg": {}})(), "t1")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def scenario():
        # Simulate active warm task
        warm_task = asyncio.create_task(asyncio.sleep(100))
        session._warm_task = warm_task
        assert not warm_task.done()

        # aclose must cancel the task
        await session.aclose()
        assert (hasattr(warm_task, 'cancelling') and warm_task.cancelling() > 0) or warm_task.cancelled() or warm_task.done()

    loop.run_until_complete(scenario())
    loop.close()
