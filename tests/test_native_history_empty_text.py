"""An empty text block never goes upstream from the native history.

Upstream refuses an empty text block, and the cache hint lands on the last text block, so an empty user turn (or an
empty text beside tool results) in a conversation became {"type": "text", "text": "", "cache": true} in every resumed
or cold request after it (ultracode audit of the relay, C8). The text bridge already left such text out."""
import json

import native_history


def native(messages):
    return native_history.client_messages(messages, lambda value, where: dict(value))


def test_empty_text_beside_real_text_is_left_out():
    messages = [{"role": "user", "content": [{"type": "text", "text": ""}, {"type": "text", "text": "Go on."}]}]
    assert native(messages) == [{"role": "user", "content": [{"type": "text", "text": "Go on."}]}]


def test_a_user_turn_of_empty_text_disappears():
    for content in ("", [{"type": "text", "text": ""}], [""]):
        assert native([{"role": "user", "content": "Start."}, {"role": "user", "content": content}]) == [
            {"role": "user", "content": [{"type": "text", "text": "Start."}]}]


def test_a_cold_replay_with_empty_text_and_empty_results_holds_no_empty_text_block():
    messages = [
        {"role": "user", "content": "Run it."},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "true"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": ""},
                                     {"type": "text", "text": ""}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "true"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b", "content": [{"type": "text", "text": ""}]}]}]
    client = native(messages)
    cold = native_history.cache_messages(native_history.unsigned_tool_transcript(client), True, [{"name": "Bash"}])
    assert '"text": ""' not in json.dumps(cold)
    assert [m["role"] for m in cold] == ["user", "assistant", "tool", "assistant", "tool"]
