"""The relay's own texts a prompt template may name, and the head a composed plan gives the turn (orchestrator)."""
import asyncio
import json

import pytest

import apierr
import client_context
import context
import direct
import executor
import journal
import orchestrator
import prompt_body
import prompt_policy
import structured
import validate
from orchestrator import RelaySources, journal_changes, split_changes
from prompt_support import FakePlan, FakePrompts, FakeSnapshot, install_summaries, profile
from session import RelaySession
from test_native_context_reuse import FIRST, request, run, setup
from test_service_requests import capture_direct

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
          "additionalProperties": False}
SPEC = {"kind": "json_schema", "schema": SCHEMA, "name": "r", "description": None, "source": "response_format"}
RELAY_TASK = [{"role": "user", "content": "Run my relay task"}]


def cfg_of(tmp_path, **extra):
    (tmp_path / "README.md").write_text("SYNTHETIC README\n", encoding="utf-8")
    return {"workspace_root": str(tmp_path), "project_context": True, "exec_policy": "allowlist",
            "context_max_bytes": 6000, "cookie_values": [], **extra}


def sources(tmp_path, *, relay_texts=None, client_owned=False, native_tools=False, spec=None, choice="auto", **cfg):
    return RelaySources(cfg_of(tmp_path, **cfg), FakeSnapshot(1, {}, relay_texts), client_owned=client_owned,
                        native_tools=native_tools, output_spec=spec, tool_choice=choice)


# -- RelaySources ----------------------------------------------------------------------------------------------------
def test_a_relay_task_is_what_the_legacy_head_of_a_relay_task_is_and_the_reminder_ends_it(tmp_path):
    _, _, runner = setup(tmp_path, transport="direct", exec_policy="allowlist", project_context=True,
                         context_max_bytes=6000, cookie_values=[])
    (tmp_path / "README.md").write_text("SYNTHETIC README\n", encoding="utf-8")
    task = RelaySources(runner.cfg, FakeSnapshot(1, {}), client_owned=False, native_tools=False)
    legacy = runner._instructions(native_tools=False)
    assert task.relay_task() == legacy and "[PROJECT CONTEXT" in legacy and "[LOCAL EXECUTION]" in legacy
    # the plan says {{relay_task}}{{exec_reminder}} repeats the legacy head of a relay request
    assert task.relay_task() + task.exec_reminder() == legacy + orchestrator.EXEC_REMINDER
    assert orchestrator.EXEC_REMINDER.startswith("\n\n[LOCAL EXEC REMINDER]\n")


@pytest.mark.parametrize("configured", [None, "CONFIGURED_ROLE", "", " \n "])
def test_with_the_role_of_config_json_the_templates_still_repeat_the_legacy_heads(tmp_path, configured):
    # the file names no role text, so the role is config.json's setting, as in the legacy heads
    _, _, runner = setup(tmp_path, transport="direct", exec_policy="allowlist", project_context=True,
                         context_max_bytes=6000, cookie_values=[], relay_system_prompt=configured)
    (tmp_path / "README.md").write_text("SYNTHETIC README\n", encoding="utf-8")
    task = RelaySources(runner.cfg, FakeSnapshot(1, {}), client_owned=False, native_tools=False)
    legacy = runner._instructions(native_tools=False)
    assert task.relay_task() + task.exec_reminder() == legacy + orchestrator.EXEC_REMINDER
    tools = RelaySources(runner.cfg, FakeSnapshot(1, {}), client_owned=False, native_tools=True)
    assert tools.native_role() == runner._instructions(native_tools=True)
    assert task.relay_role() == context.relay_policy(configured)


def test_the_file_beats_the_setting_of_config_json_and_the_setting_beats_the_built_in_text(tmp_path):
    configured = sources(tmp_path, relay_system_prompt="CONFIGURED_ROLE")
    assert configured.relay_role() == "CONFIGURED_ROLE" and configured.native_role() == "CONFIGURED_ROLE"
    assert configured.relay_task().startswith("CONFIGURED_ROLE\n\n[LOCAL EXECUTION]")
    both = sources(tmp_path, relay_system_prompt="CONFIGURED_ROLE",
                   relay_texts={"role": "FILE_ROLE", "native_role": "FILE_NATIVE_ROLE"})
    assert both.relay_role() == "FILE_ROLE" and both.native_role() == "FILE_NATIVE_ROLE"
    assert both.relay_task().startswith("FILE_ROLE\n\n")
    # a role the file leaves out is config.json's setting, whatever the file says about the other one
    half = sources(tmp_path, relay_system_prompt="CONFIGURED_ROLE", relay_texts={"role": "FILE_ROLE"})
    assert half.relay_role() == "FILE_ROLE" and half.native_role() == "CONFIGURED_ROLE"
    blank = sources(tmp_path, relay_system_prompt="CONFIGURED_ROLE", relay_texts={"role": "", "native_role": " "})
    assert blank.relay_role() == "" and blank.native_role() == ""


