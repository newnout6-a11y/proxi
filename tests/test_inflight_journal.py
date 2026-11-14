"""session.acquire says how many turns the pool had in flight when the turn began, on its account and in all.

The journal never said: a per-account concurrency had to be rebuilt from turn.begin and turn.end of every turn (2026-11-07: peak
28-32 turns in all during the test agents' bursts, at most 3 on one account in the 68-conversation burst)."""
import asyncio
import io
import json

import direct
import journal
from test_journal_account_tags import ACCOUNT, TAG, served
from test_native_context_reuse import FIRST, Backend, request, run, setup


class Response(io.BytesIO):
    status = 200
    headers = {"request-id": "upstream-id"}


def answer(*_args, **_kwargs):
    return Response(b"\n".join(json.dumps(event).encode() for event in (
        {"event": "content_block_delta", "type": "text", "partialText": "OK"},
        {"event": "metadata", "usage": {"inputTokens": 4, "outputTokens": 3, "creditsUsed": 0.25}},
        {"event": "end_of_stream"})) + b"\n")


def acquired(events):
    return [event for event in events if event["kind"] == "session.acquire"]


def test_a_lone_turn_meets_itself_only(tmp_path, monkeypatch):
    _, journaled = served(tmp_path, monkeypatch, answer)
    (event,) = acquired(journaled)
    assert (event["data"]["inflight_account"], event["data"]["inflight_total"]) == (1, 1)


def test_a_turn_counts_the_turns_already_running_on_its_account_and_on_the_others(tmp_path, monkeypatch):
    _, pool, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[],
                            base="https://example.invalid", cache_keepwarm_seconds=0, account_id=ACCOUNT)
    first = pool.backends[0]
    first.headers = lambda: {"cookie": "synthetic"}
    other = Backend(tmp_path, provider_resource_name="other", account_id="acc-inflight-b")
    other.headers = lambda: {"cookie": "synthetic"}
    pool.backends.append(other)
    monkeypatch.setattr(direct.urllib.request, "urlopen", answer)
    before = journal.JOURNAL.last_seq

    async def scenario():
        held = [await pool.acquire("held-one"), await pool.acquire("held-two"), await pool.acquire("held-three")]
        on_first = sum(1 for lease in held if lease.backend is first)
        await run(runner, request(FIRST, key="header:the-turn"))
        for lease in held:
            await pool.release(lease)
        return on_first

    on_first = asyncio.run(scenario())
    (event,) = acquired(journal.JOURNAL.query(since=before, limit=500))
    named_first = event["data"]["account"] == TAG
    assert event["data"]["inflight_total"] == 4
    assert event["data"]["inflight_account"] == 1 + (on_first if named_first else 3 - on_first)
    assert 1 <= event["data"]["inflight_account"] <= 4
    assert pool.inflight(first) == (0, 0)  # everything was released
