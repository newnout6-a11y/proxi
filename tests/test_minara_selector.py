"""The separate Minara selector: measured catalog, cache marks, reasoning round trip, channel failures, forced choice,
credits. Measurements behind these rules: docs/minara.md."""
import asyncio
import json
import types

import pytest

import accounts
import apierr
import minara
import minara_catalog
import models


MEASURED = {
    'anthropic/claude-opus-5.5': {
        'id': 'minara/claude-opus-5-5', 'upstream_model': 'anthropic/claude-opus-5.5', 'label': 'Claude Opus 5.5',
        'provider': 'anthropic', 'status': 'ok', 'tools': True, 'vision': True, 'reasoning': True,
        'forced_tool_choice': False, 'thinking': 'stream', 'thinking_signature': True,
        'reasoning_effort_levels': ['low', 'medium', 'high', 'xhigh', 'max'], 'cache': 'explicit',
        'pricing': {'quota_type': 0, 'prompt': 4, 'completion': 20, 'cache_read': 0.2, 'cache_write': 5}},
    'openai/gpt-6.1-sol': {
        'id': 'minara/gpt-6.1-sol', 'upstream_model': 'openai/gpt-6.1-sol', 'label': 'GPT 6.1 Sol',
        'provider': 'openai', 'status': 'ok', 'tools': True, 'vision': True, 'reasoning': True,
        'forced_tool_choice': True, 'thinking': 'end', 'reasoning_effort_levels': [], 'cache': 'automatic',
        'pricing': {'quota_type': 0, 'billing_mode': 'tiered_expr', 'billing_expr': 'p * 2'}},
    'minimax/minimax-m3': {
        'id': 'minara/minimax-m3', 'upstream_model': 'minimax/minimax-m3', 'provider': 'minimax',
        'status': 'broken', 'tools': None, 'vision': None, 'reasoning': False},
}


@pytest.fixture
def measured(monkeypatch):
    monkeypatch.setattr(minara_catalog, 'MEASURED', MEASURED)
    return MEASURED


def account(**extra):
    return dict(upstream='minara', bearer_token='synthetic-test-bearer', **extra)


def test_an_account_without_a_list_serves_every_published_model(measured):
    routes = minara_catalog.build([account()])
    assert set(routes) == {'minara/claude-opus-5-5', 'minara/gpt-6.1-sol'}  # the broken MiniMax is not published
    opus = routes['minara/claude-opus-5-5']
    assert opus['upstream'] == 'minara' and opus['upstream_model'] == 'anthropic/claude-opus-5.5'
    assert opus['forced_tool_choice'] is False and opus['reasoning_effort_control'] == 'selectable'
    # A model that reasons with unmeasured levels passes the client's level on.
    assert routes['minara/gpt-6.1-sol']['reasoning_effort_control'] == 'passthrough'


def test_a_list_takes_upstream_ids_published_names_and_the_older_spelling(measured):
    routes = minara_catalog.build([account(minara_models=[
        'anthropic/claude-opus-5.5', 'minara/gpt-6.1-sol', {'id': 'minara/x-ai/grok-4.7', 'tools': False}])])
    assert set(routes) == {'minara/claude-opus-5-5', 'minara/gpt-6.1-sol', 'minara/grok-4.7'}
    assert routes['minara/grok-4.7']['tools'] is False  # an override wins over the (absent) measurement


def test_minara_names_resolve_only_to_minara_and_never_reach_retool(measured):
    cfg = {'_minara_models': minara_catalog.build([account()])}
    entry, effort = models.resolve('minara/claude-opus-5-5', 'max', cfg=cfg)
    assert entry['upstream'] == 'minara' and effort == 'max'
    # The older minara/<upstream id> spelling is an alias of the same route.
    assert models.resolve('minara/anthropic/claude-opus-5.5', cfg=cfg)[0] is entry
    assert models.resolve('minara/gpt-6.1-sol', 'xhigh', cfg=cfg)[1] == 'xhigh'
    retool, _ = models.resolve('claude-opus-5-5', cfg=cfg)
    assert retool.get('upstream') != 'minara'
    assert models.route_block_reason(entry, {'upstream': 'retool'}) == 'route requires a Minara account'
    assert models.route_block_reason(retool, account()) == 'route requires a Retool account'
    with pytest.raises(apierr.ApiError):
        models.resolve('minara/claude-opus-4-1', cfg=cfg)