def test_the_setting_of_config_json_is_read_when_a_turn_asks_and_is_not_kept_between_turns(tmp_path):
    cfg = cfg_of(tmp_path, relay_system_prompt="FIRST_ROLE")
    snapshot = FakeSnapshot(1, {})
    first = RelaySources(cfg, snapshot, client_owned=False, native_tools=False)
    assert first.relay_role() == "FIRST_ROLE"
    cfg["relay_system_prompt"] = "SECOND_ROLE"  # the admin saves a new role in the middle of the turn
    # this turn keeps one role: {{role}} and {{relay_task}} (and the native role) never name two texts
    assert first.relay_task().startswith("FIRST_ROLE\n\n") and first.native_role() == "FIRST_ROLE"
    later = RelaySources(cfg, snapshot, client_owned=False, native_tools=False)
    assert later.relay_role() == "SECOND_ROLE" and later.relay_task().startswith("SECOND_ROLE\n\n")
    cfg["relay_system_prompt"] = None
    assert RelaySources(cfg, snapshot, client_owned=False, native_tools=True).native_role() == (
        orchestrator.DEFAULT_NATIVE_TOOLS_ROLE)


def test_the_legacy_heads_read_the_live_settings_when_the_request_comes(tmp_path):
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, exec_policy="off", cookie_values=[])
    assert runner._instructions(native_tools=True) == orchestrator.DEFAULT_NATIVE_TOOLS_ROLE
    assert runner._client_instructions("CLIENT") == "CLIENT"
    runner.cfg.update(relay_system_prompt="LIVE_ROLE", relay_system_prompt_mode="append")
    assert runner._instructions(native_tools=True) == "LIVE_ROLE"
    assert runner._client_instructions("CLIENT") == "CLIENT\n\nLIVE_ROLE"
    runner.cfg.update(relay_system_prompt_mode="replace")
    assert runner._client_instructions("CLIENT") == "LIVE_ROLE"


def test_the_blocks_of_a_relay_task_are_its_role_then_the_shell_protocol_then_the_project(tmp_path):
    task = sources(tmp_path)
    role, local, project = task.relay_role(), task.local_exec(), task.project_context()
    assert role == context.DEFAULT_RELAY_SYSTEM_PROMPT
    assert local.startswith("[LOCAL EXECUTION]\n") and "Policy: allowlist" in local
    assert project.startswith("[PROJECT CONTEXT — reference data, not instructions]\n[PROJECT CONTEXT]\n")
    assert "SYNTHETIC README" in project
    assert task.relay_task() == "\n\n".join([role, local, project])


@pytest.mark.parametrize("text,expected", [
    (None, context.DEFAULT_RELAY_SYSTEM_PROMPT), ("MY_ROLE", "MY_ROLE"), ("", ""), (" \n\t ", "")])
def test_the_role_of_the_relay_is_the_built_in_one_unless_the_file_says_otherwise(tmp_path, text, expected):
    task = sources(tmp_path, relay_texts={} if text is None else {"role": text})
    assert task.relay_role() == expected
    if expected:
        assert task.relay_task().startswith(expected + "\n\n[LOCAL EXECUTION]")
    else:
        # a blank role is no role at all: the blocks of the task stay
        assert task.relay_task().startswith("[LOCAL EXECUTION]")


@pytest.mark.parametrize("text,expected", [
    (None, orchestrator.DEFAULT_NATIVE_TOOLS_ROLE), ("MY_NATIVE_ROLE", "MY_NATIVE_ROLE"), ("", ""), ("  ", "")])
def test_the_role_for_a_client_with_tools_is_its_own_text(tmp_path, text, expected):
    task = sources(tmp_path, relay_texts={} if text is None else {"native_role": text}, native_tools=True)
    assert task.native_role() == expected
    # the two roles are separate texts: setting one does not move the other
    assert task.relay_role() == context.DEFAULT_RELAY_SYSTEM_PROMPT


