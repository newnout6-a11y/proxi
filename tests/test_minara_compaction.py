"""Compaction on Minara: a side request's tail is not cached, a Codex compaction reply is text, the dialog's effort is
kept, and only a real change of the head is said."""
import asyncio
import types

import client_context
import journal
import minara
import session as session_module
from test_minara_selector import Response, stream_chunks

HEAD = "You are Codex. " * 200
TOOLS = [{'type': 'function', 'function': {'name': 'shell', 'parameters': {'type': 'object', 'properties': {}}}}]


def marked(body):
    return [index for index, message in enumerate(body['messages']) if isinstance(message.get('content'), list)
            and any('cache_control' in part for part in message['content'] if isinstance(part, dict))]


def conversation():
    return types.SimpleNamespace(_native_allow_cold_replay=True, _native_client_messages=[
        {'role': 'user', 'content': [{'type': 'text', 'text': 'work'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'done'}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Summarize the conversation.'}]}])


def test_a_side_requests_tail_gets_no_mark_and_the_dialogs_prefix_is_still_read():
    provider = {'model': 'anthropic/claude-opus-5.5'}
    full = minara.build_body({}, provider, HEAD, '', None, None, 100, session=conversation())
    side = minara.build_body({}, provider, HEAD, '', None, None, 100, session=conversation(), mark_tail=False)
    assert marked(full) == [0, 1, 3] and marked(side) == [0, 1]  # head and the previous turn's end, not the tail


def run_compaction(answers, task='compaction', restored='restored'):
    posts = []

    def post(*args, **kwargs):
        posts.append(kwargs['json'])
        return answers.pop(0)
    session = types.SimpleNamespace(backend=types.SimpleNamespace(_minara_post=post), _cancelled=False,
                                    _native_allow_cold_replay=True, _native_client_messages=conversation()._native_client_messages)
    token = client_context.bind({'client': 'codex', 'client_task': task, 'compaction_tools': restored})
    try:
        events = asyncio.run(_collect(session))
    finally:
        client_context._CURRENT.reset(token)
    return events, posts


async def _collect(session):
    return [event async for event in minara.iter_events({'bearer_token': 't'}, session, {'model': 'openai/gpt-6.1-sol'},
                                                        'User: work\nAssistant: done', HEAD, TOOLS, 'auto', 100)]


def call(name='shell'):
    return Response(200, lines=stream_chunks({'tool_calls': [{'index': 0, 'id': 'c1', 'function': {
        'name': name, 'arguments': '{}'}}]}, finish='tool_calls'))


def text(words='The summary.'):
    return Response(200, lines=stream_chunks({'content': words}))


def test_a_codex_compaction_that_calls_a_tool_is_asked_again_and_then_sent_without_tools():
    before = journal.JOURNAL.last_seq
    events, posts = run_compaction([call(), call(), text('Summary at last.')])
    assert [e['delta'] for e in events if e['type'] == 'text'] == ['Summary at last.']
    assert not any(e['type'] == 'tool_call' for e in events)
    assert len(posts) == 3 and 'tools' in posts[0] and 'tools' in posts[1] and 'tools' not in posts[2]
    assert posts[1]['messages'][-1]['content'].startswith('IMPORTANT: your next message must be plain text')
    misses = [e for e in journal.JOURNAL.query(since=before, limit=50) if e['kind'] == 'turn.compaction_tool_call']
    assert len(misses) == 2


def test_a_codex_compaction_answered_in_text_goes_through_at_once_and_other_requests_are_untouched():
    events, posts = run_compaction([text()])
    assert [e['delta'] for e in events if e['type'] == 'text'] == ['The summary.'] and len(posts) == 1
    events, posts = run_compaction([call()], task='')  # not a compaction: the call is the reply
    assert any(e['type'] == 'tool_call' for e in events) and len(posts) == 1


def test_a_change_of_the_attribution_line_alone_is_no_head_change():
    relay_session = session_module.RelaySession.__new__(session_module.RelaySession)
    relay_session._cached_head, relay_session._direct, relay_session._registered_tools = None, None, {}
    relay_session.thread_id = 't'
    before = journal.JOURNAL.last_seq
    first = 'x-anthropic-billing-header: cc_version=2.1.317.c5b; cch=aaa;\nYou are Claude Code.'
    relay_session.set_direct(first, None, None, 100)
    relay_session.set_direct(first.replace('c5b; cch=aaa', '29f; cch=bbb'), None, None, 100)
    assert not [e for e in journal.JOURNAL.query(since=before, limit=50) if e['kind'] == 'cache.head_changed']
    relay_session.set_direct('x-anthropic-billing-header: cc_version=1; cch=c;\nYou are another agent.', None, None, 100)
    assert [e for e in journal.JOURNAL.query(since=before, limit=50) if e['kind'] == 'cache.head_changed']


def test_a_minara_compaction_keeps_the_dialogs_effort_unless_it_would_lower_the_clients():
    import orchestrator
    runner = types.SimpleNamespace(cfg={'reasoning_effort': 'auto'})
    entry = {'id': 'minara/claude-opus-4-7', 'upstream': 'minara', 'provider': 'anthropic', 'reasoning': True,
             'reasoning_effort_levels': ['low', 'medium', 'high'], 'reasoning_effort_control': 'mapped',
             'effort_map': {'none': 'low', 'minimal': 'low', 'low': 'low', 'medium': 'medium', 'high': 'high',
                            'xhigh': 'high', 'max': 'high'}, 'default_reasoning_effort': 'high'}
    method = orchestrator.TurnRunner._minara_dialog_effort

    def parsed(effort, model='minara/claude-opus-4-7'):
        return types.SimpleNamespace(entry=entry, effort=effort, model=model)
    dialog = types.SimpleNamespace(_minara_effort='high', _minara_effort_set=True, _minara_effort_model='minara/claude-opus-4-7')
    assert method(runner, parsed('low'), dialog) == 'high'  # the compaction asked low: the dialog's high is kept
    assert method(runner, parsed('high'), dialog) is None  # the same: nothing to change
    low_dialog = types.SimpleNamespace(_minara_effort='low', _minara_effort_set=True,
                                       _minara_effort_model='minara/claude-opus-4-7')
    assert method(runner, parsed('high'), low_dialog) is None  # never lowered to save a cache
    assert method(runner, parsed('low'), types.SimpleNamespace()) is None  # a dialog with no turn recorded
    # a compaction on another model never takes the dialog's level raw (its own map may send it as another)
    assert method(runner, parsed('low', model='minara/claude-haiku-5-5'), dialog) is None
