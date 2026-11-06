"""Journal cost metrics: which attempts are unknown, running turns, cache pings, invalid prices, turn traces."""
import asyncio
import http.client
import io
import json
import math
import socket
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

import apierr
import direct
import journal
from test_native_context_reuse import FIRST, run
from test_native_history import close, harness, parsed as parse_request
from test_review_fixes_pr19 import response, text


class Feed:
    """A private journal with a controllable clock."""

    def __init__(self):
        self.now = 1_800_000_000.0
        self.log = journal.Journal(clock=lambda: self.now)

    def __call__(self, kind, **fields):
        level = fields.pop("level", "info")
        return self.log.emit(kind, kind, level, **fields)

    def play(self, events):
        for kind, fields in events:
            self(kind, **dict(fields))
        return self.log.metrics()["totals"]


def begin(turn):
    return "turn.begin", dict(turn_id=turn)


def start(turn, attempt):
    return "upstream.usage_start", dict(turn_id=turn, attempt=attempt)


def usage(turn, attempt, credits, **extra):
    return "upstream.usage", dict(turn_id=turn, attempt=attempt, credits=credits, **extra)


def end(turn, reason="stop"):
    return "turn.end", dict(turn_id=turn, reason=reason)


MARKER = dict(credits=None, credits_reported=False)

# (events, credits, unknown attempts); the scenarios of the review plus their variants.
SCENARIOS = [
    pytest.param([begin("t"), start("t", 1), usage("t", 1, .01)], .01, 0, id="normal"),
    pytest.param([begin("t")], 0, 0, id="turn.begin only: running before any attempt"),
    pytest.param([begin("t"), end("t", "error")], 0, 0, id="rejected before the upstream (413/402/503)"),
    pytest.param([begin("t"), start("t", 1)], 0, 0, id="attempt started, nothing reported (upstream 429/500)"),
    pytest.param([begin("t"), start("t", 1), end("t", "error")], 0, 0, id="refused attempt in an ended turn"),
    pytest.param([begin("A"), start("A", 1), usage("A", 1, **MARKER), end("A", "error"),
                  begin("B"), start("B", 2), usage("B", 2, .02, credits_reported=True), end("B")],
                 .02, 1, id="failover after an attempt that reported usage without a price"),
    pytest.param([begin("A"), start("A", 1), end("A", "error"),
                  begin("B"), start("B", 2), usage("B", 2, .02, credits_reported=True), end("B")],
                 .02, 0, id="failover after a refused attempt (no marker)"),
    pytest.param([begin("t"), start("t", 1), usage("t", 1, .01), start("t", 2), usage("t", 2, .02)],
                 .03, 0, id="exec step: two priced attempts"),
    pytest.param([begin("t"), start("t", 1), usage("t", 1, .01), start("t", 2), end("t")],
                 .01, 0, id="second attempt never reported anything"),
    pytest.param([begin("t"), start("t", 1), usage("t", 1, .01), start("t", 2), usage("t", 2, **MARKER)],
                 .01, 1, id="second attempt reported usage without a price"),
    pytest.param([begin("r"), start("r", 1), usage("r", 1, .01), begin("r"), start("r", 1), usage("r", 1, .02)],
                 .03, 0, id="two turns sharing one key"),
    pytest.param([begin("t"), start("t", 1), usage("t", 1, .01),
                  usage("t", 1, None, credits_reported=True)], .01, 0, id="tokens-only update after the price"),
    pytest.param([begin("t"), start("t", 1), usage("t", 1, None), usage("t", 1, .25)], .25, 0,
                 id="price after a tokens-only snapshot"),
    pytest.param([begin("t"), start("t", 1), usage("t", 1, 0)], 0, 0, id="a reported zero is a price"),
    pytest.param([begin("t"), start("t", 1), end("t", "cancelled")], 0, 1,
                 id="cancelled while the attempt was silent: may have been billed"),
    pytest.param([begin("t"), end("t", "cancelled")], 0, 0, id="cancelled before any attempt started"),
    pytest.param([begin("t"), start("t", 1), usage("t", 1, **MARKER), end("t", "cancelled")], 0, 1,
                 id="graceful cancel: the marker and the cancelled end count once"),
    pytest.param([begin("t"), start("t", 1), usage("t", 1, .01), start("t", 2), end("t", "cancelled")], .01, 1,
                 id="cancelled second attempt after a priced first one"),
    pytest.param([begin("t"), start("t", 1), usage("t", 1, .01), end("t", "cancelled")], .01, 0,
                 id="cancelled after the price arrived"),
]


