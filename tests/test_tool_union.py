"""A client that declares its tools late is given the list it will have, so the cached prefix stops changing under it.

Codex starts its MCP servers after the first request: request 1 carries 44-46 tools and request 2 50-60 (27 of 27
models in the Codex test run of 2026-11-06, a2). The tool list is part of the cached prefix and of the key of a signed
branch, so each change was a cache rewrite and a reset (31 credits at 334k tokens, 22 times a warm request), and
`codex exec resume`, a new process per turn, paid it twice per turn."""
import copy
import json

import pytest

import tool_union
from prompt_support import client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_conversation_state import signatures_in, stored
from test_review_fixes_pr19 import END, THINKING, signature, text
from tool_union import ToolUnion, client_key, project_of

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def tool(name, description="does " ):
    return {"type": "function", "name": name, "description": description + name, "parameters": SCHEMA}


def codex(thread="t1", *, originator="codex_exec", agent=None):
    headers = {"originator": originator, "user-agent": "%s/0.167.0 (Windows 10.0.26300; x86_64) xterm-256color" % originator,
               "session-id": thread, "x-codex-window-id": thread + ":0"}
    if agent:
        headers["x-openai-subagent"] = agent
    return headers


def request(names, *, folder="C:\\work\\proj", messages=None, model="claude-opus-5-5"):
    environment = {"role": "user", "content": [{"type": "input_text", "text":
                   "<environment_context>\n  <cwd>%s</cwd>\n</environment_context>" % folder}]}
    body = client_request("responses", [environment] + (copy.deepcopy(messages) if messages
                                                         else [{"role": "user", "content": "inspect ORIGINAL_TASK"}]),
                          system="BASE", tools=False, model=model)
    body["tools"] = [tool(name) for name in names]
    return body


def names_upstream(relay):
    return [item["name"] for item in relay.posts[-1]["tools"]]


def call(call_id, name, arguments, sig="opaque-one"):
    return [*THINKING, signature(sig), {"event": "content_block_end", "type": "thinking"},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": name},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps(arguments)},
            {"event": "content_block_end", "type": "tool_call", "toolCallId": call_id}, *END]


def send(relay, names, headers, script=None, **kwargs):
    relay.scripts.append(script if script is not None else [text("ok"), *END])
    status, payload, _ = relay.post("responses", request(names, **kwargs), headers)
    assert status == 200, payload
    return payload


# -- the registry ----------------------------------------------------------------------------------------------------

def parsed_with(*names, **fields):
    from types import SimpleNamespace
    tools = [{"type": "function", "function": {"name": name, "parameters": SCHEMA}} for name in names]
    return SimpleNamespace(**{"tools": tools, "system": "", "prompt": "", "model": "m", "prompt_plan": None, **fields})


def test_a_request_gets_its_own_tools_and_those_its_client_declared_before():
    union = ToolUnion()
    key = client_key("codex", codex(), parsed_with("a", "b", "c"))
    assert union.apply(key, parsed_with("a", "b", "c").tools) == (parsed_with("a", "b", "c").tools, [])
    merged, carried = union.apply(key, parsed_with("a", "b").tools)
    assert carried == ["c"] and [t["function"]["name"] for t in merged] == ["a", "b", "c"]


def test_the_newest_definition_of_a_tool_the_client_names_wins_and_a_carried_one_keeps_its_last():
    union = ToolUnion()
    key = ("codex", "o", "m", "root", "", "", 0)
    old = [{"type": "function", "function": {"name": "a", "description": "old", "parameters": SCHEMA}},
           {"type": "function", "function": {"name": "b", "description": "kept", "parameters": SCHEMA}}]
    new = [{"type": "function", "function": {"name": "a", "description": "new", "parameters": SCHEMA}}]
    union.apply(key, old)
    merged, carried = union.apply(key, new)
    assert carried == ["b"] and {t["function"]["name"]: t["function"]["description"] for t in merged} == {"a": "new", "b": "kept"}


def test_a_tool_not_declared_for_the_ttl_is_dropped_and_the_union_is_bounded():
    now = [0.0]
    union = ToolUnion(ttl=100, max_clients=2, max_tools=3, clock=lambda: now[0])
    key = ("k",)
    union.apply(key, parsed_with("a", "b").tools)
    now[0] += 101
    assert union.apply(key, parsed_with("c").tools)[1] == []   # a and b were not declared for a while
    union.apply(key, parsed_with("d", "e", "f", "g").tools)
    assert len(union) <= 3
    for number in range(4):
        union.apply(("client", number), parsed_with("a").tools)
    assert len(union._clients) <= 2


