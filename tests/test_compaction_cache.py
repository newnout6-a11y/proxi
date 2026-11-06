"""A client's compaction runs on the effort of its dialog, so that the signed history continues and only the tail is paid.

Live case (2026-11-05, OpenCode 2.2.5): a chat on `max` with 530 messages and 11 tools read 845,000 tokens from the cache
and wrote 1,000 per turn (3.5 credits). Its compaction, the same system and tools and the same history plus one user
message, came from the compaction agent's own variant `high`: the effort is part of the scope of a signed branch, so the
relay started the history again, the provider dropped its cached messages for the changed effort, and the whole
conversation was written (532,000 tokens, 28 credits). These tests run the real relay in front of a scripted Retool.
"""
import asyncio
import json

import pytest

import direct
import journal
import native_history
from prompt_support import after_tool_call, client_request, prompt_relay, strip_cache  # noqa: F401
from test_account_rate_limit import hourly, retool  # noqa: F401
from test_admin import admin_relay  # noqa: F401
from test_native_history import TOOLS, harness, tool_reply
from test_native_context_reuse import FIRST as TASK, request, run
from test_review_fixes_pr19 import END, text

SYSTEM = "CLIENT_SYSTEM"
INSTRUCTION = ("You MUST summarize the conversation above into a structured summary that will be given to another agent "
               "to resume the work.\n\nSynthetic template and rules, reworded since.")
UPDATE = "Update the existing checkpoint in the conversation above into one consolidated summary.\n\nSynthetic rules."
# Claude Code: 2.1.316 opens with the first line (community capture, clawdipedia.cc), earlier versions with the second.
CLAUDE_CODE_INSTRUCTION = ("CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n\n- Do NOT use Read, Bash, Grep, Glob, "
                           "Edit, Write, or ANY other tool.\n\nYour task is to create a detailed summary of the conversation "
                           "so far, paying close attention to the user's explicit requests and your previous actions.")
CLAUDE_CODE_OLD_INSTRUCTION = ("Your task is to create a detailed summary of the conversation so far, paying close attention "
                               "to the user's explicit requests and your previous actions.\nThis summary should be thorough.")
SUMMARY = [text("## Objective\n- synthetic summary"), *END]
OPENCODE = {"user-agent": "opencode/latest/2.2.5/desktop", "x-proxi-client": "opencode", "x-session-affinity": "ses_c3"}
CLAUDE_CODE = {"user-agent": "claude-cli/2.1.310 (external, cli)",
               "x-claude-code-session-id": "00000000-0000-4000-8000-000000000310"}
DIALECTS = ["chat", "anthropic", "responses"]


def effort_fields(dialect, effort):
    if dialect == "anthropic":
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
    if dialect == "responses":
        return {"reasoning": {"effort": effort}}
    return {"reasoning_effort": effort}


def with_instruction(dialect, messages, instruction):
    """The messages of the compaction request: the history as the chat sent it, then the client's own instruction.

    A Messages client sends tool results and the next user text as one turn; the other dialects as messages of their own."""
    messages = json.loads(json.dumps(messages))
    if dialect == "anthropic":
        messages[-1]["content"].append({"type": "text", "text": instruction})
        return messages
    return messages + [{"role": "user", "content": instruction}]


class Chat:
    """One OpenCode conversation as its client drives it: tool rounds on the chat's effort, then a compaction."""

    def __init__(self, relay, dialect="chat", effort="max", headers=None, model="claude-sonnet-5-5"):
        self.relay, self.dialect, self.effort, self.model = relay, dialect, effort, model
        self.headers = dict(headers or OPENCODE)
        self.messages = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]

    def ask(self, script, *, effort=None, messages=None, headers=None):
        self.relay.scripts.append(script)
        request_ = client_request(self.dialect, self.messages if messages is None else messages, system=SYSTEM,
                                  model=self.model, **effort_fields(self.dialect, effort or self.effort))
        status, payload, answer = self.relay.post(self.dialect, request_, {**self.headers, **(headers or {})})
        assert status == 200, payload
        if messages is None and any(event.get("type") == "tool_call" for event in script):
            self.messages = after_tool_call(self.dialect, self.messages, payload)
        self.relay.settle(answer)
        return self.relay.posts[-1]

    def work(self, rounds=2):
        return [self.ask(tool_reply("c%d" % number, "opaque-%d" % number)) for number in range(1, rounds + 1)]

    def compact(self, effort="high", instruction=INSTRUCTION, headers=None, script=None):
        return self.ask(script or SUMMARY, effort=effort, headers=headers,
                        messages=with_instruction(self.dialect, self.messages, instruction))


