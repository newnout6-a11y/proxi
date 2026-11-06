"""Journal events name the account that served them: a short tag, never a raw ID."""
import asyncio
import io
import json
import urllib.error
from types import SimpleNamespace

import direct
import journal
from keepwarm_harness import backend as warm_backend, drive, session as warm_session
from test_native_context_reuse import FIRST, Backend, request, run, setup

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


def test_tag_helper_never_exposes_the_identity_and_survives_missing_config():
    from accounts import account_tag, cfg_tag

    assert cfg_tag({"account_id": ACCOUNT, "page_uuid": PAGE}) == TAG
    assert cfg_tag({"page_uuid": PAGE}) == journal.tag(PAGE)
    assert cfg_tag({}) is None and cfg_tag(None) is None
    assert account_tag(None) is None and account_tag(SimpleNamespace(cfg=None)) is None
    assert account_tag(SimpleNamespace(cfg={"account_id": ACCOUNT})) == TAG
    assert ACCOUNT not in TAG and len(TAG) == 8