@pytest.mark.parametrize("events,credits,unknown", SCENARIOS)
def test_cost_scenarios(events, credits, unknown):
    totals = Feed().play(events)
    assert totals["credits"] == credits
    assert totals["credits_unknown"] == unknown
    assert totals["credits_complete"] is (unknown == 0)


def test_a_turn_that_never_reached_upstream_does_not_make_the_whole_window_incomplete():
    feed = Feed()
    for index in range(5):
        feed.play([begin("rejected%d" % index), ("turn.error", dict(turn_id="rejected%d" % index, level="error")),
                   end("rejected%d" % index, "error")])
    assert feed.log.metrics()["totals"]["credits_complete"] is True
    assert feed.log.metrics()["totals"]["errors"] == 5


def test_a_running_turn_never_makes_the_window_incomplete_but_is_counted():
    feed = Feed()
    totals = feed.play([begin("t"), start("t", 1)])
    assert totals["credits_complete"] is True and totals["credits_unknown"] == 0
    assert totals["credits_in_progress"] == 1
    totals = feed.play([begin("u"), start("u", 2)])
    assert totals["credits_in_progress"] == 2
    totals = feed.play([usage("t", 1, .25)])
    assert totals["credits_in_progress"] == 1 and totals["credits"] == .25 and totals["credits_complete"] is True
    totals = feed.play([end("u", "error")])
    assert totals["credits_in_progress"] == 0 and totals["credits_complete"] is True


def test_only_the_latest_started_attempt_of_a_running_turn_is_in_progress():
    feed = Feed()
    totals = feed.play([begin("t"), start("t", 1), start("t", 2)])
    assert totals["credits_in_progress"] == 1


def test_a_turn_that_never_ended_stops_counting_as_in_progress_after_the_ttl():
    feed = Feed()
    assert feed.play([begin("lost"), start("lost", 1)])["credits_in_progress"] == 1
    feed.now += journal.LIVE_TURN_TTL + 1
    totals = feed.log.metrics(window=600)["totals"]
    assert totals["credits_in_progress"] == 0 and totals["credits_complete"] is True


def test_the_number_of_tracked_running_turns_is_bounded(monkeypatch):
    monkeypatch.setattr(journal, "MAX_LIVE_TURNS", 3)
    feed = Feed()
    for index in range(10):
        feed.play([begin("t%d" % index), start("t%d" % index, 1)])
    assert feed.log.metrics()["totals"]["credits_in_progress"] == 3
    assert len(feed.log._rollup.turns) == 3


def test_totals_are_not_a_sum_of_rounded_buckets():
    feed = Feed()
    for index in range(30):
        feed.now += 61
        feed.play([begin("t%d" % index), start("t%d" % index, 1), usage("t%d" % index, 1, .00004),
                   ping(.00004, "keepwarm:%d" % index)])
    feed.now += 1
    totals = feed.log.metrics(window=3600, bucket=60)["totals"]
    assert totals["credits"] == .0024 and totals["keepwarm_credits"] == .0012


