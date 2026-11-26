"""What clients get by default for Minara models, and what the relay says about Minara's answers.

The level a client that must name one sends (Codex) is the one that reasons as the model does with none sent; OpenCode
sends none; Minara's error words reach the client; costs and uncached answers are in the journal."""
import json

import pytest

import minara
import minara_catalog
import models
from scripts import probe_minara as probe
from scripts import sync_opencode_catalog as opencode
from test_minara_effort import study


def test_the_natural_level_reasons_like_the_models_own_default():
    # Sonnet 5.5: medium and below do not reason, none/high/xhigh reason like the default, max more
    sonnet = probe.effort_tiers(study(default=179, none=166, minimal=0, low=0, medium=0, high=170, xhigh=160, max=522))
    assert sonnet['natural'] == 'high' and sonnet['map']['medium'] == 'medium'
    # Opus 5.5: everything up to xhigh reasons like the default
    opus = probe.effort_tiers(study(default=200, none=202, minimal=180, low=189, medium=194, high=186, xhigh=215, max=381))
    assert opus['natural'] == 'medium'
    # Haiku 4.5: no reasoning by default, every level but none turns it on
    haiku = probe.effort_tiers(study(default=0, none=0, minimal=1700, low=1700, medium=1700, high=1700, xhigh=1700, max=1700))
    assert haiku['natural'] == 'none'


def route(**extra):
    entry = {'id': 'minara/x', 'upstream_model': 'v/x', 'provider': 'v', 'status': 'ok', 'reasoning': True,
             'reasoning_effort_levels': ['max'], 'effort_control': 'mapped',
             'effort_map': {level: level for level in models.EFFORT_ORDER}, 'natural_effort': 'medium'}
    entry.update(extra)
    return entry


@pytest.fixture
def catalog(monkeypatch):
    def build(**extra):
        monkeypatch.setattr(minara_catalog, 'MEASURED', {'v/x': route(**extra)})
        built = models.build_minara_catalog([{'upstream': 'minara', 'bearer_token': 't'}])
        return next(iter(built.values()))
    return build


def test_codex_sends_the_natural_level_by_default_and_lists_it_beside_the_offered_ones(catalog):
    entry = catalog()
    row = models.codex_catalog([entry])['models'][0]
    assert row['default_reasoning_level'] == 'medium'
    assert [level['effort'] for level in row['supported_reasoning_levels']] == ['medium', 'max', 'ultra']
    # a model whose default lands on a channel without cache sends its catalog default instead
    row = models.codex_catalog([catalog(default_effort='high', natural_effort='high',
                                        reasoning_effort_levels=['low', 'medium', 'high'])])['models'][0]
    assert row['default_reasoning_level'] == 'high'
    assert [level['effort'] for level in row['supported_reasoning_levels']] == ['low', 'medium', 'high']


def test_opencode_gets_no_null_effort_and_a_minara_only_pool_gets_a_minara_small_model(catalog):
    entry = catalog(id='minara/claude-haiku-5-5', upstream_model='anthropic/claude-haiku-5.5', provider='anthropic')
    # a small model of a Retool pool that is gone: a Minara-only pool replaces it with a Minara one
    document = {'provider': {'retool-relay': {'models': {}}}, 'small_model': 'retool-relay/claude-haiku-4-5-20251001'}
    result = opencode.update_document(document, [entry], default_model='minara/claude-haiku-5-5')
    options = result['provider']['retool-relay']['models']['minara/claude-haiku-5-5'].get('options', {})
    assert 'reasoningEffort' not in options  # OpenCode refuses a null; none sent, the model reasons its own way
    assert result.get('small_model') == 'retool-relay/minara/claude-haiku-5-5'
    assert 'null' not in json.dumps(result)  # no other null anywhere


def test_a_provider_down_answer_is_a_channel_failure_and_other_refusals_say_minaras_words():
    down = b'{"error":{"message":"Invalid request. ({\\"raw\\":\\"The request could not be processed. Please check the request and try again.\\"})"}}'
    error = minara._http_error(400, {}, down)
    assert error.status == 503 and error.retry_after == minara.CHANNEL_RETRY_AFTER
    long = b'{"error":{"message":"prompt is too long: 1048577 tokens > 1048576 maximum","type":"invalid_request_error"}}'
    error = minara._http_error(400, {}, long)
    assert error.status == 400 and 'prompt is too long: 1048577 tokens' in error.message
    error = minara._http_error(413, {}, b'<html><body>Request Entity Too Large</body></html>')
    assert error.status == 413 and error.message == 'Minara rejected the upstream request (HTTP 413)'
    secret = b'{"error":{"message":"bad header Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"}}'
    assert 'eyJhbGciOiJIUzI1NiJ9' not in minara._http_error(400, {}, secret).message


