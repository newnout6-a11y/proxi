"""client_task: the sign of a client that asks for a summary of the whole conversation (OpenCode 1.x and 2.x, Claude Code).

Every wording below is the start of the prompt the client sends, as its source has it (github.com/anomalyco/opencode,
packages/core/src/session/compaction.ts and packages/opencode/src/session/compaction.ts at the tags named in each case).
The rest of every message is made up here: only the opening is the sign, and that is the point of these tests.
"""
import pytest

import client_context
import validate
from prompt_bodies import OPENCODE_COMPACTION_V1, OPENCODE_SUMMARY, OPENCODE_TITLE_V1, OPENCODE_TITLE_V2, opencode_body

CFG = {"strict_models": True, "model": "claude-opus-5-5", "show_thinking": False}
TAIL = "\n\nSynthetic template and rules, reworded since: " + "x" * 40
FIRST = "You MUST summarize the conversation above into a structured summary that will be given to another agent to resume the work."
UPDATE = "Update the existing checkpoint in the conversation above into one consolidated summary."
NUDGE = ("The previous response did not fill in the required summary template. Do not call tools. Return the summary as "
         "text using the exact section headings from the template.")
# OpenCode 1.x: the hidden agent has no tools and its own system prompt; the instruction opens its one user message.
LEGACY = {
    "v1.0.144 to v1.14.18": "Provide a detailed prompt for continuing our conversation above.\nFocus on information that "
                            "would be helpful for continuing the conversation, including what we did, what we're doing, "
                            "which files we're working on, and what we're going to do next.\nThe summary that you "
                            "construct will be used so that another agent can read it and continue the work.",
    "v1.14.19 to v1.14.20": "When constructing the summary, try to stick to this template:\n---\n## Goal\n\n[What goal(s) "
                            "is the user trying to accomplish?]\n---",
    "v1.14.21 to v1.18.16 create": "Create a new anchored summary from the conversation history above." + TAIL,
    "v1.15 to v1.18 update": "Update the anchored summary below using the conversation history above.\nPreserve "
                             "still-true details.\n<previous-summary>\nsynthetic\n</previous-summary>" + TAIL,
    "v1.17 to v1.18 create": "Create a new anchored summary from the conversation history." + TAIL,
    "v1.18.17 to v1.19.5": "Here is the conversation so far:\n\n<conversation>\nUser: synthetic\n\nAssistant: synthetic\n"
                           "</conversation>\n\nCreate a new anchored summary from the conversation history in the "
                           "<conversation> tags above so another coding agent can continue the work." + TAIL,
}
OLD_COMPACTION_SYSTEM = ("You are a helpful AI assistant tasked with summarizing conversations.\n\nWhen asked to "
                         "summarize, provide a detailed but concise summary.")


def chat(messages=None, tools=True, system="You are an AI agent running in the SyntheticCode harness."):
    body = opencode_body(rounds=2)
    body["messages"][0]["content"] = system
    if messages is not None:
        body["messages"] = [body["messages"][0], *messages]
    if not tools:
        del body["tools"]
    return validate.parse_chat(body, CFG)


def tail_messages(*texts, role="user"):
    return [{"role": role, "content": text} for text in texts]


def task(parsed, **identity):
    return client_context.client_task(parsed, {"client": "opencode", **identity})


WORK = [{"role": "user", "content": "inspect the synthetic project and report"}]


@pytest.mark.parametrize("instruction", [FIRST, UPDATE])
def test_opencode_2_ends_the_chats_own_request_with_the_instruction_and_it_is_recognised_by_its_start(instruction):
    parsed = chat([*WORK, {"role": "assistant", "content": "ok"}, *tail_messages(instruction + TAIL)])
    assert parsed.tools and task(parsed) == client_context.COMPACTION == "compaction"


