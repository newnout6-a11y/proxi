"""history.user_blocks: text edits of user messages and tool results, and the deletion rules."""
import pytest

import prompt_body
from test_prompt_body_support import (
    DIALECTS, IMAGE, REMINDER, TOOL_REMINDER, FakeSnapshot, conversation, drop_tag, history_of, neutral, parse,
    profile)


def rewrite(data, dialect, *blocks, **kw):
    return prompt_body.rewrite(data, dialect, profile(user_blocks=blocks, **kw), FakeSnapshot())


def user_text(text, dialect, *, tool=False, parts=False):
    """One conversation whose edited message carries text; tool=True puts it in a tool result."""
    value = [text] if parts else text
    if tool:
        turns = [("user", "start"), ("call", "c1", "bash"), ("result", "c1", value), ("assistant", "ok")]
        return conversation(dialect, turns), neutral(conversation(dialect, turns), dialect)
    turns = [("user", value), ("assistant", "ok"), ("user", "more")]
    return conversation(dialect, turns), None


def edited(dialect, text, *blocks, tool=False, parts=False):
    data, _ = user_text(text, dialect, tool=tool, parts=parts)
    result, changes = rewrite(data, dialect, *blocks)
    turns = neutral(result, dialect)
    return (turns[2][2] if tool else turns[0][1]), changes


