import validate

CFG = {"strict_models": True, "model": "claude-opus-5-5"}


def test_function_call_output_becomes_tool_message():
    parsed = validate.parse_responses({"model": "gpt-5.5", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run dir"}]},
        {"type": "function_call", "name": "shell", "arguments": "{\"cmd\":\"dir\"}"},
        {"type": "function_call_output", "call_id": "shell", "output": "file.txt"},
    ]}, CFG)
    assert "tool_call shell" in parsed.prompt
    assert "tool result shell" in parsed.prompt
    assert "file.txt" in parsed.prompt


def test_reasoning_item_is_skipped_not_fatal():
    parsed = validate.parse_responses({"model": "gpt-5.5", "input": [
        {"type": "reasoning", "summary": "thinking"},
        {"type": "message", "role": "user", "content": "hi"},
    ]}, CFG)
    assert parsed.prompt
