"""What the second parse of an edited body keeps of the notes the client's own body earned.

A profile may turn the system messages that come after the opening ones into user text or drop them
(history.mid_system). Pass 2 parses the rewritten body, which no longer holds those messages, so the notes they
earned are carried over from the parse of the body the client sent (proxy._carry_compatibility):
x-proxi-system-messages and the names a message of tool changes added to x-proxi-ignored-parameters.
"""
import copy

import pytest

import proxy
import validate
from prompt_support import client_request, fake_relay, prompt_relay  # noqa: F401
from test_prompt_runtime_body import Rewrites, edited

DIALECTS = ["chat", "anthropic", "responses"]
CFG = {"strict_models": True}
REMINDER = "MID_SYSTEM_REMINDER_91"
BY_VALUE = {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": {
    "name": "db_query", "description": "Run SQL", "input_schema": {"type": "object"}}}}
BY_REFERENCE = {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "Bash"}}
TOOL_CHANGES = ["tool_addition.tool_definition", "tool_removal"]
TEXT = [{"type": "text", "text": REMINDER}]


def conversation(dialect, blocks, **extra):
    """A request whose later system message holds `blocks`, in the form of the dialect."""
    messages = [{"role": "user", "content": "inspect"}, {"role": "system", "content": copy.deepcopy(blocks)},
                {"role": "user", "content": "next"}]
    return client_request(dialect, messages, system="CLIENT_SYSTEM", tools=False, **extra)


def parse(dialect, data):
    return getattr(validate, "parse_" + dialect)(copy.deepcopy(data), CFG)


def convert(mode):
    """A stand-in for prompt_body.rewrite with history.mid_system = mode, in the form P3's rewrite reports it.

    The system messages after the opening ones become user text ("wrap" inside a reminder, "plain" as it is) or leave
    the body ("drop"); the blocks of tool changes in them are not text, so they go with the message either way."""
    def result(data, dialect, profile_, snapshot):
        key = "input" if dialect == "responses" else "messages"
        body, kept, changes, opened = copy.deepcopy(data), [], [], False
        for index, message in enumerate(body[key]):
            if message.get("role") in ("system", "developer") and opened:
                content = message["content"]
                text = content if isinstance(content, str) else "\n".join(
                    block["text"] for block in content if block.get("type") == "text")
                change = {"scope": "history", "op": "mid_system", "mode": mode, "index": index, "target": "system",
                          "removed_chars": len(text)}
                if mode != "drop":
                    shown = "<system-reminder>\n%s\n</system-reminder>" % text if mode == "wrap" else text
                    kept.append({"role": "user", "content": shown})
                    change["added_chars"] = len(shown)
                changes.append(change)
                continue
            opened = opened or message.get("role") not in ("system", "developer")
            kept.append(message)
        body[key] = kept
        return body, changes
    return result


# -- the function ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("mode", ["wrap", "plain"])
def test_a_reminder_the_profile_turned_into_user_text_is_still_reported_as_inline_user_text(dialect, mode):
    data = conversation(dialect, TEXT)
    first = parse(dialect, data)
    rewritten, changes = convert(mode)(data, dialect, None, None)
    second = parse(dialect, rewritten)
    assert first.system_messages_normalized is True and second.system_messages_normalized is False
    proxy._carry_compatibility(first, second, changes)
    assert second.system_messages_normalized is True and REMINDER in second.prompt


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_reminder_the_profile_dropped_is_not_passed_and_not_reported_as_passed(dialect):
    data = conversation(dialect, TEXT)
    first = parse(dialect, data)
    rewritten, changes = convert("drop")(data, dialect, None, None)
    second = parse(dialect, rewritten)
    proxy._carry_compatibility(first, second, changes)
    assert first.system_messages_normalized is True
    assert second.system_messages_normalized is False and REMINDER not in second.prompt


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_note_about_system_messages_is_never_invented(dialect):
    plain = client_request(dialect, [{"role": "user", "content": "inspect"}], system="CLIENT_SYSTEM", tools=False)
    first = second = parse(dialect, plain)
    claim = {"scope": "history", "op": "mid_system", "mode": "wrap", "index": 1, "target": "system"}
    proxy._carry_compatibility(first, second, [claim])
    assert second.system_messages_normalized is False
    # a conversion the rewrite refused did not happen: the second parse reports what is in its body
    data = conversation(dialect, TEXT)
    refused = dict(claim, skipped=True, reason="it would end the conversation")
    first, second = parse(dialect, data), parse(dialect, conversation(dialect, TEXT))
    second.system_messages_normalized = False
    proxy._carry_compatibility(first, second, [refused])
    assert second.system_messages_normalized is False
    proxy._carry_compatibility(first, second, [claim])
    assert second.system_messages_normalized is True


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("mode", ["wrap", "plain", "drop"])
def test_tool_changes_named_in_a_reminder_the_profile_rewrote_stay_among_the_ignored_parameters(dialect, mode):
    data = conversation(dialect, [*TEXT, BY_VALUE, BY_REFERENCE], temperature=1)
    first = parse(dialect, data)
    assert first.inline_tool_changes == TOOL_CHANGES and "temperature" in first.declared_ignored
    rewritten, changes = convert(mode)(data, dialect, None, None)
    second = parse(dialect, rewritten)
    # the blocks went with the message: the second parse can name nothing of them
    assert second.inline_tool_changes == [] and not set(TOOL_CHANGES) & set(second.declared_ignored)
    proxy._carry_compatibility(first, second, changes)
    assert second.inline_tool_changes == first.inline_tool_changes
    assert second.declared_ignored == first.declared_ignored


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_message_of_tool_changes_alone_is_named_even_when_the_profile_dropped_it(dialect):
    data = conversation(dialect, [BY_REFERENCE, BY_VALUE])
    first = parse(dialect, data)
    rewritten, changes = convert("drop")(data, dialect, None, None)
    second = parse(dialect, rewritten)
    proxy._carry_compatibility(first, second, changes)
    assert first.system_messages_normalized is False and second.system_messages_normalized is False
    assert second.inline_tool_changes == ["tool_removal", "tool_addition.tool_definition"]
    assert set(second.inline_tool_changes) <= set(second.declared_ignored)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_what_the_second_parse_names_itself_is_not_named_twice(dialect):
    lead = {"role": "system", "content": [*TEXT, copy.deepcopy(BY_REFERENCE)]}
    data = client_request(dialect, [lead, {"role": "user", "content": "inspect"}], system="", tools=False)
    first = parse(dialect, data)
    rewritten, changes = convert("wrap")(data, dialect, None, None)
    assert changes == [] and rewritten == data  # an opening system message is the head: no rewrite touches it
    second = parse(dialect, rewritten)
    proxy._carry_compatibility(first, second, changes)
    assert second.inline_tool_changes == ["tool_removal"] and second.declared_ignored.count("tool_removal") == 1


