"""A call of a tool the client did not declare is named in the journal instead of disappearing without a trace.

Only declared tools can reach the client, so such a call is not passed on; before, nothing recorded that it had been
made, and a turn that seemed to stop for no reason could not be explained (ultracode audit of the relay, C4)."""
import asyncio
import copy
import json

import journal
from test_conversation_state import stored
from test_native_context_reuse import FIRST, request, run
from test_native_history import TOOLS, close, harness
from test_review_fixes_pr19 import END, text


def call(call_id, name, arguments):
    return [{"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": name},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps(arguments)},
            {"event": "content_block_end"}]


def test_an_undeclared_call_is_journaled_and_the_declared_one_still_reaches_the_client(tmp_path, monkeypatch):
    script = [*call("c1", "list_dir", {"path": "."}), *call("c2", "read_file", {"path": "a"}), *END]
    pool, runner, sent = harness(tmp_path, monkeypatch, [script, [text("done"), *END]])
    seq = journal.JOURNAL.last_seq

    async def scenario():
        try:
            return await run(runner, request(copy.deepcopy(FIRST), tools=TOOLS))
        finally:
            await close(pool)

    events = asyncio.run(scenario())
    assert {e["name"] for e in events if e["type"] == "tool_call"} == {"read_file"}
    dropped = stored(seq, "tool.call_dropped")
    assert [(e["data"]["name"], e["data"]["call_id"], e["data"]["declared"]) for e in dropped] == [("list_dir", "c1", 1)]
