"""metrics() copies the slots it needs under the journal lock a portion at a time and sums them outside it,
so emit() never waits for the calculation (24 h of dense traffic took about 35 ms under the lock)."""
import array
import gc
import random
import threading
import time

import pytest

import journal


def dense_journal(clock):
    """A 24 h rollup in which every 10 s slot has traffic and every minute slot carries full distributions."""
    log = journal.Journal(clock=clock)
    rollup = log._rollup
    start = clock() - journal.RETENTION + 5
    for step in range(journal.RETENTION // journal.FINE):
        slot = rollup.fine_slot(int((start + step * journal.FINE) // journal.FINE))
        slot.requests, slot.errors, slot.latency_sum, slot.latency_n = 5, 1, 5000.0 + step % 7, 5
        slot.ttft_sum, slot.ttft_n, slot.credits, slot.keepwarm = 2000.0, 5, 0.5 + step % 3 / 10, 0.1
        slot.keepwarm_pings, slot.cache_read, slot.cache_write, slot.input = 1, 1000, 100, 500
        slot.output, slot.turns, slot.tools, slot.commands, slot.unknown = 200, 2, 3, 1, int(step % 5 == 0)
    for step in range(journal.RETENTION // journal.COARSE):
        slot = rollup.coarse_slot(clock(), int((start + step * journal.COARSE) // journal.COARSE))
        slot.statuses = {str(200 + k): 3 for k in range(journal._LABELS)}
        slot.models = {'model-%d' % k: 2 + k % 3 for k in range(journal._LABELS)}
        slot.categories = {'cat-%d' % k: 5 for k in range(journal._LABELS)}
        slot.levels = {'debug': 1, 'info': 2, 'warn': 3, 'error': 4}
        slot.latency = array.array('d', [float(k * 3 % 997) for k in range(journal.MAX_SAMPLES)])
        slot.ttft = array.array('d', [float(k * 7 % 991) for k in range(journal.MAX_SAMPLES)])
    return log


def random_journal(seed, clock):
    """A journal fed with a seeded mix of every event kind the rollup counts, across about 22 hours."""
    rng = random.Random(seed)
    log = journal.Journal(clock=lambda: clock['now'])
    for turn in range(400):
        clock['now'] += rng.uniform(1, 400)
        name = 't%d' % turn
        log.emit('turn.begin', '', turn_id=name, model=rng.choice(['m1', 'm2', 'm3']))
        for attempt in range(rng.randint(0, 3)):
            log.emit('upstream.usage_start', '', turn_id=name, attempt=attempt)
            log.emit('upstream.first_token', '', turn_id=name, ttft_ms=rng.randint(50, 9000))
            log.emit('upstream.usage', '', turn_id=name, attempt=attempt, credits=rng.choice([None, 0.0, rng.random(), -1]),
                     input_tokens=rng.randint(0, 900), output_tokens=rng.randint(0, 400),
                     cache_read=rng.randint(0, 5000), cache_write=rng.randint(0, 800))
        log.emit('tool.call', '', turn_id=name)
        log.emit('exec.result', '', turn_id=name)
        if rng.random() < .3:
            log.emit('cache.keepwarm_usage', '', actual_credits=rng.choice([None, rng.random()]))
        log.emit('http.request', '', 'error' if rng.random() < .1 else 'info', status=rng.choice([200, 200, 429, 502]),
                 duration_ms=rng.randint(10, 20000), route=rng.choice(['chat', 'responses', 'anthropic', 'admin']))
        if rng.random() < .7:
            log.emit('turn.end', '', turn_id=name, reason=rng.choice(['stop', 'cancelled', 'error']))
    clock['now'] += 30
    return log


WINDOWS = [(3600, 60), (21600, 60), (86400, 60), (86400, 300), (600, 10), (900, 1)]


@pytest.mark.parametrize('window,bucket', WINDOWS)
def test_the_portion_size_never_changes_a_figure(monkeypatch, window, bucket):
    log = random_journal(11, {'now': 1_800_000_000.0})
    monkeypatch.setattr(journal, '_PORTION', 10 ** 9)
    whole = log.metrics(window, bucket)
    for size in (1, 7, 256):
        monkeypatch.setattr(journal, '_PORTION', size)
        assert log.metrics(window, bucket) == whole
    assert whole['totals']['requests'] > 0 and (window < 3600 or whole['totals']['credits'] > 0)


def test_a_dense_day_gives_the_same_figures_whatever_the_portion(monkeypatch):
    log = dense_journal(lambda: 1_800_000_000.0)
    monkeypatch.setattr(journal, '_PORTION', 10 ** 9)
    whole = log.metrics(86400, 60)
    monkeypatch.setattr(journal, '_PORTION', 64)
    assert log.metrics(86400, 60) == whole
    assert whole['totals']['requests'] > 40000 and len(whole['statuses']) == journal._LABELS


class TimedCondition(threading.Condition):
    """The journal's lock, recording how long each hold lasts."""

    def __init__(self):
        super().__init__()
        self.holds, self._since = [], 0.0

    def __enter__(self):
        result = super().__enter__()
        self._since = time.perf_counter()
        return result

    def __exit__(self, *exc):
        self.holds.append(time.perf_counter() - self._since)
        return super().__exit__(*exc)


def test_no_single_hold_of_the_lock_comes_near_the_cost_of_the_calculation():
    log = dense_journal(time.time)
    log._cond = TimedCondition()
    gc.collect()
    gc.disable()  # a collection inside a hold would be the collector's pause, not the design's
    try:
        worst = []
        for _ in range(3):
            log._cond.holds.clear()
            log.metrics(86400, 60)
            worst.append(max(log._cond.holds))
    finally:
        gc.enable()
    # Before the fix one hold took about 35 ms: the whole sum of 24 h.
    assert len(log._cond.holds) > 3 and min(worst) < 0.008, worst


def test_emit_is_not_blocked_by_the_calculation(monkeypatch):
    log = dense_journal(time.time)
    pause, real, inside = 0.6, journal._aggregate, threading.Event()

    def slow(*args, **kwargs):
        inside.set()
        time.sleep(pause)  # a calculation that takes as long as it likes: the lock must not be held for it
        return real(*args, **kwargs)

    monkeypatch.setattr(journal, '_aggregate', slow)
    result = []
    thread = threading.Thread(target=lambda: result.append(log.metrics(86400, 60)))
    thread.start()
    assert inside.wait(10)
    began, waits = time.perf_counter(), []
    while time.perf_counter() - began < pause / 2:
        before = time.perf_counter()
        log.emit('http.request', 'x', 'info', status=200, duration_ms=5, route='chat')
        waits.append(time.perf_counter() - before)
        time.sleep(0.005)
    assert max(waits) < 0.1, max(waits)
    assert log._cond.acquire(timeout=0.5)
    log._cond.release()
    thread.join(10)
    assert result and result[0]['totals']['requests'] > 40000


def test_emit_stays_fast_while_a_dense_day_is_summed_in_a_loop():
    log = dense_journal(time.time)
    stop, sums = threading.Event(), []

    def hammer():
        while not stop.is_set():
            started = time.perf_counter()
            log.metrics(86400, 60)
            sums.append(time.perf_counter() - started)

    thread = threading.Thread(target=hammer)
    thread.start()
    timer = threading.Timer(3, stop.set)
    timer.start()
    latencies = []
    while not stop.is_set():
        started = time.perf_counter()
        log.emit('http.request', 'x', 'info', status=200, duration_ms=5, route='chat')
        latencies.append(time.perf_counter() - started)
        time.sleep(0.001)
    thread.join(10)
    timer.cancel()
    latencies.sort()
    median = latencies[len(latencies) // 2]
    assert sums and len(latencies) > 100
    # Before the fix emit() waited for the whole sum under the lock: p50 about 33 ms, p95 about 48 ms.
    # Now it competes for the interpreter only; the median stays far below a millisecond or two.
    assert median < 0.005, (median, latencies[int(len(latencies) * .95)], latencies[-1])
