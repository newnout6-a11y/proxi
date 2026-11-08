"""Reported output throughput is distinct from tokens guessed from text or decoder timing."""
import json
import asyncio
import time

import orchestrator
import apierr
from test_account_rate_limit import retool, hourly, turn  # noqa: F401 - fixture
from test_review_fixes_pr19 import response, text
from test_native_context_reuse import run as async_run

import pytest

import journal
from test_project_workflow import run, setup
from test_slot_wait import slow_slot
from conftest import FakeRunner


@pytest.mark.parametrize("count,duration,expected", [
    (100, 2000, 50), (0, 2000, 0), (None, 1000, None), (True, 1000, None),
    (1, False, None), (1, 0, None), (-1, 1000, None), (1.5, 1000, None),
    (float('inf'), 1000, None), (1, float('nan'), None), (10 ** 400, 1000, None),
])
def test_speed_requires_valid_reported_counts_and_positive_finite_duration(count, duration, expected):
    assert journal.request_speed(count, duration) == expected


def end(log, key, output, duration, reason="stop"):
    return log.emit("request.speed", "done", turn_id=key, reason=reason,
                    output_tokens=output, duration_ms=duration)


def test_overview_speed_is_weighted_and_excludes_incomplete_or_failed_requests():
    log = journal.Journal(clock=lambda: 1_800_000_000)
    end(log, "a", 100, 1000)
    end(log, "b", 100, 9000, "tool_calls")
    end(log, "zero", 0, 1000)
    for index, reason in enumerate(("error", "cancelled", "length", "timeout")):
        end(log, str(index), 1000000, 1, reason)
    end(log, "unknown", None, 1000)
    end(log, "invalid", True, 1000)
    totals = log.metrics()["totals"]
    assert totals["tokens_per_second"] == round(200 / 11, 3)
    assert totals["speed_samples"] == 3
    assert log.turns()[0]["tokens_per_second"] is None


def test_no_data_is_unknown_not_zero():
    assert journal.Journal(clock=lambda: 1_800_000_000).metrics()["totals"]["tokens_per_second"] is None


def test_speed_rollup_survives_ring_turnover_and_restore(tmp_path):
    path = tmp_path / "journal.jsonl"
    clock = lambda: 1_800_000_000
    log = journal.Journal(capacity=2, clock=clock)
    log.configure({}, str(path))
    end(log, "a", 20, 2000)
    for _ in range(10):
        log.emit("noise", "noise")
    assert log.metrics()["totals"]["tokens_per_second"] == 10
    restored = journal.Journal(clock=clock)
    restored.configure({}, str(path))
    assert restored.metrics()["totals"]["tokens_per_second"] == 10
    assert restored.metrics()["totals"]["speed_samples"] == 1
    assert json.loads(path.read_text().splitlines()[0])["data"]["output_tokens"] == 20


@pytest.mark.parametrize("events,expected", [
    ([{"type": "usage", "usage": {"output_tokens": 20}},
      {"type": "credits", "value": 1, "usage": {"output_tokens": 20}}], 20),
    ([{"type": "usage", "usage": {"output_tokens": 0}}], 0),
    ([{"type": "usage", "usage": {}}], None),
    ([{"type": "usage", "usage": {"output_tokens": 3}}, {"type": "usage_start"},
      {"type": "usage", "usage": {"output_tokens": 7}}], 10),
    ([{"type": "usage", "usage": {"output_tokens": 3}}, {"type": "usage_start"}], None),
])
def test_runner_emits_complete_attempt_counts_once(tmp_path, monkeypatch, events, expected):
    emitted = []
    real = journal.emit

    def capture(kind, message, *args, **fields):
        if kind == "request.speed":
            emitted.append(fields)
        return real(kind, message, *args, **fields)

    monkeypatch.setattr(journal, "emit", capture)
    # The transport normally begins every attempt with usage_start.
    script = [{"type": "usage_start"}, *events, {"type": "text", "delta": "ok"},
              {"type": "end", "reason": "stop"}]
    _, parsed, _, runner = setup(tmp_path, script)
    run(runner, parsed)
    assert emitted[-1]["output_tokens"] == expected