@pytest.mark.parametrize("parts", [False, True], ids=["string", "parts"])
@pytest.mark.parametrize("tool", [False, True], ids=["user", "tool-result"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_drop_tag_removes_the_block_from_user_text_and_from_tool_results(dialect, tool, parts):
    text, changes = edited(dialect, "Fix it.\n\n" + REMINDER, drop_tag(), tool=tool, parts=parts)
    assert text == "Fix it."
    assert len(changes) == 1 and changes[0]["op"] == "drop_tag"
    assert changes[0]["target"] == ("tool_result" if tool else "user")
    assert changes[0]["removed_chars"] == len("\n\n" + REMINDER)


@pytest.mark.parametrize("text,expected", [
    ("Hello\n\n<system-reminder>\nPlan mode\n</system-reminder>", "Hello"),
    ("<system-reminder>\nX\n</system-reminder>\n\nHello", "Hello"),
    ("A\n\n<system-reminder>X</system-reminder>\n\nB", "A\n\nB"),
    ("A\n<system-reminder>X</system-reminder>\nB", "A\nB"),
    ("A\r\n\r\n<system-reminder>\r\nX\r\n</system-reminder>\r\n\r\nB", "A\r\n\r\nB"),
    ("A\n\n<system-reminder>1</system-reminder>\n<system-reminder>2</system-reminder>\n\nB", "A\n\nB"),
    ("A\n\n<system-reminder>X</system-reminder>\n\n\n", "A"),
    ("  <system-reminder>\n  indented\n  </system-reminder>\nrest", "rest"),
    ("A\n<system-reminder>X</system-reminder> and more\nB", "A\n and more\nB"),
    ("A\n<system-reminder>X</system-reminder>", "A"),
    ("first\n<system-reminder>1</system-reminder>\nmiddle\n<system-reminder>2</system-reminder>\nlast",
     "first\nmiddle\nlast"),
], ids=["end", "start", "middle-blank-lines", "middle-single-newline", "crlf", "two-blocks", "trailing-blanks",
        "indented", "text-after-close", "no-newline-before-end", "three-lines"])
def test_drop_tag_leaves_no_gap_where_the_block_stood(text, expected):
    result, _ = edited("chat", text, drop_tag())
    assert result == expected


@pytest.mark.parametrize("text", [
    "The tag <system-reminder> opens a block and </system-reminder> closes it.",
    'REMINDER = ("<system-reminder>", "</system-reminder>")',
    "inline <system-reminder>mention</system-reminder> in a sentence",
    "<system-reminder>\nnever closed\nand more text",
    "<system-reminders>\nother tag\n</system-reminders>",
    "<system-reminder/>\nself closing",
    "no tags at all",
])
def test_drop_tag_never_cuts_a_mention_inside_a_line_or_an_unclosed_or_different_tag(text):
    data = conversation("chat", [("user", text), ("assistant", "ok"), ("user", "more")])
    result, changes = rewrite(data, "chat", drop_tag())
    assert result == data and changes == []


def test_drop_tag_handles_attributes_nesting_and_an_unclosed_opening_tag_before_a_real_block():
    cases = [
        ('Intro\n<system-reminder source="agents" n="2">\nBody\n</system-reminder>\nOutro', "Intro\nOutro"),
        ("Intro\n<system-reminder>\nouter\n<system-reminder>\ninner\n</system-reminder>\nmore\n</system-reminder>\n"
         "Outro", "Intro\nOutro"),
        ("<system-reminder>\nunclosed\nA\n\n<system-reminder>\nreal\n</system-reminder>\nB",
         "<system-reminder>\nunclosed\nA\n\nB"),
    ]
    for text, expected in cases:
        assert edited("chat", text, drop_tag())[0] == expected


def test_drop_tag_takes_any_tag_name_and_only_that_one():
    text = "<env>\nHOME=/x\n</env>\n\nkeep this\n\n<system-reminder>\nnote\n</system-reminder>"
    assert edited("chat", text, drop_tag("env"))[0] == "keep this\n\n<system-reminder>\nnote\n</system-reminder>"


def test_drop_tag_with_contains_removes_only_the_blocks_that_hold_the_text():
    text = ("Do it.\n\n<system-reminder>\nPlan mode is active.\n</system-reminder>\n\n"
            "<system-reminder>\nUse tabs.\n</system-reminder>")
    assert edited("chat", text, drop_tag(contains="Plan mode"))[0] == (
        "Do it.\n\n<system-reminder>\nUse tabs.\n</system-reminder>")
    assert edited("chat", text, drop_tag(contains="plan mode"))[0] == text      # case-sensitive substring


def test_drop_tag_with_pattern_filters_blocks_by_regex_and_flags():
    text = "<system-reminder>\nPLAN mode on\n</system-reminder>\nrest"
    assert edited("chat", text, drop_tag(pattern=r"plan\s+mode"))[0] == text
    assert edited("chat", text, drop_tag(pattern=r"plan\s+mode", flags="i"))[0] == "rest"
    assert edited("chat", text, drop_tag(pattern=r"on$", flags="m"))[0] == "rest"
    both = "<system-reminder>\nPlan mode on\n</system-reminder>\nrest"
    assert edited("chat", both, drop_tag(contains="Plan", pattern=r"off"))[0] == both
    assert edited("chat", both, drop_tag(contains="Plan", pattern=r"on"))[0] == "rest"


@pytest.mark.parametrize("text,rule,expected", [
    ("<n>a</n><n>b</n>\nrest", drop_tag("n"), "rest"),
    ("<n>a</n> <n>b</n>\nrest", drop_tag("n"), "rest"),
    ("A\n<n>a</n><n>b</n><n>c</n>\nB", drop_tag("n"), "A\nB"),
    ("A\n<n>a</n>  <n>b</n>\nB", drop_tag("n"), "A\nB"),
    ("<n>a</n>text<n>b</n>", drop_tag("n"), "text<n>b</n>"),
    ("<n>a</n><n>keep</n>", drop_tag("n", contains="a"), "<n>keep</n>"),
    ("<n>keep</n><n>a</n>", drop_tag("n", contains="a"), "<n>keep</n><n>a</n>"),
    ("<n>a</n><n>b</n><n>a</n>\nrest", drop_tag("n", contains="a"), "<n>b</n><n>a</n>\nrest"),
], ids=["adjacent", "blank-between", "three-in-a-line", "indented-second", "text-between", "filtered-second",
        "filtered-first-keeps-second", "filtered-middle"])
def test_blocks_that_follow_a_cut_block_on_its_line_go_too_so_a_second_run_finds_nothing(text, rule, expected):
    data = conversation("chat", [("user", text), ("assistant", "ok"), ("user", "more")])
    result, _ = rewrite(data, "chat", rule)
    assert neutral(result, "chat")[0][1] == expected
    assert rewrite(result, "chat", rule) == (result, [])


@pytest.mark.parametrize("dialect", DIALECTS)
def test_remove_regex_applies_flags_in_every_dialect(dialect):
    text = "Keep.\nSECRET-TOKEN-1\nsecret-token-2\nEnd."
    plain, changes = edited(dialect, text, {"op": "remove_regex", "pattern": r"SECRET-TOKEN-\d\n"})
    assert plain == "Keep.\nsecret-token-2\nEnd." and changes[0]["removed_chars"] == len("SECRET-TOKEN-1\n")
    folded, _ = edited(dialect, text, {"op": "remove_regex", "pattern": r"secret-token-\d\n", "flags": "i"})
    assert folded == "Keep.\nEnd."
    multi, _ = edited(dialect, text, {"op": "remove_regex", "pattern": r"^secret.*$\n", "flags": "im"})
    assert multi == "Keep.\nEnd."
    dotall, _ = edited(dialect, text, {"op": "remove_regex", "pattern": r"Keep\..*End", "flags": "s"})
    assert dotall == "."


@pytest.mark.parametrize("dialect", DIALECTS)
def test_replace_text_replaces_every_occurrence_or_only_the_first(dialect):
    text = "use npm, then npm again; never yarn"
    every, changes = edited(dialect, text, {"op": "replace_text", "find": "npm", "with": "pnpm"})
    assert every == "use pnpm, then pnpm again; never yarn"
    assert changes == [{"scope": "history", "op": "replace_text", "edit": 0, "index": 0 + (dialect == "chat"),
                        "target": "user", "removed_chars": 6, "added_chars": 8}]
    first, changes = edited(dialect, text, {"op": "replace_text", "find": "npm", "with": "pnpm", "all": False})
    assert first == "use pnpm, then npm again; never yarn" and changes[0]["removed_chars"] == 3
    kept, changes = edited(dialect, text, {"op": "replace_text", "find": "NPM", "with": "pnpm"})
    assert kept == text and changes == []


def test_replace_text_that_grows_the_text_does_it_once_even_when_run_again():
    rule = {"op": "replace_text", "find": "git push", "with": "git push (ask first)"}
    data = conversation("chat", [("user", "run git push now, then git push (ask first) and git push"),
                                 ("assistant", "ok"), ("user", "more")])
    once, changes = rewrite(data, "chat", rule)
    assert neutral(once, "chat")[0][1] == "run git push (ask first) now, then git push (ask first) and git push (ask first)"
    assert changes[0]["removed_chars"] == 2 * len("git push")
    assert rewrite(once, "chat", rule) == (once, [])
    first_only = dict(rule, all=False)
    one, _ = rewrite(data, "chat", first_only)
    assert neutral(one, "chat")[0][1].startswith("run git push (ask first) now, then git push (ask first) and git push")
    assert neutral(one, "chat")[0][1].count("(ask first)") == 2
    assert rewrite(one, "chat", first_only) == (one, [])


@pytest.mark.parametrize("dialect", DIALECTS)
def test_images_and_foreign_blocks_are_never_touched_and_keep_the_message_alive(dialect):
    image = {"chat": {"type": "image_url", "image_url": {"url": IMAGE}},
             "responses": {"type": "input_image", "image_url": IMAGE},
             "anthropic": {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
             }[dialect]
    other = {"type": "mystery", "text": REMINDER}
    data = conversation(dialect, [("user", [REMINDER, image, other]), ("assistant", "ok"), ("user", "more")])
    result, changes = rewrite(data, dialect, drop_tag())
    content = history_of(result, dialect)[1 if dialect == "chat" else 0]["content"]
    assert content == [image, other] and changes[0]["removed_chars"] == len(REMINDER)
    assert "deleted" not in changes[0]
    with_image = conversation(dialect, [("user", [REMINDER, image]), ("assistant", "ok"), ("user", "more")])
    kept, _ = rewrite(with_image, dialect, drop_tag())
    assert parse(dialect, kept).native_messages[0]["content"][0]["type"] == "image"
    image_only = conversation(dialect, [("user", [image]), ("assistant", "ok"), ("user", "more")])
    assert rewrite(image_only, dialect, drop_tag(), {"op": "remove_regex", "pattern": "[A-Za-z]*Plan"}) == (
        image_only, [])


@pytest.mark.parametrize("dialect", DIALECTS)
def test_assistant_turns_tool_calls_and_the_head_are_never_edited(dialect):
    turns = [("user", "start"), ("assistant", "I saw " + REMINDER), ("call", "c1", "bash"),
             ("result", "c1", "fine"), ("assistant", "done " + REMINDER), ("user", "more")]
    data = conversation(dialect, turns, system="HEAD " + REMINDER)
    result, changes = rewrite(data, dialect, drop_tag(), {"op": "replace_text", "find": "bash", "with": "sh"},
                              {"op": "remove_regex", "pattern": "HEAD"})
    assert result == data and changes == []


def test_thinking_blocks_and_their_signatures_of_a_messages_assistant_turn_stay_untouched():
    thinking = {"type": "thinking", "thinking": "plan " + REMINDER, "signature": "opaque-" + REMINDER}
    redacted = {"type": "redacted_thinking", "data": "opaque"}
    use = {"type": "tool_use", "id": "c1", "name": "bash", "input": {"cmd": REMINDER}}
    data = {"model": "claude-opus-5-5", "max_tokens": 64, "system": "HEAD", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "go\n\n" + REMINDER}]},
        {"role": "assistant", "content": [thinking, redacted, use]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok\n\n" + REMINDER}]}]}
    result, changes = rewrite(data, "anthropic", drop_tag())
    assert result["messages"][1] == data["messages"][1]
    assert result["messages"][0]["content"] == [{"type": "text", "text": "go"}]
    assert result["messages"][2]["content"][0]["content"] == "ok"
    assert [c["target"] for c in changes] == ["user", "tool_result"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_message_that_every_edit_empties_is_deleted_and_the_rest_stays_in_order(dialect):
    turns = [("user", "start"), ("assistant", "one"), ("user", REMINDER), ("user", "again"), ("assistant", "two"),
             ("user", "more")]
    result, changes = rewrite(conversation(dialect, turns), dialect, drop_tag())
    assert neutral(result, dialect) == [("user", "start"), ("assistant", "one"), ("user", "again"),
                                        ("assistant", "two"), ("user", "more")]
    assert changes == [{"scope": "history", "op": "drop_tag", "edit": 0, "index": 2 + (dialect == "chat"),
                        "target": "user", "removed_chars": len(REMINDER), "added_chars": 0, "deleted": True}]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_deleted_message_stops_the_later_rules_for_it(dialect):
    turns = [("user", "start"), ("user", REMINDER), ("assistant", "ok"), ("user", "more")]
    result, changes = rewrite(conversation(dialect, turns), dialect, drop_tag(),
                              {"op": "replace_text", "find": "Plan", "with": "Plot"})
    assert [(c["op"], c.get("deleted")) for c in changes] == [("drop_tag", True)]
    assert neutral(result, dialect) == [("user", "start"), ("assistant", "ok"), ("user", "more")]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_parts_that_end_up_blank_leave_the_message_and_the_others_stay(dialect):
    turns = [("user", ["first", REMINDER, "  ", "second"]), ("assistant", "ok"), ("user", "more")]
    result, changes = rewrite(conversation(dialect, turns), dialect, drop_tag())
    content = history_of(result, dialect)[1 if dialect == "chat" else 0]["content"]
    assert [part["text"] for part in content] == ["first", "  ", "second"]      # an untouched blank part stays
    assert len(changes) == 1 and "deleted" not in changes[0]


def test_part_keys_other_than_the_text_are_kept():
    data = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "go\n\n" + REMINDER, "cache_control": {"type": "ephemeral"}},
                                     {"type": "text", "text": REMINDER, "cache_control": {"type": "ephemeral"}}]}]}
    result, _ = rewrite(data, "anthropic", drop_tag())
    assert result["messages"][0]["content"] == [
        {"type": "text", "text": "go", "cache_control": {"type": "ephemeral"}}]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_tool_result_is_never_deleted_when_its_text_is_removed(dialect):
    turns = [("user", "start"), ("call", "c1", "bash"), ("result", "c1", TOOL_REMINDER), ("assistant", "ok"),
             ("user", "more")]
    result, changes = rewrite(conversation(dialect, turns), dialect, drop_tag())
    assert neutral(result, dialect) == [("user", "start"), ("call", "c1", "bash"), ("result", "c1", ""),
                                        ("assistant", "ok"), ("user", "more")]
    assert changes[0]["target"] == "tool_result" and "deleted" not in changes[0]
    parsed = parse(dialect, result)
    assert [m["role"] for m in parsed.native_messages] == ["user", "assistant", "tool", "assistant", "user"]
    assert parsed.native_messages[2]["content"] == {"toolCallId": "c1", "toolResult": ""}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_tool_result_made_of_parts_keeps_its_pair_when_the_parts_all_go(dialect):
    turns = [("user", "start"), ("call", "c1", "bash"), ("result", "c1", [TOOL_REMINDER]), ("assistant", "ok")]
    result, _ = rewrite(conversation(dialect, turns), dialect, drop_tag())
    assert neutral(result, dialect)[2] == ("result", "c1", "")
    assert parse(dialect, result).native_messages[2]["role"] == "tool"
    message = history_of(result, dialect)[3 if dialect == "chat" else 2]
    # An emptied list of parts becomes the empty string, the one shape every upstream takes for "no output".
    raw = {"chat": message.get("content"), "responses": message.get("output"),
           "anthropic": message["content"][0]["content"] if dialect == "anthropic" else None}[dialect]
    assert raw == ""


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_tool_result_whose_text_is_only_whitespace_afterwards_becomes_the_empty_string(dialect):
    turns = [("user", "start"), ("call", "c1", "bash"), ("result", "c1", "ok  \n"), ("assistant", "ok"), ("user", "more")]
    result, changes = rewrite(conversation(dialect, turns), dialect, {"op": "remove_regex", "pattern": "ok"})
    assert neutral(result, dialect)[2] == ("result", "c1", "") and changes[0]["removed_chars"] == 2
    message = history_of(result, dialect)[3 if dialect == "chat" else 2]
    raw = {"chat": message.get("content"), "responses": message.get("output"),
           "anthropic": message["content"][0]["content"] if dialect == "anthropic" else None}[dialect]
    assert raw == ""


