"""S1 / plan I9: who owns a request, whether anything runs for it and which pool session it takes follow the request as the
client sent it. A profile that hides every tool must not turn a client's job into a relay task."""
import copy
import json
import types

import pytest

import executor
import orchestrator
import structured
from orchestrator import ClientOrigin, RelaySources, acquire_plan, client_origin, client_tools
from prompt_support import READ_FILE, client_request, head_of, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_review_fixes_pr19 import END, text

COMMAND = "<exec_command>echo PWNED</exec_command>"
TEMPLATE = "{{client_system}}{{relay_task}}{{exec_reminder}}"
DOCUMENT = {"version": 1, "profiles": {
    "plain": {"head": TEMPLATE},
    "hideall": {"head": TEMPLATE, "tools": {"edits": [{"tool": "*", "hide": True}]}}},
    "rules": [{"name": "by header", "if": {"header_profile": "*"}, "use": "$header"}]}
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]
RELAY_TASK_MARKS = ("[LOCAL EXECUTION]", "[PROJECT CONTEXT", "[LOCAL EXEC REMINDER]")


@pytest.fixture
def commands(monkeypatch):
    """The commands the relay was asked to run on this machine: none of these tests may see one."""
    ran = []
    monkeypatch.setattr(executor, "run_command", lambda command, *a, **k: ran.append(command) or (0, "SHELL OUTPUT"))
    return ran


def served(prompt_relay, tmp_path):
    (tmp_path / "prompts.json").write_text(json.dumps(DOCUMENT), encoding="utf-8")
    (tmp_path / "README.md").write_text("SYNTHETIC README OF THE PROJECT\n", encoding="utf-8")
    return prompt_relay(exec_policy="allow_all", exec_allowlist=[], project_context=True)


def ask(relay, profile, conversation, system=""):
    """One turn of a client that sent tools; the model answers with a shell command in text."""
    relay.scripts.append([text(COMMAND), *END])
    headers = {"x-session-affinity": conversation}
    if profile:
        headers["x-proxi-profile"] = profile
    status, payload, _ = relay.post("chat", client_request("chat", USER, system=system, tools=True), headers)
    assert status == 200, payload
    return payload


@pytest.mark.parametrize("profile", [None, "plain", "hideall"])
def test_a_client_with_tools_and_no_system_never_gets_the_shell_protocol_or_runs_a_command(
        prompt_relay, tmp_path, commands, profile):
    relay = served(prompt_relay, tmp_path)
    payload = ask(relay, profile, "dialog-" + str(profile))
    head = head_of(relay.posts[-1]) or ""
    assert not [mark for mark in RELAY_TASK_MARKS if mark in head], head[:300]
    assert "SYNTHETIC README" not in json.dumps(relay.posts[-1])  # no project context either
    assert commands == [], "the relay ran a command the client's own job never asked for"
    assert payload["choices"][0]["message"]["content"] == COMMAND  # the client gets the model's text as it is
    assert ("tools" in relay.posts[-1] and bool(relay.posts[-1]["tools"])) == (profile != "hideall")


def test_the_session_of_a_conversation_is_the_same_with_the_tools_hidden_as_without_a_profile(prompt_relay, tmp_path,
                                                                                            commands):
    relay = served(prompt_relay, tmp_path)
    ask(relay, None, "dialog-none")
    ask(relay, "hideall", "dialog-hidden")
    conversations = relay.runtime.pool._conversations
    assert "header:dialog-none" in conversations and "header:dialog-hidden" in conversations
    assert ("header:dialog-hidden", "text") not in conversations and ("header:dialog-none", "text") not in conversations


def test_hiding_the_tools_does_not_make_the_request_a_text_chat_for_the_pool(prompt_relay, tmp_path, commands,
                                                                           monkeypatch):
    relay = served(prompt_relay, tmp_path)
    pool, asked = relay.runtime.pool, []
    refusal, acquire = pool.model_refusal, pool.acquire

    async def spy(key, **kwargs):
        asked.append({name: value for name, value in kwargs.items() if name in ("isolated", "context_scope")})
        return await acquire(key, **kwargs)

    monkeypatch.setattr(pool, "model_refusal", lambda entry, **scope: asked.append(("refusal", scope)) or refusal(
        entry, **scope))
    monkeypatch.setattr(pool, "acquire", spy)
    ask(relay, "hideall", "dialog-spied")
    assert asked == [("refusal", {}), {}]  # one question before the answer starts, and acquire says the same