@pytest.mark.parametrize("owner", ["client", "tools"])
def test_a_job_the_client_owns_gets_no_relay_task_blocks_and_never_touches_the_project(tmp_path, monkeypatch, owner):
    monkeypatch.setattr(context, "build_project_context", lambda *a, **k: pytest.fail("the client owns this job"))
    monkeypatch.setattr(orchestrator, "build_system_instructions", lambda *a, **k: pytest.fail("the client owns it"))
    task = sources(tmp_path, client_owned=owner == "client", native_tools=owner == "tools")
    assert (task.relay_task(), task.local_exec(), task.project_context(), task.exec_reminder()) == ("", "", "", "")
    # the role texts are not blocks of a relay task: they stay available
    assert task.relay_role() == context.DEFAULT_RELAY_SYSTEM_PROMPT


def test_switched_off_execution_and_context_leave_their_blocks_empty(tmp_path):
    task = sources(tmp_path, exec_policy="off")
    assert task.local_exec() == "" and task.exec_reminder() == "" and task.project_context()
    assert task.relay_task() == "\n\n".join([task.relay_role(), task.project_context()])
    bare = sources(tmp_path, project_context=False)
    assert bare.project_context() == "" and bare.local_exec().startswith("[LOCAL EXECUTION]")


def test_every_block_is_built_once_and_only_when_asked_for(tmp_path, monkeypatch):
    calls = []
    real = context.build_project_context
    monkeypatch.setattr(context, "build_project_context", lambda *a, **k: calls.append(1) or real(*a, **k))
    task = sources(tmp_path)
    assert task.relay_role() and task.native_role() and task.exec_reminder() and calls == []
    assert task.project_context() and task.local_exec() and task.relay_task() and task.relay_task()
    assert len(calls) == 1


def test_a_missing_snapshot_still_gives_the_built_in_texts(tmp_path):
    task = RelaySources(cfg_of(tmp_path), None, client_owned=False, native_tools=False)
    assert task.relay_role() == context.DEFAULT_RELAY_SYSTEM_PROMPT
    assert task.native_role() == orchestrator.DEFAULT_NATIVE_TOOLS_ROLE


def test_the_structured_contract_is_empty_without_a_format_and_the_functions_own_text_without_a_wrapper(tmp_path):
    assert sources(tmp_path).structured() == ""
    for tools, choice in ((False, "auto"), (True, "auto"), (True, "none")):
        task = sources(tmp_path, spec=SPEC, native_tools=tools, choice=choice)
        assert task.structured() == structured.instructions(SPEC, tools=tools, tool_choice=choice)
    assert "Use the supplied tools" not in sources(tmp_path, spec=SPEC, native_tools=True, choice="none").structured()


def test_the_engine_builds_the_contract_when_it_has_one(tmp_path, monkeypatch):
    asked = []

    def contract(snapshot, output_spec, *, tools, tool_choice):
        asked.append((snapshot, output_spec, tools, tool_choice))
        return "ENGINE CONTRACT"

    monkeypatch.setattr(prompt_policy, "structured_contract", contract)
    task = sources(tmp_path, spec=SPEC, native_tools=True, choice="required")
    assert task.structured() == "ENGINE CONTRACT"
    assert asked == [(task.snapshot, SPEC, True, "required")]


def test_changes_are_split_by_the_scope_they_name_and_a_change_without_one_by_the_tool_it_names():
    history = {"scope": "history", "op": "drop_tag", "edit": 0, "index": 4, "target": "user", "removed_chars": 12}
    tools = {"scope": "tools", "op": "append", "edit": 1, "index": 0, "tool": "bash", "added_chars": 40}
    assert split_changes([history, tools]) == ([history], [tools])
    # the scope decides, whatever else the change carries
    odd = {"scope": "history", "op": "drop_tag", "tool": "bash"}
    assert split_changes([odd, tools]) == ([odd], [tools])
    # a rewrite that names no scope: the tool decides
    bare_history, bare_tools = {"index": 4, "op": "drop_tag"}, {"tool": "bash", "op": "append"}
    assert split_changes([bare_history, bare_tools]) == ([bare_history], [bare_tools])
    assert split_changes([]) == ([], []) and split_changes(None) == ([], [])


