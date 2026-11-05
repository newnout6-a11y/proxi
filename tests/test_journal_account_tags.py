"""Journal events name the account that served them: a short tag, never a raw ID."""
import asyncio
import copy
import io
import json
import time
import urllib.error
from types import SimpleNamespace

import pytest

import direct
import journal
from keepwarm_harness import backend as warm_backend, drive, session as warm_session
from test_account_rate_limit import hourly, retool, turn  # noqa: F401 - fixture
from test_native_context_reuse import FIRST, Backend, request, run, setup
from test_serving_accounts import M, account, relay_pool  # noqa: F401 - fixture
from test_stream_status import park

ACCOUNT, PAGE = "acc-journal-a", "page-journal-a"
TAG = journal.tag(ACCOUNT)


class Response(io.BytesIO):
    status = 200
    headers = {"request-id": "upstream-id"}


def stream(*events):
    return Response(b"\n".join(json.dumps(event).encode() for event in events) + b"\n")


def served(tmp_path, monkeypatch, opener):
    backend, pool, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[],
                                  base="https://example.invalid", cache_keepwarm_seconds=0,
                                  upstream_error_dir=str(tmp_path), account_id=ACCOUNT, page_uuid=PAGE)
    backend.headers = lambda: {"cookie": "synthetic"}
    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(runner, request(FIRST)))
    return events, journal.JOURNAL.query(since=before, limit=500)


def by_kind(events):
    result = {}
    for event in events:
        result.setdefault(event["kind"], []).append(event)
    return result


def test_every_stage_of_a_served_turn_carries_the_account_tag(tmp_path, monkeypatch):
    events, journaled = served(tmp_path, monkeypatch, lambda *_a, **_kw: stream(
        {"event": "content_block_delta", "type": "text", "partialText": "OK"},
        {"event": "metadata", "usage": {"inputTokens": 4, "outputTokens": 3, "creditsUsed": 0.25}},
        {"event": "end_of_stream"}))
    assert events[-1] == {"type": "end", "reason": "stop"}
    kinds = by_kind(journaled)
    for kind in ("session.acquire", "upstream.request", "upstream.attempt", "upstream.usage", "turn.end"):
        assert kinds.get(kind), kind
        assert all(event["data"].get("account") == TAG for event in kinds[kind]), kind
    encoded = json.dumps(journaled, ensure_ascii=False)
    assert ACCOUNT not in encoded and PAGE not in encoded


def test_a_failed_turn_names_its_account_in_turn_error_and_turn_end(tmp_path, monkeypatch):
    def down(req, **_kw):
        raise urllib.error.HTTPError(req.full_url, 503, "down", {}, io.BytesIO(b"down"))

    events, journaled = served(tmp_path, monkeypatch, down)
    assert events[-1]["type"] == "error"
    kinds = by_kind(journaled)
    for kind in ("turn.error", "turn.end"):
        assert kinds.get(kind), kind
        assert all(event["data"].get("account") == TAG for event in kinds[kind]), kind


def test_a_turn_that_never_acquired_a_session_has_no_account(tmp_path, monkeypatch):
    backend, pool, runner = setup(tmp_path, transport="direct", project_context=False, account_id=ACCOUNT)

    async def refuse(*_args, **_kw):
        raise RuntimeError("no session")

    monkeypatch.setattr(pool, "acquire", refuse)
    before = journal.JOURNAL.last_seq
    asyncio.run(run(runner, request(FIRST)))
    ended = [e for e in journal.JOURNAL.query(since=before) if e["kind"] == "turn.end"]
    assert ended and all("account" not in event["data"] for event in ended)