def test_the_journal_says_the_head_was_empty_and_names_no_session_of_the_text_chat(prompt_relay, tmp_path, commands):
    relay = served(prompt_relay, tmp_path)
    mark = relay.mark()
    ask(relay, "hideall", "dialog-journal")
    (acquire,) = relay.events(mark, "session.acquire")
    assert acquire["data"]["context_scope"] is None and acquire["data"]["isolated"] is False
    assert not relay.events(mark, "exec.start")
    (plan,) = relay.events(mark, "prompt.plan")
    assert plan["data"]["head_chars"] == 0 and plan["data"]["profile"] == "hideall"


def test_a_client_that_sent_a_system_prompt_is_unchanged_by_what_is_hidden(prompt_relay, tmp_path, commands):
    relay = served(prompt_relay, tmp_path)
    ask(relay, "hideall", "dialog-system", system="CLIENT_SYSTEM")
    assert head_of(relay.posts[-1]) == "CLIENT_SYSTEM" and commands == []
    assert "header:dialog-system" in relay.runtime.pool._conversations


# -- the pieces ---------------------------------------------------------------------------------------------------
def parsed_request(**fields):
    base = dict(tools=[], system="", request_purpose="client-tools", conversation_key="header:k",
                parent_conversation_key=None, structured_output=None)
    return types.SimpleNamespace(**{**base, **fields})


CFG = {"transport": "direct", "session_mode": "per_conversation", "exec_policy": "allow_all", "max_exec_turns": 0}


def test_the_origin_keeps_the_three_facts_ownership_depends_on():
    tool = {"type": "function", "function": READ_FILE}
    assert client_origin(parsed_request(tools=[tool], system="S", structured_output={"kind": "json_object"})) == (
        ClientOrigin(tools=True, system=True, structured=True))
    assert client_origin(parsed_request(system=" \n ")) == ClientOrigin(tools=False, system=False, structured=False)
    assert client_origin(parsed_request()) == ClientOrigin(False, False, False)


def test_a_request_with_an_origin_is_judged_by_it_and_one_without_is_judged_by_its_own_body():
    tool = {"type": "function", "function": READ_FILE}
    client = parsed_request(tools=[tool])
    hidden = parsed_request(tools=[], prompt_origin=client_origin(client))
    assert client_tools(client) is True and client_tools(hidden) is True
    assert acquire_plan(CFG, hidden) == acquire_plan(CFG, client) and acquire_plan(CFG, hidden).kind == "session"
    # without the origin the same tool-less body is a text chat, as it always was
    assert client_tools(parsed_request()) is False and acquire_plan(CFG, parsed_request()).kind == "text"
    assert orchestrator._client_owns_instructions(hidden, "client-tools", []) is False
    # a JSON-only reply the client asked for is its own job when it brought no tools, and not when it did
    spec = {"kind": "json_schema", "name": "r", "schema": {"type": "object"}, "source": "response_format"}
    bare = parsed_request(structured_output=spec)
    assert orchestrator._client_owns_instructions(bare, "generation", []) is True
    with_tools = parsed_request(tools=[tool], structured_output=spec)
    emptied = parsed_request(structured_output=spec, prompt_origin=client_origin(with_tools))
    assert orchestrator._client_owns_instructions(emptied, "generation", []) is False
    assert orchestrator._client_owns_instructions(with_tools, "generation", [tool]) is False


def test_the_sources_of_a_template_are_those_of_a_client_with_tools_when_the_model_is_offered_none(tmp_path):
    (tmp_path / "README.md").write_text("SYNTHETIC README\n", encoding="utf-8")
    cfg = {"workspace_root": str(tmp_path), "project_context": True, "exec_policy": "allowlist",
           "context_max_bytes": 6000, "cookie_values": []}
    snapshot = types.SimpleNamespace(relay_text=lambda name: None, _data=types.SimpleNamespace(relay_templates={}))
    spec = {"kind": "json_schema", "name": "r", "schema": {"type": "object"}, "description": None, "source": "x"}
    hidden = RelaySources(cfg, snapshot, client_owned=False, native_tools=False, client_tools=True, output_spec=spec)
    assert (hidden.relay_task(), hidden.local_exec(), hidden.project_context(), hidden.exec_reminder()) == ("",) * 4
    # what the model is offered still decides how the structured contract reads
    assert hidden.structured() == structured.instructions(spec, tools=False, tool_choice="auto")
    task = RelaySources(cfg, snapshot, client_owned=False, native_tools=False)
    assert task.relay_task() and task.exec_reminder()
    assert RelaySources(cfg, snapshot, client_owned=False, native_tools=True).client_tools is True


def test_an_origin_is_not_part_of_what_the_client_trace_calls_the_validated_request():
    parsed = parsed_request(prompt_origin=ClientOrigin(True, False, False), prompt_plan=object())
    shown = {key: value for key, value in vars(parsed).items() if not key.startswith("prompt_")}
    assert "prompt_origin" not in shown
    assert copy.deepcopy(ClientOrigin(True, False, False)) == (True, False, False)
