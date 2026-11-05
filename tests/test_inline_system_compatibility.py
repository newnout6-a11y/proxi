"""Retool accepts one system message; later reminders keep their place as user text."""
import asyncio
import copy
import json

import pytest

import journal
import native_history
import validate
from apierr import ApiError
from test_native_history import append_result, close, harness, parsed, strip_cache, tool_reply
from test_native_context_reuse import FIRST, run
from test_review_fixes_pr19 import END, text

MARKER = 'MID_SYSTEM_KEEP_THIS_91'
DIALECTS = ['chat', 'responses', 'anthropic']
CFG = {'strict_models': True}


def parse(dialect, messages, *, system=None, **extra):
    """A minimal request of the given dialect; `system` is its client-level instruction."""
    data = {'model': 'claude-opus-5-5', 'max_tokens': 2048, **extra}
    if dialect == 'chat':
        data['messages'] = ([{'role': 'system', 'content': system}] if system else []) + copy.deepcopy(messages)
    elif dialect == 'responses':
        data.update(input=copy.deepcopy(messages), **({'instructions': system} if system else {}))
    else:
        data.update(messages=copy.deepcopy(messages), **({'system': system} if system else {}))
    return getattr(validate, 'parse_' + dialect)(data, CFG)


def user_content(body):
    return json.dumps([m for m in body['messages'] if m['role'] == 'user'], ensure_ascii=False)


@pytest.mark.parametrize('dialect', DIALECTS)
@pytest.mark.parametrize('shape', ['text', 'blocks'])
def test_reminder_after_a_tool_round_trip_keeps_head_signature_and_cache_key(tmp_path, monkeypatch, dialect, shape):
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), tool_reply('c2', 'opaque-two'),
                                                        [text('done'), *END]])
    reminder = (MARKER + '. Reply in Russian.' if shape == 'text' else
                [{'type': 'text', 'text': MARKER + '. Reply in Russian.', 'cache_control': {'type': 'ephemeral'}}])

    async def scenario():
        messages = copy.deepcopy(FIRST)
        try:
            first = parsed(messages, dialect)
            messages = append_result(messages, first, await run(runner, first), dialect)
            messages.append({'role': 'system', 'content': reminder})
            second = parsed(messages, dialect)
            assert second.system_messages_normalized is True and MARKER not in second.system
            messages = append_result(messages, second, await run(runner, second), dialect)
            last = await run(runner, parsed(messages, dialect))
            assert any(event.get('delta') == 'done' for event in last)
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 3
    heads = [strip_cache(body['messages'][0]) for body in sent]
    assert heads[0] == heads[1] == heads[2] and heads[0]['role'] == 'system'
    for body in sent:
        assert sum(message['role'] == 'system' for message in body['messages']) == 1
        assert MARKER not in json.dumps(body['messages'][0])
    for body in sent[1:]:
        assert MARKER in user_content(body)
        assert body['openAICacheKey'] == sent[0]['openAICacheKey']
    # The reminder sits right after the tool result it followed, then stays put.
    assert [m['role'] for m in sent[1]['messages'][-2:]] == ['tool', 'user']
    assert MARKER in json.dumps(sent[1]['messages'][-1])
    assert 'opaque-one' in json.dumps(sent[1]) and 'opaque-two' in json.dumps(sent[2])
    assert strip_cache(sent[1]['messages'][:len(sent[0]['messages'])]) == strip_cache(sent[0]['messages'])
    assert strip_cache(sent[2]['messages'][:len(sent[1]['messages'])]) == strip_cache(sent[1]['messages'])
    kinds = {event['kind'] for event in log.query()}
    assert 'native_history.resume' in kinds
    assert not kinds & {'native_history.reset', 'cache.head_changed', 'cache.key_changed'}


@pytest.mark.parametrize('dialect', DIALECTS)
def test_inline_text_order_is_retained_in_place_and_the_head_stays_the_client_system(dialect):
    messages = [{'role': 'user', 'content': 'start'}, {'role': 'system', 'content': 'FIRST_UPDATE'},
                {'role': 'assistant', 'content': 'ready'}, {'role': 'developer', 'content': 'SECOND_UPDATE'}]
    request = parse(dialect, messages, system='GLOBAL')
    assert request.system == 'GLOBAL'
    assert request.message_chunks == [
        'User: start', native_history.reminder('FIRST_UPDATE'), 'Assistant: ready',
        native_history.reminder('SECOND_UPDATE')]
    assert [m['role'] for m in request.native_messages] == ['user', 'user', 'assistant', 'user']
    assert request.native_messages[1]['content'] == [{'type': 'text', 'text': native_history.reminder('FIRST_UPDATE')}]
    assert request.prompt.index('FIRST_UPDATE') < request.prompt.index('ready') < request.prompt.index('SECOND_UPDATE')