def test_an_unpriced_attempt_becomes_known_when_the_price_arrives_in_the_bucket_it_started():
    feed = Feed()
    feed("turn.begin", turn_id="t")
    feed("upstream.usage_start", turn_id="t", attempt=1)
    feed.now += 130
    feed("upstream.usage", turn_id="t", attempt=1, credits=None)
    metrics = feed.log.metrics(window=600, bucket=60)
    assert metrics["totals"]["credits_unknown"] == 1
    assert [row["credits_unknown"] for row in metrics["buckets"]].count(1) == 1
    unknown_bucket = next(row for row in metrics["buckets"] if row["credits_unknown"])
    assert unknown_bucket["t"] <= 1_800_000_000 < unknown_bucket["t"] + 60
    assert unknown_bucket["credits_complete"] is False
    feed.now += 30
    feed("upstream.usage", turn_id="t", attempt=1, credits=.5)
    metrics = feed.log.metrics(window=600, bucket=60)
    assert metrics["totals"]["credits_unknown"] == 0 and metrics["totals"]["credits"] == .5
    assert all(row["credits_complete"] for row in metrics["buckets"])


@pytest.mark.parametrize("price", [-1, -0.5, math.nan, math.inf, -math.inf, True, "0.5", [0.5]])
@pytest.mark.parametrize("flag", [True, None])
def test_an_invalid_price_is_unknown_even_when_it_claims_to_be_reported(price, flag):
    totals = Feed().play([begin("t"), start("t", 1), usage("t", 1, price, credits_reported=flag), end("t")])
    assert totals["credits"] == 0
    assert totals["credits_unknown"] == 1 and totals["credits_complete"] is False


def test_the_reported_flag_alone_never_creates_a_price_but_keeps_an_earlier_one():
    assert Feed().play([begin("t"), start("t", 1), usage("t", 1, None, credits_reported=True)]
                       )["credits_unknown"] == 1
    totals = Feed().play([begin("t"), start("t", 1), usage("t", 1, .1), usage("t", 1, -1, credits_reported=True),
                          usage("t", 1, math.nan, credits_reported=True)])
    assert totals["credits"] == .1 and totals["credits_unknown"] == 0


# -- cache refresh pings ---------------------------------------------------------------

def ping(credits, billing_id="keepwarm:x", **extra):
    return "cache.keepwarm_usage", dict(actual_credits=credits, billing_id=billing_id, **extra)


def test_cache_refresh_cost_is_part_of_the_totals_and_reported_separately():
    totals = Feed().play([begin("t"), start("t", 1), usage("t", 1, .01), ping(.5)])
    assert totals["credits"] == .51 and totals["keepwarm_credits"] == .5
    assert totals["keepwarm_pings"] == 1 and totals["keepwarm_unknown"] == 0
    assert totals["credits_complete"] is True


def test_a_ping_without_a_price_is_unknown_not_free():
    totals = Feed().play([begin("t"), start("t", 1), usage("t", 1, .01), ping(None)])
    assert totals["credits"] == .01 and totals["keepwarm_credits"] == 0
    assert totals["keepwarm_pings"] == 1 and totals["keepwarm_unknown"] == 1
    assert totals["credits_unknown"] == 1 and totals["credits_complete"] is False


@pytest.mark.parametrize("price", [-1, math.nan, True, "0.5"])
def test_an_invalid_ping_price_is_unknown(price):
    totals = Feed().play([ping(price)])
    assert totals["credits"] == 0 and totals["keepwarm_unknown"] == 1 and totals["credits_complete"] is False


def test_a_zero_priced_ping_is_a_price():
    totals = Feed().play([ping(0)])
    assert totals["keepwarm_pings"] == 1 and totals["credits_complete"] is True


def test_each_ping_counts_once_and_its_sibling_event_is_not_added_again():
    totals = Feed().play([ping(.2, "keepwarm:a"), ("cache.keepwarm", dict(actual_credits=.2, credits_total=.2)),
                          ping(.3, "keepwarm:b"), ("cache.keepwarm", dict(actual_credits=.3, credits_total=.5))])
    assert totals["credits"] == .5 and totals["keepwarm_credits"] == .5 and totals["keepwarm_pings"] == 2


