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


@pytest.mark.parametrize('model', ['claude-sonnet-5-5', 'claude-opus-5-5', 'gpt-4.1'])
@pytest.mark.parametrize('value', [False, 1, [], {}, '', 'extreme', 'LOW'])
def test_invalid_verbosity_is_still_rejected_where_it_would_be_ignored(model, value):
    for dialect, field in (('chat', {'verbosity': value}), ('responses', {'text': {'verbosity': value}})):
        data = _body(dialect, model=model, **field)
        with pytest.raises(apierr.ApiError) as error:
            getattr(validate, 'parse_' + dialect)(data, CFG)
        assert error.value.status == 422 and error.value.param in ('verbosity', 'text.verbosity')


def test_conflicting_verbosity_values_are_rejected_with_and_without_levels():
    with pytest.raises(apierr.ApiError, match='conflicting'):
        parse('responses', verbosity='low', text={'verbosity': 'high'})
    with pytest.raises(apierr.ApiError, match='conflicting'):
        validate.parse_responses(_body('responses', model='claude-sonnet-5-5', verbosity='low',
                                       text={'verbosity': 'high'}), CFG)


@pytest.mark.parametrize('model', ['claude-sonnet-5-5', 'claude-opus-5-5', 'gpt-4.1', 'gemini-2.5-pro'])
def test_verbosity_is_an_optional_hint_that_models_without_levels_ignore(model):
    chat = validate.parse_chat(_body('chat', model=model, verbosity='low'), CFG)
    assert chat.verbosity is None and chat.declared_ignored == ['verbosity']
    responses = validate.parse_responses(_body('responses', model=model, text={'verbosity': 'high'}), CFG)
    assert responses.verbosity is None and responses.declared_ignored == ['text.verbosity']
    both = validate.parse_responses(_body('responses', model=model, verbosity='medium',
                                          text={'verbosity': 'medium'}), CFG)
    assert both.verbosity is None and both.declared_ignored == ['verbosity', 'text.verbosity']
    anthropic = validate.parse_anthropic(_body('anthropic', model=model, verbosity='high'), CFG)
    assert anthropic.verbosity is None and anthropic.declared_ignored == ['verbosity']


def test_verbosity_is_still_forwarded_where_the_catalog_has_levels():
    p = validate.parse_chat(_body('chat', model='gpt-5.5', verbosity='high'), CFG)
    assert p.verbosity == 'high' and p.declared_ignored == []


def test_ignored_verbosity_never_reaches_the_upstream_body(tmp_path, monkeypatch):
    from test_native_context_reuse import run, setup
    from test_service_requests import capture_direct
    _, _, runner = setup(tmp_path, transport='direct', project_context=False, cookie_values=[],
                         provider_resource_name='test-anthropic', openai_provider_resource_name='test-openai')
    sent = capture_direct(monkeypatch)
    ignored = validate.parse_chat(_body('chat', model='claude-sonnet-5-5', verbosity='low'), CFG)
    forwarded = validate.parse_chat(_body('chat', model='gpt-5.5', verbosity='low'), CFG)
    for parsed in (ignored, forwarded):
        events = asyncio.run(run(runner, parsed))
        assert events[-1] == {'type': 'end', 'reason': 'stop'}
    assert 'verbosity' not in sent[0][1] and sent[1][1]['verbosity'] == 'low'


