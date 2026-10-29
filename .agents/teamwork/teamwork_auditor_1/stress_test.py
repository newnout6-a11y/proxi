import json
import pytest
import direct
import models
import validate
from session import RelaySession

def test_stress_tool_normalization():
    print("[STRESS] Testing tool canonicalization edge cases...")
    # Empty tools
    assert direct.tools_to_retool([]) == []
    assert direct.tools_to_retool(None) == []
    
    # Broken tool objects
    broken_tools = [
        {},
        {"type": "other"},
        {"function": None},
        {"function": {}},
        {"function": {"name": ""}},
        {"function": {"name": "valid_tool", "parameters": None}},
        {"function": {"name": "another_tool", "parameters": {"properties": {"z": None, "a": {"type": "string"}}}}}
    ]
    res = direct.tools_to_retool(broken_tools)
    assert len(res) == 2
    assert [t["name"] for t in res] == ["another_tool", "valid_tool"]
    print("  -> Tool normalization edge cases passed.")

def test_stress_effort_clamping():
    print("[STRESS] Testing effort clamping edge cases...")
    # Opus with various efforts
    for eff in ["high", "HIGH", "High"]:
        entry, val = models.resolve_tolerant("claude-opus-5-5", eff)
        assert val == "high"
    
    # Haiku with crazy efforts
    for crazy in ["xhigh", "maximum", "ultra", "extreme", "none", "LOW"]:
        entry, val = models.resolve_tolerant("claude-haiku-4-5-20251001", crazy)
        assert val in ("low", "medium", "high", "max")
    
    # Non-reasoning model
    entry, val = models.resolve_tolerant("zai-org/GLM-5.3-Flash", "high")
    assert val is None
    print("  -> Effort clamping edge cases passed.")

def test_stress_massive_context():
    print("[STRESS] Testing massive context preservation (500 chunks, 250k characters)...")
    chunks = [f"Turn {i}: " + ("x" * 500) for i in range(500)]
    body = direct.build_body({"direct_cache": True}, {"model": "claude-opus-5-5"}, "SYSTEM PROMPT", "", None, "auto", 100, chunks=chunks)
    
    assert len(body["messages"]) == 501  # 1 system + 500 chunks
    assert body["messages"][-1]["content"][0]["cache"] is True
    assert "Turn 499:" in body["messages"][-1]["content"][0]["text"]
    assert len(body["messages"][-1]["content"][0]["text"]) > 500
    print("  -> Massive context preservation passed (zero truncation, zero dropping).")

def test_stress_cache_key_determinism():
    print("[STRESS] Testing cache key determinism across permutations...")
    head = "Stable instructions for prompt caching verification"
    k1 = direct.cache_key_for(head)
    k2 = direct.cache_key_for(head)
    assert k1 == k2
    assert len(k1) == 24
    print("  -> Cache key determinism passed.")

if __name__ == "__main__":
    test_stress_tool_normalization()
    test_stress_effort_clamping()
    test_stress_massive_context()
    test_stress_cache_key_determinism()
    print("[STRESS] All adversarial stress tests completed successfully!")
