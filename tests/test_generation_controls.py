"""Generation controls must be forwarded, explicitly rejected, or named as ignored."""
import asyncio
import copy
import json

import pytest

import apierr
import validate
from test_client_contracts import CFG, _body


def parse(dialect, **changes):
    body = _body(dialect, model='gpt-6-sol', **changes)
    return getattr(validate, 'parse_' + dialect)(body, CFG)


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('level', ['low', 'medium', 'high'])
def test_verbosity_mapping_is_typed_and_explicit(dialect, level):
    data = {'text': {'verbosity': level}} if dialect == 'responses' else {'verbosity': level}
    assert parse(dialect, **data).verbosity == level


@pytest.mark.parametrize('value', [False, 1, [], {}, '', 'extreme'])
def test_invalid_verbosity_is_rejected(value):
    with pytest.raises(apierr.ApiError) as error:
        parse('chat', verbosity=value)
    assert error.value.status == 422 and error.value.param == 'verbosity'


def test_conflicting_verbosity_aliases_and_unverified_models_are_rejected():
    with pytest.raises(apierr.ApiError, match='conflicting'):
        parse('responses', verbosity='low', text={'verbosity': 'high'})
    with pytest.raises(apierr.ApiError, match='verbosity'):
        validate.parse_chat(_body('chat', verbosity='low'), CFG)


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('changes,param', [
    ({'response_format': {'type': 'json_schema', 'json_schema': {'name': 'private-schema'}}}, 'response_format'),
    ({'response_format': {'type': 'json_object'}}, 'response_format'),
    ({'text': {'format': {'type': 'json_schema', 'schema': {}}}}, 'text.format'),
    ({'output_config': {'format': {'type': 'json_schema', 'schema': {}}}}, 'output_config.format'),
    ({'stop': ['PRIVATE-STOP']}, 'stop'), ({'stop_sequences': ['PRIVATE-STOP']}, 'stop_sequences'),
    ({'n': 2}, 'n'), ({'previous_response_id': 'PRIVATE-ID'}, 'previous_response_id'),
    ({'store': True}, 'store'), ({'background': True}, 'background'),
    ({'conversation': {'id': 'PRIVATE-ID'}}, 'conversation'), ({'truncation': 'auto'}, 'truncation'),
])
def test_material_unsupported_semantics_fail_before_generation(dialect, changes, param):
    with pytest.raises(apierr.ApiError) as error:
        parse(dialect, **changes)
    assert error.value.status == 422 and error.value.param == param
    assert 'PRIVATE' not in str(error.value)


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_harmless_explicit_defaults_do_not_break_clients(dialect):
    p = parse(dialect, response_format={'type': 'text'}, text={'format': {'type': 'text'}},
              stop=[], stop_sequences=[], n=1, store=False, background=False, truncation='disabled')
    assert p.declared_ignored == []


@pytest.mark.parametrize('dialect', ['chat', 'anthropic'])
def test_existing_local_conversation_affinity_is_preserved(dialect):
    p = parse(dialect, conversation='local-dialog')
    assert p.prompt and validate.conversation_key({'conversation': 'local-dialog'}) == 'conversation:local-dialog'


def test_responses_conversation_string_cannot_pretend_to_restore_server_state():
    with pytest.raises(apierr.ApiError) as error:
        parse('responses', conversation='server-dialog')
    assert error.value.param == 'conversation'


@pytest.mark.parametrize('changes', [{'temperature': True}, {'temperature': -1}, {'temperature': 3},
    {'top_p': 'bad'}, {'top_p': 1.1}, {'top_k': 1.2}, {'seed': True}, {'presence_penalty': float('inf')},
    {'frequency_penalty': -3}, {'logprobs': 'yes'}, {'top_logprobs': False}, {'n': True}, {'store': 'false'},
    {'stop': [1]}, {'stop_sequences': [1]}, {'text': 'bad'}, {'background': 1}])
def test_ignored_controls_still_validate_their_types_and_ranges(changes):
    with pytest.raises(apierr.ApiError) as error:
        parse('chat', **changes)
    assert error.value.status == 422