@pytest.mark.parametrize('dialect', DIALECTS)
def test_only_leading_instructions_join_the_head_in_order(dialect):
    messages = [{'role': 'system', 'content': 'LEAD_ONE'}, {'role': 'developer', 'content': 'LEAD_TWO'},
                {'role': 'user', 'content': 'start'}, {'role': 'system', 'content': 'LATER'}]
    if dialect == 'anthropic':
        request = parse(dialect, messages, system='GLOBAL')
        assert request.system == 'GLOBAL\n\nLEAD_ONE\n\nLEAD_TWO'
    else:
        request = parse(dialect, messages)
        assert request.system == 'LEAD_ONE\n\nLEAD_TWO'
    assert 'LATER' not in request.system and 'LATER' in request.prompt
    assert request.message_chunks[0] == 'User: start'


@pytest.mark.parametrize('dialect', DIALECTS)
def test_reminders_are_never_merged_so_earlier_requests_stay_a_prefix(dialect):
    first_messages = [{'role': 'user', 'content': 'start'}, {'role': 'system', 'content': 'R1'}]
    later = first_messages + [{'role': 'user', 'content': 'next'}, {'role': 'system', 'content': 'R2'},
                              {'role': 'system', 'content': 'R3'}]
    first, second = parse(dialect, first_messages, system='G'), parse(dialect, later, system='G')
    assert second.message_chunks[:len(first.message_chunks)] == first.message_chunks
    assert second.native_messages[:len(first.native_messages)] == first.native_messages
    assert second.message_fingerprints[:len(first.message_fingerprints)] == first.message_fingerprints
    assert second.prompt.startswith(first.prompt)
    # Consecutive reminders and user turns stay separate messages.
    assert [m['role'] for m in second.native_messages] == ['user'] * 5
    assert len(second.message_chunks) == 5


@pytest.mark.parametrize('dialect', DIALECTS)
def test_a_reminder_that_already_carries_its_tags_is_not_wrapped_twice(dialect):
    tagged = '<system-reminder>\nTagged by the client.\n</system-reminder>'
    request = parse(dialect, [{'role': 'user', 'content': 'a'}, {'role': 'system', 'content': tagged}])
    assert request.message_chunks[1] == tagged
    assert request.native_messages[1]['content'][0]['text'] == tagged


@pytest.mark.parametrize('dialect', DIALECTS)
def test_a_blank_reminder_leaves_no_trace_in_the_history(dialect):
    messages = [{'role': 'user', 'content': 'a'}, {'role': 'system', 'content': '  \n'},
                {'role': 'assistant', 'content': 'b'}]
    request = parse(dialect, messages)
    assert request.message_chunks == ['User: a', 'Assistant: b']
    assert [m['role'] for m in request.native_messages] == ['user', 'assistant']
    assert request.system_messages_normalized is False


@pytest.mark.parametrize('dialect', DIALECTS)
@pytest.mark.parametrize('count', [1, 4])
def test_empty_effort_entries_do_not_change_the_head_and_the_last_effort_wins(dialect, count):
    plain = [{'role': 'user', 'content': 'hello'}, {'role': 'assistant', 'content': 'hi'},
             {'role': 'user', 'content': 'again'}]
    efforts = ['low', 'medium', 'high', 'low'][:count]
    entries = [{'role': 'system', 'content': [], 'output_config': {'effort': effort}} for effort in efforts]
    interleaved = [plain[0], entries[0], plain[1], plain[2], *entries[1:]]
    # One entry may also precede the whole conversation.
    leading = [entries[0], *plain, *entries[1:]]
    base = parse(dialect, plain, system='GLOBAL')
    for messages in (interleaved, leading):
        request = parse(dialect, messages, system='GLOBAL')
        assert request.system == base.system == 'GLOBAL'
        assert request.message_chunks == base.message_chunks
        assert request.native_messages == base.native_messages
        assert request.system_messages_normalized is False
        assert request.effort == request.requested_effort == efforts[-1]
    assert base.effort is None


@pytest.mark.parametrize('dialect', DIALECTS)
def test_effort_entries_alone_do_not_invent_a_system_prompt(dialect):
    entries = [{'role': 'system', 'content': [], 'output_config': {'effort': 'low'}},
               {'role': 'user', 'content': 'hello'},
               {'role': 'system', 'content': [], 'output_config': {'effort': 'medium'}}]
    request = parse(dialect, entries)
    assert request.system == '' and request.effort == 'medium'


@pytest.mark.parametrize('dialect', DIALECTS)
def test_blank_system_text_is_an_empty_system(dialect):
    messages = [{'role': 'system', 'content': ' \n\t'}, {'role': 'user', 'content': 'hello'}]
    if dialect == 'anthropic':
        request = parse(dialect, messages, system='  ')
    elif dialect == 'responses':
        request = parse(dialect, messages, system='\n')
    else:
        request = parse(dialect, messages)
    assert request.system == ''


