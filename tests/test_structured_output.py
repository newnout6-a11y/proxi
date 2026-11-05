"""JSON output formats are emulated: contract in the instructions, whole reply validated first."""
import asyncio
import json
import socket
import time
import urllib.error

import pytest

import adapters
import apierr
import journal
import structured
import validate
from session import RelaySession
from test_client_contracts import CFG, _body
from test_native_context_reuse import run, setup
from test_service_requests import capture_direct

DIALECTS = ['chat', 'responses', 'anthropic']
PATHS = {'chat': '/v1/chat/completions', 'responses': '/v1/responses', 'anthropic': '/v1/messages'}
MEMORY = {'type': 'object', 'properties': {'selected_memories': {'type': 'array', 'items': {'type': 'string'}}},
          'required': ['selected_memories'], 'additionalProperties': False}
VALID = '{"selected_memories":["a.md"]}'
TOOLS = {'chat': {'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}},
         'responses': {'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}},
         'anthropic': {'name': 'read', 'input_schema': {'type': 'object'}}}
CALL = {'type': 'tool_call', 'id': 'call_1', 'name': 'read', 'arguments': '{"path":"a"}'}


def format_field(dialect, schema):
    if dialect == 'chat':
        return {'response_format': {'type': 'json_schema',
                                    'json_schema': {'name': 'memory', 'schema': schema, 'strict': True}}}
    if dialect == 'responses':
        return {'text': {'format': {'type': 'json_schema', 'name': 'codex_output_schema',
                                    'strict': True, 'schema': schema}}}
    return {'output_config': {'format': {'type': 'json_schema', 'schema': schema}}}


def body(dialect, schema=MEMORY, **changes):
    data = _body(dialect, **format_field(dialect, schema))
    data.update(changes)
    return data


def parse(dialect, schema=MEMORY, **changes):
    return getattr(validate, 'parse_' + dialect)(body(dialect, schema, **changes), CFG)


class Writer:
    def __init__(self):
        self.events = []

    def data(self, payload):
        self.events.append(('data', payload))

    def event(self, name, payload):
        self.events.append((name, payload))

    def done(self):
        self.events.append(('done', {}))


def emitted_text(dialect, events):
    out = []
    for name, payload in events:
        if dialect == 'chat' and name == 'data':
            out.extend(choice['delta'].get('content') or '' for choice in payload.get('choices', []))
        elif dialect == 'responses' and name == 'response.output_text.delta':
            out.append(payload['delta'])
        elif dialect == 'anthropic' and name == 'content_block_delta' and payload['delta']['type'] == 'text_delta':
            out.append(payload['delta']['text'])
    return ''.join(out)


def payload_text(dialect, payload):
    if dialect == 'chat':
        return payload['choices'][0]['message']['content']
    return payload['output_text'] if dialect == 'responses' else payload['content'][0]['text']


def sse_events(raw):
    events = []
    for block in raw.split('\n\n'):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith('event: '):
                name = line[7:]
            elif line.startswith('data: '):
                data = line[6:]
        if data and data != '[DONE]':
            events.append((name or 'data', json.loads(data)))
    return events


# -- the validator ---------------------------------------------------------

@pytest.mark.parametrize('value,schema', [
    ({'a': 1, 'b': ['x']}, {'type': 'object', 'properties': {'a': {'type': 'integer'},
        'b': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['a'], 'additionalProperties': False}),
    (1.0, {'type': 'integer'}), (2, {'type': 'number'}), (None, {'type': ['string', 'null']}),
    (None, {'type': 'string', 'nullable': True}), ('b', {'enum': ['a', 'b']}), (3, {'const': 3}),
    ([], {'type': 'array', 'items': {'type': 'string'}}), ({'x': 1}, {'type': 'object'}),
    (None, {'anyOf': [{'type': 'string'}, {'type': 'null'}]}), (5, {'oneOf': [{'type': 'integer'}, {'type': 'number'}]}),
    ({'a': 'x'}, {'allOf': [{'type': 'object'}, {'required': ['a']}]}),
    ({'n': {'n': None}}, {'$ref': '#/$defs/node', '$defs': {'node': {'type': 'object', 'properties': {
        'n': {'anyOf': [{'type': 'null'}, {'$ref': '#/$defs/node'}]}}}}}),
    ({'k': 1}, {'type': 'object', 'additionalProperties': {'type': 'integer'}}),
    ([1, 'a'], {'prefixItems': [{'type': 'integer'}, {'type': 'string'}]}),
    ('anything', {}), ('anything', True),
])
def test_validator_accepts_conforming_values(value, schema):
    assert structured.check(value, schema) is None


@pytest.mark.parametrize('value,schema,expected', [
    ('x', {'type': 'integer'}, '$: expected integer'), (1.5, {'type': 'integer'}, 'expected integer'),
    (True, {'type': 'number'}, 'expected number'), (1, {'type': 'boolean'}, 'expected boolean'),
    (None, {'type': 'string'}, 'expected string'), ({}, {'type': ['string', 'array']}, 'expected string or array'),
    ({}, {'type': 'object', 'required': ['title']}, "missing required property 'title'"),
    ({'t': 1, 'x': 2}, {'type': 'object', 'properties': {'t': {}}, 'additionalProperties': False}, 'unexpected property'),
    ({'t': 'x'}, {'type': 'object', 'properties': {'t': {'type': 'number'}}}, '$.t: expected number'),
    ({'k': 'x'}, {'type': 'object', 'additionalProperties': {'type': 'integer'}}, '$.*: expected integer'),
    ([1, 'x'], {'type': 'array', 'items': {'type': 'integer'}}, '$[1]: expected integer'),
    ('c', {'enum': ['a', 'b']}, 'allowed options'), (True, {'enum': [1]}, 'allowed options'),
    (1, {'const': True}, 'required constant'),
    (3, {'anyOf': [{'type': 'string'}, {'type': 'null'}]}, 'none of the allowed alternatives'),
    ({'n': {'n': 7}}, {'$ref': '#/$defs/node', '$defs': {'node': {'type': 'object', 'properties': {
        'n': {'anyOf': [{'type': 'null'}, {'$ref': '#/$defs/node'}]}}}}}, 'none of the allowed alternatives'),
    ('x', False, 'no value is allowed'),
])
def test_validator_names_the_first_violation(value, schema, expected):
    assert expected in structured.check(value, schema)


@pytest.mark.parametrize('schema', [
    {'type': 'string', 'minLength': 99, 'maxLength': 1, 'pattern': '^z+$', 'format': 'uuid'},
    {'type': 'string', 'x-vendor': {'a': 1}, 'unknownKeyword': True, 'title': 'T', 'description': 'D'},
    {'$ref': 'https://example.invalid/schema.json'}, {'$ref': '#/$defs/missing'}, {'type': 'unheard-of'},
    {'enum': []}, {'properties': 'not a dict', 'required': 'nope'}, {'items': 'not a schema'},
    {'type': 'object', 'patternProperties': {'[unclosed': {'type': 'integer'}}},
])
def test_unknown_or_unusable_keywords_never_reject(schema):
    value = {'k': 'v'} if schema.get('type') == 'object' else 'abc'
    assert structured.check(value, schema) is None


@pytest.mark.parametrize('schema', [{'allOf': [{'$ref': '#'}, {'$ref': '#'}]}, {'anyOf': [{'$ref': '#'}, {'$ref': '#'}]},
                                    {'allOf': [{'$ref': '#'}] * 6, 'type': 'object'}])
def test_degenerate_self_referencing_schema_terminates_and_accepts(schema):
    started = time.monotonic()
    assert structured.check({'a': 1}, schema) is None
    assert time.monotonic() - started < 10


@pytest.mark.parametrize('text,expected', [
    ('{"a":1}', '{"a":1}'), ('```json\n{"a":1}\n```', '{"a":1}'), ('```\n{"a":1}\n```', '{"a":1}'),
    ('  ```JSON\r\n{"a":1}\r\n```  ', '{"a":1}'),
    ('```json\n{"code":"```js\\nx\\n```"}\n```', '{"code":"```js\\nx\\n```"}'),
])
def test_one_outer_fence_is_removed(text, expected):
    assert structured.verify(text, {'kind': 'json_object'}) == expected


@pytest.mark.parametrize('text', [
    '', 'not json', '{"a":1', '{"a":1} trailing', 'Here: {"a":1}', '```json\n{"a":1}\n```\nDone.',
    'Sure!\n```json\n{"a":1}\n```', '{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '[1]', '"s"', '```json\n```',
])
def test_prose_duplicates_constants_and_non_objects_are_rejected(text):
    with pytest.raises(ValueError):
        structured.verify(text, {'kind': 'json_object'})


def test_problems_quote_no_reply_content():
    with pytest.raises(ValueError) as error:
        structured.verify('{"selected_memories": "PRIVATE-REPLY", "PRIVATE-KEY": 1}',
                          {'kind': 'json_schema', 'schema': MEMORY})
    assert 'PRIVATE' not in str(error.value)


# -- the request ------------------------------------------------------------

@pytest.mark.parametrize('dialect', DIALECTS)
def test_every_dialect_yields_the_same_contract(dialect):
    spec = parse(dialect).structured_output
    assert spec['kind'] == 'json_schema' and spec['schema'] == MEMORY and spec['title'] is False
    assert spec['label'] == 'validated-json-schema'
    assert spec['source'] == {'chat': 'response_format', 'responses': 'text.format',
                              'anthropic': 'output_config.format'}[dialect]


def test_names_and_descriptions_are_kept_short_and_the_flat_chat_form_works():
    spec = parse('chat', response_format={'type': 'json_schema', 'schema': MEMORY, 'description': 'd' * 900,
                                          'name': 'n' * 300}).structured_output
    assert spec['schema'] == MEMORY and len(spec['description']) == 500 and len(spec['name']) == 80


@pytest.mark.parametrize('dialect', DIALECTS)
def test_json_object_needs_no_schema(dialect):
    field = {'chat': {'response_format': {'type': 'json_object'}},
             'responses': {'text': {'format': {'type': 'json_object'}}},
             'anthropic': {'output_config': {'format': {'type': 'json_object'}}}}[dialect]
    spec = getattr(validate, 'parse_' + dialect)(_body(dialect, **field), CFG).structured_output
    assert spec['kind'] == 'json_object' and spec['schema'] is None and spec['label'] == 'validated-json-object'


@pytest.mark.parametrize('dialect', DIALECTS)
def test_plain_text_is_not_a_contract(dialect):
    p = getattr(validate, 'parse_' + dialect)(_body(dialect, response_format={'type': 'text'}), CFG)
    assert p.structured_output is None and not p.structured_title


@pytest.mark.parametrize('dialect', DIALECTS)
def test_inline_system_format_is_honoured_like_a_top_level_one(dialect):
    data = _body(dialect)
    inline = {'role': 'system', 'content': 'remember', 'output_config': {
        'format': {'type': 'json_schema', 'schema': MEMORY}}}
    data['input' if dialect == 'responses' else 'messages'] = [inline, {'role': 'user', 'content': 'hello'}]
    spec = getattr(validate, 'parse_' + dialect)(data, CFG).structured_output
    assert spec['schema'] == MEMORY and spec['source'] == 'messages[0].output_config.format'


def test_identical_formats_in_two_places_are_one_contract_and_different_ones_conflict():
    both = {**format_field('chat', MEMORY), **format_field('anthropic', MEMORY)}
    assert validate.parse_chat(_body('chat', **both), CFG).structured_output['schema'] == MEMORY
    other = {**format_field('chat', MEMORY), **format_field('anthropic', {'type': 'object'})}
    with pytest.raises(apierr.ApiError) as error:
        validate.parse_chat(_body('chat', **other), CFG)
    assert error.value.status == 422 and error.value.param == 'output_config.format'


@pytest.mark.parametrize('dialect', DIALECTS)
@pytest.mark.parametrize('fmt', [{'type': 'grammar'}, {'type': 'json_schema'}, {'type': 'json_schema', 'schema': []},
                                 {'type': 'text', 'extra': 1}, 'json', {'schema': {}}])
def test_formats_the_relay_cannot_honour_fail_before_inference(dialect, fmt):
    field = {'chat': 'response_format', 'responses': 'text', 'anthropic': 'output_config'}[dialect]
    value = {'format': fmt} if dialect != 'chat' else fmt
    with pytest.raises(apierr.ApiError) as error:
        getattr(validate, 'parse_' + dialect)(_body(dialect, **{field: value}), CFG)
    assert error.value.status == 422
    assert ('CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS=1' in str(error.value)) == (dialect == 'anthropic')


FORCED = [('chat', 'required'), ('chat', {'type': 'function', 'function': {'name': 'read'}}),
          ('responses', 'required'), ('responses', {'type': 'function', 'name': 'read'}),
          ('anthropic', {'type': 'any'}), ('anthropic', {'type': 'tool', 'name': 'read'})]


@pytest.mark.parametrize('dialect,choice', FORCED)
def test_format_with_a_forced_tool_call_is_rejected_with_a_clear_text(dialect, choice):
    with pytest.raises(apierr.ApiError) as error:
        parse(dialect, tools=[TOOLS[dialect]], tool_choice=choice)
    assert error.value.status == 422 and error.value.param == 'tool_choice'
    assert 'required tool call' in str(error.value)
    assert ('CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS=1' in str(error.value)) == (dialect == 'anthropic')


@pytest.mark.parametrize('dialect', DIALECTS)
@pytest.mark.parametrize('choice', [None, 'auto', 'none'])
def test_format_with_optional_tools_is_supported(dialect, choice):
    extra = {} if choice is None else {'tool_choice': choice}
    p = parse(dialect, tools=[TOOLS[dialect]], **extra)
    assert p.structured_output['schema'] == MEMORY and p.tools


# -- the reply ----------------------------------------------------------------

@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('dialect', DIALECTS)
def test_valid_reply_reaches_the_client_only_after_validation_and_cost_is_kept(dialect, stream):
    writer = Writer() if stream else None
    adapter = adapters.for_dialect(dialect, parse(dialect), writer)
    adapter.begin()
    adapter.feed({'type': 'usage_start'})
    adapter.feed({'type': 'text', 'delta': '{"selected_memories":'})
    adapter.feed({'type': 'credits', 'value': .75, 'usage': {'input_tokens': 4, 'output_tokens': 9}})
    if stream:
        assert emitted_text(dialect, writer.events) == ''
    adapter.feed({'type': 'text', 'delta': '["a.md"]}'})
    if stream:
        assert emitted_text(dialect, writer.events) == ''
    payload = adapter.end('stop')
    assert payload_text(dialect, payload) == VALID
    if stream:
        assert emitted_text(dialect, writer.events) == VALID
        assert writer.events[-1] == ('done', {})
    assert adapter.adapter.credits == .75
    assert payload['usage']['output_tokens' if dialect != 'chat' else 'completion_tokens'] == 9


@pytest.mark.parametrize('fence', ['```json\n{"selected_memories":["a.md"]}\n```', '```\n{"selected_memories":["a.md"]}\n```'])
@pytest.mark.parametrize('dialect', DIALECTS)
def test_one_outer_markdown_fence_is_removed_before_delivery(dialect, fence):
    writer = Writer()
    adapter = adapters.for_dialect(dialect, parse(dialect), writer)
    adapter.begin()
    adapter.feed({'type': 'text', 'delta': fence})
    assert payload_text(dialect, adapter.end('stop')) == VALID
    assert emitted_text(dialect, writer.events) == VALID


INVALID = [
    ('not json', 'stop'), ('{"selected_memories":"PRIVATE-REPLY"}', 'stop'),
    ('{"selected_memories":["a"],"PRIVATE-EXTRA":1}', 'stop'), ('{}', 'stop'),
    ('{"selected_memories":[1]}', 'stop'), ('Here you go: {"selected_memories":[]}', 'stop'),
    ('```json\n{"selected_memories":[]}\n```\nDone.', 'stop'), ('{"selected_memories":[]}', 'max_exec'),
    ('{"selected_memories":[]}', 'hitl'), ('{"selected_memories":[', 'stop'), ('', 'stop'),
    ('{"selected_memories":[],"selected_memories":["x"]}', 'stop'),
]


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('text,reason', INVALID)
@pytest.mark.parametrize('dialect', DIALECTS)
def test_invalid_reply_is_an_error_before_any_content_and_still_costs(dialect, text, reason, stream):
    writer = Writer() if stream else None
    adapter = adapters.for_dialect(dialect, parse(dialect), writer)
    adapter.begin()
    adapter.feed({'type': 'usage_start'})
    adapter.feed({'type': 'text', 'delta': text})
    adapter.feed({'type': 'credits', 'value': .5, 'usage': {'input_tokens': 4, 'output_tokens': 9}})
    with pytest.raises(apierr.ApiError) as error:
        adapter.end(reason)
    assert error.value.status == 502 and error.value.code == 'upstream_invalid_output'
    assert error.value.headers == {'x-should-retry': 'false'}
    assert 'PRIVATE' not in str(error.value)
    assert not text or text not in str(error.value)
    assert ('CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS=1' in str(error.value)) == (dialect == 'anthropic')
    if stream:
        assert emitted_text(dialect, writer.events) == ''
        assert not any(name in ('response.completed', 'message_stop', 'done') for name, _ in writer.events)
    assert adapter.adapter.credits == .5 and adapter.adapter.output_tokens == 9


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('dialect', DIALECTS)
def test_a_timed_out_turn_is_an_upstream_timeout_with_no_content_and_the_cost_kept(dialect, stream):
    writer = Writer() if stream else None
    adapter = adapters.for_dialect(dialect, parse(dialect), writer)
    adapter.begin()
    adapter.feed({'type': 'usage_start'})
    adapter.feed({'type': 'text', 'delta': '{"selected_memories":['})
    adapter.feed({'type': 'credits', 'value': .5, 'usage': {'input_tokens': 4, 'output_tokens': 9}})
    with pytest.raises(apierr.ApiError) as error:
        adapter.end('timeout')
    assert (error.value.status, error.value.code) == (504, 'upstream_timeout')
    assert 'x-should-retry' not in error.value.headers and 'PRIVATE' not in str(error.value)
    if stream:
        assert emitted_text(dialect, writer.events) == ''
        assert not any(name in ('response.completed', 'response.incomplete', 'message_stop', 'done')
                       for name, _ in writer.events)
    assert adapter.adapter.credits == .5 and adapter.adapter.output_tokens == 9


@pytest.mark.parametrize('text', ['{"selected_memories":[', '{"selected_memories":"PRIVATE"}', VALID, ''])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('dialect', DIALECTS)
def test_a_cancelled_turn_is_not_validated_and_ends_like_any_other_request(dialect, stream, text):
    writer = Writer() if stream else None
    adapter = adapters.for_dialect(dialect, parse(dialect), writer)
    adapter.begin()
    adapter.feed({'type': 'usage_start'})
    adapter.feed({'type': 'text', 'delta': text})
    adapter.feed({'type': 'credits', 'value': .5, 'usage': {'input_tokens': 4, 'output_tokens': 9}})
    payload = adapter.end('cancelled')
    assert payload_text(dialect, payload) == ''
    if stream:
        assert emitted_text(dialect, writer.events) == '' and 'error' not in dict(writer.events)
        assert writer.events[-1] == ('done', {})
    assert adapter.adapter.credits == .5 and adapter.adapter.output_tokens == 9


@pytest.mark.parametrize('dialect', DIALECTS)
def test_error_envelope_is_dialect_shaped_and_names_the_failure(dialect):
    adapter = adapters.for_dialect(dialect, parse(dialect))
    adapter.feed({'type': 'text', 'delta': '{"selected_memories":"x"}'})
    with pytest.raises(apierr.ApiError) as error:
        adapter.end('stop')
    envelope = adapters.error_payload(dialect, error.value)
    source = {'chat': 'response_format', 'responses': 'text.format', 'anthropic': 'output_config.format'}[dialect]
    assert source in error.value.message and '$.selected_memories: expected array' in error.value.message
    if dialect == 'anthropic':
        assert envelope['type'] == 'error' and envelope['error']['type'] == 'api_error'
    elif dialect == 'responses':
        assert envelope['type'] == 'error' and envelope['code'] == 'upstream_invalid_output'
    else:
        assert envelope['error']['code'] == envelope['error']['type'] == 'upstream_invalid_output'


@pytest.mark.parametrize('dialect', DIALECTS)
def test_buffer_is_bounded(dialect):
    writer = Writer()
    adapter = adapters.for_dialect(dialect, parse(dialect), writer)
    adapter.begin()
    with pytest.raises(apierr.ApiError):
        adapter.feed({'type': 'text', 'delta': 'x' * (structured.MAX_CHARS + 1)})
    assert emitted_text(dialect, writer.events) == ''


@pytest.mark.parametrize('kind', ['tool_call', 'hitl', 'exec_start', 'exec_result'])
@pytest.mark.parametrize('dialect', DIALECTS)
def test_actions_cannot_appear_in_a_toolless_json_reply(dialect, kind):
    adapter = adapters.for_dialect(dialect, parse(dialect), Writer())
    with pytest.raises(apierr.ApiError):
        adapter.feed({'type': kind})


@pytest.mark.parametrize('dialect', DIALECTS)
def test_toolless_reply_drops_thinking_like_the_title_path(dialect):
    writer = Writer()
    adapter = adapters.for_dialect(dialect, parse(dialect), writer)
    adapter.begin()
    for event in ({'type': 'thinking_start'}, {'type': 'thinking', 'delta': 'THINK-MARK'},
                  {'type': 'thinking_signature', 'signature': 'sig'}, {'type': 'text', 'delta': VALID}):
        adapter.feed(event)
    adapter.end('stop')
    assert 'THINK-MARK' not in json.dumps(writer.events)


@pytest.mark.parametrize('dialect', DIALECTS)
def test_tool_turn_is_released_unchanged_and_only_the_final_reply_is_validated(dialect):
    parsed = parse(dialect, tools=[TOOLS[dialect]])
    writer = Writer()
    first = adapters.for_dialect(dialect, parsed, writer)
    first.begin()
    first.feed({'type': 'usage_start'})
    first.feed({'type': 'thinking', 'delta': 'THINK-MARK'})
    first.feed({'type': 'text', 'delta': 'Let me look at it first. '})
    assert 'Let me look' not in emitted_text(dialect, writer.events)
    first.feed(CALL)
    assert 'Let me look' in emitted_text(dialect, writer.events)
    first.feed({'type': 'credits', 'value': .25})
    payload = first.end('tool_calls')
    assert 'THINK-MARK' in json.dumps(writer.events)
    assert 'call_1' in json.dumps(payload) and first.adapter.credits == .25
    # The next request carries the tool result and gets the validated answer.
    final = adapters.for_dialect(dialect, parsed, Writer())
    final.feed({'type': 'text', 'delta': 'Done: not json'})
    with pytest.raises(apierr.ApiError):
        final.end('stop')
    valid = adapters.for_dialect(dialect, parsed)
    valid.feed({'type': 'text', 'delta': VALID})
    assert payload_text(dialect, valid.end('stop')) == VALID


@pytest.mark.parametrize('dialect', DIALECTS)
def test_declared_tools_do_not_exempt_a_final_reply_from_validation(dialect):
    adapter = adapters.for_dialect(dialect, parse(dialect, tools=[TOOLS[dialect]]), Writer())
    adapter.feed({'type': 'text', 'delta': 'plain prose'})
    with pytest.raises(apierr.ApiError):
        adapter.end('stop')


# -- over HTTP ------------------------------------------------------------------

def post(relay, dialect, payload):
    try:
        with relay.request(PATHS[dialect], payload) as response:
            return response.status, response.headers, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode()


def script(text, cost=.5):
    return [{'type': 'text', 'delta': text}, {'type': 'credits', 'value': cost}, {'type': 'end', 'reason': 'stop'}]


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('dialect', DIALECTS)
def test_http_valid_fenced_reply_is_delivered_as_bare_json(relay, dialect, stream):
    relay.fake.script = script('```json\n' + VALID + '\n```')
    status, headers, raw = post(relay, dialect, body(dialect, stream=stream))
    assert status == 200 and headers['x-proxi-output-format'] == 'validated-json-schema'
    if stream:
        assert emitted_text(dialect, sse_events(raw)) == VALID
    else:
        assert payload_text(dialect, json.loads(raw)) == VALID
    assert relay.credits.used == [('turn', .5)]


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('dialect', DIALECTS)
def test_http_invalid_reply_fails_visibly_without_content_and_keeps_cost(relay, dialect, stream):
    relay.fake.script = script('{"selected_memories":"PRIVATE-REPLY"}')
    status, headers, raw = post(relay, dialect, body(dialect, stream=stream))
    assert 'PRIVATE' not in raw
    if stream:
        assert status == 200 and 'event: error' in raw
        events = sse_events(raw)
        assert emitted_text(dialect, events) == ''
        assert not any(name in ('response.completed', 'message_stop') for name, _ in events)
        failure = dict(events)['error']
        assert 'upstream reply does not satisfy' in json.dumps(failure)
    else:
        assert status == 502 and headers['x-should-retry'] == 'false'
        message = json.loads(raw)['error']
        assert message['type'] == ('api_error' if dialect == 'anthropic' else 'upstream_invalid_output')
    assert relay.credits.used == [('turn', .5)]


def abort_midway(relay, dialect, payload):
    """A client that sends a request and drops the connection while the model is still writing."""
    data = json.dumps(payload).encode()
    host, port = relay.url[len('http://'):].split(':')
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(('POST %s HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
                      'Authorization: Bearer %s\r\nContent-Length: %d\r\n\r\n'
                      % (PATHS[dialect], relay.key, len(data))).encode() + data)
        if payload.get('stream'):
            sock.settimeout(5)
            seen = b''
            while b'\r\n\r\n' not in seen:
                seen += sock.recv(4096)
        time.sleep(0.4)


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.05)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('format_requested', [True, False])
def test_http_a_client_that_aborts_is_no_error_with_or_without_a_format(relay, monkeypatch, stream, format_requested):
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    holders = []

    async def slow(parsed, out, holder):
        holders.append(holder)
        try:
            out.put({'type': 'usage_start'})
            out.put({'type': 'text', 'delta': '{"selected_memories":['})
            await asyncio.sleep(30)  # the proxy cancels this future once the client has gone
        finally:
            out.put(None)

    relay.fake.run = slow
    payload = (body if format_requested else lambda dialect, **extra: _body(dialect, **extra))('anthropic', stream=stream)
    abort_midway(relay, 'anthropic', payload)
    wait_for(lambda: [e for e in log.query() if e['kind'] == 'http.request'])
    events = log.query()
    assert not [e for e in events if e['kind'] == 'http.error']
    request = next(e for e in events if e['kind'] == 'http.request')
    assert request['level'] != 'error' and request['data']['status'] == 200
    assert log.metrics(3600, 60)['totals']['errors'] == 0
    assert holders and holders[-1]['cancel_event'].is_set()


def error_type(dialect, failure):
    return failure['code'] if dialect == 'responses' else failure['error']['type']


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('dialect', DIALECTS)
def test_http_a_timed_out_structured_turn_is_a_504_and_keeps_its_cost(relay, dialect, stream):
    relay.fake.script = [{'type': 'text', 'delta': '{"selected_memories":['},
                         {'type': 'credits', 'value': .5}, {'type': 'end', 'reason': 'timeout'}]
    status, headers, raw = post(relay, dialect, body(dialect, stream=stream))
    expected = 'timeout_error' if dialect == 'anthropic' else 'upstream_timeout'
    if stream:
        assert status == 200 and error_type(dialect, dict(sse_events(raw))['error']) == expected
        assert emitted_text(dialect, sse_events(raw)) == ''
    else:
        assert status == 504 and json.loads(raw)['error']['type'] == expected
    assert relay.credits.used == [('turn', .5)]


def test_http_json_object_label_and_validation(relay):
    payload = _body('chat', response_format={'type': 'json_object'})
    relay.fake.script = script('{"a": 1}')
    status, headers, raw = post(relay, 'chat', payload)
    assert status == 200 and headers['x-proxi-output-format'] == 'validated-json-object'
    relay.fake.script = script('[1, 2]')
    assert post(relay, 'chat', payload)[0] == 502


def test_http_plain_requests_carry_no_output_format_label(relay):
    status, headers, _ = post(relay, 'chat', _body('chat', response_format={'type': 'text'}))
    assert status == 200 and 'x-proxi-output-format' not in headers


def test_http_forced_tool_with_a_format_is_422_with_the_claude_code_hint(relay):
    payload = body('anthropic', tools=[TOOLS['anthropic']], tool_choice={'type': 'tool', 'name': 'read'})
    status, _, raw = post(relay, 'anthropic', payload)
    message = json.loads(raw)
    assert status == 422 and message['type'] == 'error'
    assert message['error']['type'] == 'invalid_request_error'
    assert 'CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS=1' in message['error']['message']
    assert not relay.fake.seen


# -- the instructions the model receives ------------------------------------------

def test_contract_and_schema_reach_the_model_and_anonymous_requests_get_no_relay_protocol(tmp_path, monkeypatch):
    import context
    import executor
    _, _, runner = setup(tmp_path, transport='direct', project_context=True, exec_policy='allow_all',
                         cookie_values=[])
    monkeypatch.setattr(context, 'build_project_context', lambda *a, **kw: pytest.fail('JSON-only request'))
    monkeypatch.setattr(executor, 'run_command', lambda *a, **kw: pytest.fail('JSON-only request'))
    sent = capture_direct(monkeypatch)
    events = asyncio.run(run(runner, parse('chat')))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}
    head = sent[0][1]['messages'][0]['content'][0]['text']
    assert head.startswith('[STRUCTURED OUTPUT FORMAT]')
    assert 'exactly one JSON value that conforms to the JSON Schema below' in head
    assert 'Do not wrap it in Markdown fences' in head
    assert 'JSON Schema: ' + json.dumps(MEMORY, separators=(',', ':')) in head
    assert 'LOCAL EXEC' not in json.dumps(sent[0][1])


def test_model_output_cannot_trigger_local_execution_of_a_json_request(tmp_path, monkeypatch):
    import executor
    _, _, runner = setup(tmp_path, transport='direct', project_context=False, exec_policy='allow_all',
                         cookie_values=[])

    async def stream(*args, **kwargs):
        yield {'type': 'text', 'delta': '<exec_command>synthetic-command</exec_command>'}
        yield {'type': 'end', 'reason': 'stop'}

    monkeypatch.setattr(RelaySession, 'stream', stream)
    monkeypatch.setattr(executor, 'extract_command', lambda *_: pytest.fail('JSON request must not execute'))
    events = asyncio.run(run(runner, parse('chat')))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}