def test_pool_events_name_the_account_a_conversation_moves_from_and_to(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False, account_id=ACCOUNT)
    first, other = pool.backends[0], Backend(tmp_path, provider_resource_name="other", account_id="acc-journal-b")
    pool.backends.append(other)
    before = journal.JOURNAL.last_seq

    async def scenario():
        lease = await pool.acquire("tagged-chat")
        assert lease.backend is first
        await pool.release(lease)
        first.cfg["enabled"] = False
        lease = await pool.acquire("tagged-chat")
        assert lease.backend is other
        await pool.release(lease)

    asyncio.run(scenario())
    journaled = journal.JOURNAL.query(since=before)
    kinds = by_kind(journaled)
    assert [e["data"]["account"] for e in kinds["session.create"]] == [TAG]
    moved = kinds["account.migrate"]
    assert [(e["data"]["from_account"], e["data"]["to_account"]) for e in moved] == [(TAG, journal.tag("acc-journal-b"))]
    encoded = json.dumps(journaled, ensure_ascii=False)
    assert ACCOUNT not in encoded and "acc-journal-b" not in encoded


def test_keepwarm_ping_events_carry_the_account_tag(monkeypatch):
    target = warm_session(backend=warm_backend(ACCOUNT), _warm_at=0)
    before = journal.JOURNAL.last_seq
    drive(monkeypatch, target, seconds=3)
    pinged = [e for e in journal.JOURNAL.query(since=before) if e["kind"] == "cache.keepwarm"]
    assert pinged and all(event["data"]["account"] == TAG for event in pinged)


def test_keepwarm_failure_events_carry_the_account_tag(monkeypatch):
    target = warm_session(backend=warm_backend(ACCOUNT), _warm_at=0)

    def fail(*_args):
        raise OSError("synthetic failure")

    before = journal.JOURNAL.last_seq
    drive(monkeypatch, target, seconds=3, ping=fail)
    failed = [e for e in journal.JOURNAL.query(since=before) if e["kind"] == "cache.keepwarm" and e["level"] == "warn"]
    assert failed and all(event["data"]["account"] == TAG for event in failed)


def test_keepwarm_billing_event_carries_the_account_tag(monkeypatch):
    target = SimpleNamespace(backend=SimpleNamespace(cfg={"base": "https://upstream.invalid", "account_id": ACCOUNT},
                                                     headers=lambda: {}),
                             thread_id="thread", _credits_manager=None, _cancelled=False, _request_tag="req")
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_kw: stream(
        {"event": "metadata", "usage": {"creditsUsed": 0.1, "promptTokensReadFromCache": 1000}},
        {"event": "end_of_stream"}))
    before = journal.JOURNAL.last_seq
    assert direct._ping(target, {"model": "claude-opus-5-5"}) == 0.1
    billed = [e for e in journal.JOURNAL.query(since=before) if e["kind"] == "cache.keepwarm_usage"]
    assert len(billed) == 1 and billed[0]["data"]["account"] == TAG
    assert ACCOUNT not in json.dumps(billed)


def test_a_turn_never_borrows_the_tag_of_the_account_it_failed_over_from(retool):
    retool.script["acc-a"] = hourly
    before = journal.JOURNAL.last_seq
    asyncio.run(run(retool.runner, turn()))
    events = [e for e in journal.JOURNAL.query(since=before, limit=2000) if e.get("turn_id")]
    first, second = journal.tag("acc-a"), journal.tag("acc-b")
    # turn.begin comes before the account is chosen, so it has no tag; the later events of each attempt name theirs.
    begins = [e for e in events if e["kind"] == "turn.begin"]
    assert len(begins) == 2 and all("account" not in e["data"] for e in begins)
    assert [e["data"]["account"] for e in events if e["kind"] == "session.acquire"] == [first, second]
    assert [e["data"]["account"] for e in events if e["kind"] == "turn.end"] == [first, second]
    assert [e["data"]["account"] for e in events if e["kind"] == "turn.error"] == [first]


def test_upstream_events_after_acquire_carry_the_account_tag_without_passing_it_around(tmp_path, monkeypatch):
    events, journaled = served(tmp_path, monkeypatch, lambda *_a, **_kw: stream(
        {"event": "content_block_start", "type": "thinking"},
        {"event": "content_block_delta", "type": "text", "partialText": "OK"},
        {"event": "metadata", "usage": {"inputTokens": 4, "outputTokens": 3, "creditsUsed": 0.25}},
        {"event": "end_of_stream"}))
    assert events[-1] == {"type": "end", "reason": "stop"}
    signals = by_kind(journaled).get("upstream.thinking_signal")
    assert signals and all(event["data"].get("account") == TAG for event in signals)


