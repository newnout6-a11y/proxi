"""Metrics coverage: rollups outlive the raw ring buffer and the response says how much of a window they cover."""
import json
import threading

import pytest

import journal

T0 = 1_800_000_000.0  # a whole minute


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def request(log, duration=1000, status=200, level="info"):
    return log.emit("http.request", "POST /v1/messages", level, route="anthropic", status=status,
                    duration_ms=duration)


def steady(log, clock, minutes):
    """One request at every minute mark for ``minutes`` minutes; returns the timestamps."""
    stamps = []
    for _ in range(minutes):
        clock.now += 60
        stamps.append(request(log)["ts"])
    return stamps


def test_windows_stay_complete_after_the_ring_buffer_turned_over():
    clock = Clock()
    log = journal.Journal(capacity=20, clock=clock)
    stamps = steady(log, clock, 180)
    clock.now += 1
    metrics = log.metrics(window=7200, bucket=300)
    start = metrics["buckets"][0]["t"]
    expected = len([ts for ts in stamps if ts >= start])
    assert metrics["buffered"] == 20 and expected > 100
    assert metrics["totals"]["requests"] == expected
    assert sum(row["requests"] for row in metrics["buckets"]) == expected
    assert metrics["coverage"]["complete"] is True


def test_a_window_older_than_the_journal_is_reported_as_partially_covered():
    clock = Clock()
    log = journal.Journal(clock=clock)
    clock.now += 1800
    request(log)
    hour = log.metrics(window=3600, bucket=60)["coverage"]
    assert hour["complete"] is False
    assert hour["covered_from"] == T0 and hour["covered_seconds"] == 1800
    assert hour["window_start"] == T0 - 1800
    quarter = log.metrics(window=900, bucket=60)["coverage"]
    assert quarter["complete"] is True and quarter["covered_seconds"] == 900
    day = log.metrics(window=86400)["coverage"]
    assert day["complete"] is False and day["covered_seconds"] == 1800


def test_a_window_that_starts_exactly_when_the_journal_started_is_complete():
    clock = Clock()
    log = journal.Journal(clock=clock)
    clock.now += 3600
    assert log.metrics(window=3600, bucket=60)["coverage"]["complete"] is True


def test_coverage_also_describes_the_raw_buffer():
    clock = Clock()
    log = journal.Journal(capacity=3, clock=clock)
    stamps = []
    for _ in range(5):
        clock.now += 10
        stamps.append(request(log)["ts"])
    coverage = log.metrics(window=600, bucket=60)["coverage"]
    assert coverage["buffer_events"] == 3 and coverage["buffer_capacity"] == 3
    assert coverage["buffer_oldest_ts"] == stamps[2]
    assert coverage["oldest_event_ts"] == stamps[0]


def test_an_empty_journal_has_no_oldest_event():
    coverage = journal.Journal(clock=Clock()).metrics()["coverage"]
    assert coverage["oldest_event_ts"] is None and coverage["buffer_oldest_ts"] is None
    assert coverage["buffer_events"] == 0


def test_window_start_is_a_whole_minute_not_later_than_requested():
    clock = Clock(T0 + 37.5)
    log = journal.Journal(clock=clock)
    metrics = log.metrics(window=3600, bucket=60)
    start = metrics["buckets"][0]["t"]
    assert start % 60 == 0 and metrics["coverage"]["window_start"] == start
    assert clock.now - 3600 - 60 < start <= clock.now - 3600
    assert len(metrics["buckets"]) == 60


def test_fine_buckets_are_still_served():
    clock = Clock()
    log = journal.Journal(clock=clock)
    for offset in (5, 25, 599):
        clock.now = T0 + offset
        request(log)
    clock.now = T0 + 600
    metrics = log.metrics(window=600, bucket=10)
    assert len(metrics["buckets"]) == 60 and metrics["bucket_seconds"] == 10
    assert [index for index, row in enumerate(metrics["buckets"]) if row["requests"]] == [0, 2, 59]


# -- restoring the JSONL tail ------------------------------------------------------------

def write_history(path, minutes):
    clock = Clock()
    writer = journal.Journal(clock=clock)
    writer.configure(None, str(path), load=False)
    stamps = steady(writer, clock, minutes)
    writer.configure(None, None)
    return clock, stamps


def test_restored_history_feeds_the_rollup_beyond_the_ring_buffer(tmp_path):
    path = tmp_path / "journal.jsonl"
    clock, stamps = write_history(path, 90)
    clock.now += 3600  # the relay was down for an hour
    log = journal.Journal(capacity=10, clock=clock)
    log.configure(None, str(path))
    assert log.metrics()["buffered"] == 10
    three_hours = log.metrics(window=3 * 3600, bucket=900)
    assert three_hours["totals"]["requests"] == 90
    assert three_hours["coverage"]["complete"] is False
    assert three_hours["coverage"]["covered_from"] == stamps[0]
    assert three_hours["coverage"]["oldest_event_ts"] == stamps[0]
    hour = log.metrics(window=3600, bucket=60)
    # The hour of downtime is covered (nothing happened), so only the last request of the history is in it.
    assert hour["coverage"]["complete"] is True and hour["totals"]["requests"] == 1


