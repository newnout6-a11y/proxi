"""A client that fills an argument's default before it echoes the call keeps its native history.

Claude Code's Edit has an optional replace_all with the default false. When the model leaves it out, Claude Code fills
the default before it echoes the assistant message, the relay's echo check failed, and the next turn went cold: the
whole cached conversation was written again (Claude Desktop test run of 2026-11-06: 233k tokens, 30.7 credits for one
Edit; inside an Ultracode agent 26k)."""
import asyncio
import copy
import json

import native_history
from test_native_context_reuse import FIRST, request, run
from test_native_history import close, harness
from test_review_fixes_pr19 import END, THINKING, signature, text

EDIT = {"type": "function", "function": {"name": "Edit", "parameters": {"type": "object", "properties": {
    "file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
    "replace_all": {"type": "boolean", "default": False}}, "required": ["file_path", "old_string", "new_string"]}}}
ARGS = {"file_path": "a.py", "old_string": "x", "new_string": "y"}


def call_reply(sig):
    return [*THINKING, signature(sig), {"event": "content_block_end"},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": "e1", "toolName": "Edit"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps(ARGS)},
            {"event": "content_block_end"}, *END]


def echo(arguments):
    return {"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "e1", "toolName": "Edit",
                                              "toolInput": arguments}]}


def test_the_defaults_come_from_the_clients_schemas():
    assert native_history.tool_defaults([EDIT]) == {"Edit": {"replace_all": False}}


def test_an_argument_at_its_default_counts_as_absent_on_both_sides():
    defaults = {"Edit": {"replace_all": False}}
    filled = echo(dict(ARGS, replace_all=False))
    assert native_history.same_echo(filled, echo(ARGS), defaults)
    assert native_history.same_echo(echo(ARGS), filled, defaults)
    assert not native_history.same_echo(filled, echo(ARGS), None)
    assert not native_history.same_echo(echo(dict(ARGS, replace_all=True)), echo(ARGS), defaults)
    assert not native_history.same_echo(echo(dict(ARGS, new_string="z")), echo(ARGS), defaults)


def test_the_next_turn_resumes_when_the_client_echoes_the_default(tmp_path, monkeypatch):
    pool, runner, sent = harness(tmp_path, monkeypatch, [call_reply("sig-edit"), [text("done"), *END]])

    async def scenario():
        try:
            messages = copy.deepcopy(FIRST)
            await run(runner, request(messages, dialect="anthropic", tools=[EDIT]))
            messages = messages + [
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "sig-edit"},
                                                  {"type": "tool_use", "id": "e1", "name": "Edit",
                                                   "input": dict(ARGS, replace_all=False)}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "e1", "content": "ok"}]}]
            await run(runner, request(messages, dialect="anthropic", tools=[EDIT]))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2
    assert "sig-edit" in json.dumps(sent[1])  # resumed natively with the signed reply
    replayed = next(m for m in sent[1]["messages"] if m["role"] == "assistant")
    call = next(b for b in replayed["content"] if b["type"] == "tool_call")
    assert call["toolInput"] == ARGS  # upstream gets the reply as the model wrote it