def test_cache_refresh_cost_lands_in_the_bucket_of_its_ping():
    feed = Feed()
    feed.play([ping(.25)])
    feed.now += 300
    feed.play([ping(.5)])
    feed.now += 60
    buckets = feed.log.metrics(window=900, bucket=300)["buckets"]
    assert [row["keepwarm_credits"] for row in buckets if row["keepwarm_credits"]] == [.25, .5]
    assert sum(row["credits"] for row in buckets) == .75


def test_a_real_refresh_ping_reaches_the_metrics(monkeypatch):
    log = journal.Journal()
    monkeypatch.setattr(journal, "JOURNAL", log)

    def session():
        backend = SimpleNamespace(cfg={"base": "https://upstream.invalid"}, headers=lambda: {})
        return SimpleNamespace(backend=backend, thread_id="t", _cancelled=False, _request_tag="r",
                               _credits_manager=SimpleNamespace(record_turn=lambda *_: None,
                                                                mark_usage_unknown=lambda: None))

    def serve(usage_payload):
        monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(
            b"\n".join(json.dumps(item).encode() for item in (
                {"event": "metadata", "usage": usage_payload}, {"event": "end_of_stream"})) + b"\n"))

    serve({"creditsUsed": .12567, "promptTokensReadFromCache": 18000})
    assert direct._ping(session(), {"model": "claude-opus-5-5"}) == .12567
    totals = log.metrics()["totals"]
    assert totals["keepwarm_credits"] == .1257 and totals["credits"] == .1257
    assert totals["credits_complete"] is True
    serve({"promptTokensReadFromCache": 18000})
    assert direct._ping(session(), {"model": "claude-opus-5-5"}) is None
    totals = log.metrics()["totals"]
    assert totals["keepwarm_pings"] == 2 and totals["keepwarm_unknown"] == 1
    assert totals["credits_complete"] is False


# -- turn traces (/admin/api/turns) -----------------------------------------------------

def traces_of(events):
    feed = Feed()
    for kind, fields in events:
        feed(kind, **dict(fields))
    return {trace["turn_id"]: trace for trace in feed.log.turns()}


def test_turn_traces_report_an_unknown_price_as_none_not_zero():
    traces = traces_of([begin("priced"), start("priced", 1), usage("priced", 1, .25), end("priced"),
                        begin("unknown"), start("unknown", 1), usage("unknown", 1, **MARKER), end("unknown"),
                        begin("partial"), start("partial", 1), usage("partial", 1, .5), start("partial", 2),
                        usage("partial", 2, **MARKER), end("partial"),
                        begin("rejected"), end("rejected", "error"),
                        begin("refused"), start("refused", 1), end("refused", "error"),
                        begin("zero"), start("zero", 1), usage("zero", 1, 0), end("zero")])
    assert (traces["priced"]["credits"], traces["priced"]["credits_complete"]) == (.25, True)
    assert (traces["unknown"]["credits"], traces["unknown"]["credits_complete"]) == (None, False)
    assert traces["unknown"]["credits_unknown"] == 1
    assert (traces["partial"]["credits"], traces["partial"]["credits_complete"]) == (.5, False)
    assert traces["partial"]["credits_unknown"] == 1
    for name in ("rejected", "refused", "zero"):
        assert (traces[name]["credits"], traces[name]["credits_complete"], traces[name]["credits_unknown"]) == (
            0.0, True, 0)


def test_a_running_turn_has_no_final_price_yet():
    traces = traces_of([begin("waiting"), start("waiting", 1),
                        begin("between"), start("between", 1), usage("between", 1, .1)])
    assert (traces["waiting"]["credits"], traces["waiting"]["credits_complete"]) == (None, False)
    assert (traces["between"]["credits"], traces["between"]["credits_complete"]) == (.1, False)
    assert traces["waiting"]["status"] == "running"