def test_messages_tool_results_are_edited_inside_and_keep_their_message_and_flags():
    data = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "bash", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "is_error": True, "cache_control": {"type": "ephemeral"},
             "content": [{"type": "text", "text": "boom\n" + REMINDER}, {"type": "text", "text": TOOL_REMINDER}]},
            {"type": "text", "text": REMINDER}]}]}
    result, changes = rewrite(data, "anthropic", drop_tag())
    assert result["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "is_error": True, "cache_control": {"type": "ephemeral"},
         "content": [{"type": "text", "text": "boom"}]}]
    assert [c["target"] for c in changes] == ["tool_result"]
    assert changes[0]["removed_chars"] == len("\n" + REMINDER) + len(TOOL_REMINDER) + len(REMINDER)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_deleting_a_user_message_is_allowed_between_users_at_the_start_and_at_the_end(dialect):
    middle = conversation(dialect, [("user", "start"), ("user", REMINDER), ("user", "again"), ("assistant", "ok")])
    first = conversation(dialect, [("user", REMINDER), ("user", "start"), ("assistant", "ok"), ("user", "more")])
    last = conversation(dialect, [("user", "start"), ("assistant", "ok"), ("user", "more"), ("user", REMINDER)])
    after_result = conversation(dialect, [("user", "start"), ("call", "c1", "bash"), ("result", "c1", "ok"),
                                          ("user", REMINDER)])
    for data in (middle, first, last, after_result):
        result, changes = rewrite(data, dialect, drop_tag())
        assert changes[0].get("deleted") is True and len(history_of(result, dialect)) == len(history_of(data, dialect)) - 1
        assert parse(dialect, result).native_messages is not None


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("name,turns,reason", [
    ("between-assistants", [("user", "start"), ("assistant", "one"), ("user", REMINDER), ("assistant", "two"),
                            ("user", "more")], "would put two assistant messages next to each other"),
    ("last-after-assistant", [("user", "start"), ("assistant", "one"), ("user", REMINDER)],
     "would end the conversation with an assistant message"),
    ("first-before-assistant", [("user", REMINDER), ("assistant", "one"), ("user", "more")],
     "would start the conversation with an assistant or tool message"),
    ("first-before-tool-call", [("user", REMINDER), ("call", "c1", "bash"), ("result", "c1", "ok")],
     "would start the conversation with an assistant or tool message"),
    ("only-message", [("user", REMINDER)], "would leave the conversation empty"),
])
def test_deleting_that_would_break_the_roles_is_refused_and_the_message_stays(dialect, name, turns, reason):
    data = conversation(dialect, turns)
    result, changes = rewrite(data, dialect, drop_tag())
    assert result == data
    assert changes == [{"scope": "history", "op": "drop_tag", "edit": 0, "index": turns.index(("user", REMINDER)) +
                        (dialect == "chat"), "target": "user", "skipped": True, "reason": reason}]
    assert rewrite(result, dialect, drop_tag())[0] == result


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_user_message_that_ends_up_as_the_first_cannot_pull_a_system_message_into_the_head(dialect):
    # [user (only a reminder), system, ...]: dropping the user message would make the system message
    # a leading instruction, that is a different head.
    data = conversation(dialect, [("user", REMINDER), ("system", "Late rule."), ("user", "more"), ("assistant", "ok")])
    result, changes = rewrite(data, dialect, drop_tag())
    assert result == data
    assert changes[0]["skipped"] and changes[0]["reason"] == "would move a later system message into the head"
    assert parse(dialect, result).system == "HEAD"


