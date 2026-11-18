"""GPT's encrypted reasoning blocks on Retool are a real sign that the model reasons (A03-01, R1a R2).

GPT on Retool (gpt-6-sol, gpt-6.1-sol, gpt-6.1-terra, gpt-5.6-luna) streams no reasoning text. Each reasoning item comes
whole, as content_block_start/redacted_thinking {data, responseItemId}, one every 3.5-12 s, 5-10 of them before the first
word of the answer (13-70 s in the probes of 2026-11-18). _drain dropped them: no thinking status reached the client, the
stall watchdog had no thinking sign and cut the turn at the first-token budget (300 s) though Retool kept proving that the
model was at work, and an attempt that held only such blocks was classified `empty` instead of `thinking_only`."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import apierr
import direct
import journal
import stream_watchdog
from test_review_fixes_pr19 import END, text
from upstream_attempts import AttemptBudget

GPT = "gpt-6.1-sol"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)


def redacted(number):
    return {"event": "content_block_start", "type": "redacted_thinking", "data": "gAAAAA%d" % number + "x" * 64,
            "responseItemId": "rs_fixture_%d" % number}


class Scripted:
    """An HTTP 200 NDJSON body: dicts are lines, numbers are pauses of a live upstream that sends nothing meanwhile."""
    status = 200
    headers = {"request-id": "fixture"}

    def __init__(self, script):
        self.script, self.closed = script, False

    def __iter__(self):
        for item in self.script:
            if isinstance(item, (int, float)):
                end = time.monotonic() + item
                while time.monotonic() < end and not self.closed:
                    time.sleep(0.01)
            elif not self.closed:
                yield (json.dumps(item) + "\n").encode()

    def read(self, *_):
        return b""

    def close(self):
        self.closed = True


def body_stream(tmp_path, monkeypatch, script, model=GPT, **cfg_extra):
    cfg = {"base": "https://fixture.invalid", "upstream_error_dir": str(tmp_path / "diag"), **cfg_extra}
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda request, **kw: Scripted(script))
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {}), _cancelled=False, thread_id="t")
    return cfg, direct._stream_body(cfg, session, {"model": model})


def collect(agen):
    async def run():
        return [event async for event in agen]
    return asyncio.run(run())


def gpt_answer(blocks=3, gap=0.0):
    script = []
    for number in range(blocks):
        script += [gap, redacted(number), {"event": "content_block_end"}]
    return script + [{"event": "content_block_start", "type": "text"}, text("Answer."), {"event": "content_block_end"},
                     *END]


def test_the_first_encrypted_block_tells_the_client_once_that_the_model_reasons(tmp_path, monkeypatch):
    _, agen = body_stream(tmp_path, monkeypatch, gpt_answer(blocks=4))
    events = collect(agen)
    kinds = [event["type"] for event in events]
    assert kinds.index("thinking_status") < kinds.index("text")
    assert [event for event in events if event["type"] == "thinking_status"] == [
        {"type": "thinking_status", "source": "upstream"}], "one status for the run of blocks, from upstream"
    # No text of any kind is made up for it: the blocks are encrypted and stay in the history alone.
    assert not [event for event in events if event["type"] == "thinking"]
    assert [event["delta"] for event in events if event["type"] == "text"] == ["Answer."]
    assert events[-1] == {"type": "end", "reason": "stop"}


def test_reasoning_that_comes_back_after_a_call_is_told_again(tmp_path, monkeypatch):
    call = [{"event": "content_block_start", "type": "tool_call", "toolCallId": "call_1", "toolName": "read_file"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"path": "a"}'},
            {"event": "content_block_end"}]
    script = [redacted(0), {"event": "content_block_end"}, *call, redacted(1), {"event": "content_block_end"},
              redacted(2), {"event": "content_block_end"}, text("done"), *END]
    _, agen = body_stream(tmp_path, monkeypatch, script)
    events = collect(agen)
    assert sum(event["type"] == "thinking_status" for event in events) == 2


def test_the_watchdog_waits_for_a_model_whose_reasoning_blocks_keep_coming(tmp_path, monkeypatch):
    # Scaled down: the first-token budget is 0.5 s (300 s live), the blocks come 0.3 s apart (3.5-12 s live), and the first
    # word comes after 1.2 s of them (13-70 s live).
    cfg, agen = body_stream(tmp_path, monkeypatch, gpt_answer(blocks=4, gap=0.3), stream_first_token_timeout=0.5,
                            stream_idle_timeout=0.5, stream_thinking_timeout=5)
    events = collect(stream_watchdog.guard(agen, cfg))
    assert [event["delta"] for event in events if event["type"] == "text"] == ["Answer."]
    assert events[-1] == {"type": "end", "reason": "stop"}


def test_the_watchdog_still_cuts_a_stream_that_sends_nothing(tmp_path, monkeypatch):
    cfg, agen = body_stream(tmp_path, monkeypatch, [1.0, *gpt_answer(blocks=1)], stream_first_token_timeout=0.3,
                            stream_idle_timeout=0.3, stream_thinking_timeout=5)
    with pytest.raises(apierr.ApiError) as caught:
        collect(stream_watchdog.guard(agen, cfg))
    assert caught.value.status == 504 and "first-token" in caught.value.message


def test_an_attempt_of_encrypted_reasoning_alone_is_thinking_only_not_empty(tmp_path, monkeypatch):
    seq = journal.JOURNAL.last_seq
    _, agen = body_stream(tmp_path, monkeypatch, [redacted(0), {"event": "content_block_end"}, redacted(1),
                                                  {"event": "content_block_end"}, *END], upstream_empty_retries=0)
    collect(agen)
    outcomes = [event["data"]["outcome"] for event in journal.JOURNAL.query(since=seq, limit=1000)
                if event["kind"] == "upstream.attempt"]
    assert outcomes == ["thinking_only"]