def test_an_upstream_rejection_names_the_account_even_from_the_transport_thread(tmp_path, monkeypatch):
    def rejected(req, **_kw):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"message": "not accepted"}'))

    events, journaled = served(tmp_path, monkeypatch, rejected)
    assert events[-1]["type"] == "error"
    refusals = by_kind(journaled).get("upstream.rejected")
    assert refusals and all(event["data"].get("account") == TAG for event in refusals)
    assert ACCOUNT not in json.dumps(journaled, ensure_ascii=False)


@pytest.mark.parametrize("edited,kind", [(False, "native_history.resume"), (True, "native_history.reset")])
def test_native_history_events_carry_the_account_tag(tmp_path, monkeypatch, edited, kind):
    from test_native_history import append_result, close, harness, parsed, tool_reply
    from test_review_fixes_pr19 import END, text

    pool, runner, _ = harness(tmp_path, monkeypatch, [tool_reply(), [text("done"), *END]])
    pool.backends[0].cfg["account_id"] = ACCOUNT
    before = journal.JOURNAL.last_seq

    async def scenario():
        try:
            first = parsed(FIRST)
            messages = append_result(copy.deepcopy(FIRST), first, await run(runner, first), "chat")
            if edited:
                messages[0]["content"] = "edited task"  # the signed prefix no longer matches
            await run(runner, parsed(messages))
        finally:
            await close(pool)

    asyncio.run(scenario())
    found = [e for e in journal.JOURNAL.query(since=before, limit=2000) if e["kind"] == kind]
    assert len(found) == 1 and found[0]["data"].get("account") == TAG


def test_the_tag_leaves_the_context_before_the_session_is_released(tmp_path):
    # Releasing can evict other conversations: those events must not be labelled with this turn's account.
    import client_context
    _, pool, runner = setup(tmp_path, transport="direct", project_context=False, account_id=ACCOUNT)
    seen, real = [], pool.release

    async def spy(lease):
        seen.append(client_context.current().get("account"))
        await real(lease)

    pool.release = spy
    asyncio.run(run(runner, request(FIRST)))
    assert seen == [None]


# -- the events of the HTTP layer ------------------------------------------------------------------------------
CHAT = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}
TWO = [account("acc-a", provider_resource_name=M), account("acc-b", provider_resource_name=M)]


def logged(request_id, kind):
    """The events of one request; http.request is written after the response, so wait for it."""
    deadline = time.monotonic() + 3
    while True:
        found = [e for e in journal.JOURNAL.query(limit=3000) if e["kind"] == kind and e.get("request_id") == request_id]
        if found or time.monotonic() > deadline:
            return found
        time.sleep(0.01)


def test_the_request_event_names_the_account_that_served_it(relay_pool):
    call = relay_pool(TWO)
    park(call.runtime, call.runtime.accounts.backends[0])
    status, _, headers = call("/v1/chat/completions", CHAT)
    assert status == 200
    events = logged(headers["x-request-id"], "http.request")
    assert len(events) == 1 and events[0]["data"]["account"] == journal.tag("acc-b")
    assert "acc-b" not in json.dumps(events)


def test_the_error_and_request_events_of_a_failed_turn_name_its_account(relay_pool, monkeypatch):
    call = relay_pool(TWO[:1])

    def down(req, **_kw):
        raise urllib.error.HTTPError(req.full_url, 500, "down", {}, io.BytesIO(b"down"))

    monkeypatch.setattr(direct.urllib.request, "urlopen", down)
    status, _, headers = call("/v1/chat/completions", CHAT)
    assert status == 502
    errors = logged(headers["x-request-id"], "http.error")
    assert len(errors) == 1 and errors[0]["data"]["account"] == journal.tag("acc-a")
    assert errors[0]["data"]["status"] == 502
    requests = logged(headers["x-request-id"], "http.request")
    assert requests and requests[0]["data"]["account"] == journal.tag("acc-a")