def test_a_giant_tool_definition_is_not_carried():
    union = ToolUnion()
    big = [{"type": "function", "function": {"name": "big", "description": "x" * (tool_union.MAX_TOOL_CHARS + 1), "parameters": SCHEMA}}]
    union.apply(("k",), big)
    assert union.apply(("k",), parsed_with("a").tools)[1] == []


def test_the_key_keeps_clients_models_agents_projects_and_profiles_apart():
    base = client_key("codex", codex(), parsed_with("a"))
    assert base[:2] == ("codex", "codex_exec") and base[3] == "root"
    assert client_key("codex", codex(originator="Codex Desktop"), parsed_with("a")) != base
    assert client_key("codex", codex(agent="collab_spawn"), parsed_with("a"))[3] == "sub"
    other_model = parsed_with("a")
    other_model.model = "gpt-6.1-sol"
    assert client_key("codex", codex(), other_model) != base
    assert client_key("codex", codex(), parsed_with("a")) == base
    assert client_key("codex", codex(), parsed_with()) is None  # nothing declared: nothing to keep a prefix for


def test_the_project_is_the_folder_a_request_names():
    assert project_of(parsed_with("a", prompt="User: <environment_context><cwd>C:/Work/Proj/</cwd></environment_context>")) == "c:\\work\\proj"
    from types import SimpleNamespace
    assert project_of(SimpleNamespace(system="<env>\n  Working directory: D:\\x\n</env>", prompt="")) == "d:\\x"
    assert project_of(SimpleNamespace(system="", prompt="no folder here")) == ""


# -- the request path --------------------------------------------------------------------------------------------------

def test_a_new_codex_conversation_that_starts_with_fewer_tools_declares_the_tools_the_next_request_will_bring(prompt_relay):
    relay = prompt_relay()
    mark = relay.mark()
    send(relay, ["a", "b"], codex("t1"))
    assert names_upstream(relay) == ["a", "b"]
    send(relay, ["a", "b", "c"], codex("t1"))            # the late server came up: the one change that is the client's
    settled = relay.posts[-1]
    assert names_upstream(relay) == ["a", "b", "c"]
    send(relay, ["a", "b"], codex("t2"))                 # a new thread, the server not up yet
    assert names_upstream(relay) == ["a", "b", "c"]
    assert relay.posts[-1]["openAICacheKey"] == settled["openAICacheKey"]
    send(relay, ["a", "b", "c"], codex("t2"))
    assert names_upstream(relay) == ["a", "b", "c"] and relay.posts[-1]["openAICacheKey"] == settled["openAICacheKey"]
    (carried,) = stored(mark, "tools.carried")
    assert carried["data"]["declared"] == 2 and carried["data"]["carried"] == 1 and carried["data"]["names"] == ["c"]
    # one announcement per change of what is carried, not one per request
    assert len(stored(mark, "tools.carried")) == 1


def test_codex_exec_resume_starts_each_turn_with_fewer_tools_and_the_signed_history_still_resumes(prompt_relay):
    relay = prompt_relay()
    mark = relay.mark()
    thread = codex("t1")
    send(relay, ["a", "b", "c"], thread, call("c1", "a", {"q": "x"}))
    first = relay.posts[-1]
    messages = [{"role": "user", "content": "inspect ORIGINAL_TASK"},
                {"type": "function_call", "call_id": "c1", "name": "a", "arguments": json.dumps({"q": "x"})},
                {"type": "function_call_output", "call_id": "c1", "output": "result"}]
    send(relay, ["a", "b"], thread, messages=messages)   # the next process: two of the servers are not up
    assert names_upstream(relay) == ["a", "b", "c"]
    assert len(relay.events(mark, "native_history.resume")) == 1 and not relay.events(mark, "cache.tools_changed")
    assert signatures_in(relay.posts[-1]) == ["opaque-one"] and relay.posts[-1]["openAICacheKey"] == first["openAICacheKey"]


