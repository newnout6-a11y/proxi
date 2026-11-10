"""Parallel Minara requests with one uncached Claude head: the first writes it, the others wait for its answer to begin
and read it (Claude Code starts several subagents at once; each wrote the whole head at the 1h price before)."""
import asyncio
import threading
import time
import types
import uuid

import journal
import minara
from test_minara_selector import Response, stream_chunks

HEAD = "You are a careful agent. " * 200  # about 5k characters: a head big enough to be cached


def provider():
    return {'model': 'anthropic/claude-opus-5.5', 'upstream_model': 'anthropic/claude-opus-5.5'}


async def one(cfg, session, head, tools):
    return [event async for event in minara.iter_events(cfg, session, provider(), 'hi', head, tools, None, 50)]


def test_parallel_requests_with_one_head_wait_for_the_first_answer_to_begin():
    tools = [{'type': 'function', 'function': {'name': 'probe_%s' % uuid.uuid4().hex[:8], 'parameters': {
        'type': 'object', 'properties': {}}}}]  # a head of its own: no other test shares the gate
    release = threading.Event()
    posted = []

    def post(*args, **kwargs):
        posted.append(time.monotonic())
        if len(posted) == 1:
            release.wait(5)  # the leader's answer begins only when the test lets it
        return Response(200, lines=stream_chunks({'content': 'OK'}))
    sessions = [types.SimpleNamespace(backend=types.SimpleNamespace(_minara_post=post), _cancelled=False)
                for _ in range(3)]
    cfg = {'bearer_token': 'secret'}
    before = journal.JOURNAL.last_seq

    async def scenario():
        tasks = [asyncio.create_task(one(cfg, session, HEAD, tools)) for session in sessions]
        await asyncio.sleep(0.5)
        assert len(posted) == 1  # the other two wait for the leader instead of writing the head themselves
        release.set()
        return await asyncio.gather(*tasks)
    results = asyncio.run(scenario())
    assert all([e['delta'] for e in events if e['type'] == 'text'] == ['OK'] for events in results)
    assert len(posted) == 3
    waits = [e for e in journal.JOURNAL.query(since=before, limit=200) if e['kind'] == 'cache.head_wait']
    assert len(waits) == 2 and {e['data']['outcome'] for e in waits} == {'leader_started'}


def test_a_small_head_and_a_failed_leader_never_hold_anyone():
    assert minara._head_key(minara.build_body({}, provider(), 'Be brief.', 'hi', None, None, 50)) is None
    assert minara._head_key(minara.build_body({}, {'model': 'openai/gpt-6.1-sol'}, HEAD, 'hi', None, None, 50)) is None
    tools = [{'type': 'function', 'function': {'name': 'fail_%s' % uuid.uuid4().hex[:8], 'parameters': {
        'type': 'object', 'properties': {}}}}]
    session = types.SimpleNamespace(backend=types.SimpleNamespace(
        _minara_post=lambda *a, **kw: Response(400, b'{"error":{"message":"bad schema"}}')), _cancelled=False)
    try:
        asyncio.run(one({'bearer_token': 'secret'}, session, HEAD, tools))
    except Exception:  # noqa: BLE001 - the refusal itself is not what is tested
        pass
    ok = types.SimpleNamespace(backend=types.SimpleNamespace(
        _minara_post=lambda *a, **kw: Response(200, lines=stream_chunks({'content': 'OK'}))), _cancelled=False)
    started = time.monotonic()
    asyncio.run(one({'bearer_token': 'secret'}, ok, HEAD, tools))
    assert time.monotonic() - started < 2  # the failed leader released the head: the next request leads at once


def test_a_follower_that_stops_waiting_never_releases_its_leaders_gate():
    import direct
    key = 'test-gate-' + uuid.uuid4().hex
    now = time.monotonic()
    role, gate = direct._claim_head(key, now, owner=1, warm=minara.HEAD_WARM)
    assert role == 'lead'
    assert direct._claim_head(key, now, owner=2, warm=minara.HEAD_WARM) == ('follow', gate)
    # a follower that ends without an answer: the transport releases only a gate it leads
    assert direct._claim_head(key, now + 1, owner=3, warm=minara.HEAD_WARM)[0] == 'follow'
    direct._head_started(gate, key)
    # an hour-long cache: a head used 40 minutes ago is still read, nobody waits
    assert direct._claim_head(key, now + 2400, owner=4, warm=minara.HEAD_WARM)[0] == 'cached'
    assert direct._claim_head(key, now + 2400, owner=4)[0] == 'lead'  # Retool's 270 s window would lead again
