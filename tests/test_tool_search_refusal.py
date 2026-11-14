"""Claude Code's tool search is turned down, so that it loads every tool up front and the tool list stops changing.

Claude Code 2.1.304 and later defers its MCP tools behind ToolSearch behind a gateway: each tool the model loads is added to
`tools`, and the relay puts `tools` first in the cached prompt, so every load rewrote the conversation (four loads were
41% of the tokens written in the Claude Code dialog of 2026-11-06, one of them 33.8 credits at 336k tokens). A 400 makes the
client (2.1.305 and later; checked against 2.1.310 on a fake gateway) send the request again with all tools loaded."""
import copy

import pytest

import journal
import tool_search_policy as policy
from test_conversation_state import stored

CLAUDE = {"user-agent": "claude-cli/2.1.310 (external, sdk-cli)", "x-claude-code-session-id": "11111111-2222-3333-4444-555555555555"}
SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def request(*, deferred=True, referenced=False):
    tools = [{"name": "Read", "description": "Read a file", "input_schema": SCHEMA},
             {"name": "ToolSearch", "description": "Search for tools", "input_schema": SCHEMA}]
    if deferred:
        tools.append({"name": "DeferredToolPlaceholder", "description": "keeps deferred loading active", "input_schema": SCHEMA,
                      "defer_loading": True})
    messages = [{"role": "user", "content": "use the demo tool"}]
    if referenced:
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "ToolSearch", "input": {"query": "x"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": [{"type": "tool_reference", "tool_name": "mcp__demo__ping"}]}]}]
    return {"model": "claude-opus-5-5", "max_tokens": 64, "system": "You are Claude Code.", "messages": messages, "tools": tools}


def ask(relay, body, headers=None):
    return relay.json_any("/v1/messages", body, headers=headers or CLAUDE)


# -- the pieces --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("agent,version", [("claude-cli/2.1.310 (external, sdk-cli)", (2, 1, 310)), ("claude-code/2.1.304", (2, 1, 304)),
                                           ("Claude/2.1.317 (desktop)", (2, 1, 317)),
                                           ("claude-cli/2.1.317 (external, claude-desktop-3p, agent-sdk/0.3.317)", (2, 1, 317)),
                                           ("claude-cli/2.1.317 (external, local-agent, agent-sdk/0.3.317)", (2, 1, 317)),
                                           ("opencode/2.2.5", None), ("", None), (None, None)])
def test_the_version_is_read_from_the_user_agent_of_a_claude_client_only(agent, version):
    assert policy.client_version(agent) == version


def test_tool_search_is_found_in_the_tools_and_in_the_history_and_nowhere_else():
    assert policy.deferral(request()) == "2"
    assert policy.deferral(request(deferred=False, referenced=True)) == "reference"
    assert policy.deferral(request(deferred=False)) is None
    assert policy.deferral({"tools": "nope", "messages": [None, {"content": "text"}]}) is None
    plain = request(deferred=False)
    plain["tools"][0]["defer_loading"] = False
    assert policy.deferral(plain) is None


def test_the_error_names_what_the_client_matches_on():
    assert policy.message("2").startswith("tools.2.defer_loading: Extra inputs are not permitted.")
    assert policy.message("reference").startswith("messages: tool_reference blocks are not permitted.")


def test_a_conversation_is_refused_a_few_times_and_then_served():
    now = [0.0]
    guard = policy.LoopGuard(limit=3, window=100, clock=lambda: now[0])
    assert [guard.allow("a") for _ in range(4)] == [True, True, True, False]
    assert guard.allow("b") is True
    now[0] += 101
    assert guard.allow("a") is True


# -- the request path --------------------------------------------------------------------------------------------------

def test_a_request_that_carries_tool_search_is_refused_before_anything_is_spent(relay):
    seq = journal.JOURNAL.last_seq
    status, body = ask(relay, request())
    assert status == 400 and body["type"] == "error" and body["error"]["type"] == "invalid_request_error"
    assert "defer_loading" in body["error"]["message"] and not relay.fake.seen
    (event,) = stored(seq, "tool_search.refused")
    assert event["data"]["version"] == "2.1.310" and event["data"]["where"] == "2"
    assert all(item["level"] == "debug" for item in stored(seq, "http.error"))   # expected: not a warning of the relay


