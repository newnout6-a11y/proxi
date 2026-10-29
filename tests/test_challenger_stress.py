"""Adversarial stress test suite written by Challenger 1.
Validates latency elimination, prompt cache prefix stability, tool superset normalization,
isolated request alignment, reasoning effort preservation, and context preservation.
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


def test_adversarial_tool_schema_permutations():
    """Stress-test tool schema normalization with varying tool order,
    varying property declaration order, required fields in different orders,
    and unicode descriptions."""
    def make_complex_tool(name, prop_order, req_order):
        props = {}
        for p in prop_order:
            if p == "enum_field":
                props[p] = {"type": "string", "enum": ["beta", "alpha", "gamma"]}
            elif p == "array_field":
                props[p] = {"type": "array", "items": {"type": "string"}}
            else:
                props[p] = {"type": "string", "description": f"Field {p}"}
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": "Unicode test \u2705 \u26a0\ufe0f",
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": req_order,
                },
            },
        }

    # Two tools with completely shuffled dictionary key insertion orders and tool array orders
    t1 = make_complex_tool("action_b", ["b", "enum_field", "array_field", "a"], ["a", "b"])
    t2 = make_complex_tool("action_a", ["z", "y", "x"], ["x"])
    tools_order_1 = [t1, t2]

    t1_shuffled = make_complex_tool("action_b", ["a", "array_field", "b", "enum_field"], ["b", "a"])
    t2_shuffled = make_complex_tool("action_a", ["x", "z", "y"], ["x"])
    tools_order_2 = [t2_shuffled, t1_shuffled]

    res1 = direct.tools_to_retool(tools_order_1)
    res2 = direct.tools_to_retool(tools_order_2)

    assert len(res1) == 2
    assert len(res2) == 2
    # Both sets must sort tools alphabetically: action_a first, action_b second
    assert [t["name"] for t in res1] == ["action_a", "action_b"]
    assert [t["name"] for t in res2] == ["action_a", "action_b"]
    # Parameter properties must be canonically sorted
    assert list(res1[0]["params"].keys()) == ["x", "y", "z"]
    assert list(res2[0]["params"].keys()) == ["x", "y", "z"]
    assert list(res1[1]["params"].keys()) == ["a", "array_field", "b", "enum_field"]
    assert list(res2[1]["params"].keys()) == ["a", "array_field", "b", "enum_field"]
    # Full canonical JSON serialization must be 100% byte-for-byte identical
    assert json.dumps(res1, sort_keys=True) == json.dumps(res2, sort_keys=True)


def test_multiturn_dynamic_tool_superset_growth_and_subagent_churn():
    """Simulate a multi-turn session where:
    Turn 1: declares Tools [Tool_A, Tool_C]
    Turn 2 (Subagent 1): declares Tools [Tool_B]
    Turn 3 (Subagent 2): declares Tools [Tool_D, Tool_A]
    Turn 4 (Tool-less compact): declares []
    Turn 5 (Parent resumes): declares Tools [Tool_C]
    Verify that canonical tools monotonically preserve the full superset [Tool_A, Tool_B, Tool_C, Tool_D].
    """
    session = RelaySession(type("B", (), {"cfg": {}})(), "sess_churn")
    
    def t(name):
        return {"name": name, "description": f"Desc of {name}", "params": {}, "strict": True}

    # Turn 1
    session.set_direct("head_base", [t("Tool_A"), t("Tool_C")], "auto", 100)
    assert [x["name"] for x in session.get_canonical_tools()] == ["Tool_A", "Tool_C"]

    # Turn 2: Subagent 1 declares Tool_B
    session.register_tools([t("Tool_B")])
    assert [x["name"] for x in session.get_canonical_tools()] == ["Tool_A", "Tool_B", "Tool_C"]

    # Turn 3: Subagent 2 declares Tool_D and Tool_A
    session.register_tools([t("Tool_D"), t("Tool_A")])
    assert [x["name"] for x in session.get_canonical_tools()] == ["Tool_A", "Tool_B", "Tool_C", "Tool_D"]

    # Turn 4: Tool-less compact registers empty list
    session.register_tools([])
    assert [x["name"] for x in session.get_canonical_tools()] == ["Tool_A", "Tool_B", "Tool_C", "Tool_D"]

    # Turn 5: Parent resumes with Tool_C
    session.register_tools([t("Tool_C")])
    canonical = session.get_canonical_tools()
    assert [x["name"] for x in canonical] == ["Tool_A", "Tool_B", "Tool_C", "Tool_D"]
    assert len(canonical) == 4


def test_interleaved_tool_and_toolless_requests_orchestrator(tmp_path):
    """Stress-test the TurnRunner orchestrator under rapid alternation between
    tool-bearing turns and tool-less (/compact, /goal, title) turns on the same conversation key.
    Ensure cached head and openAICacheKey NEVER diverge or inject 30k project context.
    """
    cfg = dict(config.DEFAULTS, workspace_root=str(tmp_path), cookie_values=[],
               transport="direct", project_context=True, context_max_bytes=30000,
               provider_resource_name="res", openai_provider_resource_name="ores")

    pool = SessionPool(Backend(cfg))
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *_: None})(), pool)

    conv_key = "claude-metadata:adversarial-stress-session-001"

    tools = [
        {"type": "function", "function": {"name": "exec_cmd", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "fetch_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    ]

    out = queue.Queue()

    # Step 1: Initial parent turn with tools
    p1 = validate.parse_anthropic({
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "system": "Parent system instruction",
        "messages": [{"role": "user", "content": "Parent query 1"}],
        "tools": tools,
    }, cfg)
    p1.conversation_key = conv_key

    holder1 = {}
    asyncio.run(runner.run(p1, out, holder1))
    s1 = holder1["session"].session
    cached_head_1 = s1._cached_head
    cache_key_1 = direct.cache_key_for(cached_head_1)
    assert "[PROJECT CONTEXT]" not in cached_head_1
    assert "Parent system instruction" in cached_head_1
    assert len(s1.get_canonical_tools()) == 2

    # Step 2: Tool-less compaction request
    p2 = validate.parse_anthropic({
        "model": "claude-opus-5-5",
        "max_tokens": 50,
        "messages": [
            {"role": "user", "content": "Parent query 1"},
            {"role": "assistant", "content": "Parent answer 1"},
            {"role": "user", "content": "/compact summarize now"}
        ],
    }, cfg)
    p2.conversation_key = conv_key

    holder2 = {}
    asyncio.run(runner.run(p2, out, holder2))
    s2 = holder2["session"].session
    # Tool-less turn must have EXACT same cached head and cache key
    assert s2._direct["head"] == cached_head_1
    assert direct.cache_key_for(s2._direct["head"]) == cache_key_1
    # Must use canonical tools with choice "none"
    assert s2._direct["tool_choice"] == "none"
    assert len(s2._direct["tools"]) == 2

    # Step 3: Tool-less /goal stop hook request
    p3 = validate.parse_anthropic({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 30,
        "messages": [
            {"role": "user", "content": "Check stop hook"}
        ],
    }, cfg)
    p3.conversation_key = conv_key

    holder3 = {}
    asyncio.run(runner.run(p3, out, holder3))
    s3 = holder3["session"].session
    assert s3._direct["head"] == cached_head_1
    assert direct.cache_key_for(s3._direct["head"]) == cache_key_1
    assert s3._direct["tool_choice"] == "none"

    # Step 4: Parent turn resumes with tools
    p4 = validate.parse_anthropic({
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "system": "Parent system instruction",
        "messages": [
            {"role": "user", "content": "Parent query 2"}
        ],
        "tools": tools,
    }, cfg)
    p4.conversation_key = conv_key

    holder4 = {}
    asyncio.run(runner.run(p4, out, holder4))
    s4 = holder4["session"].session
    assert s4._cached_head == cached_head_1
    assert direct.cache_key_for(s4._cached_head) == cache_key_1
    assert len(s4.get_canonical_tools()) == 2


def test_adversarial_reasoning_effort_matrix():
    """Verify reasoning effort resolution across a wide matrix of models and input values."""
    # 1. Models that support high: claude-opus-5-5, claude-sonnet-5
    for m in ["claude-opus-5-5", "claude-sonnet-5"]:
        entry, eff = models.resolve_tolerant(m, "high")
        assert eff == "high", f"Effort high must be preserved on {m}"
        entry, eff = models.resolve_tolerant(m, "max")
        assert eff == "max", f"Effort max must be preserved on {m}"
        entry, eff = models.resolve_tolerant(m, "medium")
        assert eff == "medium", f"Effort medium must be preserved on {m}"
        entry, eff = models.resolve_tolerant(m, "low")
        assert eff == "low", f"Effort low must be preserved on {m}"

    # 2. Haiku only accepts low, medium, high, max:
    # Test valid levels
    for valid_lvl in ["low", "medium", "high", "max"]:
        entry_h, eff_h = models.resolve_tolerant("claude-haiku-4-5-20251001", valid_lvl)
        assert eff_h == valid_lvl

    # Test clamping of invalid levels on Haiku
    entry_h_clamp, eff_h_clamp = models.resolve_tolerant("claude-haiku-4-5-20251001", "xhigh")
    assert eff_h_clamp == "high", "Haiku unsupported 'xhigh' must clamp to 'high'"

    entry_h2, eff_h2 = models.resolve_tolerant("claude-haiku-4-5-20251001", "invalid_value")
    assert eff_h2 in ("low", "medium", "high", "max"), "Invalid effort must safely clamp"

    # 3. Non-reasoning models must return None without raising unhandled 400
    for non_reasoning in ["zai-org/GLM-5.3-Flash"]:
        entry_nr, eff_nr = models.resolve_tolerant(non_reasoning, "high")
        assert eff_nr is None, f"Non-reasoning model {non_reasoning} must return None effort"


def test_stress_full_context_preservation_huge_dialogue():
    """Verify that a 300-turn conversation with massive dialogue content
    is never truncated, capped (no 100k cap), or dropped."""
    messages = []
    for i in range(300):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"Turn {i:03d} - Detailed log message with tokens: " + ("x" * 200)
        messages.append({"role": role, "content": content})

    system, chunks = validate._render_chunks(messages)
    assert len(chunks) == 300
    assert chunks[0].startswith("User: Turn 000")
    assert chunks[-1].startswith("Assistant: Turn 299")

    # Build direct body
    cfg = {"direct_cache": True}
    model_provider = {"model": "claude-opus-5-5"}
    body = direct.build_body(cfg, model_provider, "System Head", "", None, "auto", 100, chunks=chunks)

    # Total messages: 1 system + 300 user chunks = 301 messages
    assert len(body["messages"]) == 301
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"][0]["cache"] is True
    # Breakpoint on the last chunk
    assert body["messages"][-1]["content"][0]["cache"] is True
    assert "Turn 299" in body["messages"][-1]["content"][0]["text"]
    assert "openAICacheKey" in body