def history():
    return [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'read the file'}]},
        {'role': 'assistant', 'content': [{'type': 'tool_call', 'toolCallId': 'call_1', 'toolName': 'read',
                                           'toolInput': {'path': 'a'}}]},
        {'role': 'tool', 'content': {'toolCallId': 'call_1', 'toolResult': 'FILE CONTENT'}},
    ]


def marks(body):
    found = []
    for index, message in enumerate(body['messages']):
        for part in message['content'] if isinstance(message.get('content'), list) else []:
            if isinstance(part, dict) and 'cache_control' in part:
                found.append((index, message['role'], part['cache_control']))
    return found


def test_claude_gets_cache_breakpoints_with_a_one_hour_ttl_and_other_models_none():
    session = types.SimpleNamespace(_native_client_messages=history(), _native_allow_cold_replay=True)
    body = minara.build_body({}, {'model': 'anthropic/claude-opus-5.5'}, 'SYSTEM', '', None, None, 100, session=session)
    one_hour = {'type': 'ephemeral', 'ttl': '1h'}
    # The system head (tools come before it), the last tool result, and the end of the turn before the latest reply.
    assert marks(body) == [(0, 'system', one_hour), (1, 'user', one_hour), (3, 'tool', one_hour)]
    plain = minara.build_body({}, {'model': 'openai/gpt-6.1-sol'}, 'SYSTEM', '', None, None, 100, session=session)
    assert marks(plain) == []  # GPT, Grok, GLM and DeepSeek cache by themselves
    short = minara.build_body({'minara_cache_ttl': '5m'}, {'model': 'anthropic/claude-opus-5.5'}, 'SYSTEM', '', None,
                              None, 100, session=session)
    assert marks(short)[0][2] == {'type': 'ephemeral', 'ttl': '5m'}
    off = minara.build_body({'direct_cache': False}, {'model': 'anthropic/claude-opus-5.5'}, 'SYSTEM', '', None, None,
                            100, session=session)
    assert marks(off) == []


def test_marks_never_exceed_four_and_skip_parts_that_cannot_carry_one():
    long_history = []
    for turn in range(6):
        long_history += [{'role': 'user', 'content': [{'type': 'text', 'text': 'q%d' % turn}]},
                         {'role': 'assistant', 'content': [{'type': 'text', 'text': 'a%d' % turn}]}]
    long_history.append({'role': 'user', 'content': [{'type': 'image', 'imageData': 'AAAA', 'mimeType': 'image/png'}]})
    session = types.SimpleNamespace(_native_client_messages=long_history, _native_allow_cold_replay=True)
    body = minara.build_body({}, {'model': 'anthropic/claude-opus-5.5'}, 'SYSTEM', '', None, None, 100, session=session)
    found = marks(body)
    assert len(found) <= 4
    # The image-only tail passes its mark to the message before it.
    assert all(body['messages'][index]['content'][-1].get('type') == 'text' for index, _, _ in found)


