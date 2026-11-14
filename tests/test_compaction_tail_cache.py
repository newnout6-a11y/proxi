"""A client's compaction does not write its own instruction to the cache (item 17, a3: OpenCode's compaction request).

OpenCode's compaction is a one-shot: the client replaces its history by the summary and never sends this request's tail again.
The relay put the newest cache hint at the end of it all the same: 98,956 tokens written to a cache nobody read (10.87 credits,
866b7de6, a3 2026-11-06), then 33,308 rewritten for the first normal request. The hint on the shared prefix stays: it is what
lets a compaction that shares the chat's system, tools and history read them."""
import json

import pytest

import direct
import native_history
from prompt_support import client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_compaction_cache import Chat, OPENCODE, SUMMARY, with_instruction
from test_native_history import tool_reply

OC_COMPACT = "You are an anchored context summarization assistant for coding sessions."
CONVERSATION = "User: fix the parser\n\nAssistant: I read src/parser.py and changed it.\n\nUser: now run the tests " * 40


def flags(body):
    """One string per message of a body posted to Retool: F for a block with a cache hint, - for one without.

    A tool result is one message: F when a text block of its result carries the hint (item 16)."""
    rows = []
    for message in body["messages"]:
        content = message["content"]
        if isinstance(content, dict):
            result = content.get("toolResult")
            rows.append("F" if isinstance(result, list) and any(isinstance(block, dict) and block.get("cache")
                                                                for block in result) else "-")
        else:
            rows.append("".join("F" if isinstance(block, dict) and block.get("cache") else "-" for block in content)
                        if isinstance(content, list) else "-")
    return rows


# --- the text path: OpenCode 1.x flattens the whole conversation into one message ---------------------------------

def flattened(system, relay, content=CONVERSATION):
    request_ = client_request("chat", [{"role": "user", "content": content}], system=system, tools=False,
                              model="claude-sonnet-5-5")
    status, payload, _ = relay.post("chat", request_, OPENCODE)
    assert status == 200, payload
    return relay.posts[-1]


def test_a_flattened_compaction_has_no_hint_on_the_conversation_it_flattens(prompt_relay):
    relay = prompt_relay()
    body = flattened(OC_COMPACT, relay)
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert flags(body) == ["F", "-"]  # the system prompt is cached, the whole conversation is not written to the cache


def test_a_flattened_compaction_sent_as_two_messages_writes_neither(prompt_relay):
    """OpenCode 2.2.6 sends the flattened dialog and the instruction as two user messages: the dialog was hinted and written
    (2,281 and 3,075 tokens, v3 run of 2026-11-07, 3d0b8da709e7, 6a61f23eef9b), though the summary replaces it."""
    relay = prompt_relay()
    request_ = client_request("chat", [{"role": "user", "content": CONVERSATION},
                                       {"role": "user", "content": "Summarize the conversation above."}],
                              system=OC_COMPACT, tools=False, model="claude-sonnet-5-5")
    status, payload, _ = relay.post("chat", request_, OPENCODE)
    assert status == 200, payload
    assert "F" not in "".join(flags(relay.posts[-1])[1:])


def test_the_same_request_of_another_purpose_is_cached_at_its_end_as_before(prompt_relay):
    relay = prompt_relay()
    body = flattened("You are a helpful assistant.", relay)
    assert flags(body) == ["F", "F"]


# --- the native path: a compaction that continues the chat's signed history --------------------------------------

def test_a_compaction_that_resumes_the_dialog_leaves_its_instruction_unmarked_and_keeps_the_prefix_hint(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay, effort="max")
    chat.work(2)
    summary = chat.compact(effort="max")  # the dialog's own effort: the signed history resumes
    rows = flags(summary)
    assert rows[-1] == "-" * len(summary["messages"][-1]["content"])  # the instruction
    assert rows[0] == "F" and any("F" in row for row in rows[1:-1])  # the head and the chat's own prefix
    # an ordinary message at the end of the same history is where the newest hint goes
    ordinary = chat.ask(SUMMARY, effort="max", messages=with_instruction("chat", chat.messages, "Now say hello."))
    assert "F" in flags(ordinary)[-1]