def test_reconfiguring_does_not_count_restored_events_twice(tmp_path):
    path = tmp_path / "journal.jsonl"
    clock = Clock()
    log = journal.Journal(clock=clock)
    log.configure(None, str(path), load=False)
    steady(log, clock, 3)
    log.configure(None, str(path), load=True)
    assert log.metrics(window=3600)["totals"]["requests"] == 3


def test_turns_unfinished_in_a_previous_run_are_not_in_progress(tmp_path):
    path = tmp_path / "journal.jsonl"
    clock = Clock()
    writer = journal.Journal(clock=clock)
    writer.configure(None, str(path), load=False)
    writer.emit("turn.begin", "begin", turn_id="running")
    writer.emit("upstream.usage_start", "start", turn_id="running", attempt=1)
    writer.emit("turn.begin", "begin", turn_id="lost")
    writer.emit("upstream.usage_start", "start", turn_id="lost", attempt=2)
    writer.emit("upstream.usage", "usage", turn_id="lost", attempt=2, credits=None)
    assert writer.metrics()["totals"]["credits_in_progress"] == 1
    clock.now += 600
    restored = journal.Journal(clock=clock)
    restored.configure(None, str(path))
    totals = restored.metrics()["totals"]
    # The process that ran them is gone: nothing is still running, and the unpriced attempt stays unknown.
    assert totals["credits_in_progress"] == 0
    assert totals["credits_unknown"] == 1 and totals["credits_complete"] is False


def test_restored_events_without_a_usable_timestamp_do_not_break_or_stretch_coverage(tmp_path):
    def line(seq, **extra):
        event = {"seq": seq, "kind": "http.request", "category": "http", "level": "info", "message": "x",
                 "data": {"route": "chat", "status": 200, "duration_ms": 5}}
        event.update(extra)
        return json.dumps(event)

    path = tmp_path / "journal.jsonl"
    path.write_text("\n".join([line(1, ts=0.0), line(2), line(3, ts="yesterday"), line(4, ts=None),
                               line(5, ts=T0 - 600), line(6, ts=T0 - 300, data="not an object")]) + "\n",
                    encoding="utf-8")
    log = journal.Journal(clock=Clock())
    log.configure(None, str(path))
    metrics = log.metrics(window=3600, bucket=60)
    # Only the two events with a usable timestamp reach the aggregates; the odd payload still counts as a request.
    assert metrics["totals"]["requests"] == 2 and metrics["statuses"] == {"200": 1, "None": 1}
    assert metrics["coverage"]["oldest_event_ts"] == T0 - 600
    assert metrics["coverage"]["covered_from"] == T0 - 600


# -- clearing, failures and bounds -------------------------------------------------------

def test_clearing_the_journal_restarts_coverage_and_drops_aggregates():
    clock = Clock()
    log = journal.Journal(clock=clock)
    steady(log, clock, 10)
    assert log.metrics(window=600)["totals"]["requests"] == 10
    log.reset()
    clock.now += 30
    metrics = log.metrics(window=3600, bucket=60)
    assert metrics["totals"]["requests"] == 0 and metrics["buffered"] == 0
    assert metrics["coverage"]["covered_from"] == T0 + 600
    assert metrics["coverage"]["complete"] is False and metrics["coverage"]["covered_seconds"] == 30
    assert metrics["coverage"]["oldest_event_ts"] is None


def test_a_failed_ingest_keeps_the_event_and_shows_up_as_a_coverage_gap(monkeypatch):
    clock = Clock()
    log = journal.Journal(clock=clock)
    clock.now += 100
    request(log)
    original = journal._Rollup.ingest

    def broken(self, event, now, restored=False):
        if event["kind"] == "boom":
            raise RuntimeError("synthetic ingest failure")
        return original(self, event, now, restored)

    monkeypatch.setattr(journal._Rollup, "ingest", broken)
    clock.now += 100
    event = log.emit("boom", "x")
    assert event is not None and log.query()[-1]["kind"] == "boom"
    coverage = log.metrics(window=300, bucket=60)["coverage"]
    assert coverage["complete"] is False and coverage["covered_from"] == event["ts"]


def test_slots_are_pruned_beyond_retention_and_stay_bounded(monkeypatch):
    monkeypatch.setattr(journal, "RETENTION", 3600)
    monkeypatch.setattr(journal, "_COARSE_MAX", 68)
    monkeypatch.setattr(journal, "_FINE_MAX", 368)
    clock = Clock()
    log = journal.Journal(clock=clock)
    stamps = steady(log, clock, 200)
    assert len(log._rollup.coarse) <= 68 and len(log._rollup.fine) <= 368
    metrics = log.metrics(window=3600, bucket=60)
    start = metrics["buckets"][0]["t"]
    assert metrics["totals"]["requests"] == len([ts for ts in stamps if ts >= start]) == 61
    assert metrics["coverage"]["complete"] is True
    assert log.metrics(window=7200, bucket=60)["coverage"]["complete"] is False


