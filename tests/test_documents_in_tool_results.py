"""Documents and search results in tool results reach the model as text, and the conversation keeps its native history.

Claude Code's Read answers a PDF with an Anthropic document block, and search tools answer with search_result blocks.
The relay had no shape for them: the model got the first 200 characters of their JSON, and the native reader refused
the block, so the whole conversation went upstream as a text transcript from then on (ultracode audit of the relay, C3)."""
import asyncio
import base64
import copy
import json

import native_history
import validate
from test_client_contracts import CFG
from test_native_context_reuse import FIRST, request, run
from test_native_history import close, harness
from test_review_fixes_pr19 import END, THINKING, signature, text

PDF = {"type": "document", "title": "report.pdf",
       "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(b"%PDF" * 1024).decode()}}
SEARCH = {"type": "search_result", "source": "https://example.com/a", "title": "Release notes",
          "content": [{"type": "text", "text": "Version 2 adds streaming."}, {"type": "text", "text": "Fixes a crash."}]}
TOOLS = [{"type": "function", "function": {"name": "Read", "parameters": {
    "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}}]


def anthropic_body(result):
    return {"model": "claude-opus-5-5", "max_tokens": 1024, "messages": [
        {"role": "user", "content": "Read the report."},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "r1", "name": "Read",
                                           "input": {"file_path": "report.pdf"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1", "content": result}]}],
        "tools": [{"name": "Read", "input_schema": TOOLS[0]["function"]["parameters"]}]}


def test_a_document_and_a_search_result_read_as_text():
    note = native_history.readable_block(PDF)
    assert note.startswith("[document report.pdf, application/pdf, 4 KB: the content of this file cannot be read "
                           "through this route and was not given to the model.") and note.endswith("]")
    assert native_history.readable_block(SEARCH) == (
        "[search result: Release notes, https://example.com/a]\nVersion 2 adds streaming.\nFixes a crash.")
    plain = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "Hello."}}
    assert native_history.readable_block(plain) == "[document]\nHello."
    assert native_history.readable_block({"type": "image"}) is None


def test_the_native_view_keeps_a_conversation_that_holds_them():
    parsed = validate.parse_anthropic(anthropic_body([{"type": "text", "text": "Page 1"}, PDF, SEARCH]), CFG)
    assert parsed.native_messages is not None
    result = parsed.native_messages[-1]["content"]["toolResult"]
    lines = [block["text"].split("\n")[0] for block in result]
    assert lines[0] == "Page 1" and lines[2] == "[search result: Release notes, https://example.com/a]"
    assert lines[1].startswith("[document report.pdf, application/pdf, 4 KB: the content of this file cannot be read")


def test_the_text_bridge_shows_their_text_instead_of_cut_json():
    parsed = validate.parse_anthropic(anthropic_body([PDF, SEARCH]), CFG)
    assert "[document report.pdf, application/pdf" in parsed.prompt and "Version 2 adds streaming." in parsed.prompt
    assert '"type": "document"' not in parsed.prompt and PDF["source"]["data"][:40] not in parsed.prompt


def test_the_turn_after_a_pdf_read_continues_natively(tmp_path, monkeypatch):
    call = [*THINKING, signature("sig-pdf"), {"event": "content_block_end"},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": "r1", "toolName": "Read"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps({"file_path": "a.pdf"})},
            {"event": "content_block_end"}, *END]
    pool, runner, sent = harness(tmp_path, monkeypatch, [call, [text("It is a report."), *END]])

    async def scenario():
        try:
            messages = copy.deepcopy(FIRST)
            await run(runner, request(messages, dialect="anthropic", tools=TOOLS))
            messages = messages + [
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "sig-pdf"},
                                                  {"type": "tool_use", "id": "r1", "name": "Read",
                                                   "input": {"file_path": "a.pdf"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1", "content": [PDF]}]}]
            await run(runner, request(messages, dialect="anthropic", tools=TOOLS))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2
    assert "sig-pdf" in json.dumps(sent[1])  # the signed reply went back: native continuation, not a transcript
    assert sent[1]["messages"][-1]["role"] == "tool"