def test_a_request_refused_before_any_account_is_chosen_has_no_account_tag(relay_pool):
    call = relay_pool(TWO)
    park(call.runtime)
    for stream in (False, True):
        status, _, headers = call("/v1/chat/completions", dict(CHAT, stream=stream))
        assert status == 429
        for kind in ("http.error", "http.request"):
            events = logged(headers["x-request-id"], kind)
            assert events and all("account" not in event["data"] for event in events), (kind, stream)


def test_tag_helper_never_exposes_the_identity_and_survives_missing_config():
    from accounts import account_tag, cfg_tag

    assert cfg_tag({"account_id": ACCOUNT, "page_uuid": PAGE}) == TAG
    assert cfg_tag({"page_uuid": PAGE}) == journal.tag(PAGE)
    assert cfg_tag({}) is None and cfg_tag(None) is None
    assert account_tag(None) is None and account_tag(SimpleNamespace(cfg=None)) is None
    assert account_tag(SimpleNamespace(cfg={"account_id": ACCOUNT})) == TAG
    assert ACCOUNT not in TAG and len(TAG) == 8


def test_the_default_says_what_an_account_without_identity_is_called():
    from accounts import UNNAMED, account_tag, cfg_tag

    assert UNNAMED == "account"
    assert cfg_tag({}, UNNAMED) == account_tag(None, UNNAMED) == account_tag(SimpleNamespace(cfg=None), UNNAMED) == "account"
    # An identity wins over the default, whatever it is.
    assert cfg_tag({"account_id": ACCOUNT}, "x") == TAG and account_tag(SimpleNamespace(cfg={"page_uuid": PAGE}), "x") == journal.tag(PAGE)


IDENTITIES = [({"account_id": ACCOUNT, "page_uuid": PAGE}, TAG), ({"page_uuid": PAGE}, journal.tag(PAGE)), ({}, None)]


@pytest.mark.parametrize("cfg,tag", IDENTITIES)
def test_every_surface_names_an_account_through_the_one_helper(cfg, tag):
    import account_report
    import admin
    from accounts import AccountDispatcher
    from session import SessionPool
    from test_account_dispatcher import Account, Clock

    label = tag or "account"
    account = Account("x")
    account.cfg = dict(cfg)

    class Manager:
        def get_balance(self, force_refresh=False):
            return {"available": 3.0, "monthly_allocation": 500.0}

        def balance_snapshot(self):
            return {"available": 3.0}

        def usage(self):
            return {"session_used": 0.0, "turns": 0, "recent": []}

    manager = Manager()
    dispatcher = AccountDispatcher([account], {"session_refresh_interval": 0}, {account: manager}, clock=Clock())
    runtime = SimpleNamespace(accounts=dispatcher, backend=account, credits=manager, cfg={})

    # What a person reads: reports, status rows and messages call it by the tag, or by one placeholder.
    assert dispatcher.status()[0]["account"] == label
    assert account_report.balance_report(account_report.collect(runtime))["accounts"][0]["account"] == label
    assert account_report.usage_report(runtime)["accounts"][0]["account"] == label
    assert admin.Admin._record_tag(cfg) == label
    account.unavailable = {"reason": "exhausted", "available": 3.0, "threshold": 20.0}
    assert "%s: 3.0" % label in SessionPool._unusable_error([account]).message

    # What a journal field holds: the tag, or nothing. The placeholder never lands in an event.
    before = journal.JOURNAL.last_seq
    account.unavailable = None
    dispatcher.mark_rate_limited(account)
    events = [e for e in journal.JOURNAL.query(since=before) if e["kind"] == "account.rate_limited"]
    assert len(events) == 1 and events[0]["data"].get("account") == tag


def test_the_identity_rule_is_written_once():
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    sources = [*root.glob("*.py"), *(root / "scripts").glob("*.py")]
    offenders = [path.name for path in sources if path.name != "accounts.py"
                 and re.search(r"journal\.tag\([^)\n]*(account_id|page_uuid)", path.read_text(encoding="utf-8"))]
    assert offenders == []