def test_a_call_of_a_carried_tool_reaches_the_client_and_a_call_of_an_unknown_one_still_does_not(prompt_relay):
    relay = prompt_relay()
    send(relay, ["a", "b", "c"], codex("t1"))
    mark = relay.mark()
    payload = send(relay, ["a", "b"], codex("t2"), call("c1", "c", {"q": "x"}))
    assert [(item["name"]) for item in payload["output"] if item["type"] == "function_call"] == ["c"]
    (made,) = [event for event in relay.events(mark, "tool.call")]
    assert made["data"]["name"] == "c" and made["data"]["carried"] is True
    mark = relay.mark()
    relay.scripts.append(call("c2", "never_declared", {"q": "x"}))
    status, _, _ = relay.post("responses", request(["a", "b"]), codex("t3"))
    assert status == 502  # what a turn of nothing but undeclared calls always was (the remainder of audit item C4)
    assert relay.events(mark, "tool.call_dropped") and not relay.events(mark, "tool.call")


@pytest.mark.parametrize("other", [
    {"folder": "C:\\another\\project"},
    {"model": "gpt-6.1-sol"},
])
def test_another_project_or_model_does_not_inherit_the_tools(prompt_relay, other):
    relay = prompt_relay()
    send(relay, ["a", "b", "c"], codex("t1"))
    send(relay, ["a", "b"], codex("t2"), **other)
    assert names_upstream(relay) == ["a", "b"]


def test_a_sub_agent_and_a_different_originator_keep_their_own_lists(prompt_relay):
    relay = prompt_relay()
    send(relay, ["a", "b", "c"], codex("t1"))
    send(relay, ["a", "b"], codex("t2", agent="collab_spawn"))
    assert names_upstream(relay) == ["a", "b"]
    send(relay, ["a", "b"], codex("t3", originator="Codex Desktop"))
    assert names_upstream(relay) == ["a", "b"]


def test_other_clients_get_exactly_the_tools_they_declare(prompt_relay):
    relay = prompt_relay()
    opencode = {"user-agent": "opencode/1.19.3 ai-sdk/provider-utils/3.0.20 runtime/bun/1.3.5", "x-opencode-session": "s1"}
    send(relay, ["a", "b", "c"], opencode)
    send(relay, ["a", "b"], opencode)
    assert names_upstream(relay) == ["a", "b"] and len(relay.runtime.tool_union) == 0


def test_a_claude_code_session_that_starts_before_its_mcp_server_declares_the_tools_the_server_will_bring(prompt_relay):
    """Claude Code starts MCP servers in the background: request 1 of an interactive session can go out without a slow
    server's tools (59 against 90 in a real session, 107,650 tokens written by request 2). A later session of the project
    is given the tools its sessions declared before, so nothing changes upstream when the server comes up."""
    relay = prompt_relay()
    claude = {"user-agent": "claude-cli/2.1.310 (external, cli)", "x-claude-code-session-id": "s1"}
    send(relay, ["a", "b", "c"], claude)
    send(relay, ["a", "b"], dict(claude, **{"x-claude-code-session-id": "s2"}))
    assert names_upstream(relay) == ["a", "b", "c"]


CLAUDE_CODE_SYSTEM = ("You are Claude Code, Anthropic's official CLI for Claude.\n\nYou are an interactive agent.\n\n<env>\n"
                      " - Primary working directory: C:\\work\\proj\n - Is a git repository: true\n</env>")


def claude_code(relay, session, attribution, names, deferred=()):
    """A Claude Code request as it is sent: Messages, the attribution line first (its version ends in three characters
    drawn from the session's first message), the environment bullet, and maybe tool search (tools with defer_loading)."""
    body = client_request("anthropic", [{"role": "user", "content": "inspect ORIGINAL_TASK"}],
                          system=[{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.310.%s; "
                                                           "cc_entrypoint=cli;" % attribution},
                                  {"type": "text", "text": CLAUDE_CODE_SYSTEM}], tools=False)
    body["tools"] = ([{"name": name, "description": "does " + name, "input_schema": SCHEMA} for name in names]
                     + [{"name": name, "description": "does " + name, "input_schema": SCHEMA, "defer_loading": True}
                        for name in deferred])
    relay.scripts.append([text("ok"), *END])
    headers = {"user-agent": "claude-cli/2.1.310 (external, cli)", "x-claude-code-session-id": session}
    status, payload, _ = relay.post("anthropic", body, headers)
    assert status == 200, payload
    return names_upstream(relay)