def test_a_client_with_tools_and_no_system_keeps_the_native_tools_role_beside_the_contract(tmp_path, monkeypatch):
    from test_native_context_reuse import request
    _, _, runner = setup(tmp_path, transport='direct', project_context=False, exec_policy='off', cookie_values=[])
    sent = capture_direct(monkeypatch)
    parsed = request([{'role': 'user', 'content': 'Inspect ORIGINAL_TASK.'}], system='')
    parsed.structured_output = parse('chat').structured_output
    events = asyncio.run(run(runner, parsed))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}
    head = sent[0][1]['messages'][0]['content'][0]['text']
    assert head.startswith('[ROLE]\nYou are a coding assistant using the client\'s tools.')
    assert '[STRUCTURED OUTPUT FORMAT]\nUse the supplied tools as usual.' in head
    assert 'JSON Schema: ' + json.dumps(MEMORY, separators=(',', ':')) in head


TITLE_SCHEMA = {'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title'],
                'additionalProperties': False}


@pytest.mark.parametrize('dialect', ['chat', 'responses'])
def test_title_shaped_schema_of_openai_clients_is_not_a_claude_service_title(dialect):
    import client_context
    # ai-sdk generateObject({title: string}) is an ordinary request, not a Claude session title.
    p = parse(dialect, TITLE_SCHEMA)
    assert p.structured_output['label'] == 'validated-json-schema' and not p.structured_title
    assert client_context.request_kind(p, {'client': 'claude-code'}) != 'session-title'
    assert client_context.request_kind(p, {}) == 'generation'


