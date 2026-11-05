"""Cold/restarted contexts retain native tool roles without trusting signatures."""
import asyncio
import copy
import json

import pytest

import native_history
from test_native_history import close, harness, parsed
from test_native_context_reuse import FIRST, run
from test_review_fixes_pr19 import text, END


def transcript():
    return [{'role': 'user', 'content': [{'type': 'text', 'text': 'Read the marker.'}]},
            {'role': 'assistant', 'content': [{'type': 'tool_call', 'toolCallId': 'call-1',
                                             'toolName': 'read_file', 'toolInput': {'path': 'marker'}}]},
            {'role': 'tool', 'content': {'toolCallId': 'call-1', 'toolResult': 'RESULT-MARKER'}}]


def test_canonical_cold_transcript_is_copied_and_preserves_native_result_roles():
    client = transcript()
    result = native_history.unsigned_tool_transcript(client)
    assert result == client and result is not client
    result[-1]['content']['toolResult'] = 'changed'
    assert client[-1]['content']['toolResult'] == 'RESULT-MARKER'


def test_complete_parallel_batch_accepts_reordered_results_and_multiple_tool_rounds():
    client = transcript()
    client[1]['content'].append({'type': 'tool_call', 'toolCallId': 'call-2',
                                 'toolName': 'read_file', 'toolInput': {'path': 'other'}})
    client.insert(2, {'role': 'tool', 'content': {'toolCallId': 'call-2', 'toolResult': 'OTHER'}})
    client += [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'Both read.'}]},
               {'role': 'user', 'content': [{'type': 'text', 'text': 'Continue.'}]}]
    assert native_history.unsigned_tool_transcript(client) == client


@pytest.mark.parametrize('case', ['no-results', 'orphan', 'duplicate', 'unfinished', 'interleaved', 'thinking'])
def test_incomplete_or_noncanonical_cold_histories_do_not_create_native_replay(case):
    client = transcript()
    if case == 'no-results': client = client[:1]
    elif case == 'orphan': client = client[:1] + client[-1:]
    elif case == 'duplicate': client.append(copy.deepcopy(client[-1]))
    elif case == 'unfinished': client = client[:-1]
    elif case == 'interleaved': client.insert(2, copy.deepcopy(client[0]))
    elif case == 'thinking': client[1]['content'].insert(0, {'type': 'thinking', 'signature': 'UNTRUSTED'})
    assert native_history.unsigned_tool_transcript(client) is None


@pytest.mark.parametrize('dialect', ['chat', 'anthropic', 'responses'])
def test_first_tool_followup_on_a_new_session_rebuilds_roles_without_client_thinking(tmp_path, monkeypatch, dialect):
    from test_native_history import append_result
    # Normalize each real dialect via its adapter, then send only the follow-up
    # through a fresh pool: no saved signed branch exists in this process.
    first = parsed(copy.deepcopy(FIRST), dialect)
    normalized = [{'type': 'tool_call', 'id': 'call-1', 'name': 'read_file', 'arguments': {'path': 'marker'}}]
    messages = append_result(copy.deepcopy(FIRST), first, normalized, dialect, value='RESULT-MARKER')
    pool, runner, sent = harness(tmp_path, monkeypatch, [[text('RESULT-MARKER'), *END]])
    current = parsed(messages, dialect)
    async def scenario():
        try:
            return await run(runner, current)
        finally:
            await close(pool)
    events = asyncio.run(scenario())
    assert any(e.get('delta') == 'RESULT-MARKER' for e in events)
    wire = sent[0]['messages']
    assistant = next(m for m in wire if m['role'] == 'assistant')
    result = next(m for m in wire if m['role'] == 'tool')
    assert assistant['content'][0]['type'] == 'tool_call'
    assert result['content']['toolResult'] == 'RESULT-MARKER'
    assert result['content']['toolCallId'] == assistant['content'][0]['toolCallId']
    assert not any(b.get('type') in ('thinking', 'redacted_thinking') for m in wire
                   for b in m.get('content', []) if isinstance(b, dict))


def test_client_supplied_thinking_is_removed_before_cold_replay(tmp_path, monkeypatch):
    from test_native_history import append_result
    first = parsed(copy.deepcopy(FIRST), 'anthropic')
    messages = append_result(copy.deepcopy(FIRST), first, [
        {'type': 'tool_call', 'id': 'call-1', 'name': 'read_file', 'arguments': {'path': 'marker'}}],
        'anthropic', value='RESULT-MARKER')
    messages[-2]['content'].insert(0, {'type': 'thinking', 'thinking': 'UNTRUSTED-THINKING',
                                      'signature': 'UNTRUSTED-SIGNATURE'})
    pool, runner, sent = harness(tmp_path, monkeypatch, [[text('RESULT-MARKER'), *END]])
    async def scenario():
        try:
            return await run(runner, parsed(messages, 'anthropic'))
        finally:
            await close(pool)
    asyncio.run(scenario())
    wire = json.dumps(sent[0])
    assert 'UNTRUSTED' not in wire
    assert any(message['role'] == 'tool' for message in sent[0]['messages'])
