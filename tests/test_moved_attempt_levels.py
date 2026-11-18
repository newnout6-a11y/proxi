"""An attempt that moves to another account is no error of the request (audit A02-06 / A03-04, the owner's "false red").

Each refused attempt used to log turn.error and turn.end at level error before _run_request moved the request on, and the
journal counted it in the red error figures of the dashboard and the turn list: 304 such events in requests that ended
with HTTP 200 (journal of 2026-11-15 to 11-18). Only an error that reaches the client is red.
"""
import asyncio
import io
import urllib.error
from email.message import Message

import journal
import upstream_attempts
from test_account_rate_limit import hourly, retool, turn  # noqa: F401 - the fixture and its helpers
from test_native_context_reuse import run
from test_retool_plan_refusal import refused


def _request_events(since, request_id="req-conv-1"):
    return [event for event in journal.JOURNAL.query(since=since, limit=5000) if event.get("request_id") == request_id]


def _of(events, kind):
    return [event for event in events if event["kind"] == kind]


def test_a_plan_refusal_that_moves_logs_no_error_for_a_request_that_ends_well(retool):
    retool.script["acc-a"] = refused
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(retool.runner, turn()))
    assert events[-1] == {"type": "end", "reason": "stop"} and retool.posts == ["acc-a", "acc-b"]
    logged = _request_events(before)
    assert [event["kind"] for event in logged if event["level"] == "error"] == []
    failed = _of(logged, "turn.error")
    assert len(failed) == 1 and failed[0]["level"] == "warn" and failed[0]["data"]["outcome"] == "moved"
    ends = _of(logged, "turn.end")
    assert [(event["level"], event["data"]["reason"]) for event in ends] == [("warn", "error"), ("success", "stop")]
    assert ends[0]["data"]["outcome"] == "moved" and "outcome" not in ends[1]["data"]


def test_an_hourly_limit_that_moves_logs_no_error_either(retool):
    retool.script["acc-a"] = hourly
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(retool.runner, turn()))
    assert events[-1] == {"type": "end", "reason": "stop"}
    logged = _request_events(before)
    assert [event["kind"] for event in logged if event["level"] == "error"] == []
    assert [(event["level"], event["data"]["outcome"]) for event in _of(logged, "turn.error")] == [("warn", "moved")]


def test_a_refusal_that_reaches_the_client_stays_an_error(retool, monkeypatch):
    monkeypatch.setattr(upstream_attempts, "MAX_ATTEMPTS", 1)
    retool.script["acc-a"] = refused
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(retool.runner, turn()))
    assert events[-1]["type"] == "error"
    logged = _request_events(before)
    failed = _of(logged, "turn.error")
    assert len(failed) == 1 and failed[0]["level"] == "error" and "outcome" not in failed[0]["data"]
    assert [event["level"] for event in _of(logged, "turn.end")] == ["error"]


def test_an_ordinary_failure_is_still_an_error_of_the_turn(retool):
    def down(request_):
        raise urllib.error.HTTPError(request_.full_url, 400, "bad", Message(), io.BytesIO(b'{"message": "bad"}'))

    retool.script["acc-a"] = down
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(retool.runner, turn()))
    assert events[-1]["type"] == "error" and retool.posts == ["acc-a"]
    logged = _request_events(before)
    assert [event["level"] for event in _of(logged, "turn.error")] == ["error"]
    assert [event["level"] for event in _of(logged, "turn.end")] == ["error"]