@pytest.mark.parametrize('dialect', DIALECTS)
def test_inline_effort_update_is_not_discarded_with_an_empty_system_message(dialect):
    messages = [{'role': 'user', 'content': 'hello'},
                {'role': 'system', 'content': '', 'output_config': {'effort': 'low'}}]
    request = parse(dialect, messages, output_config={'effort': 'high'})
    assert request.effort == request.requested_effort == 'low'
    assert 'System:' not in request.prompt and request.message_chunks == ['User: hello']


@pytest.mark.parametrize('dialect', DIALECTS)
def test_inline_effort_still_applies_when_the_same_entry_carries_text(dialect):
    messages = [{'role': 'user', 'content': 'hello'},
                {'role': 'system', 'content': MARKER, 'output_config': {'effort': 'low'}}]
    request = parse(dialect, messages, output_config={'effort': 'high'})
    assert request.effort == 'low' and request.system_messages_normalized is True
    assert MARKER in request.prompt and MARKER not in request.system


@pytest.mark.parametrize('case,flag', [
    ('leading-only', False), ('blank-effort-only', False), ('blank-text', False),
    ('reminder', True), ('reminder-and-effort', True), ('reminder-then-blank', True)])
@pytest.mark.parametrize('dialect', DIALECTS)
def test_normalization_flag_names_only_reminders_that_were_really_converted(dialect, case, flag):
    effort = {'role': 'system', 'content': [], 'output_config': {'effort': 'low'}}
    messages = {
        'leading-only': [{'role': 'system', 'content': 'LEAD'}, {'role': 'user', 'content': 'a'}],
        'blank-effort-only': [{'role': 'user', 'content': 'a'}, effort],
        'blank-text': [{'role': 'user', 'content': 'a'}, {'role': 'system', 'content': ' '}],
        'reminder': [{'role': 'user', 'content': 'a'}, {'role': 'system', 'content': 'R'}],
        'reminder-and-effort': [{'role': 'user', 'content': 'a'}, effort, {'role': 'system', 'content': 'R'}],
        'reminder-then-blank': [{'role': 'user', 'content': 'a'}, {'role': 'system', 'content': 'R'}, effort],
    }[case]
    assert parse(dialect, messages).system_messages_normalized is flag


@pytest.mark.parametrize('block', [
    {'type': 'tool_removal', 'tool': {'type': 'tool_reference', 'name': 'Bash'}},
    {'type': 'tool_addition', 'tool': {'type': 'tool_definition', 'definition': {'name': 'db_query'}}}])
@pytest.mark.parametrize('position', ['later', 'leading'])
@pytest.mark.parametrize('dialect', DIALECTS)
def test_inline_tool_changes_are_rejected_instead_of_becoming_text_in_the_head(dialect, position, block):
    system = {'role': 'system', 'content': [{'type': 'text', 'text': 'Tools changed.'}, copy.deepcopy(block)]}
    messages = ([system] if position == 'leading' else []) + [{'role': 'user', 'content': 'hi'}]
    messages += [system] if position == 'later' else []
    with pytest.raises(ApiError) as caught:
        parse(dialect, messages)
    error = caught.value
    index = 0 if position == 'leading' else 1
    assert error.status == 422 and error.param == 'messages[%d].content[1]' % index
    assert block['type'] in error.message and "'tools'" in error.message


def test_http_inline_tool_change_is_a_clear_422_and_nothing_reaches_the_runner(relay):
    payload = {'model': 'claude-opus-5-5', 'max_tokens': 64, 'system': 'G', 'messages': [
        {'role': 'user', 'content': 'hi'},
        {'role': 'system', 'content': [{'type': 'tool_removal',
                                        'tool': {'type': 'tool_reference', 'name': 'Bash'}}]}]}
    status, body = relay.json_any('/v1/messages', payload)
    assert status == 422 and 'messages[1].content[0]' in body['error']['message']
    assert 'tool_removal' in body['error']['message'] and not relay.fake.seen


def test_http_header_and_journal_describe_reminders_without_warning(relay, monkeypatch):
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    base = {'model': 'claude-opus-5-5', 'max_tokens': 64, 'system': 'G'}
    quiet = {**base, 'messages': [{'role': 'user', 'content': 'hi'},
                                  {'role': 'system', 'content': [], 'output_config': {'effort': 'low'}}]}
    with relay.request('/v1/messages', quiet) as response:
        assert response.status == 200 and 'x-proxi-system-messages' not in response.headers
    assert not [e for e in log.query() if e['kind'] == 'request.compatibility']
    reminded = {**base, 'messages': [{'role': 'user', 'content': 'hi'}, {'role': 'system', 'content': MARKER}]}
    with relay.request('/v1/messages', reminded) as response:
        assert response.headers['x-proxi-system-messages'] == 'inline-as-user-text'
    entries = [e for e in log.query() if e['kind'] == 'request.compatibility']
    assert len(entries) == 1 and entries[0]['level'] != 'warn'
    assert entries[0]['data']['handling'] == 'inline_user_text' and MARKER not in json.dumps(entries)
    assert relay.fake.seen[-1].system == 'G' and MARKER in relay.fake.seen[-1].prompt


