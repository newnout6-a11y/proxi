"""Only authoritative billed amounts enter the balance, even after cancellation."""
import asyncio
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

import apierr
import console_log
import credits
import direct
import journal


def manager():
    value = credits.CreditsManager("https://upstream.invalid", lambda: {})
    value._balance = {"available": 100.0, "monthly_allocation": 500.0}
    value._cached_at = time.monotonic()
    return value


def session(value):
    backend = SimpleNamespace(cfg={"base": "https://upstream.invalid"}, headers=lambda: {})
    return SimpleNamespace(backend=backend, thread_id="test-thread", _credits_manager=value,
                           _cancelled=False, _request_tag="owner-request")


def response(events):
    return io.BytesIO(b"\n".join(json.dumps(event).encode() for event in events) + b"\n")


@pytest.mark.parametrize("ending", ["complete", "truncated", "error", "error-with-usage"])
def test_keepwarm_debits_reported_cost_once_even_if_its_stream_fails(monkeypatch, capsys, ending):
    value = manager()
    target = session(value)
    events = [{"event": "metadata", "usage": {"creditsUsed": 0.12567, "promptTokensReadFromCache": 18000}}] * 2
    if ending == "complete":
        events.append({"event": "end_of_stream"})
    elif ending == "error":
        events.append({"event": "error", "error": "synthetic failure"})
    elif ending == "error-with-usage":
        events = [{"event": "error", "error": "synthetic failure", "usage": events[0]["usage"]}]
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_kw: response(events))
    before = journal.JOURNAL.last_seq
    if ending == "complete":
        assert direct._ping(target, {"model": "claude-opus-5-5"}) == 0.12567
    else:
        with pytest.raises(apierr.ApiError):
            direct._ping(target, {"model": "claude-opus-5-5"})
    assert value.session_used == 0.12567
    assert value.balance_snapshot()["available"] == 99.8743
    assert value.usage()["turns"] == 1
    assert target._keepwarm_cost == 0.12567
    output = capsys.readouterr().out
    assert "balance=99.8743" in output and "spent=0.1257" in output and "[keepwarm]" in output
    entries = [e for e in journal.JOURNAL.query(since=before) if e["kind"] == "cache.keepwarm_usage"]
    assert len(entries) == 1 and entries[0]["data"]["actual_credits"] == 0.12567


def test_truncated_keepwarm_without_usage_cannot_be_assumed_free(monkeypatch):
    value = manager()
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_kw: response([]))
    with pytest.raises(apierr.ApiError):
        direct._ping(session(value), {"model": "claude-opus-5-5"})
    assert value.balance_snapshot()["unreported_usage"] is True and value.session_used == 0


@pytest.mark.parametrize("cost", [None, True, -1, "NaN", "Infinity", "invalid"])
def test_unreported_or_invalid_keepwarm_cost_is_unknown_and_never_debited(monkeypatch, cost):
    value = manager()
    target = session(value)
    events = [{"event": "metadata", "usage": {"creditsUsed": cost, "promptTokensReadFromCache": 50000}},
              {"event": "end_of_stream"}]
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_kw: response(events))
    assert direct._ping(target, {"model": "claude-opus-5-5"}) is None
    assert value.session_used == 0 and value.balance_snapshot()["available"] == 100
    assert value.balance_snapshot()["unreported_usage"] is True
    assert "UNREPORTED USAGE" in console_log.balance_line("req", value.balance_snapshot())


def test_zero_keepwarm_cost_is_a_reported_zero(monkeypatch):
    value = manager()
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_kw: response([
        {"event": "metadata", "usage": {"creditsUsed": 0}}, {"event": "end_of_stream"}]))
    assert direct._ping(session(value), {"model": "claude-opus-5-5"}) == 0
    assert value.session_used == 0 and not value.balance_snapshot().get("unreported_usage")


def test_each_paid_ping_is_a_distinct_transaction(monkeypatch):
    value = manager()
    target = session(value)
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_kw: response([
        {"event": "metadata", "usage": {"creditsUsed": 0.12567}}, {"event": "end_of_stream"}]))
    for _ in range(2):
        direct._ping(target, {"model": "claude-opus-5-5"})
    assert value.session_used == 0.25134 and value.usage()["turns"] == 2
    assert len({entry["turn_id"] for entry in value.usage()["recent"]}) == 2


