"""Retool's redacted_retool_contents block is kept in the signed branch like redacted_thinking, with its mandatory id.

Retool's stream schema (public frontend 4.99.0, RedactedRetoolContentsContentBlockSchema) has a third block of model state
beside thinking and redacted_thinking: redacted_retool_contents, {data, responseItemId}, both required. Its frontend keeps it
and sends it back in order with the other blocks (isModelStateBlock). The relay knew no such block: ReplyCapture marked the
whole reply invalid, the signed branch of that turn was not kept (native_history.unavailable), the next request went cold
without any signature, and a stored conversation could never hold it (conversation_state._REPLY_BLOCKS). Accepting it must
not change conversation_state.STAMP: a new stamp drops every stored conversation as `format`, and the next turn of each
writes its whole prefix again."""
import asyncio
import copy
import json
import shutil

import conversation_state as cs
import journal
import native_history
from test_conversation_state import DONE, KEY, processes, signature, state_file, stored  # noqa: F401 - processes is a fixture
from test_native_context_reuse import FIRST
from test_native_history import append_result, parsed
from test_review_fixes_pr19 import END, THINKING
from test_review_fixes_pr19 import signature as signature_event

BLOCK = {"type": "redacted_retool_contents", "data": "RRC-opaque==", "responseItemId": "rrc_item_1"}
# The stamp of every file the relay has written since conversation_state existed (its header field "blocks").
STAMP_OF_STORED_FILES = "0f679f42d8ef"


def start(block):
    return {"event": "content_block_start", **block}


def model_state_reply(number):
    """A tool step whose reply carries Retool's opaque state block between the reasoning and the call."""
    return [*THINKING, signature_event(signature(number)), {"event": "content_block_end"},
            start({"type": "redacted_retool_contents", "data": "RRC-%d==" % number, "responseItemId": "rrc_%d" % number}),
            {"event": "content_block_end"},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": "call-%d" % number,
             "toolName": "read_file"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps({"path": "f-%d" % number})},
            {"event": "content_block_end"}, *END]


def blocks_of(body, kind):
    return [block for message in body["messages"] if isinstance(message.get("content"), list)
            for block in message["content"] if isinstance(block, dict) and block.get("type") == kind]


# -- what one reply keeps ----------------------------------------------------------------------------------------------
def test_the_capture_keeps_the_block_with_its_id_in_its_place():
    capture = native_history.ReplyCapture()
    for event in [start(BLOCK), {"event": "content_block_end"}, start({"type": "text"}),
                  {"event": "content_block_delta", "type": "text", "partialText": "answer"}]:
        capture.feed(event)
    assert capture.finish() == [BLOCK, {"type": "text", "text": "answer"}]


def test_a_block_without_its_id_is_no_block_retool_takes_back():
    for broken in ({"type": "redacted_retool_contents", "data": "x"},
                   {"type": "redacted_retool_contents", "data": "x", "responseItemId": 7},
                   {"type": "redacted_retool_contents", "responseItemId": "rrc"}):
        capture = native_history.ReplyCapture()
        capture.feed(start(broken))
        assert capture.finish() is None, broken


def test_a_cut_reply_resumes_with_the_block_as_it_does_with_its_thinking():
    thought = {"type": "thinking", "thinking": "", "signature": "sig-1"}
    full = "Progress report. " * 60
    history = native_history.History()
    client = [{"role": "user", "content": [{"type": "text", "text": "task"}]}]
    blocks = [thought, BLOCK, {"type": "text", "text": full}]
    history.record("scope", client, [{"role": "user", "content": [{"type": "text", "text": "upstream task"}]}], blocks,
                   native_history.canonical_assistant([blocks[2]]))
    cut = native_history.canonical_assistant([{"type": "text", "text": full[:300]}])
    found, messages = history.resume("scope", client + [cut, {"role": "user", "content": [{"type": "text", "text": "go"}]}])
    assert found is not None
    assert messages[-2]["content"] == [thought, BLOCK, {"type": "text", "text": full[:300]}]


# -- what a stored conversation may hold ------------------------------------------------------------------------------
def test_the_stamp_of_the_stored_files_stays_when_the_block_is_accepted():
    assert cs.STAMP == STAMP_OF_STORED_FILES, "a new stamp drops every stored conversation as `format`"
    assert cs._reply_block(dict(BLOCK)) is True
    assert cs._reply_block({"type": "redacted_retool_contents", "data": "x"}) is False


def test_the_turn_after_it_continues_the_signed_branch_and_returns_the_block(tmp_path, processes):
    relay = processes(tmp_path / "state", scripts=[model_state_reply(0), DONE])
    seq = journal.JOURNAL.last_seq

    async def story():
        messages = copy.deepcopy(FIRST)
        asked = parsed(messages)
        events = await relay.turn(asked)
        assert not any(event["type"] == "error" for event in events), events
        await relay.turn(parsed(append_result(messages, asked, events, "chat")))

    asyncio.run(story())
    second = relay.sent[1].body
    assert blocks_of(second, "redacted_retool_contents") == [
        {"type": "redacted_retool_contents", "data": "RRC-0==", "responseItemId": "rrc_0"}]
    assert [block["signature"] for block in blocks_of(second, "thinking")] == [signature(0)]
    assert not stored(seq, "native_history.unavailable", "native_history.cold", "native_history.reset")
    assert len(stored(seq, "native_history.resume")) == 1


def test_a_stored_conversation_with_the_block_comes_back_after_a_restart(tmp_path, processes):
    state, copied = tmp_path / "state", tmp_path / "state-at-restart"
    before = processes(state, scripts=[model_state_reply(0), model_state_reply(1), DONE])

    async def never_stopped():
        messages = copy.deepcopy(FIRST)
        for _ in range(2):
            asked = parsed(messages)
            events = await before.turn(asked)
            assert not any(event["type"] == "error" for event in events), events
            messages = append_result(messages, asked, events, "chat")
        shutil.copytree(state, copied)
        await before.turn(parsed(messages))
        return messages

    messages = asyncio.run(never_stopped())
    seq = journal.JOURNAL.last_seq
    after = processes(copied, scripts=[DONE])
    asyncio.run(after.turn(parsed(messages)))
    assert after.sent[0].data == before.sent[-1].data
    assert len(blocks_of(after.sent[0].body, "redacted_retool_contents")) == 2
    assert len(stored(seq, "conversation_state.restored")) == 1 and not stored(seq, "conversation_state.dropped")
    assert cs.decode(state_file(copied, KEY).read_bytes(), 10 ** 9)[0]["blocks"] == STAMP_OF_STORED_FILES
