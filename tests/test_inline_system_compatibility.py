"""Retool rejects multiple native system messages; guidance must still survive."""
import asyncio
import copy
import json

import pytest

import validate
from apierr import ApiError
from test_native_history import append_result, close, harness, parsed, strip_cache, tool_reply
from test_native_context_reuse import FIRST, run
from test_review_fixes_pr19 import END, text


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_inline_guidance_survives_tool_round_trip_without_claiming_native_mid_system(tmp_path, monkeypatch, dialect):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), tool_reply('c2', 'new-prefix-signature'),
                                                        [text('done'), *END]])
    async def scenario():
        messages = copy.deepcopy(FIRST)
        try:
            first = parsed(messages, dialect)
            messages = append_result(messages, first, await run(runner, first), dialect)
            messages.append({'role': 'system', 'content': 'MID_SYSTEM_KEEP_THIS_91. Reply in Russian.'})
            second = parsed(messages, dialect)
            assert second.system_messages_normalized is True
            messages = append_result(messages, second, await run(runner, second), dialect)
            last = await run(runner, parsed(messages, dialect))
            assert any(event.get('delta') == 'done' for event in last)
        finally:
            await close(pool)
    asyncio.run(scenario())
    assert len(sent) == 3
    for body in sent[1:]:
        assert sum(message['role'] == 'system' for message in body['messages']) == 1
        assert 'MID_SYSTEM_KEEP_THIS_91' in json.dumps(body['messages'][0])
    assert 'opaque-one' not in json.dumps(sent[1])  # A changed head must never reuse its old signature.
    assert 'new-prefix-signature' in json.dumps(sent[2])
    assert strip_cache(sent[2]['messages'][:len(sent[1]['messages'])]) == strip_cache(sent[1]['messages'])


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_inline_effort_update_is_not_discarded_with_an_empty_system_message(dialect):
    data = {'model': 'claude-opus-5-5', 'max_tokens': 2048,
            'output_config': {'effort': 'high'}}
    messages = [{'role': 'user', 'content': 'hello'},
                {'role': 'system', 'content': '', 'output_config': {'effort': 'low'}}]
    if dialect == 'responses':
        data['input'] = messages
    else:
        data['messages'] = messages
    request = getattr(validate, 'parse_' + dialect)(data, {'strict_models': True})
    assert request.effort == request.requested_effort == 'low'
    assert 'System:' not in request.prompt


def test_inline_system_text_order_is_retained_in_the_single_compatible_head():
    data = {'model': 'claude-opus-5-5', 'max_tokens': 2048, 'system': 'GLOBAL', 'messages': [
        {'role': 'user', 'content': 'start'}, {'role': 'system', 'content': 'FIRST_UPDATE'},
        {'role': 'assistant', 'content': 'ready'}, {'role': 'system', 'content': 'SECOND_UPDATE'}]}
    request = validate.parse_anthropic(data, {'strict_models': True})
    assert request.system == 'GLOBAL\n\nFIRST_UPDATE\n\nSECOND_UPDATE'
    assert 'FIRST_UPDATE' not in request.prompt


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_turn_scoped_system_lifetime_cannot_silently_become_persistent(dialect):
    data = {'model': 'claude-opus-5-5', 'max_tokens': 1024}
    messages = [{'role': 'user', 'content': 'start'},
                {'role': 'system', 'content': 'ONCE_ONLY', 'clear_at': 'next_user_message'},
                {'role': 'user', 'content': 'next'}]
    data['input' if dialect == 'responses' else 'messages'] = messages
    with pytest.raises(ApiError) as caught:
        getattr(validate, 'parse_' + dialect)(data, {'strict_models': True})
    assert caught.value.status == 422 and 'clear_at' in caught.value.param


@pytest.mark.parametrize('model', ['gpt-6-sol', 'claude-opus-5-5'])
@pytest.mark.parametrize('thinking,effort', [({'type': 'disabled'}, 'low'),
    ({'type': 'enabled', 'budget_tokens': 1024}, 'high'), ({'type': 'adaptive', 'effort': 'high'}, 'low')])
def test_inline_effort_cannot_override_an_incompatible_explicit_thinking_control(model, thinking, effort):
    data = {'model': model, 'max_tokens': 2048, 'thinking': thinking,
            'messages': [{'role': 'user', 'content': 'hello'},
                         {'role': 'system', 'content': '', 'output_config': {'effort': effort}}]}
    with pytest.raises(ApiError) as caught:
        validate.parse_anthropic(data, {'strict_models': True})
    assert caught.value.status == 422 and 'conflicting' in caught.value.message