def test_usage_says_whether_upstream_reported_a_cache_write():
    openrouter = minara.normalize_usage({'prompt_tokens': 10, 'completion_tokens': 1, 'prompt_tokens_details': {
        'cached_tokens': 0, 'cache_write_tokens': 0}})
    bedrock = minara.normalize_usage({'prompt_tokens': 10, 'completion_tokens': 1, 'prompt_tokens_details': {
        'cached_tokens': 0, 'text_tokens': 0}})
    assert openrouter['cache_write_reported'] is True and bedrock['cache_write_reported'] is False
    assert 'cache_write_reported' not in minara.normalize_usage({'prompt_tokens': 10, 'completion_tokens': 1})


def test_claude_codes_attribution_line_never_reaches_minara():
    billing = 'x-anthropic-billing-header: cc_version=2.1.317.2e4; cc_entrypoint=cli; cch=7f1aa;'
    head = billing + '\nYou are Claude Code. RULE: answer in capitals.'
    body = minara.build_body({}, {'upstream_model': 'anthropic/claude-opus-5.5'}, head, 'hi', None, None, 100)
    system = body['messages'][0]
    text = ''.join(part['text'] for part in system['content']) if isinstance(system['content'], list) else system['content']
    assert text == 'You are Claude Code. RULE: answer in capitals.' and 'billing' not in json.dumps(body)
    assert system['content'][-1]['cache_control']['ttl'] == '1h'  # the instructions keep their cache mark
    # two conversations (two fingerprints) send the same head, so the cache of one serves the other
    other = minara.build_body({}, {'upstream_model': 'anthropic/claude-opus-5.5'},
                              billing.replace('7f1aa', '0c0c0') + '\nYou are Claude Code. RULE: answer in capitals.',
                              'hi', None, None, 100)
    assert other['messages'][0] == system


def test_a_tool_result_of_text_blocks_reaches_the_model_as_its_text():
    blocks = [{'type': 'text', 'text': 'line "one"\nline two'}, {'type': 'text', 'text': '\tindented'}]
    message = minara._message({'role': 'tool', 'content': {'toolCallId': 'c1', 'toolResult': blocks}})
    assert message == {'role': 'tool', 'tool_call_id': 'c1', 'content': 'line "one"\nline two\n\tindented'}
    structured = minara._message({'role': 'tool', 'content': {'toolCallId': 'c2', 'toolResult': {'rows': [1, 2]}}})
    assert structured['content'] == '{"rows": [1, 2]}'  # a JSON value stays JSON
    mixed = minara._message({'role': 'tool', 'content': {'toolCallId': 'c3', 'toolResult': [{'type': 'text', 'text': 'a'}, 3]}})
    assert mixed['content'] == '[{"type": "text", "text": "a"}, 3]'


def _forced_session(answers):
    import types
    posts = []

    def post(*args, **kwargs):
        posts.append(kwargs['json'])
        return answers.pop(0)
    return types.SimpleNamespace(backend=types.SimpleNamespace(_minara_post=post), _cancelled=False), posts


def test_a_refusal_or_a_cap_spent_on_reasoning_is_not_asked_again():
    import asyncio
    from test_minara_selector import Response, stream_chunks
    tools = [{'type': 'function', 'function': {'name': 'get_weather', 'parameters': {'type': 'object', 'properties': {}}}}]
    provider = {'model': 'anthropic/claude-opus-5.5', 'refuses_forced_tool_choice': True}

    async def run(session):
        return [event async for event in minara.iter_events({'bearer_token': 't'}, session, provider, 'hi', '', tools,
                                                            'required', 50)]
    session, posts = _forced_session([Response(200, lines=stream_chunks({'content': ''}, finish='content_filter'))])
    events = asyncio.run(run(session))
    assert len(posts) == 1 and events[-1] == {'type': 'end', 'reason': 'content_filter'}
    session, posts = _forced_session([Response(200, lines=stream_chunks({'reasoning': 'thinking'}, finish='length'))])
    events = asyncio.run(run(session))
    assert len(posts) == 1 and events[-1]['reason'] == 'length'
    # text instead of the call, cut by the cap: that is what the firmer second request is for
    session, posts = _forced_session([Response(200, lines=stream_chunks({'content': 'Sure, let me'}, finish='length')),
                                      Response(200, lines=stream_chunks({'tool_calls': [{'index': 0, 'id': 'c1', 'function': {
                                          'name': 'get_weather', 'arguments': '{}'}}]}, finish='tool_calls'))])
    events = asyncio.run(run(session))
    assert len(posts) == 2 and any(event['type'] == 'tool_call' for event in events)