def test_trace_prices_ignore_invalid_values_and_sum_attempts():
    traces = traces_of([begin("t"), start("t", 1), usage("t", 1, .1), usage("t", 1, -4, credits_reported=True),
                        start("t", 2), usage("t", 2, .2), end("t")])
    assert traces["t"]["credits"] == pytest.approx(.3) and traces["t"]["credits_complete"] is True


def test_a_cancelled_silent_attempt_is_unknown_in_the_trace_too():
    traces = traces_of([begin("cut"), start("cut", 1), end("cut", "cancelled"),
                        begin("early"), end("early", "cancelled")])
    assert (traces["cut"]["credits"], traces["cut"]["credits_complete"], traces["cut"]["credits_unknown"]) == (
        None, False, 1)
    assert (traces["early"]["credits"], traces["early"]["credits_complete"]) == (0.0, True)


# -- the real orchestrator + direct transport -------------------------------------------

def turn_metrics(tmp_path, monkeypatch, steps, *, prepare=None, accounts=1):
    """Run one request through TurnRunner with scripted POST outcomes; returns the private journal."""
    log = journal.Journal()
    monkeypatch.setattr(journal, "JOURNAL", log)
    pool, runner, sent = harness(tmp_path, monkeypatch, [])
    script = list(steps)

    def opener(request, **_kw):
        sent.append(request.data)
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return response(*step)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    paid = []
    runner.credits = SimpleNamespace(record_turn=lambda key, cost: paid.append(cost),
                                     mark_usage_unknown=lambda: None)
    if prepare:
        prepare(pool, runner)

    async def scenario():
        try:
            return await run(runner, parse_request(FIRST))
        finally:
            await close(pool)

    events = asyncio.run(scenario())
    return log, events, paid, sent


def http_error(status):
    return urllib.error.HTTPError("https://fixture.invalid", status, "refused", {}, io.BytesIO(b"{}"))


PRICED = [text("answer"), {"event": "metadata", "usage": {"inputTokens": 4, "outputTokens": 12, "creditsUsed": .25}},
          {"event": "end_of_stream"}]


def test_a_priced_turn_is_complete(tmp_path, monkeypatch):
    log, _, paid, _ = turn_metrics(tmp_path, monkeypatch, [PRICED])
    totals = log.metrics()["totals"]
    assert totals["credits"] == .25 and totals["credits_complete"] is True
    assert totals["credits_in_progress"] == 0 and paid == [.25]


def test_a_prompt_rejected_before_the_upstream_costs_nothing(tmp_path, monkeypatch):
    def limit(pool, runner):
        runner.cfg["max_prompt_bytes"] = 10

    log, events, _, sent = turn_metrics(tmp_path, monkeypatch, [], prepare=limit)
    assert events[-1]["error"].status == 413 and sent == []
    assert not [e for e in log.query() if e["kind"] == "upstream.usage_start"]
    totals = log.metrics()["totals"]
    assert totals["turns"] == 1 and totals["credits_unknown"] == 0 and totals["credits_complete"] is True


def test_a_turn_refused_by_the_pool_costs_nothing(tmp_path, monkeypatch):
    def refuse(pool, runner):
        async def acquire(*_a, **_k):
            raise apierr.ApiError(402, "insufficient_credits", "every account is exhausted")

        monkeypatch.setattr(pool, "acquire", acquire)

    log, events, _, sent = turn_metrics(tmp_path, monkeypatch, [], prepare=refuse)
    assert events[-1]["error"].status == 402 and sent == []
    totals = log.metrics()["totals"]
    assert totals["turns"] == 1 and totals["credits_unknown"] == 0 and totals["credits_complete"] is True