# Claude Code's summary request has the chat's system, tools and history and ends with its instruction: 2.1.316 opens it
# with a no-tools line (a community capture, clawdipedia.cc), earlier versions with the task itself.
CLAUDE_CODE = {
    "2.1.316": "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n\n- Do NOT use Read, Bash, Grep, Glob, Edit, "
               "Write, or ANY other tool.\n\nYour task is to create a detailed summary of the conversation so far, paying "
               "close attention to the user's explicit requests and your previous actions." + TAIL,
    "before the no-tools line": "Your task is to create a detailed summary of the conversation so far, paying close "
                                "attention to the user's explicit requests and your previous actions.\nThis summary should "
                                "be thorough in capturing technical details." + TAIL,
}


@pytest.mark.parametrize("name", sorted(CLAUDE_CODE))
def test_claude_codes_summary_request_is_recognised_by_the_start_of_its_instruction(name):
    parsed = chat([*WORK, {"role": "assistant", "content": "OK"}, *tail_messages(CLAUDE_CODE[name])])
    assert task(parsed, client="claude-code") == "compaction" and task(parsed) == "compaction"
    # a quoted instruction, or the same words further down a message, is no instruction
    assert task(chat([*WORK, *tail_messages("The docs say: " + CLAUDE_CODE[name])])) == ""
    assert task(chat([*WORK, *tail_messages("Please create a detailed summary of the conversation so far.")])) == ""


# Codex's template (codex-rs/prompts/templates/compact/prompt.md), sent after the history of a request without tools. It
# was not recognised, so keepwarm kept the dead tool-less body warm: 1.2-1.3 credits a ping every 4 minutes (a2 report).
CODEX_COMPACTION = ("You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that "
                    "will resume the task.\n\nInclude:\n- Current progress and key decisions made\n- Important context, "
                    "constraints, or user preferences\n- What remains to be done (clear next steps)")


def test_codexs_compaction_request_is_recognised_with_or_without_tools():
    for tools in (False, True):
        parsed = chat([*WORK, {"role": "assistant", "content": "Done."}, *tail_messages(CODEX_COMPACTION)], tools=tools)
        assert client_context.client_task(parsed, {"client": "codex"}) == "compaction"
    assert task(chat([*WORK, *tail_messages("Why is it called a CONTEXT CHECKPOINT COMPACTION?")])) == ""


@pytest.mark.parametrize("instruction", [FIRST, UPDATE])
def test_opencode_2_ends_the_chats_own_request_with_the_instruction_and_it_is_recognised_by_its_start(instruction):
    parsed = chat([*WORK, {"role": "assistant", "content": "ok"}, *tail_messages(instruction + TAIL)])
    assert parsed.tools and task(parsed) == client_context.COMPACTION == "compaction"


@pytest.mark.parametrize("instruction", [FIRST, UPDATE])
def test_only_the_start_decides_not_the_rest_of_the_text(instruction):
    reworded = chat([*WORK, *tail_messages(instruction + "\n\nA different template, other rules, other words.")])
    bare = chat([*WORK, *tail_messages(instruction)])
    padded = chat([*WORK, *tail_messages("\n\n  " + instruction + TAIL)])
    assert task(reworded) == task(bare) == task(padded) == "compaction"


def test_the_reminder_after_a_reply_that_skipped_the_template_is_still_the_same_task():
    parsed = chat([*WORK, *tail_messages(FIRST + TAIL, NUDGE)])
    assert task(parsed) == "compaction"
    # the nudge alone, or after anything but the instruction, is an ordinary message
    assert task(chat([*WORK, *tail_messages(NUDGE)])) == ""
    assert task(chat([*WORK, *tail_messages("something the user typed", NUDGE)])) == ""


@pytest.mark.parametrize("name", sorted(LEGACY))
def test_opencode_1_summarizes_on_a_hidden_agent_without_tools_and_the_instruction_opens_its_message(name):
    parsed = chat([*WORK, *tail_messages(LEGACY[name])], tools=False, system=OLD_COMPACTION_SYSTEM)
    assert not parsed.tools and task(parsed) == "compaction"
    # the instruction alone is enough when the user replaced the agent's prompt in opencode.json
    assert task(chat([*WORK, *tail_messages(LEGACY[name])], tools=False, system="My own summary prompt.")) == "compaction"


