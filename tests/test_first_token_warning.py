"""The first-token warning waits as long as the effort that was sent lets the model think before it answers.

A model reasons before its first token, and a reasoning that is not streamed is silence. One fixed 8 s made a warning of 65%
of the xhigh turns and 39% of the max turns of the live journal; the live QA run saw "Первый токен через 8609 мс" as warn
on a normal GPT xhigh turn."""
import asyncio
import time

import pytest

import journal
import orchestrator
import validate
from test_conversation_state import stored
from test_native_context_reuse import PARSE_CFG, run, setup


class Clock:
    """orchestrator's monotonic clock, advanced while the upstream is thinking."""

    def __init__(self):
        self.shift = 0.0

    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self):
        return time.monotonic() + self.shift


def first_token(tmp_path, monkeypatch, effort, seconds):
    clock = Clock()
    monkeypatch.setattr(orchestrator, "time", clock)
    backend, _pool, runner = setup(tmp_path)

    def think():
        clock.shift += seconds

    backend.on_send = think
    data = {"model": "claude-opus-5-5", "messages": [{"role": "system", "content": "CLIENT_SYSTEM"},
                                                     {"role": "user", "content": "hi"}]}
    if effort:
        data["reasoning_effort"] = effort
    parsed = validate.parse_chat(data, PARSE_CFG)
    parsed.conversation_key = "header:first-token-%s-%s" % (effort, seconds)
    seq = journal.JOURNAL.last_seq
    asyncio.run(run(runner, parsed))
    (event,) = stored(seq, "upstream.first_token")
    assert abs(event["data"]["ttft_ms"] - seconds * 1000) < 2000
    return event["level"]


@pytest.mark.parametrize("effort, seconds, level", [
    ("max", 30, "success"), ("max", 50, "warn"),
    ("high", 15, "success"), ("high", 25, "warn"),
    ("low", 4, "success"),
])
def test_the_first_token_warns_by_the_effort_sent(tmp_path, monkeypatch, effort, seconds, level):
    assert first_token(tmp_path, monkeypatch, effort, seconds) == level


@pytest.mark.parametrize("effort, threshold", [
    (None, 8_000), ("", 8_000), ("none", 8_000), ("minimal", 8_000), ("unknown", 8_000),
    ("low", 20_000), ("medium", 20_000), ("high", 20_000), ("xhigh", 45_000), ("max", 45_000), ("XHIGH", 45_000),
])
def test_the_threshold_of_each_level(effort, threshold):
    assert orchestrator.first_token_warn_ms(effort) == threshold
    assert orchestrator.first_token_level(threshold - 1, effort) == "success"
    assert orchestrator.first_token_level(threshold, effort) == "warn"
