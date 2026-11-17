"""A client that sends no conversation identity (DeepSeek Harness) still continues its signed history.

Harness test run of 2026-11-06 (a3, 13 turns, GLM and Claude models): the relay journaled `native_history.cold` on 488 of
490 requests, one throwaway session per request, because nothing in a request names its conversation. A request whose
history carries the whole history of one the relay has seen is that conversation's next one, and now takes its key."""
import copy
import hashlib

import pytest

import conversation_infer
import journal
from conversation_infer import ConversationIndex
from prompt_support import after_tool_call, client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_conversation_state import signatures_in, stored
from test_native_history import tool_reply
from test_review_fixes_pr19 import END, text

HARNESS = {"user-agent": "deepseek-harness/0.2.2 (+https://github.com/deepseek-ai/deepseek-harness)"}


def digests(*messages):
    return tuple(hashlib.sha256(message.encode()).digest() for message in messages)


# -- the index ---------------------------------------------------------------------------------------------------

def test_a_history_that_extends_a_seen_one_takes_its_key_and_an_unrelated_one_does_not():
    index = ConversationIndex()
    first, how = index.identify("m", "SYSTEM", digests("task"))
    assert how == "new" and first.startswith("inferred:")
    second, how = index.identify("m", "SYSTEM", digests("task", "reply", "result"))
    assert (second, how) == (first, "extended")
    third, how = index.identify("m", "SYSTEM", digests("task", "reply", "result", "reply 2", "result 2"))
    assert (third, how) == (first, "extended")
    other, how = index.identify("m", "SYSTEM", digests("another task"))
    assert how == "new" and other != first


def test_a_retry_of_a_request_is_its_conversation_and_adds_nothing():
    index = ConversationIndex()
    key, _ = index.identify("m", "S", digests("a", "b", "c"))
    assert index.identify("m", "S", digests("a", "b", "c")) == (key, "identical")
    assert len(index) == 1


@pytest.mark.parametrize("model,system", [("other-model", "S"), ("m", "another system prompt")])
def test_the_same_history_under_another_model_or_system_prompt_is_another_conversation(model, system):
    index = ConversationIndex()
    key, _ = index.identify("m", "S", digests("task"))
    other, how = index.identify(model, system, digests("task", "reply", "result"))
    assert how == "new" and other != key


def test_a_history_whose_earlier_message_changed_starts_a_conversation_of_its_own():
    index = ConversationIndex()
    key, _ = index.identify("m", "S", digests("task", "reply", "result"))
    pruned, how = index.identify("m", "S", digests("task", "reply", "PRUNED result", "reply 2", "result 2"))
    assert how == "new" and pruned != key


def test_two_continuations_of_one_history_share_the_key_of_that_history():
    # a fork: the same prefix and then two different ones; each carries the whole of the first request's history
    index = ConversationIndex()
    root, _ = index.identify("m", "S", digests("task"))
    one, _ = index.identify("m", "S", digests("task", "reply", "result one"))
    two, how = index.identify("m", "S", digests("task", "reply", "result two"))
    assert one == two == root and how == "extended"


def test_the_longest_seen_history_decides():
    index = ConversationIndex()
    index.identify("m", "S", digests("a"))
    index.identify("m", "S", digests("a", "b", "c"))
    assert index.identify("m", "S", digests("a", "b", "c", "d"))[1] == "extended"
    assert len(index) == 3


def test_old_and_surplus_histories_are_forgotten():
    now = [1000.0]
    index = ConversationIndex(ttl=60, max_entries=3, clock=lambda: now[0])
    key, _ = index.identify("m", "S", digests("a"))
    now[0] += 61
    assert index.identify("m", "S", digests("a", "b"))[0] != key  # expired: no longer a known history
    for number in range(5):
        index.identify("m", "S", digests("task %d" % number))
    assert len(index) <= 3


def test_the_default_limits_are_bounded():
    assert conversation_infer.MAX_ENTRIES >= 1000 and 3600 <= conversation_infer.TTL <= 7 * 24 * 3600


# -- the request path --------------------------------------------------------------------------------------------

USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]


def keys(relay, since):
    return [event["conversation"] for event in relay.events(since, "turn.begin")]


def test_the_requests_of_one_headerless_conversation_share_one_session_and_native_history_resumes(prompt_relay):
    relay = prompt_relay()
    mark, messages, sent = relay.mark(), copy.deepcopy(USER), []
    for number, script in enumerate((tool_reply("c1", "opaque-one"), tool_reply("c2", "opaque-two"), [text("done"), *END])):
        relay.scripts.append(script)
        status, payload, _ = relay.post("chat", client_request("chat", messages), HARNESS)
        assert status == 200, payload
        sent.append(relay.posts[-1])
        if number < 2:
            messages = after_tool_call("chat", messages, payload)
    tags = keys(relay, mark)
    assert len(tags) == 3 and len(set(tags)) == 1 and tags[0]
    resumed = relay.events(mark, "native_history.resume")
    assert len(resumed) == 2 and not relay.events(mark, "native_history.cold")
    assert signatures_in(sent[1]) == ["opaque-one"] and signatures_in(sent[2]) == ["opaque-one", "opaque-two"]
    assert len(stored(mark, "conversation.inferred")) == 1