def test_an_edit_that_would_empty_a_string_input_is_refused():
    data = {"model": "claude-opus-5-5", "input": REMINDER, "instructions": "HEAD"}
    result, changes = rewrite(data, "responses", drop_tag())
    assert result == data and changes[0]["reason"] == "would leave the conversation empty"
    kept = {"model": "claude-opus-5-5", "input": "Fix it.\n\n" + REMINDER}
    assert rewrite(kept, "responses", drop_tag())[0]["input"] == "Fix it."


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_refused_deletion_still_lets_the_other_rules_edit_the_message(dialect):
    turns = [("user", "start"), ("assistant", "one"), ("user", REMINDER + "\nkeep me"), ("assistant", "two"),
             ("user", "more")]
    first, second = drop_tag(), {"op": "remove_regex", "pattern": r"keep me"}
    result, changes = rewrite(conversation(dialect, turns), dialect, first, second)
    # The first rule leaves "keep me", the second empties the message and is refused.
    assert neutral(result, dialect)[2] == ("user", "keep me")
    assert [(c["op"], c.get("skipped"), c.get("deleted")) for c in changes] == [
        ("drop_tag", None, None), ("remove_regex", True, None)]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_same_message_gets_the_same_edit_whatever_follows_it(dialect):
    # A conversation that grows by appending keeps the rewritten earlier messages byte for byte.
    turns = [("user", "start\n\n" + REMINDER), ("call", "c1", "bash"), ("result", "c1", "ok\n\n" + TOOL_REMINDER)]
    more = turns + [("assistant", "done"), ("user", "next\n\n" + REMINDER), ("call", "c2", "bash"),
                    ("result", "c2", "fine\n\n" + TOOL_REMINDER)]
    first, _ = rewrite(conversation(dialect, turns), dialect, drop_tag())
    later, _ = rewrite(conversation(dialect, more), dialect, drop_tag())
    assert history_of(later, dialect)[:len(history_of(first, dialect))] == history_of(first, dialect)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_edit_fields_name_the_rule_by_its_position(dialect):
    rules = [{"op": "replace_text", "find": "zzz", "with": "y"}, drop_tag(contains="nothing"), drop_tag()]
    data = conversation(dialect, [("user", "go\n" + REMINDER), ("assistant", "ok"), ("user", "more")])
    _, changes = rewrite(data, dialect, *rules)
    assert [c["edit"] for c in changes] == [2]