def test_a_stream_may_stay_silent_as_long_as_the_turn_and_a_curl_timeout_is_a_timeout():
    import asyncio
    import types
    from test_minara_selector import Response, stream_chunks
    seen = {}

    def post(*args, **kwargs):
        seen['timeout'] = kwargs['timeout']
        return Response(200, lines=stream_chunks({'content': 'OK'}))
    session = types.SimpleNamespace(backend=types.SimpleNamespace(_minara_post=post), _cancelled=False)

    async def run(cfg):
        return [event async for event in minara.iter_events(cfg, session, {'model': 'openai/x'}, 'hi', '', None, None, 50)]
    asyncio.run(run({'bearer_token': 't', 'upstream_timeout': 120, 'turn_timeout': 900}))
    # connect in 120 s; curl adds the two: silence allowed for SILENCE_LIMIT, never past the turn's deadline
    assert seen['timeout'][0] == 120 and sum(seen['timeout']) <= minara.SILENCE_LIMIT + 1
    assert sum(seen['timeout']) > 300  # long hidden reasoning still fits

    class CurlError(Exception):
        code = 28

    def slow(*args, **kwargs):
        raise CurlError('curl: (28) Operation too slow. https://api.minara.ai/v1 Bearer secret')
    session.backend._minara_post = slow
    try:
        asyncio.run(run({'bearer_token': 't'}))
    except Exception as error:  # noqa: BLE001
        assert getattr(error, 'code', None) == 'upstream_timeout' and 'secret' not in str(error)
    else:
        raise AssertionError('a curl timeout must fail the turn')


def test_the_first_message_carries_a_mark_when_there_is_no_system_head():
    body = minara.build_body({}, {'model': 'anthropic/claude-opus-5.5'}, '', 'hello there', None, None, 100)
    assert body['messages'][0]['content'][-1]['cache_control']['ttl'] == '1h'


def test_tools_reach_minara_with_their_standard_fields_only():
    tool = {'type': 'function', 'function': {'name': 'apply_patch', 'description': 'patch', 'parameters': {
        'type': 'object'}, 'x-client-type': 'custom', 'x-format': {'grammar': 'start: x'}}}
    body = minara.build_body({}, {'model': 'openai/gpt-6.1-sol'}, '', 'hi', [tool], 'auto', 100)
    assert body['tools'] == [{'type': 'function', 'function': {'name': 'apply_patch', 'description': 'patch',
                                                               'parameters': {'type': 'object'}}}]
    assert 'x-client-type' in tool['function']  # the client's own declaration is left as it was


def test_an_unmeasured_model_an_account_lists_forwards_the_clients_level(monkeypatch):
    monkeypatch.setattr(minara_catalog, 'MEASURED', {'openai/gpt-5.5': {
        'id': 'minara/gpt-5.5', 'upstream_model': 'openai/gpt-5.5', 'provider': 'openai', 'status': 'excluded'}})
    route = minara_catalog.build([{'upstream': 'minara', 'bearer_token': 't', 'minara_models': ['openai/gpt-5.5']}])
    entry = route['minara/gpt-5.5']
    assert entry['reasoning'] and entry['reasoning_effort_control'] == 'passthrough'
    assert entry['metadata_status'] == 'unverified'
    assert models.resolve('minara/gpt-5.5', 'high', cfg={'_minara_models': route})[1] == 'high'


def test_a_cost_recorded_while_a_balance_read_is_under_way_stays_on_the_debit():
    import accounts
    state = {}

    def fetch():
        account._account({'provider_cost': 0.5})  # a turn ends while /auth/me is being read: 110 credits
        return {'creditBalanceBreakdown': {'plan': '500', 'extra': '0'}}
    account = accounts.MinaraAccount({'bearer_token': 't'}, fetch=fetch)
    account._account({'provider_cost': 1.0})  # spent before the read: the read's answer includes it
    account.get_balance(force_refresh=True)
    assert account.balance_snapshot()['available'] == 390  # 500 read, less the 110 spent during the read
    assert state == {}