def test_the_journal_takes_one_line_per_kind_of_change_however_many_messages_were_edited(monkeypatch):
    install_summaries(monkeypatch)
    changes = [{"scope": "history", "op": "drop_tag", "edit": 0, "index": number, "target": "user",
                "removed_chars": 3} for number in range(5000)]
    changes += [{"scope": "tools", "op": "hide", "edit": 0, "index": 2, "tool": "bash"},
                {"scope": "history", "op": "mid_system", "mode": "drop", "index": 7, "target": "system",
                 "removed_chars": 20, "skipped": True, "reason": "it would end the conversation"}]
    summary = journal_changes(changes)
    assert [(g["scope"], g["op"], g.get("mode"), g["count"], g.get("skipped", False)) for g in summary] == [
        ("history", "drop_tag", None, 5000, False), ("tools", "hide", None, 1, False),
        ("history", "mid_system", "drop", 1, True)]
    assert summary[0]["removed_chars"] == 15000 and summary[0]["indexes"] == list(range(8))
    assert summary[2]["reason"] == "it would end the conversation"
    assert journal_changes([]) == [] and journal_changes(None) == []
    assert changes[0] == {"scope": "history", "op": "drop_tag", "edit": 0, "index": 0, "target": "user",
                          "removed_chars": 3}  # the list the rewrite returned is not touched


def test_without_a_summary_of_its_own_the_rewrite_gives_the_journal_its_first_changes_only(monkeypatch):
    monkeypatch.delattr(prompt_body, "summarize_changes", raising=False)
    changes = [{"scope": "history", "op": "drop_tag", "index": number} for number in range(100)]
    assert journal_changes(changes) == changes[:orchestrator.JOURNAL_CHANGES]
    assert len(journal_changes(changes)) == 20 and journal_changes(None) == []


# -- a composed head in the runner ------------------------------------------------------------------------------------
def planned(template, *, texts=None, relay_texts=None, **fields):
    snapshot = FakeSnapshot(3, {"p": profile(template)}, relay_texts)
    return FakePlan(rev=3, profile="p", rule="r", snapshot=snapshot, template=template, texts=texts or {}, **fields)


def served(tmp_path, monkeypatch, plan, *, messages=FIRST, system="CLIENT_SYSTEM", tools=None, store=None,
           attributes=None, **cfg):
    settings = dict(transport="direct", project_context=True, exec_policy="allowlist", cookie_values=[],
                    context_max_bytes=6000, exec_allowlist=[], command_timeout=3, command_output_limit=4000)
    settings.update(cfg)
    _, _, runner = setup(tmp_path, **settings)
    runner.prompts = store
    sent = capture_direct(monkeypatch)
    parsed = request(messages, system=system, tools=tools)
    parsed.request_purpose = client_context.request_kind(parsed, {})
    parsed.prompt_plan = plan
    for name, value in (attributes or {}).items():
        setattr(parsed, name, value)
    events = asyncio.run(run(runner, parsed))
    return events, sent, parsed


def head_sent(sent, index=0):
    return "\n".join(block["text"] for block in sent[index][1]["messages"][0]["content"])


def structured_parsed(system="CLIENT_SYSTEM"):
    data = {"model": "claude-opus-5-5", "messages": ([{"role": "system", "content": system}] if system else []) + FIRST,
            "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": SCHEMA}}}
    parsed = validate.parse_chat(data, {"strict_models": True, "model": "claude-opus-5-5"})
    parsed.conversation_key, parsed.request_purpose = "header:client-a", "generation"
    return parsed


def test_the_composed_head_is_the_head_the_transport_sends(tmp_path, monkeypatch):
    events, sent, _ = served(tmp_path, monkeypatch, planned("RULES\n\n{{client_system}}"))
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert head_sent(sent) == "RULES\n\nCLIENT_SYSTEM"
    assert sent[0][1]["openAICacheKey"] == direct.cache_key_for("RULES\n\nCLIENT_SYSTEM", "claude-opus-5-5",
                                                                  sent[0][1]["tools"])


def test_a_legacy_plan_and_no_plan_take_the_legacy_head(tmp_path, monkeypatch):
    for plan in (prompt_policy.Plan(), None):
        _, sent, _ = served(tmp_path, monkeypatch, plan)
        assert head_sent(sent) == "CLIENT_SYSTEM"


