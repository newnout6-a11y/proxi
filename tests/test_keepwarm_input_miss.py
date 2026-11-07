"""A ping on a route that reports no cache write (GLM, gpt-5.5) and did not read its prefix ends the cycle as a rewrite does.

Such a route bills a prefix it did not read as plain input and says nothing of a write (credits.py): 10 times the price of a
read on GLM-5.3 and gpt-5.5, 5 times on GLM-5.3-Flash. The rewrite check looked only at promptTokensWrittenToCache, so a ping
that found the prefix gone passed for a healthy one: the cycle went on to its idle limit with every ping paying the whole prefix
again (4 pings and 44.8 credits for a 100k-token GLM-5.3 chat, 7 for a subagent), and the session was never marked, so the
next cycle paid again (lens-credits-2)."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

import credits
import direct
import journal
from keepwarm_harness import CFG, DUE, START, install, session as warm_session, stops

GLM = {"model": "zai-org/GLM-5.3", "openAICacheKey": "key-glm"}
MISS = {"inputTokens": 100_000, "promptTokensReadFromCache": 0, "outputTokens": 1, "creditsUsed": 11.2}
READ = {"inputTokens": 230, "promptTokensReadFromCache": 99_800, "outputTokens": 1, "creditsUsed": 1.14}


def events(mark, kind):
    return [dict(e["data"], level=e["level"]) for e in journal.JOURNAL.query(since=mark, limit=1000) if e["kind"] == kind]


# -- what counts as a miss ------------------------------------------------------------------------------------------------
MISSES = [
    (100_000, 0),        # the whole prefix billed as input: the entry was gone
    (60_000, 340_000),   # a sixth of a huge prefix, but past the absolute threshold
    (6_000, 4_000),      # a miss on a small prompt: more than half not read
    (1_024, 0),          # a prompt of one cache block, missed completely
]
READS = [
    (230, 99_800),       # a read: only the nudge and the tail past the last cached block are input
    (40_000, 150_000),   # below both thresholds: a partial miss the cycle survives
    (1_023, 0),          # below one cache block: noise
    (0, 0),
]


@pytest.mark.parametrize("missed,read", MISSES)
@pytest.mark.parametrize("write", ["omitted", None])
def test_uncached_input_is_the_miss_of_a_route_that_reports_no_write(missed, read, write):
    usage = {"inputTokens": missed, "promptTokensReadFromCache": read}
    if write is None:
        usage["promptTokensWrittenToCache"] = None
    for model in (GLM["model"], "gpt-5.5", None):
        assert direct._rewrite_of(usage, model) == {"input_tokens": missed, "read_tokens": read,
                                                    "prefix_tokens": missed + read}, model


@pytest.mark.parametrize("model", ["zai-org/GLM-5.3", "zai-org/GLM-5.3-Flash", "gpt-5.5"])
def test_a_zero_write_counter_of_a_model_that_never_writes_says_nothing_either(model):
    usage = {"inputTokens": 100_000, "promptTokensReadFromCache": 0, "promptTokensWrittenToCache": 0}
    assert direct._rewrite_of(usage, model) == {"input_tokens": 100_000, "read_tokens": 0, "prefix_tokens": 100_000}


@pytest.mark.parametrize("missed,read", READS)
@pytest.mark.parametrize("model", ["zai-org/GLM-5.3", "gpt-5.5", None])
def test_a_ping_that_read_its_prefix_on_such_a_route_is_no_miss(missed, read, model):
    assert direct._rewrite_of({"inputTokens": missed, "promptTokensReadFromCache": read}, model) is None
    assert direct._rewrite_of({"inputTokens": missed, "promptTokensReadFromCache": read,
                               "promptTokensWrittenToCache": 0}, model) is None


@pytest.mark.parametrize("usage,expected", [
    # A write counter is the measure of a miss wherever the route reports writes, whatever its uncached input.
    ({"promptTokensWrittenToCache": 0, "promptTokensReadFromCache": 0, "inputTokens": 100_000}, None),
    ({"promptTokensWrittenToCache": 120, "promptTokensReadFromCache": 190_000, "inputTokens": 60_000}, None),
    ({"promptTokensWrittenToCache": 190_000, "promptTokensReadFromCache": 0, "inputTokens": 9},
     {"written_tokens": 190_000, "read_tokens": 0, "prefix_tokens": 190_000}),
])
@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "gpt-6.1-sol", None])
def test_a_route_that_reports_writes_is_judged_by_them_as_before(usage, expected, model):
    assert direct._rewrite_of(usage, model) == expected
    assert direct._rewrite_of(usage) == expected


def test_the_routes_that_bill_a_miss_as_input_are_the_ones_whose_pings_this_reads():
    for model in ("zai-org/GLM-5.3", "zai-org/GLM-5.3-Flash", "gpt-5.5"):
        rate = credits.rates(model)
        assert rate["cache_write"] == rate["input"] > rate["cache_read"], model


# -- the cycle --------------------------------------------------------------------------------------------------------------
def usage_ping(usage):
    def ping(owner, _body):
        owner._last_warm_usage = dict(usage)
        return usage["creditsUsed"]
    return ping


@pytest.mark.parametrize("usage", [MISS, dict(MISS, promptTokensWrittenToCache=0)], ids=["no-write-counter", "zero-write"])
def test_a_glm_ping_that_missed_its_prefix_ends_the_cycle_and_marks_the_session(monkeypatch, usage):
    mark = journal.JOURNAL.last_seq
    target = warm_session(_warm_body=dict(GLM), _warm_at=START - 100, _last_prefix_tokens=100_000)
    clock, pings = install(monkeypatch, seconds=3000, ping=usage_ping(usage))
    asyncio.run(direct._keepwarm(target, dict(CFG)))
    assert len(pings) == 1, "one ping paid the whole prefix as input; the old cycle sent 4, one every 240 s"
    [stop] = stops(mark)
    assert (stop["reason"], stop["level"], stop["pings"], stop["idle_pings"]) == ("cache_rewritten", "warn", 1, 1)
    assert (stop["input_tokens"], stop["read_tokens"], stop["prefix_tokens"]) == (100_000, 0, 100_000)
    assert "written_tokens" not in stop, "nothing was reported written"
    assert stop["ping_credits"] == 11.2 and stop["credits"] == pytest.approx(11.2)
    assert target._warm_blocked == "cache_rewritten", "no later cycle pays for the same miss again"
    [ping_event] = events(mark, "cache.keepwarm")
    assert ping_event["level"] == "warn" and ping_event["cache_verified"] is False
    assert [e["message"] for e in journal.JOURNAL.query(since=mark, limit=100) if e["kind"] == "cache.keepwarm"] == [
        "Прогрев не прочитал кэш: префикс оплачен как обычный вход"]


def test_a_claude_cycle_whose_pings_report_no_write_is_judged_as_before(monkeypatch):
    """A route that reports writes keeps its own measure: a zero write is no rewrite there, whatever the input."""
    mark = journal.JOURNAL.last_seq
    usage = {"inputTokens": 100_000, "promptTokensReadFromCache": 0, "promptTokensWrittenToCache": 0, "creditsUsed": 8.0}
    target = warm_session(_warm_at=START - 100, _last_prefix_tokens=100_000)
    clock, pings = install(monkeypatch, seconds=5000, ping=usage_ping(usage))
    asyncio.run(direct._keepwarm(target, dict(CFG)))
    assert len(pings) == 4 and [stop["reason"] for stop in stops(mark)] == ["idle_limit"]


def test_a_glm_cycle_whose_pings_read_their_prefix_runs_to_its_idle_limit(monkeypatch):
    mark = journal.JOURNAL.last_seq
    target = warm_session(_warm_body=dict(GLM), _warm_at=START - 100, _last_prefix_tokens=100_000)
    clock, pings = install(monkeypatch, seconds=5000, ping=usage_ping(READ))
    asyncio.run(direct._keepwarm(target, dict(CFG)))
    assert len(pings) == 4 and [stop["reason"] for stop in stops(mark)] == ["idle_limit"]
    assert getattr(target, "_warm_blocked", None) is None
    assert all(e["level"] == "debug" and e["cache_verified"] for e in events(mark, "cache.keepwarm"))


def test_a_real_ping_whose_usage_has_no_write_counter_and_no_read_ends_the_cycle(monkeypatch):
    real_ping = direct._ping
    mark = journal.JOURNAL.last_seq
    backend = SimpleNamespace(cfg={"account_id": "acc-glm", "base": "https://upstream.invalid"}, unavailable=None,
                              headers=lambda: {})
    target = warm_session(backend=backend, _warm_body=dict(GLM), _warm_at=DUE, _cancelled=False, _credits_manager=None,
                          _last_prefix_tokens=100_000)
    clock, pings = install(monkeypatch, seconds=3000)
    monkeypatch.setattr(direct, "_ping", real_ping)
    sent = []

    def urlopen(request, **_kw):
        sent.append(clock.now)
        lines = [{"event": "metadata", "usage": dict(MISS)}, {"event": "end_of_stream"}]
        return io.BytesIO(b"\n".join(json.dumps(line).encode() for line in lines) + b"\n")

    monkeypatch.setattr(direct.urllib.request, "urlopen", urlopen)
    asyncio.run(direct._keepwarm(target, dict(CFG)))
    assert len(sent) == 1
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"], stop["input_tokens"], stop["prefix_tokens"]) == (
        "cache_rewritten", 1, 100_000, 100_000)
    assert stop["credits"] == pytest.approx(11.2) and stop["ping_credits"] == 11.2