def thinking(body):
    return [block["signature"] for message in body["messages"] if isinstance(message.get("content"), list)
            for block in message["content"] if isinstance(block, dict) and block.get("type") == "thinking"]


def count(relay, mark, kind):
    return len(relay.events(mark, kind))


def tail_after(earlier, later):
    """What `later` added to `earlier`: the messages of the body after the prefix it shares with it, cache marks aside."""
    shared = len(earlier["messages"])
    assert json.dumps(strip_cache(later["messages"][:shared]), sort_keys=True) == \
        json.dumps(strip_cache(earlier["messages"]), sort_keys=True)
    return later["messages"][shared:]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_compaction_on_another_effort_continues_the_signed_history_byte_for_byte(prompt_relay, dialect):
    relay = prompt_relay()
    chat = Chat(relay, dialect)
    first, second = chat.work()
    mark = relay.mark()
    summary = chat.compact(effort="high")

    # the provider sees the dialog's effort, and not the one its cache would be dropped for
    assert first["reasoningEffort"] == second["reasoningEffort"] == summary["reasoningEffort"] == "max"
    tail = tail_after(second, summary)
    assert [message["role"] for message in tail] == ["assistant", "tool", "user"]
    assert tail[0]["content"][0] == {"type": "thinking", "thinking": "", "signature": "opaque-2"}
    assert INSTRUCTION[:60] in json.dumps(tail[-1]) and thinking(summary) == ["opaque-1", "opaque-2"]
    assert summary["rit"] == direct.make_rit(summary["messages"], summary["tools"])
    assert summary["openAICacheKey"] == second["openAICacheKey"] == first["openAICacheKey"]
    # the transport resumed the branch: no reset, no replay without signatures
    assert count(relay, mark, "native_history.resume") == 1
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "native_history.cold") == 0
    assert count(relay, mark, "cache.key_changed") == 0 and count(relay, mark, "cache.head_changed") == 0


def test_a_long_dialog_on_max_is_compacted_on_high_and_only_the_new_tail_differs(prompt_relay):
    # the shape of the live case: many tool rounds on `max`, 11-ish tools, then the compaction agent asks on `high`
    relay = prompt_relay()
    chat = Chat(relay)
    rounds = 40
    bodies = chat.work(rounds)
    last = bodies[-1]
    assert len(last["messages"]) >= 2 * rounds and last["reasoningEffort"] == "max"
    mark = relay.mark()
    summary = chat.compact(effort="high")

    tail = tail_after(last, summary)
    assert [message["role"] for message in tail] == ["assistant", "tool", "user"]
    assert thinking(summary) == ["opaque-%d" % number for number in range(1, rounds + 1)]
    assert summary["reasoningEffort"] == "max" and summary["rit"] == direct.make_rit(summary["messages"], summary["tools"])
    assert summary["openAICacheKey"] == last["openAICacheKey"]
    # what is written anew is the instruction and the last exchange, nothing of the 40 rounds before them
    assert len(json.dumps(tail)) < 4 * len(INSTRUCTION) + 2000
    assert count(relay, mark, "native_history.resume") == 1
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "native_history.cold") == 0