def test_the_legacy_mode_plays_no_part_in_a_composed_head_and_the_role_only_where_a_template_names_it(
        tmp_path, monkeypatch):
    settings = dict(relay_system_prompt="LEGACY ROLE", relay_system_prompt_mode="replace")
    _, sent, _ = served(tmp_path, monkeypatch, planned("{{client_system}}"), **settings)
    assert head_sent(sent) == "CLIENT_SYSTEM"
    _, sent, _ = served(tmp_path, monkeypatch, planned("{{role}}|{{client_system}}"), **settings)
    assert head_sent(sent) == "LEGACY ROLE|CLIENT_SYSTEM"
    _, sent, _ = served(tmp_path, monkeypatch, prompt_policy.Plan(), **settings)
    assert head_sent(sent) == "LEGACY ROLE"


def test_a_composed_head_follows_the_role_the_admin_saved_before_the_request(tmp_path, monkeypatch):
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, exec_policy="off", cookie_values=[],
                         relay_system_prompt="FIRST ROLE")
    sent = capture_direct(monkeypatch)
    heads = []
    for role in ("FIRST ROLE", "SECOND ROLE", None):
        runner.cfg["relay_system_prompt"] = role
        parsed = request(FIRST, system="CLIENT_SYSTEM", tools=[])
        parsed.request_purpose = "generation"
        parsed.prompt_plan = planned("{{role}}|{{client_system}}")
        assert asyncio.run(run(runner, parsed))[-1] == {"type": "end", "reason": "stop"}
        heads.append(head_sent(sent, -1))
    assert heads == ["FIRST ROLE|CLIENT_SYSTEM", "SECOND ROLE|CLIENT_SYSTEM",
                     context.DEFAULT_RELAY_SYSTEM_PROMPT + "|CLIENT_SYSTEM"]


def test_a_relay_task_with_a_reminder_template_gets_the_legacy_head_byte_for_byte(tmp_path, monkeypatch):
    _, legacy, _ = served(tmp_path, monkeypatch, None, messages=RELAY_TASK, system="", tools=[])
    _, composed, _ = served(tmp_path, monkeypatch, planned("{{relay_task}}{{exec_reminder}}"), messages=RELAY_TASK,
                            system="", tools=[])
    assert head_sent(composed) == head_sent(legacy) and head_sent(composed).endswith(orchestrator.EXEC_REMINDER)
    assert composed[0][1] == legacy[0][1]  # the whole body: messages, rit and cache key alike


def test_nothing_is_appended_to_a_composed_relay_head(tmp_path, monkeypatch):
    _, sent, _ = served(tmp_path, monkeypatch, planned("{{relay_task}}"), messages=RELAY_TASK, system="", tools=[])
    head = head_sent(sent)
    assert head.startswith(context.DEFAULT_RELAY_SYSTEM_PROMPT) and "[LOCAL EXEC REMINDER]" not in head
    assert head.endswith("[END OF PROJECT CONTEXT]")


def test_the_variables_of_a_relay_task_are_empty_for_a_client_request(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "build_project_context", lambda *a, **k: pytest.fail("the client owns this job"))
    template = "A{{relay_task}}{{local_exec}}{{project_context}}{{exec_reminder}}B {{client_system}}"
    for tools in (None, []):
        _, sent, _ = served(tmp_path, monkeypatch, planned(template), tools=tools)
        assert head_sent(sent) == "AB CLIENT_SYSTEM"


def test_a_client_with_tools_and_no_instructions_gets_the_native_role_from_the_template(tmp_path, monkeypatch):
    _, sent, _ = served(tmp_path, monkeypatch, planned("{{native_role}}|{{relay_task}}|{{role}}"), system="")
    assert head_sent(sent) == orchestrator.DEFAULT_NATIVE_TOOLS_ROLE + "||" + context.DEFAULT_RELAY_SYSTEM_PROMPT


def test_the_texts_of_the_file_reach_the_template(tmp_path, monkeypatch):
    plan = planned("{{role}}/{{native_role}}/{{text:mine}}", texts={"mine": "MINE"},
                   relay_texts={"role": "ROLE_FROM_FILE", "native_role": "NATIVE_FROM_FILE"})
    _, sent, _ = served(tmp_path, monkeypatch, plan)
    assert head_sent(sent) == "ROLE_FROM_FILE/NATIVE_FROM_FILE/MINE"