def test_two_different_headerless_conversations_never_share_a_session_or_a_signature(prompt_relay):
    relay = prompt_relay()
    mark = relay.mark()
    first, second = copy.deepcopy(USER), [{"role": "user", "content": "a different task entirely"}]
    heads = []
    for task, signature in ((first, "opaque-A"), (second, "opaque-B")):
        relay.scripts.append(tool_reply("call-" + signature, signature))
        status, payload, _ = relay.post("chat", client_request("chat", task), HARNESS)
        assert status == 200, payload
        heads.append(payload)
    first, second = after_tool_call("chat", first, heads[0]), after_tool_call("chat", second, heads[1])
    sent = []
    for task in (first, second):
        relay.scripts.append([text("done"), *END])
        assert relay.post("chat", client_request("chat", task), HARNESS)[0] == 200
        sent.append(relay.posts[-1])
    assert signatures_in(sent[0]) == ["opaque-A"] and signatures_in(sent[1]) == ["opaque-B"]
    tags = keys(relay, mark)
    assert tags[0] == tags[2] and tags[1] == tags[3] and tags[0] != tags[1]


def test_a_client_that_sends_an_identity_keeps_it_and_nothing_is_inferred(prompt_relay):
    relay = prompt_relay()
    mark = relay.mark()
    relay.scripts.append([text("ok"), *END])
    assert relay.post("chat", client_request("chat", USER), {**HARNESS, "x-session-affinity": "dialog-1"})[0] == 200
    assert not stored(mark, "conversation.inferred") and len(relay.runtime.conversations) == 0


def test_a_request_without_tools_is_not_given_a_conversation(prompt_relay):
    relay = prompt_relay()
    mark = relay.mark()
    relay.scripts.append([text("title"), *END])
    assert relay.post("chat", client_request("chat", USER, tools=False), HARNESS)[0] == 200
    assert not stored(mark, "conversation.inferred") and len(relay.runtime.conversations) == 0


def test_inference_can_be_switched_off(prompt_relay):
    relay = prompt_relay(infer_conversations=False)
    mark = relay.mark()
    relay.scripts.append(tool_reply("c1", "opaque-one"))
    status, payload, _ = relay.post("chat", client_request("chat", USER), HARNESS)
    assert status == 200
    relay.scripts.append([text("done"), *END])
    assert relay.post("chat", client_request("chat", after_tool_call("chat", copy.deepcopy(USER), payload)), HARNESS)[0] == 200
    assert not relay.events(mark, "native_history.resume") and not stored(mark, "conversation.inferred")
    assert journal.JOURNAL is not None


@pytest.mark.parametrize("dialect", ["anthropic", "responses"])
def test_the_other_dialects_infer_the_same_way(prompt_relay, dialect):
    relay = prompt_relay()
    mark, messages = relay.mark(), copy.deepcopy(USER)
    relay.scripts.append(tool_reply("c1", "opaque-one"))
    status, payload, _ = relay.post(dialect, client_request(dialect, messages), {"user-agent": "headerless-agent/1"})
    assert status == 200, payload
    relay.scripts.append([text("done"), *END])
    messages = after_tool_call(dialect, messages, payload)
    assert relay.post(dialect, client_request(dialect, messages), {"user-agent": "headerless-agent/1"})[0] == 200
    assert len(relay.events(mark, "native_history.resume")) == 1
    assert signatures_in(relay.posts[-1]) == ["opaque-one"]


def test_after_a_restart_a_conversation_kept_on_disk_is_found_by_its_history():
    first = ConversationIndex()
    key, how = first.identify("m", "sys", digests("hi"))
    assert how == "new"
    stored = {key}  # the conversation's state was saved under its key, then the relay restarted: a new, empty index
    second = ConversationIndex()
    found, how = second.identify("m", "sys", digests("hi", "answer", "more"), stored.__contains__)
    assert (found, how) == (key, "stored")
    # and the next request of it continues under that key as before
    assert second.identify("m", "sys", digests("hi", "answer", "more", "again", "next"))[0] == key


def test_the_longest_stored_prefix_wins_and_nothing_stored_is_a_new_conversation():
    index = ConversationIndex()
    short = index.identify("m", "sys", digests("a"))[0]
    longer = ConversationIndex().identify("m", "sys", digests("a", "b", "c"))[0]
    found, how = ConversationIndex().identify("m", "sys", digests("a", "b", "c", "d", "e"), {short, longer}.__contains__)
    assert (found, how) == (longer, "stored")
    alone, how = ConversationIndex().identify("m", "sys", digests("x", "y"), set().__contains__)
    assert how == "new" and alone not in (short, longer)
    # another system prompt never adopts a stored conversation of this one
    other, how = ConversationIndex().identify("m", "other", digests("a", "b", "c", "d"), {short, longer}.__contains__)
    assert how == "new"


def test_a_headerless_conversation_resumes_its_signed_history_after_a_relay_restart(prompt_relay, tmp_path):
    state = str(tmp_path / "state")
    first = prompt_relay(conversation_state_dir=state)
    messages = copy.deepcopy(USER)
    for script in (tool_reply("c1", "opaque-one"), tool_reply("c2", "opaque-two")):
        first.scripts.append(script)
        status, payload, _ = first.post("chat", client_request("chat", messages), HARNESS)
        assert status == 200, payload
        messages = after_tool_call("chat", messages, payload)
    first.runtime.close()  # a restart: the index of headerless conversations is in memory, the signed history on disk
    second = prompt_relay(conversation_state_dir=state)
    mark = second.mark()
    second.scripts.append([text("done"), *END])
    assert second.post("chat", client_request("chat", messages), HARNESS)[0] == 200
    assert signatures_in(second.posts[-1]) == ["opaque-one", "opaque-two"]  # it went cold, with no signature, before
    assert [event["data"].get("how") for event in stored(mark, "conversation.inferred")] == ["stored"]
    assert not second.events(mark, "native_history.cold")
