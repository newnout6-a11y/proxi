"""Codex's multi-agent messages reach the model instead of failing the turn.

With multi_agent_version v2 (the relay's catalog offers it with Ultra), Codex 0.166+ runs sub-agents and passes the
messages between them as input items of type "agent_message" (author, recipient, content). The relay knew no such
item and refused the turn: 422 "messages[11].role must be a string". The sub-agent failed on the task it was given, its
final answer to the root said so, and the root's next turn failed on that answer too (live, 2026-11-06, Codex Desktop
engine 0.166 and app-server daemon 0.167 with gpt-6.1-sol)."""
import pytest

import apierr
import validate
from test_client_contracts import CFG

ANSWER = ("Message Type: FINAL_ANSWER\nTask name: /root\nSender: /root/count_lines\nPayload:\n"
          "The repository has 412 Python files.")
TASK = "Message Type: TASK\nTask name: /root/count_lines\nSender: /root\nPayload:\nCount the Python files."


def root_turn():
    """The root agent's request after waiting for its sub-agent, shaped like the live trace."""
    return {"model": "gpt-6.1-sol", "input": [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Rules."}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "How big is the repo?"}]},
        {"type": "function_call", "name": "spawn_agent", "namespace": "collaboration", "call_id": "c1",
         "arguments": "{\"task_name\":\"count_lines\"}"},
        {"type": "function_call_output", "call_id": "c1", "output": "{\"agent\":\"/root/count_lines\"}"},
        {"type": "function_call", "name": "wait_agent", "namespace": "collaboration", "call_id": "c2",
         "arguments": "{\"timeout_ms\":300000}"},
        {"type": "function_call_output", "call_id": "c2", "output": "{\"message\":\"Wait completed.\"}"},
        {"type": "agent_message", "id": "amsg_1", "author": "/root/count_lines", "recipient": "/root",
         "content": [{"type": "input_text", "text": ANSWER}]},
    ]}


def test_a_sub_agents_answer_reaches_the_root_as_a_turn_it_received():
    parsed = validate.parse_responses(root_turn(), CFG)
    assert ANSWER in parsed.prompt
    assert parsed.native_messages[-1] == {"role": "user", "content": [{"type": "text", "text": ANSWER}]}
    assert [m["role"] for m in parsed.native_messages] == ["user", "assistant", "tool", "assistant", "tool", "user"]


def test_a_sub_agent_starts_from_the_task_the_root_sent_it():
    body = {"model": "gpt-6.1-sol", "input": [
        {"type": "agent_message", "author": "/root", "recipient": "/root/count_lines",
         "content": [{"type": "input_text", "text": TASK}]}]}
    parsed = validate.parse_responses(body, CFG)
    assert parsed.native_messages == [{"role": "user", "content": [{"type": "text", "text": TASK}]}]


def test_an_item_type_the_route_cannot_carry_is_named_in_the_refusal():
    body = {"model": "gpt-6.1-sol", "input": [
        {"type": "message", "role": "user", "content": "hello"},
        {"type": "web_search_call", "id": "ws_1", "status": "completed"}]}
    with pytest.raises(apierr.ApiError) as raised:
        validate.parse_responses(body, CFG)
    assert raised.value.status == 422 and raised.value.param == "input[1].type"
    assert "web_search_call" in str(raised.value)


def test_the_relay_serves_the_root_turn(relay):
    status, body = relay.json_any("/v1/responses", root_turn())
    assert status == 200, body
    assert ANSWER in relay.fake.seen[-1].prompt
