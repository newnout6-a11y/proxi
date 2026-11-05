"""Regressions from actual bundled Desktop SDK requests (synthetic traffic)."""
import copy
import json

import pytest

import adapters
import apierr
import client_context
import direct
import generation
import native_history
import validate
from test_client_contracts import CFG


def title_body(**changes):
    body = {'model': 'claude-haiku-4-5-20251001', 'max_tokens': 32000,
            'thinking': {'type': 'disabled'}, 'tools': [],
            'messages': [{'role': 'user', 'content': '<session>Cache accounting</session>'}],
            'system': [{'type': 'text', 'text': 'SDK identity'},
                       {'type': 'text', 'text': client_context.SDK_TITLE_PREFIX + '\nReturn JSON with a single "title" field.'}],
            'output_config': {'format': copy.deepcopy(generation.TITLE_FORMAT)}}
    body.update(changes)
    return body


def test_haiku_disabled_is_a_verified_model_specific_effort():
    p = validate.parse_anthropic(title_body(), CFG)
    assert p.effort == 'none' and p.structured_title
    assert client_context.request_kind(p) == 'session-title'
    with pytest.raises(apierr.ApiError):
        validate.parse_anthropic(title_body(model='claude-opus-5-5'), CFG)


@pytest.mark.parametrize('display,shown,ignored', [('updates', True, ['thinking.display']),
    ('summarized', True, []), ('omitted', False, [])])
def test_display_updates_uses_summaries_with_an_explicit_notice(display, shown, ignored):
    body = title_body(thinking={'type': 'adaptive', 'display': display}, model='claude-opus-5-5',
                      output_config={'effort': 'high'})
    p = validate.parse_anthropic(body, CFG)
    assert p.show_thinking == shown and p.declared_ignored == ignored
    assert not validate.parse_anthropic(body, {**CFG, 'show_thinking': False}).show_thinking


def test_unknown_thinking_display_is_still_rejected():
    with pytest.raises(apierr.ApiError) as error:
        validate.parse_anthropic(title_body(thinking={'type': 'disabled', 'display': 'secret'}), CFG)
    assert error.value.param == 'thinking.display'


@pytest.mark.parametrize('change', ['extra_property', 'wrong_type', 'open_object', 'numeric_false', 'tools'])
def test_only_the_exact_title_contract_gets_the_title_label(change):
    body = title_body()
    schema = body['output_config']['format']['schema']
    if change == 'extra_property':
        schema['properties']['other'] = {'type': 'string'}
    elif change == 'wrong_type':
        schema['properties']['title']['type'] = 'number'
    elif change == 'open_object':
        schema['additionalProperties'] = True
    elif change == 'numeric_false':
        schema['additionalProperties'] = 0
    else:
        body['tools'] = [{'name': 'read', 'input_schema': {'type': 'object'}}]
    p = validate.parse_anthropic(body, CFG)
    # The request is still a valid structured output, validated against ITS schema.
    assert not p.structured_title and p.structured_output['label'] == 'validated-json-schema'
    assert p.structured_output['schema'] == schema


def test_title_is_recognised_by_its_schema_not_by_the_prompt_wording():
    body = title_body(system='Give this session a short name.')
    p = validate.parse_anthropic(body, CFG)
    assert p.structured_title and p.structured_output['label'] == 'validated-session-title'
    assert client_context.request_kind(p) == 'session-title'
    body['tools'] = [{'name': 'read', 'input_schema': {'type': 'object'}}]
    assert client_context.request_kind(validate.parse_anthropic(body, CFG)) == 'client-tools'


class Writer:
    def __init__(self):
        self.events = []

    def event(self, name, payload):
        self.events.append((name, payload))

    def done(self):
        self.events.append(('done', {}))


def adapter():
    writer = Writer()
    result = adapters.for_dialect('anthropic', validate.parse_anthropic(title_body(), CFG), writer)
    result.begin()
    return result, writer


def test_streaming_title_is_buffered_then_emitted_and_cost_retained():
    result, writer = adapter()
    result.feed({'type': 'usage_start'})
    result.feed({'type': 'thinking', 'delta': 'not title output'})
    result.feed({'type': 'text', 'delta': '{"title":'})
    result.feed({'type': 'credits', 'value': .75, 'usage': {'input_tokens': 4, 'output_tokens': 9}})
    assert [name for name, _ in writer.events] == ['message_start', 'ping']
    result.feed({'type': 'text', 'delta': '"Кэш"}'})
    payload = result.end('stop')
    assert payload['content'] == [{'type': 'text', 'text': '{"title":"Кэш"}'}]
    assert result.adapter.credits == .75
    assert payload['usage']['output_tokens'] == 9
    assert [name for name, _ in writer.events][-1] == 'done'


@pytest.mark.parametrize('text,reason', [('not JSON', 'stop'), ('{"title":1}', 'stop'),
    ('{"title":"a","extra":1}', 'stop'), ('{"title":"a","title":"b"}', 'stop'),
    ('{"title":"a"}', 'timeout'), ('{"title":"a"}', 'cancelled'), ('{"title":', 'stop'),
    ('Here is JSON: {"title":"a"}', 'stop'), ('["title"]', 'stop'), ('{}', 'stop'),
    ('```json\n{"title":"a"}\n```\nExplanation', 'stop')])
def test_invalid_or_truncated_title_never_leaks_partial_content(text, reason):
    result, writer = adapter()
    result.feed({'type': 'text', 'delta': text})
    with pytest.raises(apierr.ApiError) as error:
        result.end(reason)
    assert error.value.status == 502
    assert [name for name, _ in writer.events] == ['message_start', 'ping']


