"""A stored block whose type is not even a name is a damaged file, handled like any other damaged file.

_reply_block looked the type up in a dict, and a type that cannot be a key (a list, an object) raised TypeError out of
decode() instead of StateError("corrupt"): the file was not removed, the journal said restore_failed (TypeError) and
the same file was read, and failed, at every request until a turn wrote a new one over it (ultracode audit of the relay;
bug summary of 2026-11-06, section 3, item 15). A fuzz of every field of a stored branch with junk JSON values found no
other way out of decode() but StateError."""
import asyncio
import json
import zlib

import pytest

import conversation_state as cs
import journal
from test_conversation_state import DONE, converse, parsed, processes, split, state_file, stored, tool_scripts  # noqa: F401


@pytest.mark.parametrize("junk", [[], {}, [[]], {"a": []}])
def test_a_block_whose_type_cannot_be_a_name_is_no_reply_block(junk):
    assert cs._reply_block({"type": junk}) is False
    assert cs._reply_block({"type": None}) is False
    assert cs._reply_block({"type": "text", "text": "ok"}) is True


def block_of(branch, where):
    if where == "assistant":
        return branch["assistant"][0]
    if where == "echo":
        return branch["echo"]["content"][0]
    if where == "client":
        return next(block for message in branch["client"] if message["role"] == "assistant"
                    for block in message["content"])
    return next(block for message in branch["messages"] if message["role"] == "assistant"
                for block in message["content"])


@pytest.mark.parametrize("junk", [[], {"a": 1}])
@pytest.mark.parametrize("where", ["assistant", "echo", "client", "messages"])
def test_a_stored_file_with_such_a_block_is_dropped_as_corrupt_and_the_conversation_goes_on(
        tmp_path, processes, where, junk):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    path = state_file(state)
    head, payload = split(path.read_bytes())
    data = json.loads(zlib.decompress(payload))
    block_of(data["branches"][-1], where)["type"] = junk
    path.write_bytes(head + zlib.compress(json.dumps(data).encode()))
    seq = journal.JOURNAL.last_seq
    after = processes(state, scripts=[DONE])
    events = asyncio.run(after.turn(parsed(messages)))
    assert events[-1] == {"type": "end", "reason": "stop"} and not any(e["type"] == "error" for e in events)
    assert [e["data"]["reason"] for e in stored(seq, "conversation_state.dropped")] == ["corrupt"]
    assert not stored(seq, "conversation_state.restore_failed") and not stored(seq, "conversation_state.restored")
    assert "SIG-" not in json.dumps(after.sent[0].body)
    cs.decode(path.read_bytes(), 10 ** 9)  # the turn that followed wrote a good state under the same name
