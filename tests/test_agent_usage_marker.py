"""The legacy agent transport reports an attempt that gave content but no price the way the direct one does,
with an empty usage marker, so the journal and the client's adapter agree that the attempt is unpriced."""
import asyncio
import json

import pytest

import adapters
import direct
import journal
from test_native_context_reuse import FIRST, request, run, setup
from test_session_stream import collect, make

MARKER = {'type': 'usage', 'usage': {}}
TEXT = {'type': 'text', 'messageId': 'a', 'content': 'hello'}
DONE = {'type': 'agent_loop_done', 'outcome': 'success'}
PRICE = {'type': 'run_credits_persisted', 'creditsUsed': 0.25}


def kinds(events):
    return [event['type'] for event in events]


def test_content_without_a_price_gets_the_empty_marker_before_the_end():
    events = collect(make([TEXT, DONE]))
    assert events.count(MARKER) == 1
    assert kinds(events)[-2:] == ['usage', 'end'] and events[-1] == {'type': 'end', 'reason': 'stop'}


@pytest.mark.parametrize('frames', [
    [TEXT, PRICE, DONE],
    [TEXT, DONE, PRICE],  # the price arrives after agent_loop_done, inside the trailing wait
])
def test_a_price_means_no_marker(frames):
    events = collect(make(frames))
    assert MARKER not in events
    assert [event['value'] for event in events if event['type'] == 'credits'] == [0.25]


@pytest.mark.parametrize('frames,tool_names', [
    ([{'type': 'thinking', 'messageId': 't', 'content': 'hmm'}, DONE], None),
    ([{'type': 'tool_call', 'toolCallId': 'c1', 'toolName': 'bash', 'partialInputs': '{"command":"dir"}'}, DONE], ['bash']),
])
def test_thinking_or_a_tool_call_is_content_too(frames, tool_names):
    assert collect(make(frames), tool_names).count(MARKER) == 1


def test_a_turn_that_produced_nothing_stays_free():
    assert MARKER not in collect(make([DONE]))


def test_a_timeout_after_content_is_reported_unpriced_as_well():
    session = make([TEXT])
    session.backend.cfg['turn_timeout'] = 0.3
    events = collect(session)
    assert kinds(events)[-2:] == ['usage', 'end'] and events[-1]['reason'] == 'timeout'


def test_the_direct_transport_keeps_its_own_markers(monkeypatch):
    async def iter_events(*args, **kwargs):
        yield {'type': 'text', 'delta': 'hi'}
        yield {'type': 'end', 'reason': 'stop'}

    monkeypatch.setattr(direct, 'iter_events', iter_events)
    session = make([])
    session.cfg = dict(session.cfg, transport='direct')
    events = collect(session)
    assert MARKER not in events


def test_journal_and_adapter_agree_on_an_unpriced_agent_attempt(tmp_path, monkeypatch):
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    backend, _, runner = setup(tmp_path)
    backend.scripts.append([TEXT, DONE])  # text, no price
    parsed = request(FIRST, tools=[])
    events = asyncio.run(run(runner, parsed))
    adapter = adapters.ChatAdapter(parsed)
    for event in events:
        if event['type'] not in ('end', 'error'):
            adapter.feed(event)
    usage = adapter.payload('stop')['usage']
    assert (usage['credits_used'], usage['credits_complete']) == (None, False)
    totals = log.metrics()['totals']
    assert totals['credits_unknown'] == 1 and totals['credits_complete'] is False
    trace = log.turns()[0]
    assert (trace['credits'], trace['credits_complete'], trace['credits_unknown']) == (None, False, 1)


def test_a_priced_agent_attempt_is_complete_in_both(tmp_path, monkeypatch):
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    backend, _, runner = setup(tmp_path)
    backend.scripts.append([TEXT, PRICE, DONE])
    parsed = request(FIRST, tools=[])
    events = asyncio.run(run(runner, parsed))
    adapter = adapters.ChatAdapter(parsed)
    for event in events:
        if event['type'] not in ('end', 'error'):
            adapter.feed(event)
    usage = adapter.payload('stop')['usage']
    assert (usage['credits_used'], usage['credits_complete']) == (0.25, True)
    totals = log.metrics()['totals']
    assert totals['credits'] == 0.25 and totals['credits_complete'] is True and totals['credits_unknown'] == 0
    assert json.dumps(log.turns()[0]['credits']) == '0.25'
