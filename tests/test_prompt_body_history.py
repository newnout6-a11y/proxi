"""history.mid_system: what happens to a system / developer message that arrives after the head."""
import copy
import json

import pytest

import native_history
import prompt_body
from apierr import ApiError
from test_prompt_body_support import (
    DIALECTS, REMINDER, FakeSnapshot, conversation, drop_tag, history_of, neutral, parse, profile)

LATE = "Late rule: answer in Russian."
CONTROL = {"output_config": {"effort": "low"}}


def rewrite(data, dialect, mode, snapshot=None, **kw):
    return prompt_body.rewrite(data, dialect, profile(mid_system=mode, **kw), snapshot or FakeSnapshot())


def basic(dialect, **extra):
    return conversation(dialect, [("user", "start"), ("system", LATE), ("assistant", "ok"), ("user", "more")], **extra)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_keep_leaves_the_history_exactly_as_the_client_sent_it(dialect):
    data = basic(dialect)
    result, changes = rewrite(data, dialect, "keep")
    assert result == data and changes == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_wrap_turns_the_message_into_user_text_in_the_built_in_wrapper(dialect):
    result, changes = rewrite(basic(dialect), dialect, "wrap")
    assert neutral(result, dialect) == [("user", "start"), ("user", native_history.reminder(LATE)),
                                        ("assistant", "ok"), ("user", "more")]
    wrapped = native_history.reminder(LATE)
    shift = 1 if dialect == "chat" else 0
    assert changes == [{"scope": "history", "op": "mid_system", "mode": "wrap", "index": 1 + shift,
                        "target": "system", "removed_chars": 0, "added_chars": len(wrapped) - len(LATE)}]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_wrap_gives_validate_the_conversation_it_builds_itself_for_a_kept_message(dialect):
    data = basic(dialect)
    result, _ = rewrite(data, dialect, "wrap")
    kept, wrapped = parse(dialect, data), parse(dialect, result)
    assert wrapped.native_messages == kept.native_messages
    assert wrapped.system == kept.system == "HEAD"
    assert kept.system_messages_normalized is True and wrapped.system_messages_normalized is False
    assert len(wrapped.message_fingerprints) == len(kept.message_fingerprints)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_plain_turns_the_message_into_user_text_without_a_wrapper(dialect):
    result, changes = rewrite(basic(dialect), dialect, "plain")
    assert neutral(result, dialect) == [("user", "start"), ("user", LATE), ("assistant", "ok"), ("user", "more")]
    assert changes[0]["mode"] == "plain" and changes[0]["added_chars"] == 0 and changes[0]["removed_chars"] == 0
    assert parse(dialect, result).native_messages[1] == {"role": "user", "content": [{"type": "text", "text": LATE}]}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_drop_removes_the_message_and_keeps_its_neighbours(dialect):
    result, changes = rewrite(basic(dialect), dialect, "drop")
    assert neutral(result, dialect) == [("user", "start"), ("assistant", "ok"), ("user", "more")]
    assert changes == [{"scope": "history", "op": "mid_system", "mode": "drop", "index": 1 + (dialect == "chat"),
                        "target": "system", "removed_chars": len(LATE), "added_chars": 0, "deleted": True}]
    assert len(history_of(result, dialect)) == len(history_of(basic(dialect), dialect)) - 1