def test_keepwarm_write_spikes_compare_distinct_pings_of_the_same_owner(monkeypatch, capsys):
    value = manager()
    target = session(value)
    writes = iter([100, 9000, 9100, 40000])

    def fetch(*_a, **_kw):
        return response([{"event": "metadata", "usage": {
            "creditsUsed": 0.01, "promptTokensWrittenToCache": next(writes)}}, {"event": "end_of_stream"}])

    monkeypatch.setattr(direct.urllib.request, "urlopen", fetch)
    body = {"model": "claude-opus-5-5", "openAICacheKey": "scope-a"}
    direct._ping(target, body)
    direct._ping(target, body)
    direct._ping(target, body)
    direct._ping(target, dict(body, openAICacheKey="scope-b"))
    output = capsys.readouterr().out
    assert "[WRITE SPIKE +8,900]" in output
    assert output.count("[WRITE SPIKE") == 1
    assert "[owner-request] [cache]" in output


def test_cancelled_scheduler_still_accounts_for_its_inflight_ping(monkeypatch):
    value = manager()
    target = session(value)
    target._warm_body = {"model": "claude-opus-5-5"}
    target._last_request_at = time.time()
    entered, release = threading.Event(), threading.Event()

    class SlowResponse:
        def __iter__(self):
            entered.set()
            assert release.wait(3)
            yield json.dumps({"event": "metadata", "usage": {"creditsUsed": 0.25}}).encode()
            yield b'{"event":"end_of_stream"}'

        def close(self):
            pass

    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_kw: SlowResponse())

    async def scenario():
        task = asyncio.create_task(direct._keepwarm(target, {"cache_keepwarm_seconds": 240}))
        try:
            deadline = time.monotonic() + 3
            while not entered.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            task.cancel()
            await task
        finally:
            release.set()
        deadline = time.monotonic() + 3
        while value.session_used != 0.25:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        assert value.balance_snapshot()["available"] == 99.75

    asyncio.run(scenario())


def test_unknown_cost_marks_balance_until_authoritative_refresh(monkeypatch):
    value = manager()
    value.mark_usage_unknown()
    assert value.balance_snapshot()["unreported_usage"] is True
    assert value.get_balance()["stale"] is True
    monkeypatch.setattr(credits.urllib.request, "urlopen", lambda *_a, **_kw: io.BytesIO(
        b'{"org":{"balance":95,"monthlyAllocation":500}}'))
    assert value.get_balance(force_refresh=True)["available"] == 95
    assert not value.balance_snapshot()["unreported_usage"]
    assert not value.balance_snapshot()["stale"]


@pytest.mark.parametrize("unknown", [False, True])
def test_usage_during_balance_refresh_is_not_erased_or_subtracted_twice(monkeypatch, unknown):
    value = manager()

    def fetch(*_a, **_kw):
        if unknown:
            value.mark_usage_unknown()
        else:
            value.record_turn("concurrent-ping", 0.25)
        return io.BytesIO(b'{"org":{"balance":99.75,"monthlyAllocation":500}}')

    monkeypatch.setattr(credits.urllib.request, "urlopen", fetch)
    result = value.get_balance(force_refresh=True)
    assert result["available"] == 99.75 and result["stale"] is True
    assert value.get_balance()["stale"] is True
    assert value.balance_snapshot()["unreported_usage"] is unknown
    assert value.session_used == (0 if unknown else 0.25)
    monkeypatch.setattr(credits.urllib.request, "urlopen", lambda *_a, **_kw: io.BytesIO(
        b'{"org":{"balance":99.75,"monthlyAllocation":500}}'))
    assert value.get_balance(force_refresh=True)["stale"] is False


@pytest.mark.parametrize("cost", [True, -1, "NaN", "Infinity", None, "invalid"])
def test_invalid_credit_amount_never_corrupts_shared_balance(cost):
    value = manager()
    value.record_turn("invalid-event", cost)
    assert value.session_used == 0 and value.usage()["turns"] == 0
    assert value.balance_snapshot()["available"] == 100


@pytest.mark.parametrize("cost", [None, 0, 0.25])
def test_generation_metadata_without_a_cost_marks_usage_unknown(monkeypatch, cost):
    value = manager()
    target = session(value)
    usage = {"promptTokensReadFromCache": 8000}
    if cost is not None:
        usage["creditsUsed"] = cost
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_kw: response([
        {"event": "metadata", "usage": usage},
        {"event": "content_block_delta", "type": "text", "partialText": "ok"},
        {"event": "end_of_stream"}]))

    async def consume():
        return [e async for e in direct._stream_body({"turn_timeout": 3}, target, {"model": "claude-opus-5-5"})]

    asyncio.run(consume())
    assert value.balance_snapshot().get("unreported_usage", False) is (cost is None)
