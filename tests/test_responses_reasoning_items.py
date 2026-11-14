"""C16: a reasoning item of a Responses history is no turn of the conversation, in the text bridge as in the native one."""
import pytest

import apierr
import validate

CFG = {"strict_models": True, "model": "claude-opus-5-5"}
SUMMARY = "Compared both files and found the off-by-one"


def history(*extra):
    return [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the loop"}]},
            {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": SUMMARY}],
             "encrypted_content": "opaque"},
            {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": "{\"cmd\":\"dir\"}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "file.txt"}, *extra]


def parse(items):
    return validate.parse_responses({"model": "claude-opus-5-5", "input": items}, CFG)


def test_a_reasoning_summary_is_not_said_by_the_assistant():
    parsed = parse(history())
    assert SUMMARY not in parsed.prompt
    assert not any(SUMMARY in (chunk if isinstance(chunk, str) else str(chunk)) for chunk in parsed.message_chunks)
    assert not any(chunk.startswith("Assistant: ") and "off-by-one" in chunk for chunk in parsed.message_chunks)


def test_the_text_bridge_and_the_native_history_hold_the_same_turns():
    parsed = parse(history())
    assert [message["role"] for message in parsed.native_messages] == ["user", "assistant", "tool"]
    assert len(parsed.message_chunks) == 3  # the task, the call and its result
    assert len(parsed.message_fingerprints) == 3


def test_a_summary_given_as_plain_text_is_dropped_too():
    parsed = parse([{"type": "reasoning", "summary": "thinking"}, {"type": "message", "role": "user", "content": "hi"}])
    assert "thinking" not in parsed.prompt and parsed.prompt == "User: hi"


def test_an_unsupported_item_still_names_its_place_in_the_input():
    with pytest.raises(apierr.ApiError) as error:
        parse(history({"type": "mystery_item"}))
    assert error.value.param == "input[4].type"


def test_a_reasoning_item_with_a_role_is_a_message_as_before():
    parsed = parse([{"type": "message", "role": "user", "content": "hi"},
                    {"type": "reasoning", "role": "assistant", "content": "said aloud"}])
    assert "Assistant: said aloud" in parsed.prompt