def test_the_journal_says_what_the_compaction_is_and_which_effort_it_ran_on(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    mark = relay.mark()
    chat.compact(effort="high")

    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert begin["client_task"] == "compaction" and begin["request_purpose"] == "client-tools"
    assert (begin["requested_effort"], begin["effective_effort"], begin["effort"]) == ("high", "max", "max")
    assert (begin["effort_source"], begin["effort_resolution"]) == ("dialog", "cache_continuity")
    assert "сжатие" in relay.events(mark, "turn.begin")[-1]["message"]
    kept = relay.events(mark, "cache.effort_kept")
    assert len(kept) == 1 and kept[0]["data"]["dialog_effort"] == "max" and kept[0]["data"]["requested_effort"] == "high"
    assert count(relay, mark, "cache.compaction_restart") == 0
    # every event of the request, the transport's and the turn's, carries the label
    for kind in ("upstream.usage", "turn.end", "http.request", "native_history.resume", "cache.effort_kept"):
        events = relay.events(mark, kind)
        assert events and all(event["data"].get("client_task") == "compaction" for event in events), kind
    # and an ordinary turn of the same dialog does not
    relay.scripts.append([text("go on"), *END])
    ordinary_mark = relay.mark()
    relay.post("chat", client_request("chat", chat.messages + [{"role": "user", "content": "next"}], system=SYSTEM,
                                      model="claude-sonnet-5-5", reasoning_effort="max"), OPENCODE)
    assert all("client_task" not in event["data"] for event in relay.events(ordinary_mark)
               if event["kind"] in ("turn.begin", "turn.end", "http.request"))


def test_the_console_marks_the_compaction_and_the_effort_it_kept(prompt_relay, capsys):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    capsys.readouterr()
    chat.compact(effort="high")
    shown = capsys.readouterr().out
    assert "| EFFORT: HIGH | PURPOSE: client-tools | TASK: compaction" in shown  # the request as the client sent it
    assert "compaction keeps the dialog's effort max (the client asked for high)" in shown
    assert "claude-sonnet-5-5 [max]" in shown  # and the line of its end names the effort it ran on
    capsys.readouterr()
    chat.ask(tool_reply("c3", "opaque-3"))
    assert "TASK:" not in capsys.readouterr().out


def test_with_the_setting_off_the_effort_the_client_sends_decides_and_the_history_restarts(prompt_relay):
    relay = prompt_relay(compaction_keep_effort=False)
    chat = Chat(relay)
    chat.work()
    mark = relay.mark()
    summary = chat.compact(effort="high")

    assert summary["reasoningEffort"] == "high" and thinking(summary) == []
    assert count(relay, mark, "native_history.reset") == 1 and count(relay, mark, "native_history.cold") == 1
    assert count(relay, mark, "native_history.resume") == 0 and count(relay, mark, "cache.effort_kept") == 0
    # the setting off is the old behaviour: no look at the dialog at all
    assert count(relay, mark, "cache.compaction_restart") == 0
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    # still named: the label is what shows why the call was dear
    assert begin["client_task"] == "compaction"
    assert (begin["requested_effort"], begin["effective_effort"], begin["effort_resolution"]) == ("high", "high", "exact")
    assert all(event["data"].get("client_task") == "compaction" for event in relay.events(mark, "native_history.reset"))


def test_an_ordinary_turn_on_another_effort_restarts_the_history_as_before(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    mark = relay.mark()
    # the user changes the effort in the middle of the dialog: theirs to change
    following = chat.ask(tool_reply("c3", "opaque-3"), effort="high")
    assert following["reasoningEffort"] == "high" and thinking(following) == []
    assert count(relay, mark, "native_history.reset") == 1 and count(relay, mark, "cache.effort_kept") == 0
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert "client_task" not in begin and begin["effort_resolution"] == "exact" and begin["effort_source"] == "client"


def test_a_text_that_only_quotes_the_instruction_is_an_ordinary_message(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    mark = relay.mark()
    quoting = chat.compact(effort="high", instruction="Here is what the docs say: " + INSTRUCTION)
    assert quoting["reasoningEffort"] == "high"
    assert count(relay, mark, "native_history.reset") == 1 and count(relay, mark, "cache.effort_kept") == 0
    assert "client_task" not in relay.events(mark, "turn.begin")[-1]["data"]


def test_the_first_request_of_a_dialog_has_no_branch_to_continue_and_keeps_its_own_effort(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    mark = relay.mark()
    summary = chat.ask(SUMMARY, effort="high", messages=with_instruction("chat", chat.messages, INSTRUCTION))
    assert summary["reasoningEffort"] == "high"
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert begin["client_task"] == "compaction" and begin["effort_resolution"] == "exact"
    assert count(relay, mark, "cache.effort_kept") == 0
    restart = relay.events(mark, "cache.compaction_restart")
    assert len(restart) == 1 and restart[0]["data"]["reason"] == "no_branch" and "differs" not in restart[0]["data"]
    assert restart[0]["data"]["client_task"] == "compaction" and "записана заново" in restart[0]["message"]


def test_a_history_that_is_not_the_dialogs_own_keeps_the_clients_effort(prompt_relay, capsys):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    mark = relay.mark()
    capsys.readouterr()
    edited = json.loads(json.dumps(chat.messages))
    edited[0]["content"] = "inspect EDITED_TASK"
    summary = chat.ask(SUMMARY, effort="high", messages=with_instruction("chat", edited, INSTRUCTION))
    assert summary["reasoningEffort"] == "high" and thinking(summary) == []
    assert count(relay, mark, "cache.effort_kept") == 0 and count(relay, mark, "native_history.reset") == 1
    assert relay.events(mark, "turn.begin")[-1]["data"]["client_task"] == "compaction"
    # and the journal and the console say why it is dear
    assert [event["data"]["reason"] for event in relay.events(mark, "cache.compaction_restart")] == ["history"]
    assert "compaction does not continue the signed history (history)" in capsys.readouterr().out


def test_the_same_effort_needs_no_help_and_the_history_continues(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    _, second = chat.work()
    mark = relay.mark()
    summary = chat.compact(effort="max")
    assert summary["reasoningEffort"] == "max" and thinking(summary) == ["opaque-1", "opaque-2"]
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert (begin["client_task"], begin["effort_resolution"], begin["effort_source"]) == ("compaction", "exact", "client")
    assert count(relay, mark, "cache.effort_kept") == 0 and count(relay, mark, "native_history.reset") == 0
    assert count(relay, mark, "cache.compaction_restart") == 0


def test_a_client_that_sends_no_effort_at_all_gets_the_dialogs(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    mark = relay.mark()
    relay.scripts.append(SUMMARY)
    request_ = client_request("chat", with_instruction("chat", chat.messages, INSTRUCTION), system=SYSTEM,
                              model="claude-sonnet-5-5")
    assert "reasoning_effort" not in request_
    assert relay.post("chat", request_, OPENCODE)[0] == 200
    assert relay.posts[-1]["reasoningEffort"] == "max"
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert begin["effort_resolution"] == "cache_continuity" and begin["effective_effort"] == "max"


def test_the_update_instruction_and_a_nudge_after_it_are_the_same_task(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    mark = relay.mark()
    summary = chat.ask(SUMMARY, effort="high", messages=with_instruction("chat", chat.messages, UPDATE))
    assert summary["reasoningEffort"] == "max"
    assert relay.events(mark, "turn.begin")[-1]["data"]["client_task"] == "compaction"


def test_claude_code_says_it_with_its_headers_and_the_instruction_needs_no_recognising(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay, "anthropic", headers=CLAUDE_CODE)
    chat.work()
    mark = relay.mark()
    summary = chat.compact(effort="high", instruction="A summary request in words the relay does not know.",
                           headers={"x-claude-code-request-class": "compaction", "x-claude-code-compaction": "auto"})
    assert summary["reasoningEffort"] == "max" and thinking(summary) == ["opaque-1", "opaque-2"]
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert (begin["client_task"], begin["effort_resolution"], begin["requested_effort"]) == (
        "compaction", "cache_continuity", "high")
    assert begin["compaction_trigger"] == "auto" and begin["request_class"] == "compaction"
    assert count(relay, mark, "native_history.reset") == 0


def test_claude_code_without_the_hint_headers_is_read_by_the_start_of_its_instruction(prompt_relay):
    # CLAUDE_CODE_GATEWAY_HINT_HEADERS is off by default behind a custom base URL: only the text says what the request is for
    relay = prompt_relay()
    chat = Chat(relay, "anthropic", headers=CLAUDE_CODE)
    chat.work()
    for index, wording in enumerate((CLAUDE_CODE_INSTRUCTION, CLAUDE_CODE_OLD_INSTRUCTION)):
        mark = relay.mark()
        summary = chat.compact(effort="high", instruction=wording)
        assert summary["reasoningEffort"] == "max" and thinking(summary) == ["opaque-1", "opaque-2"], index
        begin = relay.events(mark, "turn.begin")[-1]["data"]
        assert begin["client_task"] == "compaction" and begin["effort_resolution"] == "cache_continuity"
        assert "request_class" not in begin and "compaction_trigger" not in begin


def test_claude_code_text_that_merely_resembles_its_instruction_is_an_ordinary_request(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay, "anthropic", headers=CLAUDE_CODE)
    chat.work()
    mark = relay.mark()
    summary = chat.compact(effort="high", instruction="Please create a detailed summary of the conversation so far.")
    assert summary["reasoningEffort"] == "high"
    assert "client_task" not in relay.events(mark, "turn.begin")[-1]["data"]


def branches(relay):
    """The signed branches of the one dialog the relay holds, as (messages of its client history, signatures)."""
    sessions = list(relay.runtime.pool._conversations.values())
    assert len(sessions) == 1
    return [(len(branch.client), [block["signature"] for block in branch.assistant if block["type"] == "thinking"])
            for branch in sessions[0]._native_history.branches]


def test_a_compaction_leaves_the_branches_of_its_dialog_as_the_chat_left_them(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    before = branches(relay)
    chat.compact(effort="high")
    # nothing recorded, nothing replaced: the summary is a side request the client never continues
    assert branches(relay) == before and len(before) == 1


def test_the_chat_goes_on_from_its_branch_after_a_compaction_the_client_abandoned(prompt_relay):
    # a compaction that was cancelled or failed leaves the history as it was: the next turn resumes
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    chat.compact(effort="high")
    mark = relay.mark()
    following = chat.ask(tool_reply("c3", "opaque-3"))
    assert thinking(following) == ["opaque-1", "opaque-2"] and following["reasoningEffort"] == "max"
    assert count(relay, mark, "native_history.resume") == 1 and count(relay, mark, "native_history.reset") == 0


def test_a_compaction_repeated_or_nudged_resumes_the_same_branch_again(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    mark = relay.mark()
    # OpenCode 2.x asks again, with a reminder after the instruction, when the reply skipped the template
    nudge = ("The previous response did not fill in the required summary template. Do not call tools. Return the summary "
             "as text using the exact section headings from the template.")
    first = chat.compact(effort="high")
    second = chat.ask(SUMMARY, effort="high", messages=with_instruction(
        "chat", with_instruction("chat", chat.messages, INSTRUCTION), nudge))
    third = chat.compact(effort="high", instruction=UPDATE)
    for body in (first, second, third):
        assert body["reasoningEffort"] == "max" and thinking(body) == ["opaque-1", "opaque-2"]
    assert count(relay, mark, "native_history.resume") == 3 and count(relay, mark, "native_history.reset") == 0
    assert [event["data"]["client_task"] for event in relay.events(mark, "turn.begin")] == ["compaction"] * 3


def test_a_compaction_that_cannot_resume_records_no_branch_either(prompt_relay):
    relay = prompt_relay(compaction_keep_effort=False)
    chat = Chat(relay)
    chat.work()
    before = branches(relay)
    chat.compact(effort="high")  # the client's own effort: the history restarts, cold
    assert branches(relay) == before


def test_the_header_of_a_plugin_names_the_task_when_the_wording_is_unknown(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    mark = relay.mark()
    summary = chat.compact(effort="high", instruction="A wording of a later release.", headers={"x-proxi-task": "compaction"})
    assert summary["reasoningEffort"] == "max"
    assert relay.events(mark, "turn.begin")[-1]["data"]["client_task"] == "compaction"


def test_each_dialog_keeps_its_own_effort_unless_that_would_lower_the_clients(prompt_relay):
    """Owner's rule, 2026-11-06: the relay never lowers an effort to save cost. A dialog on max is compacted on max for a
    client that asks for high (a higher effort is no lowering); a dialog on low is not: the client asked for more, and the
    compaction runs on high, with the history written again. It used to run on low, the lowest the client never asked for."""
    relay = prompt_relay()
    one, other = Chat(relay, effort="max"), Chat(relay, effort="low", headers={**OPENCODE, "x-session-affinity": "ses_other"})
    one.work(), other.work()
    assert one.compact(effort="high")["reasoningEffort"] == "max"
    mark = relay.mark()
    summary = other.compact(effort="high")
    assert summary["reasoningEffort"] == "high"
    assert [event["data"]["reason"] for event in relay.events(mark, "cache.compaction_restart")] == ["lower"]
    assert count(relay, mark, "cache.effort_kept") == 0
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert (begin["requested_effort"], begin["effective_effort"], begin["effort_source"]) == ("high", "high", "client")


def test_a_model_the_dialog_was_not_on_has_nothing_to_continue(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    chat.model = "claude-opus-5-5"
    mark = relay.mark()
    summary = chat.compact(effort="high")
    # another model is another provider scope and another cache: its own effort stays
    assert summary["reasoningEffort"] == "high" and count(relay, mark, "cache.effort_kept") == 0
    restart = relay.events(mark, "cache.compaction_restart")
    assert [(event["data"]["reason"], event["data"]["differs"]) for event in restart] == [("scope", ["provider"])]


def test_a_compaction_retried_on_another_account_decides_its_effort_again(retool):
    # the first attempt keeps the dialog's effort and meets the hourly limit; the dialog moves to the other account, which
    # has none of its branches, so the retry is the client's own request (and the console line must not say otherwise)
    import adapters
    from test_review_fixes_pr19 import response

    first = request(TASK, key="conv-1")
    first.effort = "max"
    retool.script["acc-a"] = lambda _request: response(*tool_reply("c1", "opaque-1"))
    events = asyncio.run(run(retool.runner, first))
    adapter = adapters.ChatAdapter(first, None)
    for event in events:
        adapter.feed(event)
    assistant = adapter.payload("tool_calls")["choices"][0]["message"]
    messages = json.loads(json.dumps(TASK)) + [
        assistant, {"role": "tool", "tool_call_id": assistant["tool_calls"][0]["id"], "content": "file contents"},
        {"role": "user", "content": INSTRUCTION}]

    compaction = request(messages, key="conv-1")
    compaction.effort, compaction.client_task, compaction.request_id = "high", "compaction", "req-retry"
    retool.script["acc-a"] = hourly
    holder, mark = {}, journal.JOURNAL.last_seq
    asyncio.run(run(retool.runner, compaction, holder))

    begun = [event["data"] for event in journal.JOURNAL.query(since=mark, limit=500, order="asc")
             if event["kind"] == "turn.begin" and event.get("request_id") == "req-retry"]
    assert [(item["effective_effort"], item["effort_resolution"]) for item in begun] == [
        ("max", "cache_continuity"), ("high", "exact")]
    assert holder.get("kept_effort") is None and retool.posts[-2:] == ["acc-a", "acc-b"]


# -- _dialog_effort on its own: what it does and does not decide -----------------------------------------------------
class Idle:
    """A conversation on `effort` that has done one tool round: its pool, its runner and the next request's messages."""

    def __init__(self, tmp_path, monkeypatch, effort="max", **cfg):
        import asyncio
        import adapters

        self.pool, self.runner, self.sent = harness(tmp_path, monkeypatch, [tool_reply("c1", "opaque-1")])
        self.runner.cfg.update(cfg)
        self.session = None

        async def first():
            parsed = request(TASK, tools=TOOLS)
            parsed.effort = effort
            events = await run(self.runner, parsed)
            adapter = adapters.ChatAdapter(parsed, None)
            for event in events:
                adapter.feed(event)
            assistant = adapter.payload("tool_calls")["choices"][0]["message"]
            self.messages = json.loads(json.dumps(TASK)) + [
                assistant, {"role": "tool", "tool_call_id": assistant["tool_calls"][0]["id"], "content": "file contents"},
                {"role": "user", "content": INSTRUCTION}]
            self.session = self.pool._conversations["header:client-a"]

        asyncio.run(first())

    def ask(self, effort, *, messages=None, head="CLIENT_SYSTEM", choice="auto", exclude=(), key="header:client-a"):
        parsed = request(self.messages if messages is None else messages, tools=TOOLS)
        parsed.effort = effort
        parsed.conversation_key = key
        return self.runner._dialog_effort(parsed, head, choice, exclude)


def test_the_dialog_effort_is_asked_only_where_it_makes_the_history_resume(tmp_path, monkeypatch):
    idle = Idle(tmp_path, monkeypatch)
    assert idle.ask("high") == ("max", None)
    # the request already asks for it: it resumes as it is, and there is nothing to say
    assert idle.ask("max") == (None, None)
    # no effort of the request at all: the configured default is what it would run on
    idle.runner.cfg["reasoning_effort"] = "low"
    assert idle.ask(None) == ("max", None)
    idle.runner.cfg["reasoning_effort"] = "max"
    assert idle.ask(None) == (None, None)


def test_a_dialog_on_a_lower_effort_is_never_taken_for_a_compaction_that_asks_for_more(tmp_path, monkeypatch):
    idle = Idle(tmp_path, monkeypatch, effort="low")
    for asked in ("medium", "high", "max"):
        assert idle.ask(asked) == (None, {"reason": "lower"}), asked
    assert idle.ask("low") == (None, None)  # asking for it resumes the history as it is
    # no effort of the request: what it would run on is the configured default, and the same rule applies to that
    idle.runner.cfg["reasoning_effort"] = "high"
    assert idle.ask(None) == (None, {"reason": "lower"})
    idle.runner.cfg["reasoning_effort"] = "low"
    assert idle.ask(None) == (None, None)


def test_a_dialog_on_a_higher_effort_is_still_taken(tmp_path, monkeypatch):
    idle = Idle(tmp_path, monkeypatch, effort="high")
    assert idle.ask("low") == ("high", None) and idle.ask("medium") == ("high", None)
    assert idle.ask("high") == (None, None)
    assert idle.ask("max") == (None, {"reason": "lower"})


def test_the_levels_are_ranked_by_the_catalog_order():
    import orchestrator
    assert orchestrator._below("low", "high") and orchestrator._below("none", "minimal") and orchestrator._below("high", "xhigh")
    assert not orchestrator._below("high", "high") and not orchestrator._below("max", "low")
    assert not orchestrator._below("auto", "high") and not orchestrator._below("low", None) and not orchestrator._below(None, "low")


REASONS = {
    "head": {"reason": "scope", "differs": ["head"]},
    "tools": {"reason": "scope", "differs": ["tools"]},
    "tool_choice": {"reason": "scope", "differs": ["tool_choice"]},
    "history": {"reason": "history"},
    "key": {"reason": "no_branch"},
    "exclude": {"reason": "no_branch"},
}


@pytest.mark.parametrize("change", sorted(REASONS))
def test_a_request_that_differs_in_anything_but_the_effort_keeps_it_and_says_what_differs(tmp_path, monkeypatch, change):
    idle = Idle(tmp_path, monkeypatch)
    arguments = {}
    if change == "head":
        arguments["head"] = "ANOTHER_HEAD"
    elif change == "tool_choice":
        arguments["choice"] = "required"
    elif change == "history":
        edited = json.loads(json.dumps(idle.messages))
        edited[0]["content"] = "inspect EDITED_TASK"
        arguments["messages"] = edited
    elif change == "key":
        arguments["key"] = "header:another-dialog"
    elif change == "exclude":
        arguments["exclude"] = (idle.session.backend,)
    parsed = request(arguments.get("messages", idle.messages), tools=TOOLS)
    if change == "tools":
        parsed.tools[0]["function"]["description"] = "changed tool"
    parsed.effort, parsed.conversation_key = "high", arguments.get("key", "header:client-a")
    assert idle.runner._dialog_effort(parsed, arguments.get("head", "CLIENT_SYSTEM"), arguments.get("choice", "auto"),
                                      arguments.get("exclude", ())) == (None, REASONS[change])


def test_a_model_or_an_account_that_is_not_the_branchs_is_named_as_the_provider(tmp_path, monkeypatch):
    idle = Idle(tmp_path, monkeypatch)
    parsed = request(idle.messages, tools=TOOLS)
    parsed.effort = "high"
    idle.session.backend.cfg["provider_resource_name"] = "another-resource"
    assert idle.runner._dialog_effort(parsed, "CLIENT_SYSTEM", "auto", ()) == (
        None, {"reason": "scope", "differs": ["provider"]})


def test_a_branch_effort_the_model_does_not_take_is_no_continuation(tmp_path, monkeypatch):
    idle = Idle(tmp_path, monkeypatch)
    history = idle.session._native_history
    branch = history.branches[0]
    doctored = json.loads(branch.scope)
    doctored[1]["reasoningEffort"] = "xhigh"  # a level the model has not offered since
    history.branches[0] = native_history.Snapshot(json.dumps(doctored, sort_keys=True, ensure_ascii=True), branch.client,
                                                  branch.messages, branch.assistant, branch.echo)
    assert idle.ask("high") == (None, {"reason": "effort"})
    # a nearest level is another prefix, so allowing one does not make it a continuation
    idle.runner.cfg["allow_effort_downgrade"] = True
    assert idle.ask("high") == (None, {"reason": "effort"})


def test_the_scope_the_transport_stores_is_the_one_native_scope_builds(tmp_path, monkeypatch):
    # iter_events keys its branches by native_scope, and the question above asks it before the turn: one definition
    idle = Idle(tmp_path, monkeypatch)
    branch = idle.session._native_history.branches[0]
    provider = {"model": "claude-opus-5-5", "reasoningEffort": "max", "providerResourceName": "test-provider"}
    tools = direct.tools_to_retool(request(TASK, tools=TOOLS).tools)
    assert branch.scope == direct.native_scope("header:client-a", provider, "CLIENT_SYSTEM", tools, "auto")
    assert direct.scope_effort(branch.scope) == "max"
    # verbosity steers the reply and is no part of what a signature belongs to
    assert direct.native_scope("k", {**provider, "verbosity": "low"}, "h", tools, "auto") == \
        direct.native_scope("k", provider, "h", tools, "auto")


def test_the_scope_names_the_effort_of_its_branch():
    scope = json.dumps(["header:a", {"model": "m", "reasoningEffort": "max", "providerResourceName": "p"},
                        "head", [], "auto"], sort_keys=True, ensure_ascii=True)
    assert direct.scope_effort(scope) == "max"
    for junk in (None, "", "not json", "[]", "[1]", json.dumps(["k", {}, "h"]), json.dumps(["k", {"reasoningEffort": ""}]),
                 json.dumps(["k", {"reasoningEffort": 3}]), json.dumps(["k", None]), json.dumps({"a": 1})):
        assert direct.scope_effort(junk) is None, junk


# -- the setting -------------------------------------------------------------------------------------------------------
def test_the_setting_is_on_by_default_checked_at_start_and_shown_in_the_example(tmp_path, monkeypatch):
    import apierr
    import config
    from test_docs_consistency import ROOT, load_with

    assert config.DEFAULTS["compaction_keep_effort"] is True
    assert load_with(tmp_path, monkeypatch)["compaction_keep_effort"] is True
    assert load_with(tmp_path, monkeypatch, compaction_keep_effort=False)["compaction_keep_effort"] is False
    for junk in ("false", 0, 1, None, "yes"):
        with pytest.raises(apierr.ApiError, match="compaction_keep_effort must be a boolean"):
            load_with(tmp_path, monkeypatch, compaction_keep_effort=junk)
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["compaction_keep_effort"] is True and example["compaction_keep_effort_note"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "| compaction_keep_effort |" in readme and "cache_continuity" in readme and "client_task" in readme


def test_the_setting_is_live_in_the_admin_and_the_next_compaction_obeys_it(admin_relay):
    status, body, _ = admin_relay.request("/admin/api/settings")
    field = body["schema"]["compaction_keep_effort"]
    assert status == 200 and field["type"] == "boolean" and field["group"] == "model"
    assert field["restart_required"] is False and body["settings"]["compaction_keep_effort"] is True
    status, answer, _ = admin_relay.patch({"compaction_keep_effort": False})
    assert status == 200 and answer["restart_required"] is False
    assert admin_relay.cfg["compaction_keep_effort"] is False
    assert json.loads(admin_relay.path.read_text(encoding="utf-8"))["compaction_keep_effort"] is False
    assert admin_relay.patch({"compaction_keep_effort": "false"})[0] == 400
    assert admin_relay.patch({"compaction_keep_effort": True})[0] == 200 and admin_relay.cfg["compaction_keep_effort"] is True


def test_switching_the_setting_changes_the_very_next_compaction(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay)
    chat.work()
    # what the admin does on a PATCH: update the cfg every request reads
    relay.runtime.cfg["compaction_keep_effort"] = False
    assert chat.compact(effort="high")["reasoningEffort"] == "high"
    relay.runtime.cfg["compaction_keep_effort"] = True
    assert chat.compact(effort="high")["reasoningEffort"] == "max"


# -- SessionPool.peek: acquire's own choice of session, without taking it ---------------------------------------------
def peek_pool(tmp_path, **cfg):
    import asyncio
    from test_native_context_reuse import setup

    backend, pool, runner = setup(tmp_path, **cfg)

    async def first(key):
        await run(runner, request(TASK, key=key))

    return backend, pool, lambda key: asyncio.run(first(key))


def test_peek_returns_the_session_of_a_dialog_and_never_creates_one(tmp_path):
    backend, pool, start = peek_pool(tmp_path)
    assert pool.peek("header:client-a") is None
    start("header:client-a")
    session = pool._conversations["header:client-a"]
    assert pool.peek("header:client-a") is session
    assert pool.peek("header:another") is None and pool.peek(None) is None
    assert list(pool._conversations) == ["header:client-a"] and backend.created == ["thread-1"]
    # the same choice acquire makes: no session for an isolated job, a text scope is a session of its own
    assert pool.peek("header:client-a", isolated=True) is None
    assert pool.peek("header:client-a", context_scope="text") is None


def test_peek_leaves_a_session_acquire_would_move_to_another_account(tmp_path):
    backend, pool, start = peek_pool(tmp_path)
    start("header:client-a")
    session = pool._conversations["header:client-a"]
    # a failed account of this very request, an account switched off, and one that cannot serve the model
    assert pool.peek("header:client-a", exclude=(session.backend,)) is None
    session.backend.unavailable = {"reason": "exhausted", "available": 0, "threshold": 20}
    assert pool.peek("header:client-a") is None
    session.backend.unavailable = None
    assert pool.peek("header:client-a") is session
    import models
    assert pool.peek("header:client-a", model_entry=models.resolve("claude-opus-5-5")[0]) is session
    session.backend.cfg["provider_resource_name"] = ""
    assert pool.peek("header:client-a", model_entry=models.resolve("claude-opus-5-5")[0]) is None


def test_peek_in_shared_mode_is_the_one_shared_session_unless_the_request_is_isolated(tmp_path):
    backend, pool, start = peek_pool(tmp_path, session_mode="shared")
    assert pool.peek("header:client-a") is None
    start("header:client-a")
    assert pool._shared is not None and pool.peek("header:client-a") is pool._shared
    assert pool.peek(None) is pool._shared and pool.peek("header:client-a", isolated=True) is None
    assert pool.peek("header:client-a", context_scope="text") is None


def test_peek_in_per_request_mode_has_nothing_to_continue(tmp_path):
    backend, pool, start = peek_pool(tmp_path, session_mode="per_request")
    start("header:client-a")
    assert pool.peek("header:client-a") is None
