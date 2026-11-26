"""Tool arguments streamed with Retool's enableFineGrainedStreaming assemble to what the wire carried, and a cut call stays cut.

Since 0d205e9 the relay asks Retool for fine-grained tool streaming on the Claude models whose registry entry has
supportsFineGrainedStreaming. Retool's own frontend pairs that mode with toolBlockFallBackParser, because the provider then
streams the arguments without validating them: a call cut at the output limit ends inside a value. Its fallback is the
parseParams of Retool's own tools; the relay has no such parser, and it must not take a cut call for a whole one (a Write
with half a file is worse than none). Checked against probe p05 of 2026-11-18 (Opus 5.5 medium, one write_file call,
5,861 characters in 333 pieces of up to 25 characters): the pieces below are its exact boundaries, its text rebuilt here."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import direct
from test_review_fixes_pr19 import END, Response
from upstream_attempts import AttemptBudget

# The lengths of p05's partialInputs pieces, in order (the first is the empty piece every call opens with).
P05_PIECES = [0, 19, 25, 15, 13, 12, 13, 15, 15, 16, 15, 16, 17, 15, 18, 14, 18, 15, 18, 18, 16, 17, 15, 18, 14, 18, 17,
              18, 18, 17, 19, 16, 20, 15, 19, 16, 18, 16, 19, 17, 15, 16, 17, 19, 16, 15, 16, 17, 19, 16, 15, 16, 16, 19,
              17, 15, 16, 17, 20, 18, 16, 16, 18, 20, 17, 16, 16, 18, 20, 18, 16, 16, 17, 20, 17, 16, 16, 18, 20, 18, 16,
              16, 17, 20, 18, 16, 16, 17, 20, 17, 16, 16, 18, 20, 18, 16, 16, 17, 20, 18, 16, 16, 17, 20, 18, 16, 16, 18,
              20, 17, 16, 16, 18, 20, 18, 16, 16, 18, 20, 17, 16, 16, 20, 17, 19, 17, 19, 18, 16, 20, 19, 15, 18, 19, 17,
              19, 17, 19, 19, 16, 20, 18, 11, 20, 19, 15, 18, 20, 17, 19, 18, 19, 19, 8, 17, 19, 18, 19, 19, 16, 20, 18,
              15, 18, 20, 17, 19, 17, 19, 19, 16, 20, 19, 15, 18, 19, 17, 19, 17, 19, 19, 16, 20, 19, 15, 18, 20, 17, 19,
              18, 19, 18, 16, 20, 18, 15, 18, 20, 17, 19, 18, 19, 18, 16, 20, 19, 15, 18, 20, 17, 19, 17, 19, 19, 16, 20,
              18, 15, 18, 20, 17, 19, 18, 19, 18, 16, 20, 19, 15, 18, 20, 17, 19, 17, 19, 18, 16, 20, 19, 15, 18, 20, 17,
              19, 18, 19, 19, 16, 20, 18, 15, 18, 19, 17, 19, 18, 19, 18, 16, 20, 18, 15, 18, 20, 17, 19, 18, 19, 19, 16,
              20, 19, 15, 18, 20, 17, 19, 17, 19, 19, 16, 20, 19, 16, 19, 20, 18, 20, 18, 20, 20, 14, 20, 17, 20, 18, 20,
              20, 17, 19, 20, 16, 18, 20, 18, 20, 18, 19, 20, 14, 20, 18, 20, 18, 20, 19, 17, 20, 19, 16, 19, 20, 18, 20,
              18, 20, 20, 17, 20, 20, 16, 7, 2]
WRITE = {"path": {"type": "string", "required": True}, "content": {"type": "string", "required": True}}


def _prime(n):
    return n > 1 and all(n % d for d in range(2, int(n ** 0.5) + 1))


CONTENT = "n,square,cube,is_prime\n" + "".join(
    "%d,%d,%d,%s\n" % (n, n * n, n ** 3, "true" if _prime(n) else "false") for n in range(1, 251))
WIRE = json.dumps({"path": "table.csv", "content": CONTENT})


def pieces():
    out, at = [], 0
    for length in P05_PIECES:
        out.append(WIRE[at:at + length])
        at += length
    assert at == len(WIRE) == 5861
    return out


def call_lines(parts, closed=True):
    lines = [{"event": "content_block_start", "type": "tool_call", "toolCallId": "toolu_p05", "toolName": "write_file"}]
    lines += [{"event": "content_block_delta", "type": "tool_call", "partialInputs": part} for part in parts]
    return lines + ([{"event": "content_block_end"}] if closed else [])


def drain(lines):
    async def run():
        queue = asyncio.Queue()
        for line in lines:
            queue.put_nowait(json.dumps(line))
        queue.put_nowait(None)
        flags = {}
        session = SimpleNamespace(_cancelled=False, thread_id="t")
        events = [event async for event in direct._drain({}, session, queue, [], [], {"write_file": WRITE}, flags)]
        return events, flags
    return asyncio.run(run())


def finals(events):
    return [event for event in events if event["type"] == "tool_call" and not event.get("partial")]


def test_the_fine_pieces_of_a_whole_call_give_the_arguments_the_wire_carried():
    events, flags = drain(call_lines(pieces()) + END)
    [final] = finals(events)
    assert final["arguments"] == WIRE and not final.get("invalid")
    assert all(final["arguments"].startswith(event["arguments"]) for event in events
               if event["type"] == "tool_call" and event.get("partial")), "every streamed piece continues the last"
    assert flags.get("whole_calls") == 1 and not flags.get("cut_calls")


@pytest.mark.parametrize("kept", range(2, len(P05_PIECES)))
def test_a_call_cut_after_any_piece_is_never_taken_for_a_whole_one(kept):
    # The output ended inside the arguments: Retool still closes the block and the stream, as at its output limit.
    events, flags = drain(call_lines(pieces()[:kept]) + END)
    [final] = finals(events)
    assert final.get("invalid") is True and flags.get("cut_calls") == 1 and not flags.get("whole_calls")
    assert WIRE.startswith(final["arguments"]), "the client keeps the text it was given, nothing invented"


def test_the_turn_of_a_cut_call_ends_as_cut_not_as_a_tool_call(tmp_path, monkeypatch):
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)
    script = call_lines(pieces()[:100]) + END
    monkeypatch.setattr(direct.urllib.request, "urlopen",
                        lambda request, **kw: Response(b"\n".join(json.dumps(e).encode() for e in script) + b"\n"))
    cfg = {"base": "https://fixture.invalid", "upstream_error_dir": str(tmp_path / "diag")}
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {}), _cancelled=False)

    async def run():
        body = {"model": "claude-opus-5-5", "tools": [{"name": "write_file", "params": WRITE}],
                "enableFineGrainedStreaming": True}
        return [event async for event in direct._stream_body(cfg, session, body, {"write_file": WRITE})]

    events = asyncio.run(run())
    assert [event.get("invalid") for event in finals(events)] == [True]
    assert events[-1] == {"type": "end", "reason": "length"}
