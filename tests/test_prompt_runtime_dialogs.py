"""A conversation through a profile keeps its signed history and its cache: pins, revisions and "apply to all"."""
import copy
import json

import pytest

import direct
from prompt_bodies import CLAUDE_SDK_HEADERS
from prompt_support import (after_tool_call, client_request, fake_relay, head_of, profile, prompt_relay,  # noqa: F401
                            runner_turns, strip_cache)
from test_native_history import tool_reply
from test_prompt_runtime_body import MARK, Rewrites
from test_review_fixes_pr19 import END, text

DIALECTS = ["chat", "anthropic", "responses"]
SYSTEM = "CLIENT_SYSTEM"
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]
TEMPLATE = "{{text:rules}}\n\n{{client_system}}"


def publish(store, rev, rules):
    store.publish(rev, {"mine": profile(TEMPLATE, texts={"rules": rules})}, default_profile="mine")


class Dialog:
    """One conversation as its client drives it: every answer is a tool call and the next request carries the result."""

    def __init__(self, relay, dialect, key="dialog-1", model="claude-opus-5-5"):
        self.relay, self.dialect, self.key, self.model = relay, dialect, key, model
        self.messages = list(USER)

    def turn(self, script):
        """Send the request, script what Retool answers, remember the answer for the next request."""
        self.relay.scripts.append(script)
        request = client_request(self.dialect, self.messages, system=SYSTEM, model=self.model)
        status, payload, _ = self.relay.post(self.dialect, request, {"x-session-affinity": self.key})
        assert status == 200, payload
        if any(event.get("type") == "tool_call" for event in script):
            self.messages = after_tool_call(self.dialect, self.messages, payload)
        return self.relay.posts[-1]


def signatures(body):
    return [block["signature"] for message in body["messages"] if isinstance(message.get("content"), list)
            for block in message["content"] if isinstance(block, dict) and block.get("type") == "thinking"]


def count(relay, mark, kind):
    return len(relay.events(mark, kind))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_conversation_on_a_profile_continues_its_signed_history_and_keeps_one_cache_key(prompt_relay, dialect):
    relay = fake_relay(prompt_relay)
    publish(relay.prompts, 1, "RULES_ONE")
    dialog, mark = Dialog(relay, dialect), relay.mark()
    first = dialog.turn(tool_reply("c1", "opaque-one"))
    second = dialog.turn(tool_reply("c2", "opaque-two"))
    third = dialog.turn([text("done"), *END])

    head = "RULES_ONE\n\n" + SYSTEM
    assert [head_of(body) for body in (first, second, third)] == [head] * 3
    keys = {body["openAICacheKey"] for body in (first, second, third)}
    assert keys == {direct.cache_key_for(head, "claude-opus-5-5", first["tools"])}
    for earlier, later, expected in ((first, second, ["opaque-one"]), (second, third, ["opaque-one", "opaque-two"])):
        assert strip_cache(later["messages"][:len(earlier["messages"])]) == strip_cache(earlier["messages"])
        assert signatures(later) == expected
        assert later["rit"] == direct.make_rit(later["messages"], later["tools"])
    assert count(relay, mark, "native_history.resume") == 2
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "cache.key_changed") == 0
    assert [plan.pinned for plan in relay.prompts.plans] == [False, True, True]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_new_revision_changes_new_conversations_and_leaves_the_active_one_alone(prompt_relay, dialect):
    relay = fake_relay(prompt_relay)
    publish(relay.prompts, 1, "RULES_ONE")
    active = Dialog(relay, dialect, "active")
    first = active.turn(tool_reply("c1", "opaque-one"))
    mark = relay.mark()

    publish(relay.prompts, 2, "RULES_TWO")  # the file changes while the conversation is open
    second = active.turn([text("done"), *END])
    newcomer = Dialog(relay, dialect, "newcomer").turn([text("hello"), *END])

    assert head_of(second) == head_of(first) == "RULES_ONE\n\n" + SYSTEM
    assert signatures(second) == ["opaque-one"] and second["openAICacheKey"] == first["openAICacheKey"]
    assert head_of(newcomer) == "RULES_TWO\n\n" + SYSTEM and newcomer["openAICacheKey"] != first["openAICacheKey"]
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "cache.key_changed") == 0
    assert [(plan.rev, plan.pinned) for plan in relay.prompts.plans] == [(1, False), (1, True), (2, False)]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_apply_to_all_costs_a_conversation_exactly_one_reset_and_then_it_resumes(prompt_relay, dialect):
    relay = fake_relay(prompt_relay)
    publish(relay.prompts, 1, "RULES_ONE")
    dialog, mark = Dialog(relay, dialect), relay.mark()
    dialog.turn(tool_reply("c1", "opaque-one"))
    before = dialog.turn(tool_reply("c2", "opaque-two"))
    assert count(relay, mark, "native_history.reset") == 0

    publish(relay.prompts, 2, "RULES_TWO")
    assert relay.prompts.unpin(None) == 1  # the admin's "apply to all"
    switched = dialog.turn(tool_reply("c3", "opaque-three"))
    resumed = dialog.turn([text("done"), *END])

    assert head_of(switched) == head_of(resumed) == "RULES_TWO\n\n" + SYSTEM
    # the old signatures never ride on the new head; the new answer is signed on it and resumes on the next turn
    assert signatures(switched) == [] and signatures(resumed) == ["opaque-three"]
    assert switched["openAICacheKey"] == resumed["openAICacheKey"] != before["openAICacheKey"]
    assert count(relay, mark, "native_history.reset") == 1 and count(relay, mark, "cache.key_changed") == 1
    assert count(relay, mark, "native_history.resume") == 2
    assert [(plan.rev, plan.pinned) for plan in relay.prompts.plans] == [(1, False), (1, True), (2, False), (2, True)]