def test_minara_settings_are_checked_when_the_config_loads(tmp_path):
    import config
    from apierr import ApiError
    for bad in ({'minara_cache_ttl': '60m'}, {'minara_min_credits': 'lots'}, {'minara_channel_retry_delays': [3, 'x']},
                {'minara_channel_retry_delays': [120]}):
        path = tmp_path / 'config.json'
        path.write_text(json.dumps(dict({'upstream': 'minara', 'bearer_token': 'eyJ.synthetic.token',
                                         'relay_api_key': 'k' * 32, 'workspace_root': str(tmp_path)}, **bad)),
                        encoding='utf-8')
        try:
            config.load(str(path))
        except ApiError as error:
            assert 'minara_' in error.message
        else:
            raise AssertionError('%s was accepted' % bad)


def test_the_emulated_choice_is_said_for_a_minara_route_too():
    import forced_choice
    tools = [{'type': 'function', 'function': {'name': 'get_weather', 'parameters': {}}}]
    entry = {'upstream': 'minara', 'forced_tool_choice': False}
    assert forced_choice.requirement('minara/claude-opus-5-5', tools, 'required', entry) is not None
    assert forced_choice.requirement('minara/gpt-6.1-sol', tools, 'required',
                                     {'upstream': 'minara', 'forced_tool_choice': True}) is None


def test_every_mark_of_a_subagent_lasts_an_hour_too():
    # Minara bills a write at its list price whatever the TTL: an hour costs nothing more, so a subagent that waits on a
    # long command still reads its own history when it comes back.
    session = types_module().SimpleNamespace(_native_allow_cold_replay=True, _native_client_messages=[
        {'role': 'user', 'content': [{'type': 'text', 'text': 'first'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'reply'}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'second'}]}])
    head = 'You are a subagent. ' * 100
    body = minara.build_body({}, {'model': 'anthropic/claude-opus-5.5', 'subagent': True}, head, '', None, None, 100,
                             session=session)
    ttls = {part['cache_control'].get('ttl') for message in body['messages'] if isinstance(message['content'], list)
            for part in message['content'] if 'cache_control' in part}
    assert ttls == {'1h'}


def types_module():
    import types
    return types


def test_gpt_and_claude_requests_carry_a_cache_key_per_conversation():
    session = types_module().SimpleNamespace(_native_allow_cold_replay=True, _native_conversation_key=('codex', 'c-1'),
                                             _native_client_messages=[{'role': 'user', 'content': [{'type': 'text',
                                                                                                    'text': 'hi'}]}])
    gpt = minara.build_body({}, {'model': 'openai/gpt-6.1-sol'}, 'head', '', None, None, 100, session=session)
    again = minara.build_body({}, {'model': 'openai/gpt-6.1-sol'}, 'head', '', None, None, 100, session=session)
    assert gpt['prompt_cache_key'].startswith('proxi-') and gpt['prompt_cache_key'] == again['prompt_cache_key']
    assert 'c-1' not in gpt['prompt_cache_key']  # a hash: no client identifier goes upstream
    other = types_module().SimpleNamespace(**dict(vars(session), _native_conversation_key=('codex', 'c-2')))
    assert minara.build_body({}, {'model': 'openai/gpt-6.1-sol'}, 'head', '', None, None, 100,
                             session=other)['prompt_cache_key'] != gpt['prompt_cache_key']
    # Identical Claude heads in different conversations no longer share a routing/cache key.
    claude = minara.build_body({}, {'model': 'anthropic/claude-opus-5.5'}, 'head', '', None, None, 100, session=session)
    claude_other = minara.build_body({}, {'model': 'anthropic/claude-opus-5.5'}, 'head', '', None, None, 100, session=other)
    assert claude['prompt_cache_key'].startswith('proxi-') and claude['prompt_cache_key'] != claude_other['prompt_cache_key']
    assert claude['prompt_cache_key'] != gpt['prompt_cache_key']