def test_the_same_title_schema_on_messages_is_the_service_title():
    import client_context
    p = parse('anthropic', TITLE_SCHEMA)
    assert p.structured_title and p.structured_output['label'] == 'validated-session-title'
    assert client_context.request_kind(p, {'client': 'claude-code'}) == 'session-title'


def test_json_object_and_tool_instructions_differ_from_the_schema_text():
    plain = structured.instructions({'kind': 'json_object', 'schema': None, 'name': None, 'description': None})
    assert 'exactly one JSON object' in plain and 'JSON Schema:' not in plain and 'Use the supplied tools' not in plain
    spec = {'kind': 'json_schema', 'schema': MEMORY, 'name': 'memory', 'description': 'picks files'}
    with_tools = structured.instructions(spec, tools=True)
    assert with_tools.startswith('[STRUCTURED OUTPUT FORMAT]\nUse the supplied tools as usual.')
    assert 'Schema name: memory' in with_tools and 'Schema description: picks files' in with_tools


CONTRACT = ('exactly one JSON value that conforms to the JSON Schema below. Output only that JSON. '
            'Do not wrap it in Markdown fences and do not add any other text.\n'
            'JSON Schema: ' + json.dumps(MEMORY, separators=(',', ':')))
TOOLLESS_HEAD = '[STRUCTURED OUTPUT FORMAT]\nYour reply must be ' + CONTRACT
TOOL_HEAD = ('[STRUCTURED OUTPUT FORMAT]\nUse the supplied tools as usual. Your final answer, the reply in which '
             'you call no tool, must be ' + CONTRACT)


