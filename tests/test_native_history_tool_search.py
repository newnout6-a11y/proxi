"""A Claude Code conversation keeps real assistant and tool turns upstream after ToolSearch loads a tool.

Claude Code 2.1 defers most MCP tools (and some of its own, WebFetch) behind ToolSearch, whose result is a list of
tool_reference blocks (live trace, 2026-11-06). The native reader refused that content, so from the first ToolSearch
on the whole conversation went upstream as a text transcript in user messages, without the model's signed thinking."""
import asyncio
import copy
import json

import native_history
from test_native_context_reuse import FIRST, request, run
from test_native_history import close, harness, strip_cache
from test_review_fixes_pr19 import END, THINKING, signature, text

TOOLS = [{"type": "function", "function": {"name": "ToolSearch", "parameters": {
             "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
         {"type": "function", "function": {"name": "read_file", "parameters": {
             "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
LOADED = TOOLS + [{"type": "function", "function": {"name": "WebFetch", "parameters": {
             "type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}}]
REFERENCES = [{"type": "tool_reference", "tool_name": "WebFetch"}]


def call(call_id, name, arguments, sig):
    return [*THINKING, signature(sig), {"event": "content_block_end"},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": name},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps(arguments)},
            {"event": "content_block_end"}, *END]


def answer(messages, call_id, name, arguments, sig, result):
    """The next request of Claude Code: its echo of the reply, then the tool result."""
    return messages + [{"role": "assistant", "content": [
                           {"type": "thinking", "thinking": "", "signature": sig},
                           {"type": "tool_use", "id": call_id, "name": name, "input": arguments}]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                                     "content": result}]}]


def test_tool_references_read_as_the_names_of_the_loaded_tools():
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s1", "content": [
        {"type": "tool_reference", "tool_name": "WebFetch"}, {"type": "tool_reference", "tool_name": "mcp__x__y"}]}]}]
    native = native_history.client_messages(messages, lambda value, where: dict(value))
    assert native == [{"role": "tool", "content": {"toolCallId": "s1", "toolResult": [
        {"type": "text", "text": "Tool loaded: WebFetch"}, {"type": "text", "text": "Tool loaded: mcp__x__y"}]}}]


def test_a_reference_without_a_tool_name_is_still_unreadable():
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s1",
                                              "content": [{"type": "tool_reference"}]}]}]
    assert native_history.client_messages(messages, lambda value, where: dict(value)) is None


def test_after_tool_search_the_conversation_keeps_its_turns_and_then_its_signatures(tmp_path, monkeypatch):
    search = ("s1", "ToolSearch", {"query": "select:WebFetch"}, "sig-one")
    fetch = ("f1", "WebFetch", {"url": "https://example.com"}, "sig-two")
    pool, runner, sent = harness(tmp_path, monkeypatch, [call(*search), call(*fetch), [text("done"), *END]])

    async def scenario():
        try:
            messages = copy.deepcopy(FIRST)
            await run(runner, request(messages, dialect="anthropic", tools=TOOLS))
            # ToolSearch put WebFetch into the tool list: another tool set is another prefix, so this turn starts cold.
            messages = answer(messages, *search, REFERENCES)
            await run(runner, request(messages, dialect="anthropic", tools=LOADED))
            messages = answer(messages, *fetch, "Example Domain")
            await run(runner, request(messages, dialect="anthropic", tools=LOADED))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 3
    cold, resumed = sent[1]["messages"], sent[2]["messages"]
    assert [m["role"] for m in cold][-2:] == ["assistant", "tool"]
    assert cold[-1]["content"]["toolResult"] == [{"type": "text", "text": "Tool loaded: WebFetch"}]
    assert "sig-one" not in json.dumps(cold)  # a signature never crosses a changed prefix
    thinking = [b["signature"] for m in resumed if isinstance(m.get("content"), list) for b in m["content"]
                if isinstance(b, dict) and b.get("type") == "thinking"]
    assert thinking == ["sig-two"]
    # The resumed turn extends the cold one: the cached prefix of the previous request stays as it was.
    assert strip_cache(resumed[:len(cold)]) == strip_cache(cold)
    assert [m["role"] for m in resumed][len(cold):] == ["assistant", "tool"]