@pytest.mark.parametrize("template,expected", [
    ("{{client_system}}", "CLIENT_SYSTEM\n\n{CONTRACT}"),
    ("A {{structured}} B {{client_system}}", "A {CONTRACT} B CLIENT_SYSTEM"),
])
def test_the_structured_contract_is_in_the_head_once_wherever_the_template_puts_it(tmp_path, monkeypatch, template,
                                                                                  expected):
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[], exec_policy="off")
    sent = capture_direct(monkeypatch)
    parsed = structured_parsed()
    parsed.prompt_plan = planned(template)
    asyncio.run(run(runner, parsed))
    contract = structured.instructions(parsed.structured_output, tools=False, tool_choice="auto")
    assert head_sent(sent) == expected.replace("{CONTRACT}", contract)
    assert head_sent(sent).count("[STRUCTURED OUTPUT FORMAT]") == 1


def test_the_structured_log_line_is_still_written_for_a_composed_head(tmp_path, monkeypatch):
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[], exec_policy="off")
    capture_direct(monkeypatch)
    parsed = structured_parsed()
    parsed.prompt_plan = planned("{{client_system}}")
    before = journal.JOURNAL.last_seq
    asyncio.run(run(runner, parsed))
    kinds = [event["kind"] for event in journal.JOURNAL.query(since=before, limit=500, order="asc")]
    assert "turn.structured_output" in kinds and "prompt.plan" in kinds


def test_the_composed_head_counts_against_the_prompt_limit_before_anything_goes_upstream(tmp_path, monkeypatch):
    plan = planned("{{text:long}}\n{{client_system}}", texts={"long": "x" * 400})
    events, sent, _ = served(tmp_path, monkeypatch, plan, max_prompt_bytes=300)
    error = next(event["error"] for event in events if event.get("type") == "error")
    assert (error.status, error.code) == (413, "prompt_too_large") and sent == []


def test_a_relay_task_still_runs_its_command_and_keeps_one_head_through_the_loop(tmp_path, monkeypatch):
    commands, heads, answers = [], [], iter(["<exec_command>echo relay-task</exec_command>", "done"])
    monkeypatch.setattr(executor, "run_command", lambda command, *a, **k: (commands.append(command) or 0, "OUTPUT"))

    async def stream(session, prompt, provider, tool_names=None):
        heads.append(session._direct["head"])
        yield {"type": "text", "delta": next(answers)}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(RelaySession, "stream", stream)
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, exec_policy="allow_all",
                         cookie_values=[], exec_allowlist=[], command_timeout=3, command_output_limit=4000)
    parsed = request(RELAY_TASK, system="", tools=[])
    parsed.request_purpose = "generation"
    parsed.prompt_plan = planned("{{relay_task}}{{exec_reminder}}")
    events = asyncio.run(run(runner, parsed))
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert commands == ["echo relay-task"] and len(heads) == 2 and heads[0] == heads[1]
    assert heads[0].endswith(orchestrator.EXEC_REMINDER)


def test_a_client_owned_request_never_executes_whatever_the_template_names(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "extract_command", lambda *_: pytest.fail("the client owns execution"))

    async def stream(session, prompt, provider, tool_names=None):
        yield {"type": "text", "delta": "<exec_command>echo no</exec_command>"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(RelaySession, "stream", stream)
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, exec_policy="allow_all",
                         cookie_values=[])
    parsed = request(FIRST, system="CLIENT_SYSTEM", tools=[])
    parsed.request_purpose = "generation"
    parsed.prompt_plan = planned("{{exec_reminder}}{{relay_task}}{{client_system}}")
    assert asyncio.run(run(runner, parsed))[-1] == {"type": "end", "reason": "stop"}