def test_supported_shape_of_ignored_sampling_controls_is_named_without_values():
    p = parse('chat', temperature=.5, top_p=.9, seed=123, frequency_penalty=1, presence_penalty=0,
              top_k=20, logprobs=False, top_logprobs=2, metadata={'private': 'DO-NOT-LOG'})
    assert set(p.declared_ignored) == {'temperature', 'top_p', 'seed', 'frequency_penalty', 'presence_penalty',
                                      'top_k', 'logprobs', 'top_logprobs', 'metadata'}
    assert 'DO-NOT-LOG' not in json.dumps(p.declared_ignored)


def test_http_compatibility_header_and_journal_expose_names_only(relay, monkeypatch):
    import journal
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    body = _body('chat', temperature=.75, metadata={'secret': 'PRIVATE-VALUE'})
    with relay.request('/v1/chat/completions', body) as response:
        assert response.status == 200
        assert response.headers['x-proxi-ignored-parameters'] == 'temperature,metadata'
    entries = [e for e in log.query() if e['kind'] == 'request.compatibility']
    assert entries and 'PRIVATE-VALUE' not in json.dumps(entries)
    assert entries[0]['data']['parameters'] == ['temperature', 'metadata']


@pytest.mark.parametrize('changes,param', [
    ({'parallel_tool_calls': False}, 'parallel_tool_calls'),
    ({'tool_choice': {'type': 'auto', 'disable_parallel_tool_use': True}}, 'tool_choice.disable_parallel_tool_use'),
    ({'prompt': {'id': 'pmpt_PRIVATE', 'version': '1'}}, 'prompt'),
    ({'service_tier': 'priority'}, 'service_tier'),
])
def test_extra_material_controls_are_not_silently_dropped(changes, param):
    tools = [{'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}}]
    with pytest.raises(apierr.ApiError) as error:
        parse('responses', tools=tools, **changes)
    assert error.value.status == 422 and error.value.param == param
    assert 'PRIVATE' not in str(error.value)


def test_optional_cache_and_include_hints_are_typed_and_named_as_ignored():
    p = parse('responses', include=['message.output_text.logprobs'], prompt_cache_key='PRIVATE',
              prompt_cache_retention='24h', service_tier='default')
    assert set(p.declared_ignored) == {'include', 'prompt_cache_key', 'prompt_cache_retention'}
    assert 'PRIVATE' not in json.dumps(p.declared_ignored)
    for extra in ({'include': [1]}, {'prompt_cache_key': 1}, {'prompt_cache_retention': 'invalid'},
                  {'parallel_tool_calls': 'no'}, {'tool_choice': {'type': 'auto', 'disable_parallel_tool_use': 'yes'}}):
        with pytest.raises(apierr.ApiError):
            parse('responses', **extra)


def test_codex_discovery_exposes_actual_verbosity_capability():
    import models
    by_id = {m['slug']: m for m in models.codex_catalog()['models']}
    assert by_id['gpt-6-sol']['support_verbosity'] is True
    assert by_id['gpt-5.5']['support_verbosity'] is True
    assert by_id['claude-opus-5-5']['support_verbosity'] is False


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_changing_only_verbosity_preserves_native_history_and_forwards_current_value(tmp_path, monkeypatch, dialect):
    from test_native_history import harness, close, append_result, tool_reply, strip_cache
    from test_native_context_reuse import FIRST, request, run
    from test_review_fixes_pr19 import END, text
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), tool_reply('c2', 'opaque-two'), [text('done'), *END]])
    async def scenario():
        messages = copy.deepcopy(FIRST)
        try:
            for level in ('low', 'high', None):
                p = request(messages, dialect=dialect)
                p.verbosity = level
                events = await run(runner, p)
                assert events[-1] == {'type': 'end', 'reason': 'tool_calls' if level is not None else 'stop'}
                if level is not None:
                    messages = append_result(messages, p, events, dialect)
        finally:
            await close(pool)
    asyncio.run(scenario())
    assert [b.get('verbosity') for b in sent] == ['low', 'high', None]
    for previous, following, signature in zip(sent, sent[1:], ('opaque-one', 'opaque-two')):
        assert strip_cache(following['messages'][:len(previous['messages'])]) == strip_cache(previous['messages'])
        assert signature in json.dumps(following)
        assert following['openAICacheKey'] == previous['openAICacheKey']