def test_unpinning_one_conversation_leaves_the_others_on_their_revision(prompt_relay):
    relay = fake_relay(prompt_relay)
    publish(relay.prompts, 1, "RULES_ONE")
    left, moved = Dialog(relay, "chat", "left"), Dialog(relay, "chat", "moved")
    left.turn([text("one"), *END])
    moved.turn([text("one"), *END])
    publish(relay.prompts, 2, "RULES_TWO")
    assert relay.prompts.unpin("header:moved") == 1
    assert head_of(left.turn([text("two"), *END])) == "RULES_ONE\n\n" + SYSTEM
    assert head_of(moved.turn([text("two"), *END])) == "RULES_TWO\n\n" + SYSTEM


@pytest.mark.parametrize("dialect", DIALECTS)
def test_body_edits_keep_the_signed_history_of_a_pinned_conversation_resuming(prompt_relay, monkeypatch, dialect):
    relay = fake_relay(prompt_relay)
    relay.prompts.publish(1, {"mine": profile(TEMPLATE, edits=True, texts={"rules": "RULES_ONE"})},
                          default_profile="mine")
    Rewrites(monkeypatch)
    dialog, mark = Dialog(relay, dialect), relay.mark()
    dialog.messages = [{"role": "user", "content": "inspect ORIGINAL_TASK" + MARK}]
    first = dialog.turn(tool_reply("c1", "opaque-one"))
    second = dialog.turn(tool_reply("c2", "opaque-two"))
    third = dialog.turn([text("done"), *END])

    # the edit is the same on every request, so fingerprints, scope and cache key do not move
    assert all(MARK not in str(body) for body in (first, second, third))
    assert signatures(second) == ["opaque-one"] and signatures(third) == ["opaque-one", "opaque-two"]
    assert len({body["openAICacheKey"] for body in (first, second, third)}) == 1
    for body in (first, second, third):
        assert body["rit"] == direct.make_rit(body["messages"], body["tools"])
    assert count(relay, mark, "native_history.resume") == 2 and count(relay, mark, "native_history.reset") == 0


def test_a_conversation_without_an_affinity_follows_the_current_revision_on_every_request(prompt_relay):
    relay = fake_relay(prompt_relay)
    publish(relay.prompts, 1, "RULES_ONE")
    request = client_request("chat", USER, system=SYSTEM, tools=False)
    assert relay.post("chat", request)[0] == 200
    publish(relay.prompts, 2, "RULES_TWO")
    assert relay.post("chat", request)[0] == 200
    assert [head_of(body) for body in relay.posts] == ["RULES_ONE\n\n" + SYSTEM, "RULES_TWO\n\n" + SYSTEM]
    assert relay.prompts.pins == {} and [plan.pinned for plan in relay.prompts.plans] == [False, False]


def drop_inline_system(data, dialect, profile_, snapshot):
    """A stand-in for prompt_body.rewrite with mid_system "drop": role:system entries after the opening are removed."""
    body = copy.deepcopy(data)
    kept, changes = [], []
    for index, message in enumerate(body["messages"]):
        if message.get("role") == "system" and kept:
            changes.append({"index": index, "op": "mid_system", "mode": "drop",
                            "removed_chars": len(json.dumps(message))})
            continue
        kept.append(message)
    body["messages"] = kept
    return body, changes


def test_the_desktop_runner_resumes_on_every_turn_when_a_profile_drops_its_reminders(prompt_relay, monkeypatch):
    relay = fake_relay(prompt_relay, allow_effort_downgrade=True)
    relay.prompts.publish(1, {"mine": profile(TEMPLATE, edits=True, texts={"rules": "RULES_ONE"})},
                          default_profile="mine")
    Rewrites(monkeypatch, drop_inline_system)
    mark = relay.mark()
    results = runner_turns(relay, CLAUDE_SDK_HEADERS)
    sent = [body for _, _, _, body in results]
    assert [status for status, _, _, _ in results] == [200, 200, 200]
    assert all("x-proxi-system-messages" not in headers for _, _, headers, _ in results)
    for body in sent:
        assert "Synthetic per-turn reminder" not in json.dumps(body["messages"])
        assert "Synthetic inline environment" not in json.dumps(body["messages"])
        assert body["rit"] == direct.make_rit(body["messages"], body["tools"])
    # the billing header stays the first block of a head that starts with the user's rules
    first = sent[0]["messages"][0]["content"]
    assert first[0]["text"].startswith("x-anthropic-billing-header:") and "cache" not in first[0]
    assert first[1]["text"].startswith("RULES_ONE\n\n") and first[1]["cache"] is True
    assert len({head_of(body) for body in sent}) == 1 and len({body["openAICacheKey"] for body in sent}) == 1
    assert signatures(sent[-1]) == ["opaque-0", "opaque-1"]
    assert count(relay, mark, "native_history.resume") == 2 and count(relay, mark, "native_history.reset") == 0
    assert [len(entry["history_changes"]) for entry in relay.prompts.records] == [1, 2, 3]