@pytest.mark.parametrize("failure", [
    pytest.param(http_error(429), id="429"),
    pytest.param(http_error(500), id="500"),
    pytest.param(http_error(503), id="503"),
    pytest.param(urllib.error.URLError(socket.gaierror(-2, "name resolution failed")), id="dns"),
    pytest.param(urllib.error.URLError(ConnectionRefusedError(111, "refused")), id="refused"),
])
def test_an_attempt_the_upstream_never_accepted_is_not_a_missing_price(tmp_path, monkeypatch, failure):
    # a 429 is asked again on the same account (direct.RATE_LIMIT_WAITS): all four attempts are refused
    attempts = 4 if getattr(failure, "code", None) == 429 else 1
    steps = [http_error(429) for _ in range(attempts)] if attempts > 1 else [failure]  # a fresh refusal each time
    log, events, paid, sent = turn_metrics(tmp_path, monkeypatch, steps)
    assert events[-1]["type"] == "error" and len(sent) == attempts and paid == []
    assert [e for e in log.query() if e["kind"] == "upstream.usage_start"]
    totals = log.metrics()["totals"]
    assert totals["credits_unknown"] == 0 and totals["credits_complete"] is True
    assert totals["credits_in_progress"] == 0


@pytest.mark.parametrize("failure", [
    pytest.param([text("partial")], id="truncated stream"),
    pytest.param(TimeoutError("read timed out"), id="read timeout after sending"),
    pytest.param(http.client.RemoteDisconnected("closed without an answer"), id="closed after sending"),
])
def test_an_attempt_that_may_have_been_billed_stays_unknown(tmp_path, monkeypatch, failure):
    log, events, paid, sent = turn_metrics(tmp_path, monkeypatch, [failure])
    assert events[-1]["type"] == "error" and paid == []
    totals = log.metrics()["totals"]
    assert totals["credits_unknown"] == 1 and totals["credits_complete"] is False


def test_failover_after_a_refused_attempt_keeps_the_second_accounts_price(tmp_path, monkeypatch):
    from test_native_context_reuse import Backend

    def second_account(pool, runner):
        first = pool.backends[0]
        second = Backend(tmp_path, **first.cfg)
        second.headers = lambda: {}
        second.cfg["provider_resource_name"] = "other-account"
        pool.backends.append(second)

        def check(backend):
            backend.unavailable = {"reason": "exhausted", "available": 0, "threshold": 20}
            return True

        runner.accounts = SimpleNamespace(check_balance_now=check)

    log, events, paid, sent = turn_metrics(tmp_path, monkeypatch, [http_error(402), PRICED], prepare=second_account)
    assert len(sent) == 2 and paid == [.25] and events[-1]["type"] == "end"
    totals = log.metrics()["totals"]
    assert totals["credits"] == .25 and totals["credits_unknown"] == 0 and totals["credits_complete"] is True
    assert [e for e in log.query() if e["kind"] == "account.failover"]


def test_a_client_cancelling_a_silent_attempt_is_unknown_in_the_real_flow(tmp_path, monkeypatch):
    """The hard cancel path (task.cancel) emits no usage marker: only turn.end tells the journal."""
    log = journal.Journal()
    monkeypatch.setattr(journal, "JOURNAL", log)
    pool, runner, _ = harness(tmp_path, monkeypatch, [])
    entered, release = threading.Event(), threading.Event()

    class Hanging:
        status = 200
        headers = {}

        def __iter__(self):
            entered.set()
            release.wait(5)
            return iter(())

        def close(self):
            release.set()

    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_k: Hanging())

    async def scenario():
        task = asyncio.create_task(run(runner, parse_request(FIRST)))
        try:
            deadline = time.monotonic() + 3
            while not entered.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            release.set()
            await close(pool)

    asyncio.run(scenario())
    ended = [e for e in log.query() if e["kind"] == "turn.end"]
    assert ended and ended[-1]["data"]["reason"] == "cancelled"
    assert [e["data"].get("credits_reported") for e in log.query() if e["kind"] == "upstream.usage"] == []
    totals = log.metrics()["totals"]
    assert totals["credits_unknown"] == 1 and totals["credits_complete"] is False
    assert totals["credits_in_progress"] == 0