def test_a_compaction_of_a_history_that_cannot_resume_is_marked_up_to_what_precedes_the_instruction(prompt_relay):
    relay = prompt_relay(compaction_keep_effort=False)
    chat = Chat(relay, effort="max")
    chat.work(2)
    summary = chat.compact(effort="high")  # another effort: the history restarts, cold
    rows = flags(summary)
    assert rows[0] == "F"
    assert "F" not in rows[-1]
    # Nothing of a history the chat had not cached under this prefix is written: the client replaces it by the summary.
    assert not any("F" in row for row in rows[1:-1])


# --- the two builders ----------------------------------------------------------------------------------------------

CHUNKS = ["User: u1", "Assistant: a1", "User: u2", "User: the summary instruction"]


def test_build_body_leaves_the_last_chunk_unmarked_when_asked():
    cfg = {"direct_cache": True}
    marked = direct.build_body(cfg, {"model": "m"}, "head", "", None, None, 100, chunks=CHUNKS)
    unmarked = direct.build_body(cfg, {"model": "m"}, "head", "", None, None, 100, chunks=CHUNKS, mark_tail=False)
    # the head, then the chunks: the default is what it was, and the instruction is the only chunk that changes
    assert flags(marked) == ["F", "F", "-", "F", "F"]
    # A compaction without a hint of the chat to read writes none of the chunks: the summary replaces them.
    assert flags(unmarked) == ["F", "-", "-", "-", "-"]
    only = direct.build_body(cfg, {"model": "m"}, "head", "", None, None, 100, chunks=["User: one flattened text"],
                             mark_tail=False)
    assert flags(only) == ["F", "-"]


def test_build_body_still_hints_a_last_chunk_that_is_not_the_tail():
    # an image-only last chunk cannot carry a hint: the newest text chunk before it gets it in an ordinary request
    image = [{"type": "image", "imageData": "AAAA", "mimeType": "image/png"}]
    body = direct.build_body({"direct_cache": True}, {"model": "m"}, "head", "", None, None, 100,
                             chunks=["User: u1", "Assistant: a1", image])
    assert "F" in flags(body)[2]  # the last chunk with text, the assistant's message
    compaction = direct.build_body({"direct_cache": True}, {"model": "m"}, "head", "", None, None, 100,
                                   chunks=["User: u1", "Assistant: a1", image], mark_tail=False)
    assert flags(compaction)[1:] == ["-", "-", "-"]  # a compaction with no hint of the chat to read writes nothing new


def native(extra_flag=None):
    messages = [{"role": "system", "content": [{"type": "text", "text": "head", "cache": True}]},
                {"role": "user", "content": [{"type": "text", "text": "task"}]},
                {"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "c1", "toolName": "read",
                                                   "toolInput": {}, "cache": True}]},
                {"role": "tool", "content": {"toolCallId": "c1", "toolResult": "result"}},
                {"role": "user", "content": [{"type": "text", "text": "summarize"}]}]
    return messages


def test_cache_messages_leaves_the_last_message_unmarked_when_asked():
    messages = native()
    marked = native_history.cache_messages(messages, True, [{"name": "read"}])
    assert flags({"messages": marked}) == ["F", "-", "F", "-", "F"]
    unmarked = native_history.cache_messages(messages, True, [{"name": "read"}], mark_tail=False)
    assert flags({"messages": unmarked}) == ["F", "-", "F", "-", "-"]
    # nothing else moves, and the input is not changed
    assert json.dumps([m["content"] for m in messages], sort_keys=True) == json.dumps(
        [m["content"] for m in native()], sort_keys=True)


def test_cache_messages_of_a_request_that_is_only_its_tail_marks_nothing_there():
    only = [{"role": "user", "content": [{"type": "text", "text": "one flattened conversation"}]}]
    assert flags({"messages": native_history.cache_messages(only, True, None)}) == ["F"]
    assert flags({"messages": native_history.cache_messages(only, True, None, mark_tail=False)}) == ["-"]
