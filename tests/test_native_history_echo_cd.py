"""Claude Code drops `cd <its working directory> && ` from a Bash call it echoes; the native history still resumes.

Claude Code 2.1.317 stores `cd /c/specialinay_papka && ls -la` as `ls -la` when its own directory is /c/specialinay_papka,
the echo check failed and the next turn went cold without the signed thinking (journal of 2026-11-09, Claude Desktop:
53 of 57 such turns, about 360 credits of rewritten prefix in 106 minutes)."""
import asyncio
import copy
import json

import native_history
from test_native_context_reuse import FIRST, request, run
from test_native_history import close, harness
from test_review_fixes_pr19 import END, THINKING, signature, text

BASH = {"type": "function", "function": {"name": "Bash", "parameters": {"type": "object", "properties": {
    "command": {"type": "string"}, "description": {"type": "string"}}, "required": ["command"]}}}
WRITTEN = "cd /c/specialinay_papka && ls -la && git status --short | head -20"
STORED = "ls -la && git status --short | head -20"


def echo(command, **extra):
    return {"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "b1", "toolName": "Bash",
                                              "toolInput": dict({"command": command}, **extra)}]}


def test_a_command_without_its_leading_cd_is_the_echo_of_the_kept_call():
    assert native_history.same_echo(echo(STORED), echo(WRITTEN))
    assert native_history.same_echo(echo(STORED), echo('cd "C:/My Projects/x" && ' + STORED))
    assert native_history.same_echo(echo(STORED), echo("cd 'C:/My Projects/x'&&" + STORED))
    # anything else stays a different call
    assert not native_history.same_echo(echo("ls -la"), echo(WRITTEN))
    assert not native_history.same_echo(echo(STORED, description="x"), echo(WRITTEN))
    assert not native_history.same_echo(echo(WRITTEN), echo(STORED))  # the model's call never loses a cd it did not write
    assert not native_history.same_echo(echo(STORED), echo("cd /tmp; " + STORED))
    defaults = {"Bash": {"description": ""}}
    assert native_history.same_echo(echo(STORED, description=""), echo(WRITTEN), defaults)


def call_reply(sig):
    return [*THINKING, signature(sig), {"event": "content_block_end"},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": "b1", "toolName": "Bash"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps({"command": WRITTEN})},
            {"event": "content_block_end"}, *END]


def test_the_next_turn_resumes_with_the_call_as_the_model_wrote_it(tmp_path, monkeypatch):
    pool, runner, sent = harness(tmp_path, monkeypatch, [call_reply("sig-bash"), [text("done"), *END]])

    async def scenario():
        try:
            messages = copy.deepcopy(FIRST)
            await run(runner, request(messages, dialect="anthropic", tools=[BASH]))
            messages = messages + [
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "sig-bash"},
                                                  {"type": "tool_use", "id": "b1", "name": "Bash",
                                                   "input": {"command": STORED}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b1", "content": "total 0"}]}]
            await run(runner, request(messages, dialect="anthropic", tools=[BASH]))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2
    assert "sig-bash" in json.dumps(sent[1])  # resumed natively with the signed reply
    replayed = next(m for m in sent[1]["messages"] if m["role"] == "assistant")
    call = next(b for b in replayed["content"] if b["type"] == "tool_call")
    assert call["toolInput"] == {"command": WRITTEN}  # upstream gets the call as the model wrote it
