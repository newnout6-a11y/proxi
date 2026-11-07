"""DeepSeek Harness's compaction is known for a compaction by the opening of its instruction (v8, Harness 0.2.2-alpha.3).

@deepseek-ai/dsh-compaction-basic condenses the oldest span of the history with one more request: the system prompt, the tools and the first
messages of the chat byte for byte, so the provider reads the chat's cache (live run of 2026-11-07, GLM-5.3: a008077ef03a read 18,304 of 18,682
prompt tokens), then one user message that opens "You are now acting as a compaction engine for this AI coding assistant." (summarizer.ts). It
sends no header that says so and no session id, and the relay took it for an ordinary turn: the summary became the body its keepwarm pinged after
the chat had replaced that history (thread 385e552d, 0.2226 credits for the ping that was seen), and its instruction got the cache hint of a
message the chat would continue.
"""
import copy

import pytest

import client_context
from prompt_support import after_tool_call, client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_client_task import WORK, chat, tail_messages
from test_compaction_cache import count
from test_compaction_tail_cache import flags
from test_conversation_infer import HARNESS, USER
from test_native_history import tool_reply
from test_review_fixes_pr19 import END, text

# the start of the instruction as the package's README prints it; the rest is shortened here, only the opening is the sign
ENGINE = ("You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured "
          "checkpoint that lets another model resume the work with no loss of essential context.\n\nOutput EXACTLY the Markdown "
          "structure below: keep every section, in order.")
SUMMARY = [text("## Primary Request and Intent\n- inspect ORIGINAL_TASK"), *END]


def task(parsed):
    return client_context.client_task(parsed, {})


def test_the_instruction_of_harness_is_recognised_by_its_opening_with_or_without_tools():
    for tools in (False, True):
        parsed = chat([*WORK, {"role": "assistant", "content": "Done."}, *tail_messages(ENGINE)], tools=tools)
        assert task(parsed) == client_context.COMPACTION == "compaction"


def test_only_the_opening_counts_and_a_message_that_quotes_it_is_an_ordinary_one():
    quoted = "Please explain what happens when a harness says: " + ENGINE
    assert task(chat([*WORK, *tail_messages(quoted)])) == ""
    # the instruction must end the conversation: a request that moved on after it is the chat's own again
    moved_on = [*WORK, *tail_messages(ENGINE), {"role": "assistant", "content": "summary"}, *tail_messages("go on")]
    assert task(chat(moved_on)) == ""


def harness_chat(relay, rounds=3):
    """The requests of a Harness chat with no session id (tool rounds), and the messages it has by then."""
    messages, sent = copy.deepcopy(USER), []
    for number in range(1, rounds + 1):
        relay.scripts.append(tool_reply("c%d" % number, "opaque-%d" % number))
        status, payload, answer = relay.post("chat", client_request("chat", messages), HARNESS)
        assert status == 200, payload
        relay.settle(answer)
        sent.append(relay.posts[-1])
        messages = after_tool_call("chat", messages, payload)
    return messages, sent


def compact(relay, messages, span=3):
    """Harness's compaction: the first `span` messages of its chat as they were, then its instruction, the chat's tools declared."""
    relay.scripts.append(SUMMARY)
    status, payload, answer = relay.post("chat", client_request("chat", messages[:span] + [{"role": "user", "content": ENGINE}]),
                                         HARNESS)
    assert status == 200, payload
    relay.settle(answer)
    return relay.posts[-1]


def only_session(relay):
    sessions = list(relay.runtime.pool._conversations.values())
    assert len(sessions) == 1
    return sessions[0]


def test_the_compaction_of_a_harness_chat_is_labelled_and_leaves_its_conversation_as_it_was(prompt_relay):
    relay = prompt_relay(cache_keepwarm_seconds=240)
    messages, sent = harness_chat(relay)
    session = only_session(relay)
    branches = len(session._native_history.branches)
    mark = relay.mark()
    summary = compact(relay, messages)

    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert begin["client_task"] == "compaction" and begin["request_purpose"] == "client-tools" and begin["tools"] == len(summary["tools"])
    # the request of the chat's conversation, on its session: nothing new was made for it
    assert only_session(relay) is session and count(relay, mark, "session.create") == 0
    # the instruction is read, never written: no cache hint on it, and the chat's branches are as the chat left them
    assert "F" not in flags(summary)[-1] and len(session._native_history.branches) == branches
    assert summary["tools"] == sent[-1]["tools"] and summary["openAICacheKey"] == sent[-1]["openAICacheKey"]


def test_nothing_of_the_replaced_history_is_kept_warm_after_the_compaction(prompt_relay):
    relay = prompt_relay(cache_keepwarm_seconds=240)
    messages, sent = harness_chat(relay)
    session = only_session(relay)
    assert session._warm_blocked is None and len(session._warm_body["messages"]) == len(sent[-1]["messages"])
    summary = compact(relay, messages)
    # Harness replaces this history with the summary and never continues it: the cycle ends, and what it would have pinged is
    # not the body of the instruction
    assert session._warm_blocked == "compacted"
    assert session._warm_body is None or session._warm_body["messages"] != summary["messages"]


def test_the_same_request_as_an_ordinary_turn_is_still_kept_warm_and_written(prompt_relay):
    """Without the opening the request is the chat's own: the pin that tells the label is what changed the outcome."""
    relay = prompt_relay(cache_keepwarm_seconds=240)
    messages, _ = harness_chat(relay)
    session = only_session(relay)
    mark = relay.mark()
    relay.scripts.append(SUMMARY)
    status, payload, answer = relay.post("chat", client_request("chat", messages[:3] + [
        {"role": "user", "content": "Summarize the work so far in your own words."}]), HARNESS)
    assert status == 200, payload
    relay.settle(answer)
    assert "client_task" not in relay.events(mark, "turn.begin")[-1]["data"]
    assert session._warm_blocked is None and "F" in flags(relay.posts[-1])[-1]


@pytest.mark.parametrize("span", [1, 3])
def test_the_label_does_not_depend_on_how_much_of_the_chat_the_compaction_replays(prompt_relay, span):
    relay = prompt_relay()
    messages, _ = harness_chat(relay)
    mark = relay.mark()
    compact(relay, messages, span=span)
    assert relay.events(mark, "turn.begin")[-1]["data"]["client_task"] == "compaction"