@pytest.mark.parametrize("mode", ["wrap", "plain", "drop"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_leading_instructions_are_the_head_and_stay_whatever_the_mode(dialect, mode):
    turns = [("lead", "HEAD ONE"), ("lead", "HEAD TWO"), ("user", "start"), ("system", LATE), ("assistant", "ok")]
    data = conversation(dialect, turns, system=None if dialect == "chat" else "TOP")
    result, _ = rewrite(data, dialect, mode)
    assert neutral(result, dialect, head=True)[:3] == [("system", "HEAD ONE"), ("system", "HEAD TWO"),
                                                       ("user", "start")]
    assert parse(dialect, result).system == parse(dialect, data).system
    assert len(native_history.client_messages(history_of(result, dialect), lambda b, w: b)) == (
        len(native_history.client_messages(history_of(data, dialect), lambda b, w: b)) - (mode == "drop"))


@pytest.mark.parametrize("mode", ["wrap", "plain", "drop"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_blank_system_entries_leave_no_trace_and_are_never_touched(dialect, mode):
    effort_only = {"output_config": {"effort": "high"}}
    turns = [("user", "start"), ("system", [], effort_only), ("assistant", "ok"), ("system", "  \n\t"),
             ("user", "more"), ("system", [{"type": "tool_removal", "tool": {"type": "tool_reference", "name": "Bash"}}]
              if dialect != "responses" else [])]
    data = conversation(dialect, turns)
    result, changes = rewrite(data, dialect, mode)
    assert result == data and changes == []


@pytest.mark.parametrize("mode", ["wrap", "plain", "drop"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_system_message_that_carries_effort_keeps_it_in_a_blank_entry(dialect, mode):
    data = conversation(dialect, [("user", "start"), ("system", LATE, CONTROL), ("assistant", "ok"), ("user", "more")])
    before = parse(dialect, data)
    result, changes = rewrite(data, dialect, mode)
    after = parse(dialect, result)
    assert before.effort == after.effort == "low"
    assert LATE not in json.dumps(history_of(result, dialect)[1 + (dialect == "chat")]["content"])
    blank = history_of(result, dialect)[1 + (dialect == "chat")]
    assert blank["role"] in ("system", "developer") and blank["content"] == [] and blank["output_config"] == CONTROL["output_config"]
    assert changes[0]["kept"] == ["output_config"]
    texts = [t for t in neutral(result, dialect) if t[0] == "user"]
    assert (LATE in str(texts)) == (mode != "drop")
    converted = [m for m in history_of(result, dialect) if m.get("role") == "user" and LATE in json.dumps(m)]
    assert len(converted) == (mode != "drop")
    assert all("output_config" not in m and "clear_at" not in m for m in converted)
    assert after.system_messages_normalized is False
    assert [m["role"] for m in after.native_messages] == (["user", "user", "assistant", "user"] if mode != "drop"
                                                         else ["user", "assistant", "user"])


@pytest.mark.parametrize("mode", ["keep", "wrap", "plain", "drop"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_structured_output_format_on_a_system_message_survives_every_mode(dialect, mode):
    fmt = {"type": "json_schema", "schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                                              "required": ["answer"], "additionalProperties": False}}
    data = conversation(dialect, [("user", "start"), ("system", LATE, {"output_config": {"format": fmt}}),
                                  ("user", "more")])
    reference = parse(dialect, data).structured_output
    assert reference is not None
    result, _ = rewrite(data, dialect, mode)
    assert parse(dialect, result).structured_output == reference


@pytest.mark.parametrize("mode", ["keep", "wrap", "plain", "drop"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_turn_scoped_system_message_stays_refused_in_every_mode(dialect, mode):
    data = conversation(dialect, [("user", "start"), ("system", LATE, {"clear_at": "next_user_message"}),
                                  ("user", "more")])
    result, _ = rewrite(data, dialect, mode)
    with pytest.raises(ApiError) as caught:
        parse(dialect, result)
    assert caught.value.status == 422 and "clear_at" in caught.value.param


@pytest.mark.parametrize("dialect", DIALECTS)
def test_drop_is_refused_when_two_assistant_messages_would_touch(dialect):
    data = conversation(dialect, [("user", "start"), ("assistant", "one"), ("system", LATE), ("assistant", "two"),
                                  ("user", "more")])
    result, changes = rewrite(data, dialect, "drop")
    assert result == data
    assert changes == [{"scope": "history", "op": "mid_system", "mode": "drop", "index": 2 + (dialect == "chat"),
                        "target": "system", "skipped": True,
                        "reason": "would put two assistant messages next to each other"}]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_drop_is_refused_when_the_conversation_would_end_with_an_assistant_message(dialect):
    data = conversation(dialect, [("user", "start"), ("assistant", "one"), ("system", LATE)])
    result, changes = rewrite(data, dialect, "drop")
    assert result == data and changes[0]["skipped"] and changes[0]["reason"] == (
        "would end the conversation with an assistant message")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_drop_is_allowed_at_the_end_after_a_user_message_or_a_tool_result(dialect):
    after_user = conversation(dialect, [("user", "start"), ("system", LATE)])
    after_result = conversation(dialect, [("user", "start"), ("call", "c1", "bash"), ("result", "c1", "ok"),
                                          ("system", LATE)])
    for data, last in ((after_user, "user"), (after_result, "result")):
        result, changes = rewrite(data, dialect, "drop")
        assert neutral(result, dialect)[-1][0] == last and changes[0].get("deleted") is True


@pytest.mark.parametrize("dialect", DIALECTS)
def test_consecutive_system_messages_are_dropped_together_unless_that_would_break_the_roles(dialect):
    run = conversation(dialect, [("user", "start"), ("system", "S1"), ("system", "S2"), ("assistant", "ok"),
                                 ("user", "more")])
    result, changes = rewrite(run, dialect, "drop")
    assert neutral(result, dialect) == [("user", "start"), ("assistant", "ok"), ("user", "more")]
    assert [c["index"] for c in changes] == [1 + (dialect == "chat"), 2 + (dialect == "chat")]
    # Between two assistant messages the first is removed (the neighbour is still a message of the
    # conversation) and the last one has to stay.
    stuck = conversation(dialect, [("user", "start"), ("assistant", "a"), ("system", "S1"), ("system", "S2"),
                                   ("assistant", "b"), ("user", "more")])
    result, changes = rewrite(stuck, dialect, "drop")
    assert neutral(result, dialect) == [("user", "start"), ("assistant", "a"), ("system", "S2"),
                                        ("assistant", "b"), ("user", "more")]
    assert [bool(c.get("skipped")) for c in changes] == [False, True]


@pytest.mark.parametrize("mode", ["wrap", "plain"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_wrap_and_plain_never_depend_on_the_neighbours(dialect, mode):
    data = conversation(dialect, [("user", "start"), ("assistant", "one"), ("system", LATE), ("assistant", "two")])
    result, changes = rewrite(data, dialect, mode)
    assert neutral(result, dialect)[2] == ("user", LATE if mode == "plain" else native_history.reminder(LATE))
    assert not changes[0].get("skipped")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_system_text_in_parts_is_joined_the_way_validate_joins_it(dialect):
    data = conversation(dialect, [("user", "start"), ("system", ["First line.", "", "Second line."]),
                                  ("assistant", "ok")])
    result, _ = rewrite(data, dialect, "plain")
    assert neutral(result, dialect)[1] == ("user", "First line.\nSecond line.")
    assert parse(dialect, result).native_messages == parse(dialect, data).native_messages[:1] + [
        {"role": "user", "content": [{"type": "text", "text": "First line.\nSecond line."}]}] \
        + parse(dialect, data).native_messages[2:]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_inline_tool_changes_do_not_travel_into_the_user_text(dialect):
    blocks = [{"type": "text", "text": LATE},
              {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "deploy"}}]
    data = conversation(dialect, [("user", "start"), ("system", blocks), ("assistant", "ok")])
    result, _ = rewrite(data, dialect, "wrap")
    wire = json.dumps(result)
    assert "tool_addition" not in wire and "deploy" not in wire
    assert parse(dialect, result).native_messages is not None


@pytest.mark.parametrize("dialect", DIALECTS)
def test_system_content_this_module_cannot_read_is_skipped_and_left_alone(dialect):
    data = conversation(dialect, [("user", "start"), ("system", [{"type": "mystery"}]), ("assistant", "ok")])
    result, changes = rewrite(data, dialect, "drop")
    assert result == data
    assert changes == [{"scope": "history", "op": "mid_system", "mode": "drop", "index": 1 + (dialect == "chat"),
                        "target": "system", "skipped": True, "reason": "unsupported system content"}]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_wrapper_comes_from_the_snapshot_and_follows_the_template_rules(dialect):
    snapshot = FakeSnapshot(reminder="[note]\n{{text}}\n[/note] \\{{text}} {{ text }}")
    result, _ = rewrite(basic(dialect), dialect, "wrap", snapshot)
    assert neutral(result, dialect)[1] == ("user", "[note]\n" + LATE + "\n[/note] {{text}} " + LATE)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_system_text_is_substituted_once_and_never_expanded_again(dialect):
    snapshot = FakeSnapshot(reminder="<<{{text}}>>")
    data = conversation(dialect, [("user", "start"), ("system", "mentions {{text}} and \\{{text}}"),
                                  ("assistant", "ok")])
    result, _ = rewrite(data, dialect, "wrap", snapshot)
    assert neutral(result, dialect)[1] == ("user", "<<mentions {{text}} and \\{{text}}>>")


@pytest.mark.parametrize("template", ["no placeholder here", "only \\{{text}} escaped", "", None])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_template_that_would_swallow_the_text_falls_back_to_the_built_in_wrapper(dialect, template):
    result, _ = rewrite(basic(dialect), dialect, "wrap", FakeSnapshot(reminder=template))
    assert neutral(result, dialect)[1] == ("user", native_history.reminder(LATE))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_text_that_is_already_wrapped_is_not_wrapped_again(dialect):
    already = "<system-reminder>\nClient wrapped this.\n</system-reminder>"
    data = conversation(dialect, [("user", "start"), ("system", already), ("assistant", "ok")])
    assert neutral(rewrite(data, dialect, "wrap")[0], dialect)[1] == ("user", already)
    custom = FakeSnapshot(reminder="[note]\n{{text}}\n[/note]")
    mine = "[note]\nClient wrapped this.\n[/note]"
    data = conversation(dialect, [("user", "start"), ("system", mine), ("assistant", "ok")])
    assert neutral(rewrite(data, dialect, "wrap", custom)[0], dialect)[1] == ("user", mine)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_default_wrapper_written_out_as_a_template_matches_the_built_in_one(dialect):
    explicit = FakeSnapshot(reminder="<system-reminder>\n{{text}}\n</system-reminder>")
    first, _ = rewrite(basic(dialect), dialect, "wrap", explicit)
    second, _ = rewrite(basic(dialect), dialect, "wrap")
    assert first == second


@pytest.mark.parametrize("dialect", DIALECTS)
def test_user_blocks_see_the_wrapped_message_so_a_second_run_changes_nothing(dialect):
    # The wrapper is a system-reminder, so a rule that drops system-reminder blocks removes it again.
    data = basic(dialect)
    rules = [drop_tag("system-reminder")]
    result, changes = rewrite(data, dialect, "wrap", user_blocks=rules)
    assert neutral(result, dialect) == [("user", "start"), ("assistant", "ok"), ("user", "more")]
    assert [(c["op"], c.get("deleted")) for c in changes] == [("mid_system", None), ("drop_tag", True)]
    assert rewrite(result, dialect, "wrap", user_blocks=rules) == (result, [])
    # A rule that names other text leaves the wrapped message in place.
    selective = [drop_tag("system-reminder", contains="Plan mode")]
    kept, _ = rewrite(data, dialect, "wrap", user_blocks=selective)
    assert neutral(kept, dialect)[1] == ("user", native_history.reminder(LATE))


def test_the_responses_developer_role_and_a_string_input_follow_the_same_rules():
    data = conversation("responses", [("user", "start"), ("system", LATE), ("assistant", "ok")])
    assert data["input"][1]["role"] == "developer"
    plain, _ = rewrite(data, "responses", "plain")
    assert plain["input"][1] == {"type": "message", "role": "user", "content": LATE}
    single = {"model": "claude-opus-5-5", "input": "just text", "instructions": "HEAD"}
    for mode in ("wrap", "plain", "drop"):
        assert rewrite(single, "responses", mode) == (single, [])


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_string_content_stays_a_string_and_parts_stay_parts(dialect):
    as_string = conversation(dialect, [("user", "start"), ("system", LATE), ("assistant", "ok")])
    as_parts = conversation(dialect, [("user", "start"), ("system", [LATE]), ("assistant", "ok")])
    index = 1 + (dialect == "chat")
    assert history_of(rewrite(as_string, dialect, "plain")[0], dialect)[index]["content"] == LATE
    plain_parts = history_of(rewrite(as_parts, dialect, "plain")[0], dialect)[index]["content"]
    assert plain_parts == [{"type": "input_text" if dialect == "responses" else "text", "text": LATE}]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("mode,deleted", [("plain", True), ("wrap", True), ("drop", True), ("keep", False)])
def test_a_system_message_the_same_run_converts_or_drops_is_not_a_head_to_protect(dialect, mode, deleted):
    # [user with only a reminder, system message, ...]: while the system message stays one, deleting the
    # user message in front of it would make it part of the head; once it is user text or gone it is not.
    data = conversation(dialect, [("user", REMINDER), ("system", LATE), ("user", "next"), ("assistant", "ok"),
                                  ("user", "more")])
    rules = [drop_tag("system-reminder", contains="Plan mode")]
    result, changes = rewrite(data, dialect, mode, user_blocks=rules)
    turns = neutral(result, dialect)
    assert (("user", REMINDER) not in turns) == deleted
    assert any(c.get("skipped") for c in changes) == (not deleted)
    assert parse(dialect, result).system == "HEAD"
    again, more = rewrite(result, dialect, mode, user_blocks=rules)
    assert again == result and [c for c in more if not c.get("skipped")] == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_dropped_system_message_that_leaves_a_blank_entry_still_blocks_that_deletion(dialect):
    data = conversation(dialect, [("user", REMINDER), ("system", LATE, CONTROL), ("user", "next"),
                                  ("assistant", "ok"), ("user", "more")])
    result, changes = rewrite(data, dialect, "drop", user_blocks=[drop_tag("system-reminder")])
    assert neutral(result, dialect)[0] == ("user", REMINDER)
    assert [(c["op"], c.get("skipped")) for c in changes] == [("drop_tag", True), ("mid_system", None)]
    assert rewrite(result, dialect, "drop", user_blocks=[drop_tag("system-reminder")])[0] == result


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_changes_of_both_passes_are_listed_by_the_position_in_the_clients_body(dialect):
    turns = [("user", "go\n\n" + REMINDER), ("assistant", "a"), ("system", LATE), ("user", "next\n\n" + REMINDER),
             ("system", "Second late rule."), ("assistant", "b"), ("user", "more")]
    _, changes = rewrite(conversation(dialect, turns), dialect, "plain", user_blocks=[drop_tag()])
    shift = 1 if dialect == "chat" else 0
    assert [(c["op"], c["index"] - shift) for c in changes] == [
        ("drop_tag", 0), ("mid_system", 2), ("drop_tag", 3), ("mid_system", 4)]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_positions_in_the_changes_stay_those_of_the_clients_body_after_system_messages_were_dropped(dialect):
    turns = [("user", "go"), ("system", "Rule one."), ("system", "Rule two."), ("assistant", "a"),
             ("user", "next\n\n" + REMINDER), ("assistant", "b"), ("user", "more")]
    shift = 1 if dialect == "chat" else 0
    result, changes = rewrite(conversation(dialect, turns), dialect, "drop", user_blocks=[drop_tag()])
    assert len(history_of(result, dialect)) == len(turns) - 2 + shift
    assert [(c["op"], c["index"] - shift) for c in changes] == [("mid_system", 1), ("mid_system", 2), ("drop_tag", 4)]