def stream_chunks(*deltas, finish='stop', usage=None):
    lines = []
    for delta in deltas:
        lines.append(json.dumps({'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}))
    last = {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}
    if usage is not None:
        last['usage'] = usage
    lines.append(json.dumps(last))
    lines.append('[DONE]')
    return lines


def test_reasoning_details_are_shown_once_and_signed_and_never_sent_back():
    parser = minara.EventParser()
    events = []
    for data in stream_chunks(
            {'reasoning': 'Think', 'reasoning_details': [{'type': 'reasoning.text', 'text': 'Think', 'format': 'anthropic-claude-v1', 'index': 0}]},
            {'reasoning': 'ing.', 'reasoning_details': [{'type': 'reasoning.text', 'text': 'ing.', 'format': 'anthropic-claude-v1', 'index': 0, 'signature': 'SIG=='}]},
            {'tool_calls': [{'index': 0, 'id': 'call_9', 'function': {'name': 'read', 'arguments': '{"path": "b"}'}}]},
            finish='tool_calls'):
        events.extend(parser.feed(data))
    thinking = ''.join(e['delta'] for e in events if e['type'] == 'thinking')
    assert thinking == 'Thinking.'  # `reasoning` and reasoning_details carry the same text: shown once
    assert [e['signature'] for e in events if e['type'] == 'thinking_signature'] == ['SIG==']
    assert parser.reply_key() == ('calls', 'call_9')
    assert parser.reasoning_details() == [{'type': 'reasoning.text', 'text': 'Thinking.', 'format': 'anthropic-claude-v1',
                                           'signature': 'SIG=='}]
    session = types.SimpleNamespace(_native_allow_cold_replay=True, _native_client_messages=[
        {'role': 'user', 'content': [{'type': 'text', 'text': 'go'}]},
        {'role': 'assistant', 'content': [{'type': 'tool_call', 'toolCallId': 'call_9', 'toolName': 'read', 'toolInput': {'path': 'b'}}]},
        {'role': 'tool', 'content': {'toolCallId': 'call_9', 'toolResult': 'ok'}}])
    body = minara.build_body({}, {'model': 'anthropic/claude-opus-5.5'}, '', '', None, None, 100, session=session)
    reply = next(m for m in body['messages'] if m['role'] == 'assistant')
    assert 'reasoning_details' not in reply  # Minara does not pass them on (measured on ten models, 2026-11-10)


def test_encrypted_reasoning_is_never_shown():
    parser = minara.EventParser()
    events = []
    for data in stream_chunks({'reasoning_details': [{'type': 'reasoning.encrypted', 'data': 'gAAA', 'index': 0, 'format': 'openai-responses-v1'}]},
                              {'content': 'Answer'}):
        events.extend(parser.feed(data))
    assert [e['type'] for e in events if e['type'].startswith('thinking')] == []
    assert parser.reasoning_details()[0]['data'] == 'gAAA'
    assert parser.reply_key()[0] == 'text'


def test_an_error_inside_a_200_chunk_is_an_upstream_error():
    parser = minara.EventParser()
    with pytest.raises(apierr.ApiError, match='invalid api key'):
        parser.feed(json.dumps({'base_resp': {'status_code': 2049, 'status_msg': 'invalid api key'}, 'usage': {'prompt_tokens': 3}}))


def test_flat_and_tiered_prices_estimate_the_cost_minara_does_not_report():
    flat = {'quota_type': 0, 'prompt': 4, 'completion': 20, 'cache_read': 0.2, 'cache_write': 5}
    usage = minara.normalize_usage({'prompt_tokens': 1100, 'completion_tokens': 50,
                                    'prompt_tokens_details': {'cached_tokens': 1000}}, flat)
    assert usage['provider_cost'] == pytest.approx((100 * 4 + 50 * 20 + 1000 * 0.2) / 1e6)
    assert usage['provider_cost_estimated'] is True and usage['provider_cost_currency'] == 'USD'
    # GPT-6.1 Sol's tiered price: the tier is chosen by the whole prompt (len)
    gpt = {'quota_type': 0, 'billing_mode': 'tiered_expr', 'billing_expr':
           'len > 272000 ? (p * 4 + c * 15 + cr * 0.2 + cc * 5) : (p * 2 + c * 10 + cr * 0.1 + cc * 2.5)'}
    small = minara.normalize_usage({'prompt_tokens': 1100, 'completion_tokens': 50,
                                    'prompt_tokens_details': {'cached_tokens': 1000}}, gpt)
    assert small['provider_cost'] == pytest.approx((100 * 2 + 50 * 10 + 1000 * 0.1) / 1e6) and small['provider_cost_estimated']
    large = minara.normalize_usage({'prompt_tokens': 300000, 'completion_tokens': 50}, gpt)
    assert large['provider_cost'] == pytest.approx((300000 * 4 + 50 * 15) / 1e6)
    # Haiku 5.5: Minara bills every write at cc, an hour-long one too (its debits matched cc, not cc1h)
    haiku = {'quota_type': 0, 'billing_mode': 'tiered_expr', 'billing_expr': 'len > 100000 ? (p * 0.5 + c * 2.5 + cr * 0.05 '
             '+ cc * 0.625 + cc1h * 1) : (p * 0.1 + c * 0.5 + cr * 0.01 + cc * 0.125 + cc1h * 0.2)', 'group_ratio': 1}
    assert minara.estimated_cost(haiku, {'input_tokens': 4, 'output_tokens': 97, 'cache_write': 10459}) == 0.001356275
    for bad in ('__import__(1)', 'p * ', 'q * 2', '(' * 600 + 'p' + ')' * 600):
        assert minara.estimated_cost({'quota_type': 0, 'billing_mode': 'tiered_expr', 'billing_expr': bad},
                                     {'input_tokens': 1}) is None
    # Minara debits its list, not the cost upstream reports: the list prices the reply, the report is kept for diagnosis
    reported = minara.normalize_usage({'prompt_tokens': 10, 'completion_tokens': 1, 'cost': 0.5}, flat)
    assert reported['provider_cost'] == pytest.approx((10 * 4 + 1 * 20) / 1e6) and reported['upstream_cost'] == 0.5
    # without a readable list the reported cost is what there is
    unlisted = minara.normalize_usage({'prompt_tokens': 10, 'completion_tokens': 1, 'cost': 0.5}, None)
    assert unlisted['provider_cost'] == 0.5 and 'provider_cost_estimated' not in unlisted
    # a reported 0 for a reply that wrote tokens is a cost nobody reported: estimated
    zero = minara.normalize_usage({'prompt_tokens': 10, 'completion_tokens': 5, 'cost': 0}, flat)
    assert zero['provider_cost'] == pytest.approx((10 * 4 + 5 * 20) / 1e6) and zero['provider_cost_estimated']


class Response:
    def __init__(self, status, body=b'', lines=()):
        self.status_code = status
        self.headers = {'content-type': 'text/event-stream' if status == 200 else 'application/json'}
        self._body = body
        self._lines = lines
        self.closed = False

    def iter_content(self, chunk_size=None):
        if self.status_code != 200:
            yield self._body
            return
        for line in self._lines:
            yield ('data: ' + line + '\n\n').encode()

    def close(self):
        self.closed = True


async def collect(cfg, session, provider, tools=None, choice=None):
    return [event async for event in minara.iter_events(dict({'bearer_token': 'secret'}, **cfg), session, provider, 'hi', '',
                                                         tools, choice, 50)]


def test_a_channel_failure_is_asked_again_before_anything_is_generated():
    answers = [Response(500, b'{"error":{"code":"channel_selection_error","message":"Unable to route model x"}}'),
               Response(503, b''), Response(200, lines=stream_chunks({'content': 'OK'}))]
    posts = []

    def post(*args, **kwargs):
        posts.append(kwargs['json']['model'])
        return answers.pop(0)
    session = types.SimpleNamespace(backend=types.SimpleNamespace(_minara_post=post), _cancelled=False)
    events = asyncio.run(collect({'minara_channel_retry_delays': [0, 0]}, session, {'model': 'openai/x'}))
    assert [e['delta'] for e in events if e['type'] == 'text'] == ['OK'] and len(posts) == 3


def test_a_lasting_channel_failure_is_a_503_with_retry_after_and_a_real_refusal_stays_400():
    session = types.SimpleNamespace(backend=types.SimpleNamespace(
        _minara_post=lambda *a, **kw: Response(400, b'{"error":{"message":"Unknown or unsupported model: openai/x"}}')),
        _cancelled=False)
    with pytest.raises(apierr.ApiError) as raised:
        asyncio.run(collect({'minara_channel_retry_delays': [0]}, session, {'model': 'openai/x'}))
    assert raised.value.status == 503 and raised.value.retry_after == minara.CHANNEL_RETRY_AFTER
    session.backend._minara_post = lambda *a, **kw: Response(400, b'{"error":{"message":"bad schema"}}')
    with pytest.raises(apierr.ApiError) as raised:
        asyncio.run(collect({'minara_channel_retry_delays': [0]}, session, {'model': 'openai/x'}))
    assert raised.value.status == 400


TOOLS = [{'type': 'function', 'function': {'name': 'get_weather', 'parameters': {'type': 'object', 'properties': {}}}}]


def test_a_forced_tool_choice_on_a_route_that_refuses_it_is_asked_in_words():
    bodies = []
    replies = [stream_chunks({'content': 'It is sunny.'}),
               stream_chunks({'tool_calls': [{'index': 0, 'id': 'call_1', 'function': {'name': 'get_weather', 'arguments': '{}'}}]},
                             finish='tool_calls')]

    def post(*args, **kwargs):
        bodies.append(kwargs['json'])
        return Response(200, lines=replies.pop(0))
    session = types.SimpleNamespace(backend=types.SimpleNamespace(_minara_post=post), _cancelled=False)
    provider = {'model': 'anthropic/claude-opus-5.5', 'refuses_forced_tool_choice': True}
    events = asyncio.run(collect({}, session, provider, TOOLS, 'required'))
    assert [body['tool_choice'] for body in bodies] == ['auto', 'auto']
    assert bodies[0]['messages'][-1]['role'] == 'user' and 'Call one of the available tools' in bodies[0]['messages'][-1]['content']
    assert 'IMPORTANT' in bodies[1]['messages'][-1]['content']  # the one firm retry
    # Nothing of the attempt that answered in text reaches the client.
    assert [e['type'] for e in events if e['type'] in ('text', 'tool_call')] == ['tool_call', 'tool_call']
    assert events[-1] == {'type': 'end', 'reason': 'tool_calls'}


def test_a_route_that_takes_forced_choices_gets_them_natively():
    bodies = []

    def post(*args, **kwargs):
        bodies.append(kwargs['json'])
        return Response(200, lines=stream_chunks({'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'get_weather', 'arguments': '{}'}}]},
                                                 finish='tool_calls'))
    session = types.SimpleNamespace(backend=types.SimpleNamespace(_minara_post=post), _cancelled=False)
    asyncio.run(collect({}, session, {'model': 'openai/gpt-6.1-sol'}, TOOLS, 'required'))
    assert bodies[0]['tool_choice'] == 'required' and bodies[0]['messages'][-1]['content'] == 'hi'


def test_minara_credits_come_from_auth_me_and_reported_costs_are_taken_off_between_reads():
    reads = []

    def fetch():
        reads.append(1)
        return {'creditBalanceBreakdown': {'plan': '29.5', 'extra': '200'}, 'creditBalance': {'total': '300', 'reward': '200'}}
    manager = accounts.MinaraAccount({'base': 'https://api.minara.ai/v1', 'bearer_token': 't'}, fetch=fetch)
    info = manager.get_balance()
    assert info['available'] == 229.5 and info['monthly_allocation'] == 500 and info['unit'] == 'minara_credits'
    assert info['usd'] == round(229.5 / 220, 4)
    manager.record_provider_usage('a1', 'turn', {'provider_cost': 0.5, 'provider_cost_currency': 'USD'}, final=True)
    assert manager.balance_snapshot()['available'] == pytest.approx(229.5 - 110)
    assert manager.get_balance() == info and len(reads) == 1  # cached between reads
    manager.get_balance(force_refresh=True)
    assert len(reads) == 2 and manager.balance_snapshot()['available'] == 229.5


def test_an_unreadable_balance_is_unknown_not_zero():
    def fetch():
        raise ConnectionError('down')
    info = accounts.MinaraAccount({'bearer_token': 't'}, fetch=fetch).get_balance()
    assert info['available'] is None and info['error'] == 'ConnectionError'


class Backend:
    def __init__(self, cfg):
        self.cfg = cfg
        self.unavailable = None


def test_a_minara_account_below_its_own_threshold_leaves_the_pool():
    backend = Backend({'upstream': 'minara'})
    manager = accounts.MinaraAccount({'bearer_token': 't'}, fetch=lambda: {'creditBalanceBreakdown': {'plan': '-2', 'extra': '12'}})
    dispatcher = accounts.AccountDispatcher([backend], {'minara_min_credits': 15, 'min_account_balance': 1},
                                            {backend: manager}, clock=lambda: 1000)
    assert dispatcher.threshold_of(backend) == 15
    assert dispatcher.check_balance(backend) is True
    assert backend.unavailable['reason'] == 'exhausted' and backend.unavailable['threshold'] == 15


def test_a_402_keeps_the_account_out_until_its_balance_grows():
    balance = {'plan': '-3', 'extra': '200'}
    manager = accounts.MinaraAccount({'bearer_token': 't'}, fetch=lambda: {'creditBalanceBreakdown': dict(balance)})
    assert manager.get_balance()['available'] == 197
    manager.note_refusal()  # Minara said 402 although plan + extra looked positive
    assert manager.get_balance(force_refresh=True)['available'] == 0 and manager.get_balance()['refused']
    balance['plan'] = '300'  # topped up
    assert manager.get_balance(force_refresh=True)['available'] == 500 and not manager.get_balance()['refused']


def test_a_config_from_the_unified_routing_still_loads_with_a_warning(measured):
    import config
    record = account(minara_models=[{'id': 'anthropic/claude-opus-5.5', 'public_id': 'claude-opus-5-5',
                                     'reasoning_effort_levels': ['high']}])
    warnings = []
    config._check_upstream(record, 'account #25', warnings)
    assert warnings and 'public_id no longer used' in warnings[0]
    routes = minara_catalog.build([record])
    assert list(routes) == ['minara/claude-opus-5-5']  # never the Retool name
    assert routes['minara/claude-opus-5-5']['reasoning_effort_levels'] == ['high']


def test_minara_credits_never_join_retool_balance_totals():
    import account_report
    retool = {'available': 100.0, 'monthly_allocation': 120.0, 'unit': 'credits'}
    minara_a = {'available': 193.4, 'monthly_allocation': 500.0, 'unit': 'minara_credits', 'balance_supported': True}
    minara_b = {'available': None, 'unit': 'minara_credits', 'balance_supported': True}
    report = account_report.aggregate_balance([retool, minara_a, minara_b])
    assert report['available'] == 100.0 and report['monthly_allocation'] == 120.0
    assert report['accounts_total'] == 1 and not report['partial']
    assert report['minara'] == {'available': 193.4, 'unit': 'minara_credits', 'usd': round(193.4 / 220, 4),
                                'accounts_total': 2, 'unknown_accounts': 1}
    only = account_report.aggregate_balance([minara_a])
    assert only['available'] is None and only['minara']['available'] == 193.4


def test_banner_shows_minara_credits_apart_from_retool():
    import proxy
    cfg = {'host': '127.0.0.1', 'port': 8787, 'relay_api_key': 'k' * 40, 'model': 'claude-opus-5-5',
           'reasoning_effort': 'auto', 'session_mode': 'per_conversation', 'workspace_root': '.',
           'exec_policy': 'off', 'max_exec_turns': 0}
    lines = proxy.banner_lines(cfg, {'available': 100.0, 'monthly_allocation': 120.0, 'accounts': [{}],
                                     'minara': {'available': 409.5, 'usd': 1.8614, 'accounts_total': 2,
                                                'unknown_accounts': 0}})
    assert any(line.startswith(' balance:       100.0 / 120.0') for line in lines)
    assert ' minara:        409.5 credits (~$1.8614) on 2 accounts' in lines
    alone = proxy.banner_lines(cfg, {'available': None, 'accounts': [{'balance_supported': True}],
                                     'minara': {'available': 12.0, 'usd': 0.0545, 'accounts_total': 1,
                                                'unknown_accounts': 0}})
    assert not any('DEGRADED' in line for line in alone)


def test_client_catalogs_list_the_minara_models_of_the_relay_config(tmp_path, measured):
    from scripts import probe_client_settings
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'accounts': [{'label': 'M', 'upstream': 'minara', 'bearer_token': 'synthetic'}],
                                'model': 'minara/claude-opus-5-5'}), encoding='utf-8')
    ids = {entry['id'] for entry in probe_client_settings.routable_entries(str(path))}
    assert {'minara/claude-opus-5-5', 'minara/gpt-6.1-sol'} <= ids and 'minara/minimax-m3' not in ids
    opus = next(entry for entry in probe_client_settings.routable_entries(str(path)) if entry['id'] == 'minara/claude-opus-5-5')
    assert opus['context'] == models._BY_ID['claude-opus-5-5']['context']  # the model's own window, from the audit


def test_the_generator_fills_limits_reasoning_and_inherited_effort():
    from scripts import probe_minara as probe
    ladder = {level: {'accepted': True, 'reasoning_tokens': tokens} for level, tokens in
              zip(probe.EFFORTS, (0, 10, 20, 30, 40, 50, 60))}
    gpt_ladder = dict(ladder, none={'accepted': False, 'status': 400, 'detail': 'does not support reasoning effort "none"'},
                      minimal={'accepted': False, 'status': 400, 'detail': 'does not support reasoning effort "minimal"'})
    ok = {'status': 'ok', 'tools': True, 'id': 'gen-1', 'served': None}
    results = {
        'anthropic/claude-opus-5.5': {'tools': dict(ok), 'thinking': {'status': 'ok', 'thinking': 'stream'},
                                      'effort': {'status': 'ok', 'ladder': ladder}},
        'anthropic/claude-fable-5.1': {'tools': dict(ok), 'thinking': {'status': 'ok', 'thinking': 'none', 'reasoning_tokens': 0}},
        'openai/gpt-6.1-sol': {'tools': dict(ok, id='resp_1'), 'thinking': {'status': 'ok', 'thinking': 'end'},
                               'effort': {'status': 'ok', 'ladder': gpt_ladder}},
        'openai/gpt-6.1-sol-pro': {'tools': dict(ok), 'thinking': {'status': 'ok', 'thinking': 'stream'}},
        'x-ai/grok-4.7': {'tools': dict(ok), 'thinking': {'status': 'ok', 'thinking': 'end'}},
    }
    listing = [{'id': name, 'supported_endpoint_types': ['openai']} for name in results]
    published = {
        'anthropic/claude-fable-5.1': {'context_length': 1000000, 'supported_parameters': ['tools', 'reasoning'],
                                       'top_provider': {'context_length': 1000000, 'max_completion_tokens': 128000}},
        'x-ai/grok-4.7': {'context_length': 500000, 'top_provider': {'context_length': 500000, 'max_completion_tokens': 450000}},
    }
    models_ = {entry['upstream_model']: entry for entry in probe.generate(results, listing, published)['models']}
    fable = models_['anthropic/claude-fable-5.1']
    # Claude 4.x/Fable think only when asked: OpenRouter's parameters say it reasons, the vendor's ladder gives the levels.
    assert fable['reasoning'] and fable['reasoning_effort_levels'] == list(probe.EFFORTS)
    assert fable['effort_source'].startswith('vendor:') and (fable['context'], fable['max_output']) == (1000000, 128000)
    # A pro tier of GPT-6.1 takes the GPT-6.1 line's ladder (no none/minimal), not GPT-6's.
    assert models_['openai/gpt-6.1-sol-pro']['reasoning_effort_levels'] == ['low', 'medium', 'high', 'xhigh', 'max']
    # OpenRouter's whole-budget "max_completion_tokens" (450k of a 500k window) is capped at a real reply size.
    assert models_['x-ai/grok-4.7']['max_output'] == probe.OUTPUT_CEILING and models_['x-ai/grok-4.7']['context'] == 500000
    assert probe._family('openai/gpt-6.1-terra') == 'gpt-6.1' and probe._family('openai/gpt-6-luna') == 'gpt-6'
