"""The error flag of a tool result reaches the model on the text bridge as well.

An Anthropic tool_result with "is_error": true is how Claude Code says that a command failed or a tool refused. The
native history carries it upstream as isError, but the text bridge (the cold replay, a history that cannot go native)
wrote the same text for a failed result as for a good one, so the model read "exit 1" without being told that the tool
had failed (ultracode audit of the relay, C21)."""
import pytest

import direct
import validate
from test_client_contracts import CFG, _body

TOOLS = [{"name": "bash", "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}}}]
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}


def anthropic(content, **flags):
    result = {"type": "tool_result", "tool_use_id": "t1", "content": content, **flags}
    messages = [{"role": "user", "content": "run make"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash",
                                                   "input": {"cmd": "make"}}]},
                {"role": "user", "content": [result]}]
    return validate.parse_anthropic(_body("anthropic", messages=messages, tools=TOOLS), CFG)


def test_a_failed_result_says_so_in_the_text_bridge():
    failed, fine = anthropic("exit 1", is_error=True), anthropic("exit 1", is_error=False)
    assert failed.message_chunks[-1] == "User: [tool_result id=t1 is_error=true: exit 1]"
    assert fine.message_chunks[-1] == "User: [tool_result id=t1: exit 1]"
    assert anthropic("exit 1").message_chunks == fine.message_chunks
    assert failed.message_chunks != fine.message_chunks and failed.prompt != fine.prompt
    assert "is_error=true: exit 1]" in failed.prompt


def test_a_failed_result_of_blocks_says_so_as_well():
    parsed = anthropic([{"type": "text", "text": "exit 1"}, {"type": "text", "text": "see log"}], is_error=True)
    assert parsed.message_chunks[-1] == "User: [tool_result id=t1 is_error=true: exit 1\nsee log]"


def test_a_failed_result_that_is_a_picture_says_so_and_keeps_the_picture():
    chunk = anthropic([IMAGE], is_error=True).message_chunks[-1]
    assert chunk[0] == {"type": "text", "text": "User: [tool_result id=t1 is_error=true: ]"}
    assert chunk[1]["type"] == "image"


def test_the_cold_replay_of_the_text_bridge_carries_the_flag_upstream():
    parsed = anthropic("exit 1", is_error=True)
    body = direct.build_body({"direct_cache": False}, {"model": "claude-opus-5-5"}, "head", parsed.prompt, None, "auto",
                             100, chunks=parsed.message_chunks)
    assert "is_error=true" in body["messages"][-1]["content"][0]["text"]


def test_the_native_history_still_carries_it_as_it_did():
    assert anthropic("exit 1", is_error=True).native_messages[-1]["isError"] is True
    assert "isError" not in anthropic("exit 1", is_error=False).native_messages[-1]