@pytest.mark.parametrize('dialect', DIALECTS)
def test_turn_scoped_system_lifetime_cannot_silently_become_persistent(dialect):
    data = {'model': 'claude-opus-5-5', 'max_tokens': 1024}
    messages = [{'role': 'user', 'content': 'start'},
                {'role': 'system', 'content': 'ONCE_ONLY', 'clear_at': 'next_user_message'},
                {'role': 'user', 'content': 'next'}]
    data['input' if dialect == 'responses' else 'messages'] = messages
    with pytest.raises(ApiError) as caught:
        getattr(validate, 'parse_' + dialect)(data, CFG)
    assert caught.value.status == 422 and 'clear_at' in caught.value.param


@pytest.mark.parametrize('model', ['gpt-6-sol', 'claude-opus-5-5'])
@pytest.mark.parametrize('thinking,effort', [({'type': 'disabled'}, 'low'),
    ({'type': 'enabled', 'budget_tokens': 1024}, 'high'), ({'type': 'adaptive', 'effort': 'high'}, 'low')])
def test_inline_effort_cannot_override_an_incompatible_explicit_thinking_control(model, thinking, effort):
    data = {'model': model, 'max_tokens': 2048, 'thinking': thinking,
            'messages': [{'role': 'user', 'content': 'hello'},
                         {'role': 'system', 'content': '', 'output_config': {'effort': effort}}]}
    with pytest.raises(ApiError) as caught:
        validate.parse_anthropic(data, CFG)
    assert caught.value.status == 422 and 'conflicting' in caught.value.message


def test_client_messages_treat_only_the_leading_instructions_as_the_head():
    def no_images(block, where):
        raise AssertionError('instruction text carries no images')

    messages = [{'role': 'system', 'content': 'LEAD'},
                {'role': 'developer', 'content': [{'type': 'text', 'text': 'LEAD_TWO'}]},
                {'role': 'user', 'content': 'task'},
                {'role': 'system', 'content': [{'type': 'text', 'text': 'A'}, {'type': 'text', 'text': 'B'}]}]
    assert native_history.leading_instructions(messages) == 2
    assert native_history.client_messages(messages, no_images) == [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'task'}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': native_history.reminder('A\nB')}]}]
    # The caller may pin the boundary explicitly; nothing is a head then.
    assert [m['content'][0]['text'] for m in native_history.client_messages(messages, no_images, lead=0)] == [
        native_history.reminder('LEAD'), native_history.reminder('LEAD_TWO'), 'task', native_history.reminder('A\nB')]


def test_an_unsupported_block_in_a_later_system_message_keeps_the_text_bridge():
    messages = [{'role': 'user', 'content': 'x'}, {'role': 'system', 'content': [{'type': 'mystery'}]}]
    assert native_history.client_messages(messages, lambda block, where: block) is None


def test_resume_accepts_user_reminders_after_the_tool_results_and_inside_the_prefix():
    call = {'type': 'tool_call', 'toolCallId': 'c1', 'toolName': 'read_file', 'toolInput': {'path': 'a'}}
    assistant = [{'type': 'thinking', 'thinking': '', 'signature': 'opaque'}, call]
    echo = native_history.canonical_assistant([call])
    before = [{'role': 'user', 'content': [{'type': 'text', 'text': 'task'}]},
              {'role': 'user', 'content': [{'type': 'text', 'text': native_history.reminder('EARLIER')}]}]
    history = native_history.History()
    history.record('scope', before, [{'role': 'system'}, 'prefix'], assistant, echo)
    result = {'role': 'tool', 'content': {'toolCallId': 'c1', 'toolResult': 'ok'}}
    reminder = {'role': 'user', 'content': [{'type': 'text', 'text': native_history.reminder('LATER')}]}
    snapshot, messages = history.resume('scope', before + [echo, result, reminder])
    assert snapshot is not None
    assert messages == [{'role': 'system'}, 'prefix', {'role': 'assistant', 'content': assistant}, result, reminder]
    # A reminder squeezed between the call and its result cannot be answered natively.
    assert history.resume('scope', before + [echo, reminder, result]) == (None, None)
    # An edited earlier reminder is a different prefix.
    edited = copy.deepcopy(before)
    edited[1]['content'][0]['text'] = native_history.reminder('CHANGED')
    assert history.resume('scope', edited + [echo, result]) == (None, None)