def test_http_ignored_verbosity_is_named_in_header_and_journal(relay, monkeypatch):
    import journal
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    with relay.request('/v1/chat/completions', _body('chat', verbosity='low')) as response:
        assert response.status == 200
        assert response.headers['x-proxi-ignored-parameters'] == 'verbosity'
    with relay.request('/v1/responses', _body('responses', model='claude-sonnet-5-5',
                                              text={'verbosity': 'high'})) as response:
        assert response.status == 200
        assert response.headers['x-proxi-ignored-parameters'] == 'text.verbosity'
    assert [seen.verbosity for seen in relay.fake.seen] == [None, None]
    entries = [e for e in log.query() if e['kind'] == 'request.compatibility']
    assert [e['data']['parameters'] for e in entries] == [['verbosity'], ['text.verbosity']]


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('changes,param', [
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
    assert p.declared_ignored == [] and p.structured_output is None


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


def test_codex_cache_options_and_client_metadata_are_named_as_ignored_without_values():
    p = parse('responses', prompt_cache_key='PRIVATE-KEY', prompt_cache_options={'ttl': 'PRIVATE-TTL'},
              client_metadata={'x-codex-installation-id': 'PRIVATE-ID'})
    assert set(p.declared_ignored) == {'prompt_cache_key', 'prompt_cache_options', 'client_metadata'}
    assert 'PRIVATE' not in json.dumps(p.declared_ignored)
    for name, value in (('prompt_cache_options', 'PRIVATE'), ('client_metadata', ['PRIVATE'])):
        with pytest.raises(apierr.ApiError) as error:
            parse('responses', **{name: value})
        assert error.value.status == 422 and error.value.param == name
        assert 'PRIVATE' not in str(error.value)


def test_anthropic_task_budget_is_named_as_ignored_without_its_values():
    p = parse('anthropic', output_config={'task_budget': {'type': 'tokens', 'total': 123456, 'remaining': 654321}})
    assert p.declared_ignored == ['output_config.task_budget']
    assert '123456' not in json.dumps(p.declared_ignored)
    assert parse('anthropic', output_config={}).declared_ignored == []
    with pytest.raises(apierr.ApiError) as error:
        parse('anthropic', output_config={'task_budget': 'PRIVATE'})
    assert error.value.status == 422 and error.value.param == 'output_config.task_budget'
    assert 'PRIVATE' not in str(error.value)


def test_http_header_and_journal_name_the_new_ignored_controls_without_values(relay, monkeypatch):
    import journal
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    codex = _body('responses', prompt_cache_options={'ttl': 'PRIVATE-TTL'}, client_metadata={'id': 'PRIVATE-ID'})
    with relay.request('/v1/responses', codex) as response:
        assert response.status == 200
        assert response.headers['x-proxi-ignored-parameters'] == 'prompt_cache_options,client_metadata'
    claude = _body('anthropic', output_config={'task_budget': {'type': 'tokens', 'total': 99887}})
    with relay.request('/v1/messages', claude) as response:
        assert response.status == 200
        assert response.headers['x-proxi-ignored-parameters'] == 'output_config.task_budget'
    entries = [e for e in log.query() if e['kind'] == 'request.compatibility']
    assert [e['data']['parameters'] for e in entries] == [['prompt_cache_options', 'client_metadata'],
                                                          ['output_config.task_budget']]
    assert 'PRIVATE' not in json.dumps(entries) and '99887' not in json.dumps(entries)


def test_verbosity_registry_marks_live_confirmed_models_and_keeps_claude_out():
    import models
    live = {'gpt-5', 'gpt-5.1', 'gpt-5.2', 'gpt-5.4', 'gpt-5.5', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna',
            'gpt-6-sol'}
    by_id = {entry['id']: entry for entry in models.CATALOG}
    for name in live:
        assert by_id[name]['verbosity_levels'] == ['low', 'medium', 'high'], name
        assert by_id[name]['verbosity_source'] == 'live Retool route', name
        assert by_id[name]['verbosity_checked_at'] == '2026-11-05', name
    # Unavailable on the route and never probed: registry source stays, nothing claims a live check.
    assert by_id['gpt-5.3-codex']['verbosity_levels'] and by_id['gpt-5.3-codex']['verbosity_source'].startswith('https://')
    leveled = {name for name, entry in by_id.items() if entry['verbosity_levels']}
    assert leveled == live | {'gpt-5.3-codex'}
    assert not by_id['claude-sonnet-5-5']['verbosity_levels'] and 'verbosity_source' not in by_id['claude-sonnet-5-5']


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