def test_account_failover_has_one_request_sample_including_first_account_latency(retool, monkeypatch):
    def denied(req):
        time.sleep(0.12)
        return hourly(req)

    def accepted(req):
        return response(text("OK"), {"event": "metadata", "usage": {"inputTokens": 4, "outputTokens": 100,
                        "creditsUsed": 0.1}}, {"event": "end_of_stream"})

    retool.script["acc-a"], retool.script["acc-b"] = denied, accepted
    captured = []
    real = journal.emit

    def emit(kind, message="", *args, **fields):
        if kind in ("turn.end", "request.speed"):
            captured.append((kind, fields))
        return real(kind, message, *args, **fields)

    monkeypatch.setattr(journal, "emit", emit)
    started = time.monotonic()
    asyncio.run(async_run(retool.runner, turn()))
    elapsed = (time.monotonic() - started) * 1000
    ends = [fields for kind, fields in captured if kind == "turn.end"]
    samples = [fields for kind, fields in captured if kind == "request.speed"]
    assert len(ends) == 2 and len(samples) == 1
    sample = samples[0]
    assert sample["reason"] == "stop" and sample["output_tokens"] == 100
    assert sample["duration_ms"] >= 120
    assert sample["duration_ms"] <= elapsed + 5
    assert sample["duration_ms"] >= sum(end["duration_ms"] for end in ends) - 2
    assert sample["turn_id"] == ends[-1]["turn_id"]
    log = journal.Journal()
    for kind, fields in captured:
        log.emit(kind, "fixture", **fields)
    assert log.metrics()["totals"]["speed_samples"] == 1
    assert log.metrics()["totals"]["tokens_per_second"] == sample["tokens_per_second"]


def test_acquisition_and_request_duration_use_monotonic_clock(tmp_path, monkeypatch):
    captured = []
    real = journal.emit

    def emit(kind, message="", *args, **fields):
        if kind in ("session.acquire", "request.speed"):
            captured.append((kind, fields))
        return real(kind, message, *args, **fields)

    monkeypatch.setattr(journal, "emit", emit)
    monkeypatch.setattr(orchestrator.time, "time", lambda: 1_800_000_000)
    _, parsed, _, runner = setup(tmp_path, [{"type": "usage_start"},
        {"type": "usage", "usage": {"output_tokens": 20}}, {"type": "text", "delta": "ok"},
        {"type": "end", "reason": "stop"}])
    holder = {"_request_started_monotonic": time.monotonic() - 2}
    run(runner, parsed, holder)
    acquired = [fields for kind, fields in captured if kind == "session.acquire"]
    assert len(acquired) == 1 and 0 <= acquired[0]["wait_ms"] < 1000
    sample, = [fields for kind, fields in captured if kind == "request.speed"]
    assert 2000 <= sample["duration_ms"] < 3000
    assert sample["tokens_per_second"] == journal.request_speed(20, sample["duration_ms"])


def test_final_quota_does_not_turn_earlier_unknown_attempt_into_zero(tmp_path, monkeypatch):
    captured = []
    real = journal.emit

    def emit(kind, message="", *args, **fields):
        if kind == "request.speed":
            captured.append(fields)
        return real(kind, message, *args, **fields)

    monkeypatch.setattr(journal, "emit", emit)
    _, parsed, _, runner = setup(tmp_path, [{"type": "usage_start"},
        {"type": "usage", "usage": {}}, {"type": "usage_start"},
        {"type": "error", "error": apierr.account_rate_limited("hourly limit")}])
    run(runner, parsed)
    assert captured[-1]["output_tokens"] is None
    assert captured[-1]["tokens_per_second"] is None


def test_http_request_speed_includes_worker_queue_and_is_linked_to_http_event(relay, tmp_path, monkeypatch):
    _, _, _, runner = setup(tmp_path, [{"type": "usage_start"},
        {"type": "usage", "usage": {"output_tokens": 100}}, {"type": "text", "delta": "ok"},
        {"type": "end", "reason": "stop"}])

    async def real_run(self, parsed, out, holder):
        await runner.run(parsed, out, holder)

    monkeypatch.setattr(FakeRunner, "run", real_run)
    slow_slot(monkeypatch, 0.15)
    records = []
    real = journal.emit

    def capture(kind, *args, **fields):
        if kind in ("request.speed", "http.request"):
            records.append((kind, fields))
        return real(kind, *args, **fields)

    monkeypatch.setattr(journal, "emit", capture)
    status, _ = relay.json("/v1/chat/completions", {"model": "gpt-6-sol",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200
    deadline = time.monotonic() + 3
    while not any(kind == "http.request" for kind, _ in records):
        assert time.monotonic() < deadline
        time.sleep(0.005)
    sample, = [fields for kind, fields in records if kind == "request.speed"]
    http, = [fields for kind, fields in records if kind == "http.request"]
    assert sample["request_id"] == http["request_id"]
    assert sample["output_tokens"] == 100
    assert http["slot_wait_ms"] >= 100
    assert sample["duration_ms"] >= http["slot_wait_ms"]
    assert sample["tokens_per_second"] == journal.request_speed(100, sample["duration_ms"])
