"""The end of an attempt never pushes a line of its answer out of a full queue (A03-02).

The reader thread of an attempt hands each NDJSON line to the consumer through a queue of 1,024 lines and then its last
items: the error, if the read failed, and None for the end. Those went in without waiting: when the consumer lagged by a
full queue (a busy loop, a slow client chain), each took the place of the OLDEST line, and the turn ended as a clean
`stop` with a text delta, a piece of a call's arguments or the metadata missing and nothing said about it (repro on
1beaeed: 1,022 of 1,023 text deltas delivered, end reason stop, no error)."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

import apierr
import direct
from test_review_fixes_pr19 import END, Response, text
from upstream_attempts import AttemptBudget


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)


class CutAfter(Response):
    """An HTTP 200 whose lines are read in full and whose connection is then reset."""

    def __iter__(self):
        while line := self.readline():
            yield line
        raise ConnectionResetError(10054, "reset by peer")


def lagging_consumer(tmp_path, monkeypatch, payload):
    """Run _stream_body; the consumer takes usage_start and the first delta, then waits until the reader has handed over
    every line it read (the queue is full) and a little more, then reads the rest. (events, the error it ended with)."""
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda request, **kw: payload)
    reader_done = threading.Event()
    real_close = direct._force_close_response

    def close(resp):
        real_close(resp)
        if resp is payload:
            reader_done.set()  # the reader's own `finally`: every line is in the queue, its last items come next

    monkeypatch.setattr(direct, "_force_close_response", close)
    cfg = {"base": "https://fixture.invalid", "upstream_error_dir": str(tmp_path / "diag"), "upstream_empty_retries": 0}
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {}), _cancelled=False)

    async def run():
        agen = direct._stream_body(cfg, session, {"model": "claude-opus-5-5"})
        events = [await anext(agen)]
        while events[-1]["type"] != "text":
            events.append(await anext(agen))
        assert await asyncio.to_thread(reader_done.wait, 20), "the reader never got through the answer"
        await asyncio.sleep(0.3)
        try:
            async for event in agen:
                events.append(event)
        except apierr.ApiError as error:
            return events, error
        return events, None

    return asyncio.run(run())


def lines_of(deltas, tail):
    return b"".join(json.dumps(event).encode() + b"\n" for event in [text("t%d " % n) for n in range(deltas)] + tail)


def test_a_consumer_that_lags_by_a_full_queue_still_gets_every_line(tmp_path, monkeypatch):
    deltas = 1023  # with the metadata and the end, one line more than the queue holds
    events, error = lagging_consumer(tmp_path, monkeypatch, Response(lines_of(deltas, END)))
    expected = ["t%d " % number for number in range(deltas)]
    assert error is None
    assert [event["delta"] for event in events if event["type"] == "text"] == expected
    assert [event["text"] for event in events if event["type"] == "final_text"] == ["".join(expected)]
    assert any(event["type"] == "credits" for event in events), "the metadata is a line too"
    assert events[-1] == {"type": "end", "reason": "stop"} and not any(event["type"] == "error" for event in events)


def test_a_connection_lost_after_a_full_queue_gives_every_line_before_its_error(tmp_path, monkeypatch):
    deltas = 1025  # the queue is full when the reset comes: the error and the end used to push out two lines
    events, error = lagging_consumer(tmp_path, monkeypatch, CutAfter(lines_of(deltas, [])))
    assert [event["delta"] for event in events if event["type"] == "text"] == ["t%d " % n for n in range(deltas)]
    assert error is not None and error.code == "upstream_error" and "ConnectionResetError" in error.message