def test_claude_code_sessions_of_a_project_share_their_tools_whatever_their_attribution(prompt_relay):
    relay = prompt_relay()
    assert claude_code(relay, "s1", "2e4", ["a", "b", "c"]) == ["a", "b", "c"]
    assert claude_code(relay, "s2", "6cd", ["a", "b"]) == ["a", "b", "c"]


def test_a_request_with_tool_search_neither_feeds_the_union_nor_is_given_it(prompt_relay):
    """Served as it asks (claude_tool_search "native", or after a loop), a request with tool search declares the deferred
    list: the eager sessions' tools are not added to it, and its ToolSearch and deferred tools reach no other session."""
    relay = prompt_relay(claude_tool_search="native")
    claude_code(relay, "s1", "2e4", ["a", "b", "c"])
    deferred = claude_code(relay, "s2", "6cd", ["a", "ToolSearch"], deferred=["mcp__srv__slow"])
    assert "b" not in deferred and "c" not in deferred
    assert claude_code(relay, "s3", "29f", ["a", "b"]) == ["a", "b", "c"]


def test_the_union_can_be_switched_off(prompt_relay):
    relay = prompt_relay(tool_union_clients=[])
    send(relay, ["a", "b", "c"], codex("t1"))
    send(relay, ["a", "b"], codex("t2"))
    assert names_upstream(relay) == ["a", "b"]


def test_a_request_without_tools_is_left_without_tools(prompt_relay):
    relay = prompt_relay()
    send(relay, ["a", "b", "c"], codex("t1"))
    relay.scripts.append([text("summary"), *END])
    body = request([])
    body.pop("tools")
    assert relay.post("responses", body, codex("t2"))[0] == 200
    assert not relay.posts[-1].get("tools")


def test_a_tool_the_profile_hides_is_not_carried_in_from_a_conversation_whose_hide_was_refused(prompt_relay, tmp_path):
    # a conversation whose history called c keeps c (hide is refused for it, as documented), and the union stored it
    (tmp_path / "prompts.json").write_text(json.dumps({
        "version": 1, "default_profile": "hider",
        "profiles": {"hider": {"tools": {"edits": [{"tool": "c", "hide": True}]}}}}), encoding="utf-8")
    relay = prompt_relay()
    called = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
              {"type": "function_call", "call_id": "x1", "name": "c", "arguments": "{}"},
              {"type": "function_call_output", "call_id": "x1", "output": "done"}]
    send(relay, ["a", "b", "c"], codex("t1"), messages=called)
    assert names_upstream(relay) == ["a", "b", "c"]
    send(relay, ["a", "b", "c"], codex("t2"))  # a new conversation: c is hidden, and must not come back from the union
    assert names_upstream(relay) == ["a", "b"]


def test_a_tool_another_session_declares_does_not_change_the_list_of_a_running_conversation(prompt_relay):
    relay = prompt_relay()
    send(relay, ["a", "b"], codex("t1"))
    assert names_upstream(relay) == ["a", "b"]
    send(relay, ["a", "b", "d"], codex("t2"))  # another session of the same project, with one more server
    send(relay, ["a", "b"], codex("t1"))       # the running conversation keeps its list: no cache rewrite, no reset
    assert names_upstream(relay) == ["a", "b"]
    send(relay, ["a", "b"], codex("t3"))       # a new conversation still gets the whole union (late servers)
    assert names_upstream(relay) == ["a", "b", "d"]


def test_a_running_conversation_keeps_a_carried_tool_after_the_union_expired_it():
    clock = [1000.0]
    union = ToolUnion(ttl=100, clock=lambda: clock[0])
    key = ("codex", "codex_exec", "m", "root", "c:/p", "", "", "")
    first = parsed_with("a", "b", "late").tools
    union.apply(key, first, "conv-1")
    clock[0] += 500  # "late" was not declared by anyone for longer than the ttl
    merged, carried = union.apply(key, parsed_with("a", "b").tools, "conv-1")
    assert [tool["function"]["name"] for tool in merged] == ["a", "b", "late"] and carried == ["late"]
    merged, _ = union.apply(key, parsed_with("a", "b").tools, "conv-2")  # a new conversation: the union has dropped it
    assert [tool["function"]["name"] for tool in merged] == ["a", "b"]