def test_nothing_is_carried_when_the_first_parse_had_nothing_to_report():
    data = conversation("chat", TEXT)
    first = parse("chat", client_request("chat", [{"role": "user", "content": "inspect"}], tools=False))
    second = parse("chat", data)
    names = list(second.declared_ignored)
    proxy._carry_compatibility(first, second, [])
    assert second.declared_ignored == names and second.inline_tool_changes == []
    assert second.system_messages_normalized is True  # its own note, from its own body


# -- over HTTP ------------------------------------------------------------------------------------------------------
def compat(relay, mark, headers):
    """(level, control, parameters) of the request.compatibility events of the request these headers belong to."""
    return [(event["level"], event["data"].get("control"), tuple(event["data"].get("parameters", ())))
            for event in relay.events(mark, "request.compatibility") if event["request_id"] == headers["request-id"]]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["wrap", "plain"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_profile_that_turns_reminders_into_user_text_changes_nothing_the_client_is_told(
        prompt_relay, monkeypatch, dialect, mode, stream):
    relay = fake_relay(prompt_relay)
    data = conversation(dialect, [*TEXT, BY_VALUE, BY_REFERENCE], temperature=1, stream=stream)
    mark = relay.mark()
    status, _, legacy = relay.post(dialect, data)  # no revision yet: the legacy path
    assert status == 200
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch, convert(mode))
    status, _, through = relay.post(dialect, data)
    assert status == 200 and relay.prompts.plans[-1].body_edits
    assert legacy["x-proxi-system-messages"] == through["x-proxi-system-messages"] == "inline-as-user-text"
    assert through["x-proxi-ignored-parameters"] == legacy["x-proxi-ignored-parameters"]
    assert set(TOOL_CHANGES) <= set(through["x-proxi-ignored-parameters"].split(","))
    assert compat(relay, mark, through) == compat(relay, mark, legacy) and len(compat(relay, mark, through)) == 2
    for body in relay.posts:
        assert REMINDER in str(body["messages"]) and "db_query" not in str(body) and "tool_removal" not in str(body)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_profile_that_drops_reminders_passes_none_and_says_so_only_in_the_journal(
        prompt_relay, monkeypatch, dialect, stream):
    relay = fake_relay(prompt_relay)
    data = conversation(dialect, [*TEXT, BY_VALUE, BY_REFERENCE], temperature=1, stream=stream)
    mark = relay.mark()
    _, _, legacy = relay.post(dialect, data)
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch, convert("drop"))
    status, _, through = relay.post(dialect, data)
    assert status == 200
    # what the legacy path passed as user text, this one does not pass at all: no header, no note of it
    assert legacy["x-proxi-system-messages"] == "inline-as-user-text" and "x-proxi-system-messages" not in through
    assert through["x-proxi-ignored-parameters"] == legacy["x-proxi-ignored-parameters"]
    assert [control for _, control, _ in compat(relay, mark, through)] == [None]
    assert [control for _, control, _ in compat(relay, mark, legacy)] == [None, "mid_conversation_system"]
    assert REMINDER in str(relay.posts[0]["messages"]) and REMINDER not in str(relay.posts[1]["messages"])
    (plan,) = [e for e in relay.events(mark, "prompt.plan") if e["request_id"] == through["request-id"]]
    assert [(g["op"], g["mode"], g["count"]) for g in plan["data"]["history_changes"]] == [("mid_system", "drop", 1)]
