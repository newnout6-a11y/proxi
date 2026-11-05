"""A rewritten conversation keeps its fingerprints and its signed native history from turn to turn.

The rewrite runs before validate.parse_*, so the fingerprints, the head, rit and the cache key all come
from the rewritten request. These tests replay real multi-turn conversations through the runner and the
direct transport (a scripted upstream, no network) and check that every turn resumes the signed branch of
the one before it, and that without the rewrite the same clients would have reset it.
"""
import asyncio
import copy
import json

import pytest

import direct
import journal
import native_history
import prompt_body
import validate
from test_native_context_reuse import PARSE_CFG, TOOLS, run
from test_native_history import append_result, close, harness, strip_cache, tool_reply
from test_prompt_body_support import (
    DIALECTS, REMINDER, TOOL_REMINDER, FakeSnapshot, drop_tag, neutral, profile, rich_conversation)
from test_review_fixes_pr19 import END, THINKING, signature, text

KEY = "header:rewrite-client"
RESET = {"native_history.reset", "cache.head_changed", "cache.key_changed"}


def client_body(dialect, messages, system="CLIENT_SYSTEM", **extra):
    """The raw request a client sends for its conversation so far (what proxy hands to rewrite)."""
    tools = copy.deepcopy(TOOLS)
    data = {"model": "claude-opus-5-5", "tools": tools}
    if dialect == "responses":
        data.update(input=copy.deepcopy(messages), instructions=system)
        data["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
    elif dialect == "anthropic":
        data.update(messages=copy.deepcopy(messages), system=system, max_tokens=64)
        data["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                          "input_schema": t["function"]["parameters"]} for t in tools]
    else:
        data["messages"] = [{"role": "system", "content": system}] + copy.deepcopy(messages)
    data.update(extra)
    return data


def prepare(dialect, data, rules):
    """Pass 1 is the client's body; with rules the relay rewrites it and parses the result (pass 2)."""
    if rules is not None:
        data, _ = prompt_body.rewrite(data, dialect, rules, FakeSnapshot())
    parsed = getattr(validate, "parse_" + dialect)(copy.deepcopy(data), PARSE_CFG)
    parsed.conversation_key = KEY
    return parsed


def assistant_text(dialect, value):
    if dialect == "chat":
        return {"role": "assistant", "content": value}
    if dialect == "anthropic":
        return {"role": "assistant", "content": [{"type": "text", "text": value}]}
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": value}]}


def remind_last_user(messages, reminder=REMINDER):
    """What a plan-mode client does on every request: append the reminder to the newest user message."""
    messages = copy.deepcopy(messages)
    last = max(i for i, m in enumerate(messages) if m.get("role") == "user" and isinstance(m.get("content"), str))
    messages[last]["content"] += "\n\n" + reminder
    return messages


async def converse(runner, dialect, rules):
    """Three turns (two tool calls, then an answer) and a fourth after a new user message; returns the bodies."""
    parsed_turns, base = [], [{"role": "user", "content": "Inspect ORIGINAL_TASK."}]
    for stage in range(4):
        data = client_body(dialect, remind_last_user(base))
        parsed = prepare(dialect, data, rules)
        events = await run(runner, parsed)
        assert not any(e["type"] == "error" for e in events)
        parsed_turns.append(parsed)
        if stage < 2:
            base = append_result(base, parsed, events, dialect, value="file contents\n\n" + TOOL_REMINDER)
        elif stage == 2:
            base = base + [assistant_text(dialect, "done"), {"role": "user", "content": "Now the second task."}]
    return parsed_turns


SCRIPTS = lambda: [tool_reply(), tool_reply("c2", "opaque-two"), [text("done"), *END], [text("again"), *END]]  # noqa: E731


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_turn_resumes_the_signed_branch_when_the_clients_reminders_are_rewritten_away(
        tmp_path, monkeypatch, dialect):
    log = journal.Journal()
    monkeypatch.setattr(journal, "JOURNAL", log)
    pool, runner, sent = harness(tmp_path, monkeypatch, SCRIPTS())
    rules = profile(user_blocks=[drop_tag(contains="Plan mode"), drop_tag(contains="Instructions from")])

    async def scenario():
        try:
            return await converse(runner, dialect, rules)
        finally:
            await close(pool)

    turns = asyncio.run(scenario())
    assert len(sent) == 4
    kinds = [event["kind"] for event in log.query()]
    assert kinds.count("native_history.resume") == 3 and not set(kinds) & RESET
    wire = json.dumps(sent)
    assert "Plan mode" not in wire and "Instructions from" not in wire and "AGENTS" not in wire
    for earlier, later in zip(sent, sent[1:]):
        assert strip_cache(later["messages"][:len(earlier["messages"])]) == strip_cache(earlier["messages"])
        assert later["openAICacheKey"] == earlier["openAICacheKey"]
        assert later["rit"] == direct.make_rit(later["messages"], later["tools"])
    assert [m["content"] for m in sent[0]["messages"][:1]] == [m["content"] for m in sent[3]["messages"][:1]]
    assert "opaque-one" in json.dumps(sent[3]) and "opaque-two" in json.dumps(sent[3])
    for earlier, later in zip(turns, turns[1:]):
        assert later.message_fingerprints[:len(earlier.message_fingerprints)] == earlier.message_fingerprints
        assert later.native_messages[:len(earlier.native_messages)] == earlier.native_messages
        assert later.system == earlier.system == "CLIENT_SYSTEM"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_without_the_rewrite_a_reminder_that_vanishes_from_an_old_message_resets_the_branch(
        tmp_path, monkeypatch, dialect):
    # The control of the test above: a plan-mode client puts its reminder on the newest user message only, so
    # the first message loses it when a second one arrives and the old prefix no longer matches.
    log = journal.Journal()
    monkeypatch.setattr(journal, "JOURNAL", log)
    pool, runner, sent = harness(tmp_path, monkeypatch, SCRIPTS())

    async def scenario():
        try:
            return await converse(runner, dialect, None)
        finally:
            await close(pool)

    asyncio.run(scenario())
    kinds = [event["kind"] for event in log.query()]
    assert kinds.count("native_history.resume") == 2 and "native_history.reset" in kinds
    assert "opaque-one" not in json.dumps(sent[3])


# --- Desktop runner V309: a system message after every user message ---------------------------------

BILLING = "x-anthropic-billing-header: cc_version=2.1.309.abc; cc_entrypoint=sdk-cli; cch=00000;"
ENVIRONMENT = "# Environment\n" + "".join("Variable %d: value %d\n" % (i, i) for i in range(400))


def v309_system():
    return [{"type": "text", "text": BILLING},
            {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "# Harness\nYou run inside the Claude desktop app.\n# Memory\nNone.\n",
             "cache_control": {"type": "ephemeral"}}]


def v309_body(turns):
    """The body of the n-th turn as the V309 captures show it: user, system, then [assistant, user, system]
    for every later turn; the newest system message carries the cache marker, the older ones are strings."""
    first = {"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>\nAttribution notes for this session.\n</system-reminder>\n"},
        {"type": "text", "text": "Hello there."},
        {"type": "text", "text": "<command-message>skill</command-message>\n<command-name>/skill</command-name>"},
        {"type": "text", "text": "Context blob. " * 50}]}
    messages = [first, {"role": "system", "content": ENVIRONMENT, "output_config": {"effort": "high"}}]
    for turn in range(1, turns):
        messages += [{"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
                     {"role": "user", "content": [{"type": "text", "text": "Question %d." % turn}]},
                     {"role": "system", "content": "<total_tokens>%d tokens used</total_tokens>" % (turn * 1000)}]
    last = messages[-1]
    last["content"] = [{"type": "text", "text": last["content"] if isinstance(last["content"], str) else last["content"],
                        "cache_control": {"type": "ephemeral"}}]
    return {"model": "claude-opus-5-5", "max_tokens": 4096, "system": v309_system(), "messages": messages,
            "thinking": {"type": "adaptive", "display": "updates"}, "output_config": {"effort": "high"},
            "tools": [{"name": "read_file", "description": "Read the client's file",
                       "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
            "metadata": {"user_id": json.dumps({"session_id": "desktop-1"})}, "stream": True}


def v309_script(turn):
    return [*THINKING, signature("sig-%d" % turn), {"event": "content_block_end", "type": "thinking"},
            text("OK"), *END]


@pytest.mark.parametrize("blocks", [False, True], ids=["mid-system-only", "plus-user-blocks"])
@pytest.mark.parametrize("mode", ["keep", "drop", "plain", "wrap"])
def test_a_desktop_runner_that_sends_system_every_turn_keeps_its_head_and_resumes_on_every_turn(
        tmp_path, monkeypatch, mode, blocks):
    turns = 6
    log = journal.Journal()
    monkeypatch.setattr(journal, "JOURNAL", log)
    pool, runner, sent = harness(tmp_path, monkeypatch, [v309_script(n) for n in range(1, turns + 1)])
    rules = profile(mid_system=mode, user_blocks=[drop_tag(contains="Attribution")] if blocks else [])

    async def scenario():
        parsed_turns = []
        try:
            for n in range(1, turns + 1):
                parsed = prepare("anthropic", v309_body(n), rules)
                events = await run(runner, parsed)
                assert not any(e["type"] == "error" for e in events)
                parsed_turns.append(parsed)
        finally:
            await close(pool)
        return parsed_turns

    parsed_turns = asyncio.run(scenario())
    kinds = [event["kind"] for event in log.query()]
    assert kinds.count("native_history.resume") == turns - 1 and not set(kinds) & RESET
    heads = [strip_cache(body["messages"][0]) for body in sent]
    assert all(head == heads[0] for head in heads) and heads[0]["role"] == "system"
    assert heads[0]["content"][0]["text"] == BILLING and len(heads[0]["content"]) == 2
    assert len({body["openAICacheKey"] for body in sent}) == 1 and len({body["reasoningEffort"] for body in sent}) == 1
    assert all(parsed.system == parsed_turns[0].system for parsed in parsed_turns)
    for earlier, later in zip(parsed_turns, parsed_turns[1:]):
        assert later.message_fingerprints[:len(earlier.message_fingerprints)] == earlier.message_fingerprints
        assert later.native_messages[:len(earlier.native_messages)] == earlier.native_messages
    for number, body in enumerate(sent, start=1):
        wire = json.dumps(body["messages"][1:])
        assert all("sig-%d" % earlier in wire for earlier in range(1, number))
        assert ("Variable 399" in wire) == (mode in ("keep", "plain", "wrap"))
        assert ("Attribution" in wire) == (not blocks)
        assert body["rit"] == direct.make_rit(body["messages"], body["tools"])
    assert sent[0]["reasoningEffort"] == sent[-1]["reasoningEffort"]
    assert all(parsed.effort == "high" for parsed in parsed_turns)
    for parsed in parsed_turns:
        assert parsed.system_messages_normalized is (mode == "keep")


def test_the_shape_a_client_gives_an_inline_system_message_does_not_reach_the_prefix():
    # Turn n sends the newest system message as one block with a cache marker, turn n+1 sends it as a string.
    as_block, as_string = v309_body(3), v309_body(4)
    for rules in (profile(mid_system="plain"), profile(mid_system="wrap"), profile(mid_system="drop")):
        early, late = prepare("anthropic", as_block, rules), prepare("anthropic", as_string, rules)
        assert isinstance(as_block["messages"][-1]["content"], list) and isinstance(as_string["messages"][4]["content"], str)
        assert late.message_fingerprints[:len(early.message_fingerprints)] == early.message_fingerprints
        assert late.native_messages[:len(early.native_messages)] == early.native_messages
        assert late.message_chunks[:len(early.message_chunks)] == early.message_chunks


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("name", ["drop_tag", "wrap", "plain", "drop", "tools", "all"])
def test_every_prefix_of_a_conversation_rewrites_to_a_prefix_of_the_whole(dialect, name):
    rules = {
        "drop_tag": profile(user_blocks=[drop_tag()]),
        "wrap": profile(mid_system="wrap"), "plain": profile(mid_system="plain"), "drop": profile(mid_system="drop"),
        "tools": profile(tools=[{"tool": "bash", "op": "append", "text": "Careful."}]),
        "all": profile(mid_system="plain", user_blocks=[drop_tag(), {"op": "replace_text", "find": "tests",
                                                                      "with": "suite"}]),
    }[name]
    whole = rich_conversation(dialect)
    key = "input" if dialect == "responses" else "messages"
    final, _ = prompt_body.rewrite(whole, dialect, rules, FakeSnapshot())
    final_parsed = getattr(validate, "parse_" + dialect)(copy.deepcopy(final), PARSE_CFG)
    checked = 0
    for size in range(1, len(whole[key]) + 1):
        part = copy.deepcopy(whole)
        part[key] = part[key][:size]
        if neutral(part, dialect)[-1:] and neutral(part, dialect)[-1][0] in ("assistant", "call"):
            continue            # a request always ends with something the model has to answer
        result, _ = prompt_body.rewrite(part, dialect, rules, FakeSnapshot())
        if not result[key]:
            continue
        parsed = getattr(validate, "parse_" + dialect)(copy.deepcopy(result), PARSE_CFG)
        assert result["tools"] == final["tools"]
        assert neutral(result, dialect) == neutral(final, dialect)[:len(neutral(result, dialect))]
        assert final_parsed.message_fingerprints[:len(parsed.message_fingerprints)] == parsed.message_fingerprints
        assert final_parsed.native_messages[:len(parsed.native_messages)] == parsed.native_messages
        checked += 1
    assert checked >= 3