def test_the_usual_contract_texts_stay_byte_identical_because_they_are_part_of_the_cached_head():
    spec = {'kind': 'json_schema', 'schema': MEMORY, 'name': None, 'description': None}
    assert structured.instructions(spec) == TOOLLESS_HEAD
    assert structured.instructions(spec, tools=True) == TOOL_HEAD
    assert structured.instructions(spec, tools=True, tool_choice='auto') == TOOL_HEAD
    assert structured.instructions(spec, tools=False, tool_choice='auto') == TOOLLESS_HEAD


def test_tools_the_model_may_not_call_do_not_appear_in_the_contract():
    spec = {'kind': 'json_schema', 'schema': MEMORY, 'name': None, 'description': None}
    assert structured.instructions(spec, tools=True, tool_choice='none') == TOOLLESS_HEAD
    assert structured.instructions(spec, tools=False, tool_choice='none') == TOOLLESS_HEAD


@pytest.mark.parametrize('choice', ['auto', 'none'])
def test_the_head_sent_upstream_follows_the_tools_the_model_may_call(tmp_path, monkeypatch, choice):
    from test_native_context_reuse import request
    _, _, runner = setup(tmp_path, transport='direct', project_context=False, exec_policy='off', cookie_values=[])
    sent = capture_direct(monkeypatch)
    parsed = request([{'role': 'user', 'content': 'Inspect ORIGINAL_TASK.'}], system='')
    assert parsed.tools
    parsed.structured_output = parse('chat').structured_output
    parsed.tool_choice = choice
    events = asyncio.run(run(runner, parsed))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}
    head = sent[0][1]['messages'][0]['content'][0]['text']
    assert structured.instructions(parsed.structured_output, tools=choice == 'auto') in head
    assert ('Use the supplied tools' in head) is (choice == 'auto')