def test_the_request_the_client_sends_again_without_tool_search_is_served(relay):
    assert ask(relay, request())[0] == 400
    status, body = ask(relay, request(deferred=False))
    assert status == 200 and body["content"][0]["text"] == "ok"
    assert len(relay.fake.seen) == 1


def test_a_history_that_holds_tool_references_is_refused_too(relay):
    status, body = ask(relay, request(deferred=False, referenced=True))
    assert status == 400 and "tool_reference" in body["error"]["message"] and not relay.fake.seen


def test_a_stream_is_refused_with_a_real_status_not_an_event(relay):
    body = dict(request(), stream=True)
    status, payload = ask(relay, body)
    assert status == 400 and payload["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("agent", ["claude-cli/2.1.304 (external, sdk-cli)", "claude-cli/2.0.9", "claude-cli (external, sdk-cli)"])
def test_a_version_that_does_not_retry_is_served_as_it_asks(relay, agent):
    status, _ = ask(relay, request(), {**CLAUDE, "user-agent": agent})
    assert status == 200 and len(relay.fake.seen) == 1


def test_other_clients_are_served_as_they_ask(relay):
    status, _ = ask(relay, request(), {"user-agent": "opencode/2.2.5", "x-session-affinity": "ses_1"})
    assert status == 200


def test_the_old_behaviour_is_one_setting_away(relay):
    relay.cfg["claude_tool_search"] = "native"
    assert ask(relay, request())[0] == 200


@pytest.mark.parametrize("agent", ["claude-cli/2.1.317 (external, claude-desktop-3p, agent-sdk/0.3.317)",
                                   "claude-cli/2.1.317 (external, local-agent, agent-sdk/0.3.317)"])
def test_claude_desktop_and_its_cowork_agent_are_refused_like_the_cli(relay, agent):
    status, _ = ask(relay, request(), {**CLAUDE, "user-agent": agent})
    assert status == 400 and not relay.fake.seen


def test_a_client_that_keeps_sending_tool_search_is_served_after_a_few_refusals(relay):
    seq = journal.JOURNAL.last_seq
    statuses = [ask(relay, request())[0] for _ in range(policy.LOOP_LIMIT + 2)]
    assert statuses == [400] * policy.LOOP_LIMIT + [200, 200]
    assert len(stored(seq, "tool_search.refusal_loop")) == 2
    assert ask(relay, request(), {**CLAUDE, "x-claude-code-session-id": "another session"})[0] == 400


def test_a_subagent_is_counted_on_its_own(relay):
    for _ in range(policy.LOOP_LIMIT):
        assert ask(relay, request())[0] == 400
    assert ask(relay, request(), {**CLAUDE, "x-claude-code-agent-id": "agent-1"})[0] == 400


def test_the_refusals_after_each_compaction_do_not_add_up_while_the_client_falls_back_each_time(relay):
    """Claude Code asks for tool search again after every compaction and falls back after two refusals. Four such rounds in
    a few minutes are 8 refusals: the guard tripped at the third compaction (the deferred list was served, tools 29 -> 15,
    23,247 tokens written, 9bf6cbebe345). A request without tool search in between means the client is not looping."""
    seq = journal.JOURNAL.last_seq
    for _ in range(4):
        assert [ask(relay, request())[0] for _ in range(2)] == [400, 400]
        assert ask(relay, request(deferred=False))[0] == 200
    assert not stored(seq, "tool_search.refusal_loop")


def test_the_guard_counts_refusals_in_a_row_only():
    guard = policy.LoopGuard(limit=6, window=600, clock=lambda: 0.0)
    for _ in range(4):
        assert guard.allow("c") and guard.allow("c")
        guard.forgive("c")
    assert [guard.allow("c") for _ in range(7)] == [True] * 6 + [False]


def test_the_retry_without_tool_search_keeps_its_tools_in_the_list_the_model_gets(relay):
    body = request(deferred=False)
    body["tools"].append({"name": "mcp__demo__ping", "description": "Ping", "input_schema": SCHEMA})
    assert ask(relay, copy.deepcopy(body))[0] == 200
    assert [tool["function"]["name"] for tool in relay.fake.seen[-1].tools] == ["Read", "ToolSearch", "mcp__demo__ping"]