def test_edits_over_the_whole_json_never_change_what_they_do_not_name():
    data = conversation("chat", [("user", "go\n\n" + REMINDER), ("assistant", "ok"), ("user", "more")],
                        tools=[("bash", "Run.")], temperature=0.2, stream=True, metadata={"user_id": "u"},
                        tool_choice="auto", reasoning_effort="high")
    result, _ = rewrite(data, "chat", drop_tag())
    for key in ("model", "tools", "temperature", "stream", "metadata", "tool_choice", "reasoning_effort"):
        assert result[key] == data[key]
    assert list(result) == list(data)


def test_a_block_that_only_qualifies_once_the_blocks_inside_it_are_gone_goes_in_the_same_run():
    # The outer block is not a single word, so the rule skips it at first; its inner block is, and when that
    # is cut the outer one becomes one.
    text = "<n>\nouter\n<n>inner</n>\n</n>\nrest"
    rule = drop_tag("n", pattern=r"\A<n>\s*\S+\s*</n>\Z")
    once, changes = rewrite(conversation("chat", [("user", text), ("assistant", "ok"), ("user", "more")]), "chat", rule)
    assert neutral(once, "chat")[0][1] == "rest" and len(changes) == 1
    assert rewrite(once, "chat", rule) == (once, [])


@pytest.mark.parametrize("tag", ["system-reminder", "<system-reminder>", " </system-reminder> ", "<system-reminder/>"])
def test_the_tag_may_be_written_with_its_angle_brackets(tag):
    text, changes = edited("chat", "Fix it.\n\n" + REMINDER, {"op": "drop_tag", "tag": tag})
    assert text == "Fix it." and len(changes) == 1


@pytest.mark.parametrize("dialect", ["chat", "responses"])
def test_bare_strings_inside_a_list_of_parts_are_text_too(dialect):
    data = conversation(dialect, [("user", "start"), ("assistant", "ok"), ("user", ["keep", REMINDER, "more"]),
                                  ("assistant", "ok"), ("user", "last")])
    shift = 1 if dialect == "chat" else 0
    history_of(data, dialect)[2 + shift]["content"] = ["keep", REMINDER, "more"]       # plain strings, no part dicts
    result, changes = rewrite(data, dialect, drop_tag())
    assert history_of(result, dialect)[2 + shift]["content"] == ["keep", "more"]
    assert [(c["index"], c["removed_chars"]) for c in changes] == [(2 + shift, len(REMINDER))]
    assert parse(dialect, result).native_messages is not None