def test_history_older_than_a_day_is_ignored_and_reported():
    clock = Clock()
    log = journal.Journal(clock=clock)
    stamps = []
    for _ in range(432):  # three days, a request every ten minutes
        clock.now += 600
        stamps.append(request(log)["ts"])
    day = log.metrics(window=86400, bucket=900)
    start = day["buckets"][0]["t"]
    assert day["totals"]["requests"] == len([ts for ts in stamps if ts >= start])
    assert day["coverage"]["complete"] is True
    week = log.metrics(window=7 * 86400, bucket=3600)
    assert week["coverage"]["complete"] is False
    assert week["coverage"]["covered_from"] == clock.now - journal.RETENTION
    assert week["totals"]["requests"] == len([ts for ts in stamps if ts >= clock.now - journal.RETENTION - 60])


# -- figures that need more than one slot ----------------------------------------------

def test_distributions_and_percentiles_span_minutes():
    clock = Clock()
    log = journal.Journal(clock=clock)
    for duration, status, model in ((100, 200, "m1"), (200, 200, "m2"), (300, 429, "m1"), (400, 200, "m1")):
        clock.now += 60
        request(log, duration=duration, status=status)
        log.emit("turn.begin", "begin", turn_id="t%d" % duration, model=model)
        log.emit("upstream.first_token", "token", turn_id="t%d" % duration, ttft_ms=duration * 2)
    clock.now += 60
    metrics = log.metrics(window=600, bucket=60)
    totals = metrics["totals"]
    assert (totals["latency_p50"], totals["latency_p95"]) == (300, 400)
    assert (totals["ttft_p50"], totals["ttft_p95"]) == (600, 800)
    assert metrics["statuses"] == {"200": 3, "429": 1}
    assert metrics["models"] == {"m1": 3, "m2": 1}
    assert metrics["categories"]["http"] == 4 and metrics["levels"]["info"] == 12
    assert [row["latency"] for row in metrics["buckets"] if row["latency"]] == [100, 200, 300, 400]


def test_samples_are_capped_per_minute_but_averages_use_every_request(monkeypatch):
    monkeypatch.setattr(journal, "MAX_SAMPLES", 10)
    clock = Clock()
    log = journal.Journal(clock=clock)
    clock.now += 10
    for duration in range(1, 26):
        request(log, duration=duration)
    clock.now += 1
    metrics = log.metrics(window=600, bucket=60)
    assert [row["latency"] for row in metrics["buckets"] if row["latency"]] == [13]
    assert metrics["totals"]["requests"] == 25 and metrics["totals"]["latency_p50"] == 6


def test_user_controlled_labels_cannot_grow_a_slot_without_bound():
    clock = Clock()
    log = journal.Journal(clock=clock)
    clock.now += 5
    for index in range(300):
        log.emit("turn.begin", "begin", turn_id="t%d" % index, model="m%03d-" % index + "x" * 500)
    metrics = log.metrics(window=600, bucket=60)
    slot = next(iter(log._rollup.coarse.values()))
    assert len(slot.models) <= journal._LABELS + 1 and "<other>" in slot.models
    assert all(len(name) <= journal._LABEL_CHARS for name in slot.models)
    assert sum(slot.models.values()) == 300 and metrics["totals"]["turns"] == 300


def test_errors_are_counted_for_every_event_kind():
    clock = Clock()
    log = journal.Journal(clock=clock)
    clock.now += 5
    request(log, level="error")
    log.emit("turn.error", "failed", "error", turn_id="t")
    log.emit("exec.result", "failed", "error", turn_id="t")
    metrics = log.metrics(window=600, bucket=60)
    assert metrics["totals"]["errors"] == 3 and metrics["levels"]["error"] == 3
    assert metrics["totals"]["requests"] == 1 and metrics["totals"]["commands"] == 1


def test_concurrent_writers_and_readers_agree(monkeypatch):
    log = journal.Journal(capacity=64)
    failures = []
    done = threading.Event()

    def write():
        for _ in range(300):
            request(log)

    def read():
        try:
            while not done.is_set():
                log.metrics(window=3600, bucket=60)
        except Exception as exc:  # noqa: BLE001 - reported below
            failures.append(exc)

    readers = [threading.Thread(target=read) for _ in range(2)]
    writers = [threading.Thread(target=write) for _ in range(8)]
    for thread in readers + writers:
        thread.start()
    for thread in writers:
        thread.join()
    done.set()
    for thread in readers:
        thread.join()
    assert failures == []
    metrics = log.metrics(window=3600, bucket=60)
    assert metrics["totals"]["requests"] == 2400 and metrics["buffered"] == 64
    assert metrics["last_seq"] == 2400