@pytest.mark.parametrize('kind', ['tool_call', 'hitl', 'exec_start', 'exec_result'])
def test_unexpected_actions_cannot_emit_title_content(kind):
    result, writer = adapter()
    with pytest.raises(apierr.ApiError):
        result.feed({'type': kind})
    assert [name for name, _ in writer.events] == ['message_start', 'ping']


def test_title_buffer_is_bounded():
    result, writer = adapter()
    with pytest.raises(apierr.ApiError):
        result.feed({'type': 'text', 'delta': 'x' * (256 * 1024 + 1)})
    assert [name for name, _ in writer.events] == ['message_start', 'ping']


@pytest.mark.parametrize('fence', ['```json', '```'])
def test_sole_json_fence_is_removed_without_changing_title(fence):
    result, writer = adapter()
    result.feed({'type': 'text', 'delta': fence + '\n{"title":"Кэш"}\n```'})
    payload = result.end('stop')
    assert payload['content'] == [{'type': 'text', 'text': '{"title":"Кэш"}'}]


@pytest.mark.parametrize('stream', [False, True])
def test_title_adapter_rejection_cancels_running_upstream(relay, stream):
    import asyncio
    import threading
    cancelled = threading.Event()

    async def run(parsed, out, holder):
        try:
            out.put({'type': 'text', 'delta': 'x' * (256 * 1024 + 1)})
            await asyncio.sleep(30)
            pytest.fail('invalid title left a paid upstream running')
        finally:
            if holder['cancel_event'].is_set():
                cancelled.set()
            out.put(None)

    relay.fake.run = run
    if stream:
        _, raw = relay.sse('/v1/messages', title_body(stream=True))
        assert 'event: error' in raw
    else:
        status, _ = relay.json_any('/v1/messages', title_body())
        assert status == 502
    assert cancelled.wait(2)


@pytest.mark.parametrize('stream', [False, True])
def test_actual_title_request_over_http_and_accounting(relay, stream):
    relay.fake.script = [{'type': 'text', 'delta': '{"title":"Cache accounting"}'},
                         {'type': 'credits', 'value': .5}, {'type': 'end', 'reason': 'stop'}]
    with relay.request('/v1/messages', title_body(stream=stream)) as response:
        raw = response.read().decode()
        assert response.headers['x-proxi-output-format'] == 'validated-session-title'
        assert 'Cache accounting' in raw
        assert response.status == 200
    assert relay.credits.used == [('turn', .5)]


@pytest.mark.parametrize('stream', [False, True])
def test_invalid_title_over_http_is_error_without_raw_text(relay, stream):
    relay.fake.script = [{'type': 'text', 'delta': 'PRIVATE INVALID OUTPUT'},
                         {'type': 'credits', 'value': .5}, {'type': 'end', 'reason': 'stop'}]
    if stream:
        _, raw = relay.sse('/v1/messages', title_body(stream=True))
        assert 'event: error' in raw and 'message_stop' not in raw
    else:
        status, payload = relay.json_any('/v1/messages', title_body())
        assert status == 502
        raw = json.dumps(payload)
    assert 'PRIVATE INVALID OUTPUT' not in raw
    assert relay.credits.used == [('turn', .5)]


def test_updates_http_notice(relay):
    body = title_body(model='claude-opus-5-5', thinking={'type': 'adaptive', 'display': 'updates'},
                      output_config={'effort': 'high'})
    with relay.request('/v1/messages', body) as response:
        assert response.headers['x-proxi-thinking-display'] == 'summarized'
        assert response.headers['x-proxi-ignored-parameters'] == 'thinking.display'
        assert 'x-proxi-thinking-display' in response.headers['access-control-expose-headers']


@pytest.mark.parametrize('cache', [False, True])
def test_sdk_billing_attribution_cannot_swallow_system_instructions(cache):
    head = 'x-anthropic-billing-header: cc_version=2.1.295;\nSDK identity\nActual system policy'
    body = direct.build_body({'direct_cache': cache}, {'model': 'claude-opus-5-5'}, head,
                             'user task', [], 'none', 64)
    blocks = body['messages'][0]['content']
    assert [b['text'] for b in blocks] == [head.split('\n')[0], head.split('\n', 1)[1]]
    assert not blocks[0].get('cache') and bool(blocks[1].get('cache')) == cache
    replay = native_history.cache_messages(body['messages'], cache, [])
    assert not replay[0]['content'][0].get('cache')
    assert bool(replay[0]['content'][1].get('cache')) == cache
    assert body['rit'] == direct.make_rit(body['messages'])


def test_title_format_policy_reaches_upstream_with_own_system(tmp_path, monkeypatch):
    import asyncio
    from session import RelaySession
    from test_native_context_reuse import run, setup
    _, _, runner = setup(tmp_path, transport='direct', relay_system_prompt_mode='replace',
                         relay_system_prompt='UNRELATED RELAY POLICY', cookie_values=[])
    sent = []

    async def stream(session, prompt, provider, tool_names=None):
        sent.append(session._direct['head'])
        yield {'type': 'text', 'delta': '{"title":"Cache accounting"}'}
        yield {'type': 'end', 'reason': 'stop'}

    monkeypatch.setattr(RelaySession, 'stream', stream)
    parsed = validate.parse_anthropic(title_body(), CFG)
    asyncio.run(run(runner, parsed))
    assert client_context.SDK_TITLE_PREFIX in sent[0]
    assert 'Do not wrap it in Markdown' in sent[0]
    assert 'UNRELATED RELAY POLICY' not in sent[0]