@pytest.mark.parametrize("name", sorted(LEGACY))
def test_the_wording_of_opencode_1_is_not_a_sign_for_a_request_that_sends_tools(name):
    # a chat with tools is the main chat or OpenCode 2.x; only the 2.x instruction counts there
    assert task(chat([*WORK, *tail_messages(LEGACY[name])])) == ""


@pytest.mark.parametrize("system", [OPENCODE_COMPACTION_V1, OLD_COMPACTION_SYSTEM])
def test_the_system_prompt_of_the_1x_agent_marks_a_request_without_tools(system):
    parsed = chat(tail_messages("Here is something else entirely."), tools=False, system=system)
    assert task(parsed) == "compaction" and task(parsed, client="") == "compaction"
    assert task(chat(tail_messages("Here is something else entirely."), system=system)) == ""


@pytest.mark.parametrize("system", [OPENCODE_TITLE_V1, OPENCODE_TITLE_V2, OPENCODE_SUMMARY])
def test_the_title_and_summary_agents_are_not_compaction(system):
    assert task(chat(tail_messages("hello"), tools=False, system=system)) == ""


def test_an_ordinary_last_message_is_not_compaction():
    for text in ("inspect the synthetic project and report", "please summarize the conversation above for me",
                 "You MUST summarize the file below", "Update the existing checkpoint file"):
        assert task(chat([*WORK, *tail_messages(text)])) == "", text
    assert task(chat(WORK)) == ""
    # a tool result that happens to quote the instruction is not a user message at all
    quoted = chat([*WORK, {"role": "assistant", "content": None, "tool_calls": [{
        "id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": FIRST + TAIL}])
    assert task(quoted) == ""


def test_a_quoted_instruction_is_no_instruction():
    for text in ("Here is what the docs say: " + FIRST, "> " + FIRST, "The prompt reads: '" + FIRST + "'",
                 "Fix the typo in " + UPDATE):
        assert task(chat([*WORK, *tail_messages(text)])) == "", text


def test_the_instruction_in_an_earlier_message_of_a_conversation_that_went_on_is_history():
    parsed = chat([*WORK, *tail_messages(FIRST + TAIL), {"role": "assistant", "content": "## Objective ..."},
                   *tail_messages("thanks, now fix the bug")])
    assert task(parsed) == ""
    # and when a user message follows the instruction there is no telling what it is for
    assert task(chat([*WORK, *tail_messages(FIRST + TAIL, "and another thing")])) == ""


@pytest.mark.parametrize("wording", ["You MUST summarize the conversation below", "You must summarize the conversation above",
                                      "Update the existing checkpoint in the conversation below"])
def test_a_near_miss_of_the_wording_is_not_matched(wording):
    assert task(chat([*WORK, *tail_messages(wording + TAIL)])) == ""


def test_anthropic_messages_carry_the_results_and_the_instruction_in_one_user_turn():
    body = {"model": "claude-opus-5-5", "max_tokens": 64, "system": "SYSTEM",
            "tools": [{"name": "Read", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
            "messages": [{"role": "user", "content": "task"},
                         {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "text"},
                                                      {"type": "text", "text": UPDATE + TAIL}]}]}
    parsed = validate.parse_anthropic(body, CFG)
    assert task(parsed) == "compaction"
    body["messages"][-1]["content"][1]["text"] = "carry on"
    assert task(validate.parse_anthropic(body, CFG)) == ""


def test_responses_input_items_are_read_the_same_way():
    body = {"model": "claude-opus-5-5", "instructions": "SYSTEM",
            "tools": [{"type": "function", "name": "read", "description": "d", "parameters": {"type": "object",
                                                                                           "properties": {}}}],
            "input": [{"role": "user", "content": "task"},
                      {"role": "user", "content": [{"type": "input_text", "text": FIRST + TAIL}]}]}
    assert task(validate.parse_responses(body, CFG)) == "compaction"


def test_without_the_canonical_messages_the_rendered_chunks_are_read():
    parsed = chat([*WORK, *tail_messages(FIRST + TAIL)])
    parsed.native_messages = None
    assert task(parsed) == "compaction"
    ordinary = chat([*WORK, *tail_messages("go on")])
    ordinary.native_messages = None
    assert task(ordinary) == ""


def test_a_message_with_an_image_still_opens_with_its_text():
    parsed = chat([*WORK, {"role": "user", "content": [
        {"type": "text", "text": FIRST + TAIL},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}])
    assert task(parsed) == "compaction"


@pytest.mark.parametrize("identity", [{"request_class": "compaction"}, {"compaction_trigger": "auto"},
                                      {"task_hint": "compaction"}, {"agent": "compaction"}, {"agent": " Compaction "}])
def test_what_the_headers_say_is_enough(identity):
    assert task(chat([*WORK, *tail_messages("anything at all")]), **identity) == "compaction"


@pytest.mark.parametrize("client", ["claude-code", "claude-desktop", ""])
def test_the_agent_name_alone_proves_nothing_for_a_client_that_is_not_opencode(client):
    parsed = chat([*WORK, *tail_messages("anything at all")])
    assert client_context.client_task(parsed, {"client": client, "agent": "compaction"}) == ""
    # while the request class is Claude Code's own vocabulary and says it for every client
    assert client_context.client_task(parsed, {"client": client, "request_class": "compaction"}) == "compaction"


@pytest.mark.parametrize("identity", [{"request_class": "main"}, {"request_class": "auxiliary"},
                                      {"request_class": "subagent"}, {"agent": "build"}, {"agent": "title"},
                                      {"task_hint": "other"}])
def test_other_classes_and_agents_are_not_compaction(identity):
    assert task(chat([*WORK, *tail_messages("go on")]), **identity) == ""


def test_describe_reads_the_headers_and_keeps_only_their_vocabulary(tmp_path):
    cfg = {"client_metadata_home": str(tmp_path)}
    headers = {"user-agent": "claude-cli/2.1.310 (external, cli)", "X-Claude-Code-Request-Class": "Compaction",
               "X-Claude-Code-Compaction": "Manual", "X-Proxi-Task": "COMPACTION"}
    identity = client_context.describe({}, headers, cfg)
    assert (identity["request_class"], identity["compaction_trigger"], identity["task_hint"]) == (
        "compaction", "manual", "compaction")
    for junk in ("compaction\r\nx: y", "x" * 80, "1abc", "a b", ""):
        hostile = client_context.describe({}, {"x-claude-code-compaction": junk, "x-proxi-task": junk}, cfg)
        assert "compaction_trigger" not in hostile and "task_hint" not in hostile, junk
    assert "compaction_trigger" not in client_context.describe({}, {"user-agent": "opencode/2.2.5"}, cfg)


def test_the_bound_identity_is_used_when_none_is_passed():
    parsed = chat([*WORK, *tail_messages("go on")])
    token = client_context.bind({"client": "claude-code", "request_class": "compaction"})
    try:
        assert client_context.client_task(parsed) == "compaction"
    finally:
        client_context.reset(token)
    assert client_context.client_task(parsed, {}) == ""


def test_the_label_leaves_the_purposes_the_journal_and_the_prompt_layer_read_untouched():
    parsed = chat([*WORK, *tail_messages(FIRST + TAIL)])
    identity = {"client": "opencode", "agent": "general"}
    parsed.request_purpose = client_context.request_kind(parsed, identity)
    assert parsed.request_purpose == "client-tools"
    assert client_context.client_task(parsed, identity) == "compaction"
    # the compaction of OpenCode 2.x follows the chat's profile and shares its cache, as before
    assert client_context.prompt_purpose(parsed, identity) == "client-tools"
    assert client_context.request_kind(parsed, identity) == "client-tools"


def test_the_service_prompt_list_is_what_it_was_with_the_compaction_prompts_named():
    assert client_context.OPENCODE_SERVICE_PROMPTS == (
        "You are a title generator. You output ONLY a thread title. Nothing else.",
        "You name conversations so the user can find them later in a list of past sessions.",
        "You are an anchored context summarization assistant for coding sessions.",
        "You are a helpful AI assistant tasked with summarizing conversations.",
        "Summarize what was done in this conversation. Write like a pull request description.")
    assert client_context.COMPACTION_SYSTEM_PROMPTS == client_context.OPENCODE_SERVICE_PROMPTS[2:4]