def test_a_dump_stays_valid_json_with_its_secrets_masked():
    import journal as journal_module
    token = 'eyJhbGciOiJIUzI1NiJ9.' + 'b' * 300 + '.sig'
    previous = journal_module.JOURNAL._cfg
    journal_module.JOURNAL._cfg = {'cookie_values': [token], 'accounts': []}
    try:
        body = json.dumps({'messages': [{'role': 'tool', 'content': 'api_key=supersecretvalue123 and ' + token},
                                        {'role': 'user', 'content': 'say "hi"\n'}]})
        out = journal_module.redact_json(body)
    finally:
        journal_module.JOURNAL._cfg = previous
    parsed = json.loads(out)
    assert token not in out and parsed['messages'][1]['content'] == 'say "hi"\n'


def test_cookie_patterns_of_several_lists_stay_cached():
    import redact
    lists = [tuple('cookie-value-%d-%d-xxxxxxxx' % (n, m) for m in range(5)) for n in range(4)]
    for values in lists * 2:
        redact.redact('nothing here ' + values[0], cookie_values=values)
    assert all(values in redact._COOKIES for values in lists)


def test_a_channel_gap_is_unknown_and_ties_go_up():
    from scripts import probe_minara as probe
    gap = {'default': 'bedrock', 'none': 'bedrock', 'low': 'openrouter', 'high': 'openrouter', 'max': 'bedrock'}
    levels, mapping, default = probe.cached_levels(gap)
    assert mapping['medium'] == 'medium' and mapping['xhigh'] == 'xhigh'  # never measured: sent as asked
    assert mapping['none'] == 'low' and mapping['max'] == 'high' and default == 'high'
    tie = probe.cached_levels({'default': 'openrouter', 'low': 'openrouter', 'medium': 'bedrock', 'high': 'openrouter'})
    assert tie[1]['medium'] == 'high' and tie[2] is None  # a tie goes up; a caching default sends nothing


def test_a_minara_only_relays_family_default_is_the_newest_model():
    entries = [{'id': 'minara/claude-opus-4-1', 'provider': 'anthropic', 'upstream': 'minara', 'label': 'Opus 4.1'},
               {'id': 'minara/claude-opus-5-5', 'provider': 'anthropic', 'upstream': 'minara', 'label': 'Opus 5.5'},
               {'id': 'minara/claude-opus-4-7', 'provider': 'anthropic', 'upstream': 'minara', 'label': 'Opus 4.7'}]
    defaults = [entry['id'] for entry in entries if models.desktop_metadata(entry, entries)['is_family_default']]
    assert defaults == ['minara/claude-opus-5-5']


def test_a_dump_masks_members_named_like_secrets_and_survives_what_json_cannot_take():
    import journal as journal_module
    body = json.dumps({'messages': [{'role': 'assistant', 'content': [{'type': 'tool_call', 'toolName': 'configure_db',
                                                                       'toolInput': {'password': 'hunter2-not-real',
                                                                                     'api_key': 'abcdefghijklmnop1234',
                                                                                     'host': 'db.local'}}]}]})
    out = journal_module.redact_json(body)
    assert 'hunter2' not in out and 'abcdefghijklmnop1234' not in out and 'db.local' in out
    assert json.loads(out)['messages'][0]['content'][0]['toolInput']['host'] == 'db.local'
    deep = '[' * 5000 + ']' * 5000
    assert journal_module.redact_json(deep) == deep  # too deep for json: masked as text, never lost
    assert minara._upstream_message(('{"error":' * 3000 + '1' + '}' * 3000).encode()) is not None


def test_a_side_request_reads_the_dialogs_latest_round():
    session = types_module().SimpleNamespace(_native_allow_cold_replay=True, _native_client_messages=[
        {'role': 'user', 'content': [{'type': 'text', 'text': 'work'}]},
        {'role': 'assistant', 'content': [{'type': 'tool_call', 'toolCallId': 'c1', 'toolName': 'shell',
                                           'toolInput': {}}]},
        {'role': 'tool', 'content': {'toolCallId': 'c1', 'toolResult': 'latest results'}},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Summarize the conversation.'}]}])
    body = minara.build_body({}, {'model': 'anthropic/claude-opus-5.5'}, 'You are an agent. ' * 100, '', None, None, 100,
                             session=session, mark_tail=False)
    marked = [index for index, message in enumerate(body['messages']) if isinstance(message.get('content'), list)
              and any('cache_control' in part for part in message['content'] if isinstance(part, dict))]
    assert marked[-1] == 3 and 4 not in marked  # the latest tool result is marked, the instruction is not