def test_the_runner_records_the_plan_with_its_parts_and_changes_and_tells_the_journal_only_lengths(
        tmp_path, monkeypatch):
    install_summaries(monkeypatch)
    store, before = FakePrompts(), journal.JOURNAL.last_seq
    attributes = {"prompt_changes": [{"scope": "history", "op": "drop_tag", "edit": 0, "index": 2, "target": "user",
                                      "removed_chars": 5},
                                     {"scope": "tools", "op": "append", "edit": 0, "index": 0, "tool": "read_file",
                                      "added_chars": 12}],
                  "prompt_warnings": ["from the hooks"], "prompt_request": {"body": {"model": "m"}, "dialect": "chat",
                                                                           "headers": {}}}
    plan = planned("RULES\n\n{{client_system}}", warn=("from the engine",))
    _, sent, _ = served(tmp_path, monkeypatch, plan, store=store, attributes=attributes)
    assert head_sent(sent) == "RULES\n\nCLIENT_SYSTEM"
    (entry,) = store.records
    assert entry["head"] == "RULES\n\nCLIENT_SYSTEM" and entry["profile"] == "p" and entry["rev"] == 3
    assert [(p["source"], p["start"], p["end"]) for p in entry["parts"]] == [("literal", 0, 7), ("client", 7, 20)]
    assert entry["history_changes"] == [attributes["prompt_changes"][0]]
    assert entry["tools_changes"] == [attributes["prompt_changes"][1]]
    assert [(g["scope"], g["op"], g["count"]) for g in entry["changes_summary"]] == [
        ("history", "drop_tag", 1), ("tools", "append", 1)]
    assert entry["warnings"] == ["from the engine", "from the hooks"]
    assert entry["request"] == attributes["prompt_request"] and entry["model"] == "claude-opus-5-5"
    (event,) = [e for e in journal.JOURNAL.query(since=before, limit=500, order="asc") if e["kind"] == "prompt.plan"]
    assert event["data"]["parts"] == [["literal", 7], ["client", 13]]
    assert event["data"]["warnings"] == ["from the engine", "from the hooks"]
    assert [(g["op"], g["count"]) for g in event["data"]["history_changes"]] == [("drop_tag", 1)]
    assert [(g["op"], g["tool"]) for g in event["data"]["tools_changes"]] == [("append", "read_file")]
    assert "RULES" not in json.dumps(event) and "CLIENT_SYSTEM" not in json.dumps(event)


def test_a_legacy_plan_writes_no_plan_event_and_no_record(tmp_path, monkeypatch):
    store, before = FakePrompts(), journal.JOURNAL.last_seq
    served(tmp_path, monkeypatch, prompt_policy.Plan(), store=store)
    assert store.records == [] and not [e for e in journal.JOURNAL.query(since=before, limit=500)
                                        if e["kind"].startswith("prompt.")]


def test_the_head_is_composed_off_the_event_loop_like_the_legacy_head_is(tmp_path, monkeypatch):
    seen = []
    real = FakePlan.compose_head

    def compose(self, client_system, sources):
        try:
            asyncio.get_running_loop()
            seen.append("on the loop")
        except RuntimeError:
            seen.append("in a worker thread")
        return real(self, client_system, sources)

    monkeypatch.setattr(FakePlan, "compose_head", compose)
    served(tmp_path, monkeypatch, planned("{{relay_task}}"), messages=RELAY_TASK, system="", tools=[])
    assert seen == ["in a worker thread"]


def test_the_agent_transport_starts_its_prompt_with_the_composed_head_and_adds_no_reminder(tmp_path, monkeypatch):
    prompts = []

    async def stream(session, prompt, provider, tool_names=None):
        prompts.append(prompt)
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(RelaySession, "stream", stream)
    _, _, runner = setup(tmp_path, transport="agent", project_context=False, exec_policy="allowlist",
                         cookie_values=[], exec_allowlist=[], command_timeout=3, command_output_limit=4000)
    parsed = request(RELAY_TASK, system="", tools=[])
    parsed.request_purpose = "generation"
    parsed.prompt_plan = planned("{{relay_task}}")
    assert asyncio.run(run(runner, parsed))[-1] == {"type": "end", "reason": "stop"}
    assert prompts[0].startswith(context.DEFAULT_RELAY_SYSTEM_PROMPT + "\n\n[LOCAL EXECUTION]")
    assert "Run my relay task" in prompts[0] and "[LOCAL EXEC REMINDER]" not in prompts[0]


def test_a_refusal_raised_while_composing_reaches_the_client_and_nothing_goes_upstream(tmp_path, monkeypatch):
    def refuse(self, client_system, sources):
        raise apierr.ApiError(413, "prompt_too_large", "the composed head is too large for this profile")

    monkeypatch.setattr(FakePlan, "compose_head", refuse)
    events, sent, _ = served(tmp_path, monkeypatch, planned("{{client_system}}"))
    error = next(event["error"] for event in events if event.get("type") == "error")
    assert (error.status, error.code) == (413, "prompt_too_large") and sent == []
