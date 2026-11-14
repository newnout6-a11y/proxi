"""Provider counts, estimates and unknown charges must remain distinguishable."""
import asyncio
import math

import pytest

import adapters
from test_review_fixes_pr19 import response, stream_direct, text
from test_usage_consistency import Writer, parsed, stream_usages


def details(dialect, usage):
    return usage.get('completion_tokens_details' if dialect == 'chat' else 'output_tokens_details', {})


@pytest.mark.parametrize('dialect', ['chat', 'responses'])
@pytest.mark.parametrize('visible_summary', ['', 'A visible summary is not the full hidden reasoning. ' * 20],
                         ids=['no-summary', 'visible-summary'])
@pytest.mark.parametrize('stream', [False, True])
def test_reasoning_tokens_are_not_guessed_from_output_or_summary(dialect, visible_summary, stream):
    writer = Writer() if stream else None
    adapter = adapters.for_dialect(dialect, parsed('', 'question'), writer)
    adapter.begin()
    adapter.feed({'type': 'usage_start'})
    if visible_summary:
        adapter.feed({'type': 'thinking', 'delta': visible_summary})
    adapter.feed({'type': 'text', 'delta': 'abcdefghijklmnop'})
    adapter.feed({'type': 'credits', 'value': .2, 'usage': {'input_tokens': 40, 'output_tokens': 12}})
    usage = adapter.end('stop')['usage']
    assert 'reasoning_tokens' not in details(dialect, usage)
    assert usage['completion_tokens' if dialect == 'chat' else 'output_tokens'] == 12
    if stream:
        assert 'reasoning_tokens' not in details(dialect, stream_usages(dialect, writer.events)[-1])


@pytest.mark.parametrize('dialect', ['chat', 'responses'])
@pytest.mark.parametrize('counter', [0, 7])
def test_explicit_provider_reasoning_counter_is_preserved_including_zero(dialect, counter):
    adapter = adapters.for_dialect(dialect, parsed('', 'question'))
    adapter.feed({'type': 'thinking', 'delta': 'summary ' * 100})
    adapter.feed({'type': 'text', 'delta': 'answer'})
    adapter.feed({'type': 'credits', 'value': .2, 'usage': {
        'input_tokens': 40, 'output_tokens': 12, 'reasoning_tokens': counter}})
    usage = adapter.end('stop')['usage']
    assert details(dialect, usage)['reasoning_tokens'] == counter
    assert usage['completion_tokens' if dialect == 'chat' else 'output_tokens'] == 12


@pytest.mark.parametrize('counter', [True, -1, 2.5, math.nan, math.inf, 13])
def test_invalid_reasoning_counter_is_not_published_as_exact(counter):
    adapter = adapters.ChatAdapter(parsed('', 'question'))
    adapter.feed({'type': 'credits', 'value': .2, 'usage': {
        'input_tokens': 40, 'output_tokens': 12, 'reasoning_tokens': counter}})
    assert 'reasoning_tokens' not in details('chat', adapter.end('stop')['usage'])


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_actual_token_snapshot_survives_missing_credit_report(tmp_path, monkeypatch, dialect):
    _, go = stream_direct(tmp_path, monkeypatch, [lambda: response(
        text('abcdefghijklmnop'), {'event': 'metadata', 'usage': {
            'inputTokens': 4, 'outputTokens': 12, 'promptTokensReadFromCache': 100}},
        {'event': 'end_of_stream'})], upstream_empty_retries=0)
    events = asyncio.run(go())
    assert not any(event['type'] == 'credits' for event in events)
    assert any(event['type'] == 'usage' for event in events)
    adapter = adapters.for_dialect(dialect, parsed('', 'question'))
    for event in events:
        adapter.feed(event)
    usage = adapter.end('stop')['usage']
    assert usage['completion_tokens' if dialect == 'chat' else 'output_tokens'] == 12
    assert usage['credits_used'] is None and usage['credits_complete'] is False
    assert usage['reported_credits_used'] == 0
    if dialect == 'anthropic':
        assert usage['input_tokens'] == 4 and usage['cache_read_input_tokens'] == 100
    else:
        assert usage['prompt_tokens' if dialect == 'chat' else 'input_tokens'] == 104


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('known_first', [True, False])
def test_partial_price_reports_do_not_masquerade_as_complete_cost(dialect, known_first):
    adapter = adapters.for_dialect(dialect, parsed('', 'question'))
    for known in (known_first, not known_first):
        adapter.feed({'type': 'usage_start'})
        event = {'type': 'credits', 'value': .25} if known else {'type': 'usage'}
        adapter.feed(dict(event, usage={'input_tokens': 4, 'output_tokens': 12}))
    usage = adapter.end('stop')['usage']
    assert usage['credits_used'] is None and usage['reported_credits_used'] == .25
    assert usage['credits_complete'] is False


def test_later_price_report_can_complete_the_same_attempt_and_zero_is_known():
    adapter = adapters.ChatAdapter(parsed('', 'question'))
    adapter.feed({'type': 'usage_start'})
    adapter.feed({'type': 'usage', 'usage': {'input_tokens': 4, 'output_tokens': 12}})
    adapter.feed({'type': 'credits', 'value': 0, 'usage': {'input_tokens': 4, 'output_tokens': 12}})
    usage = adapter.end('stop')['usage']
    assert usage['credits_used'] == 0 and usage['credits_complete'] is True


def test_empty_attempt_without_any_usage_does_not_disappear_from_cost_completeness(tmp_path, monkeypatch):
    metadata = {'event': 'metadata', 'usage': {'inputTokens': 4, 'outputTokens': 12, 'creditsUsed': .25}}
    end = {'event': 'end_of_stream'}
    _, go = stream_direct(tmp_path, monkeypatch, [lambda: response(metadata, end),
                                                 lambda: response(end),
                                                 lambda: response(text('answer'), metadata, end)],
                          upstream_empty_retries=2)
    adapter = adapters.ChatAdapter(parsed('', 'question'))
    for event in asyncio.run(go()):
        adapter.feed(event)
    usage = adapter.end('stop')['usage']
    assert usage['credits_used'] is None and usage['reported_credits_used'] == .5
    assert usage['credits_complete'] is False


@pytest.mark.parametrize('dialect', ['chat', 'responses'])
def test_signature_only_and_retry_do_not_invent_or_retain_a_reasoning_counter(dialect):
    adapter = adapters.for_dialect(dialect, parsed('', 'question'))
    adapter.feed({'type': 'usage_start'})
    adapter.feed({'type': 'credits', 'value': .1, 'usage': {'output_tokens': 12, 'reasoning_tokens': 7}})
    adapter.feed({'type': 'usage_start'})
    adapter.feed({'type': 'thinking_signature', 'signature': 'opaque-signature'})
    adapter.feed({'type': 'credits', 'value': .1, 'usage': {'output_tokens': 12}})
    assert 'reasoning_tokens' not in details(dialect, adapter.end('stop')['usage'])


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_runner_forwards_token_usage_without_inventing_a_credit_event(tmp_path, monkeypatch, dialect):
    from test_native_history import close, harness, parsed as parse_request
    from test_native_context_reuse import FIRST, run
    pool, runner, _ = harness(tmp_path, monkeypatch, [[text('answer'),
        {'event': 'metadata', 'usage': {'inputTokens': 4, 'outputTokens': 12, 'promptTokensReadFromCache': 100}},
        {'event': 'end_of_stream'}]])
    request = parse_request(FIRST, dialect)
    async def scenario():
        try:
            return await run(runner, request)
        finally:
            await close(pool)
    events = asyncio.run(scenario())
    assert any(event['type'] == 'usage' for event in events)
    assert not any(event['type'] == 'credits' for event in events)
    adapter = adapters.for_dialect(dialect, request)
    for event in events:
        adapter.feed(event)
    usage = adapter.end('stop')['usage']
    assert usage['completion_tokens' if dialect == 'chat' else 'output_tokens'] == 12
    assert usage['credits_used'] is None and usage['reported_credits_used'] == 0


@pytest.mark.parametrize('costs,expected', [([.25, .25], .25), ([.25, .4, .4], .4),
                                          ([0, 0], 0), ([.4, .25], .4)])
def test_metadata_cost_snapshots_are_counted_once_per_http_attempt(tmp_path, monkeypatch, costs, expected):
    _, go = stream_direct(tmp_path, monkeypatch, [[text('answer'), *[
        {'event': 'metadata', 'usage': {'creditsUsed': cost, 'inputTokens': 4, 'outputTokens': 12}}
        for cost in costs], {'event': 'end_of_stream'}]], upstream_empty_retries=0)
    adapter = adapters.ChatAdapter(parsed('', 'question'))
    for event in asyncio.run(go()):
        adapter.feed(event)
    assert adapter.end('stop')['usage']['credits_used'] == expected


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_partial_updates_merge_present_counters_and_preserve_actual_zero(dialect):
    adapter = adapters.for_dialect(dialect, parsed('', 'question'))
    adapter.feed({'type': 'usage', 'usage': {'input_tokens': 4, 'output_tokens': 12,
                                          'cache_read': 100, 'reasoning_tokens': 7}})
    adapter.feed({'type': 'credits', 'value': .25, 'usage': {'reasoning_tokens': 7}})
    assert (adapter.input_tokens, adapter.output_tokens, adapter.cache_read) == (4, 12, 100)
    adapter.feed({'type': 'usage', 'usage': {'input_tokens': 0, 'output_tokens': 0, 'cache_read': 0}})
    assert (adapter.input_tokens, adapter.output_tokens, adapter.cache_read) == (0, 0, 0)
    assert adapter._reasoning_details() == {}


def test_direct_partial_usage_does_not_create_absent_fields(tmp_path, monkeypatch):
    _, go = stream_direct(tmp_path, monkeypatch, [[text('answer'),
        {'event': 'metadata', 'usage': {'inputTokens': 4, 'outputTokens': 12,
                                      'promptTokensReadFromCache': 100}},
        {'event': 'metadata', 'usage': {'creditsUsed': .25, 'reasoningTokens': 7}},
        {'event': 'end_of_stream'}]], upstream_empty_retries=0)
    adapter = adapters.ChatAdapter(parsed('', 'question'))
    for event in asyncio.run(go()):
        if event['type'] == 'credits':
            assert 'input_tokens' not in event['usage']
        adapter.feed(event)
    assert (adapter.input_tokens, adapter.output_tokens, adapter.cache_read) == (4, 12, 100)
    assert adapter._reasoning_details() == {'reasoning_tokens': 7}


@pytest.mark.parametrize('cost', [None, 0, .25])
def test_anthropic_terminal_stream_reports_same_price_as_payload(cost):
    writer = Writer()
    adapter = adapters.AnthropicAdapter(parsed('', 'question'), writer)
    adapter.begin()
    adapter.feed({'type': 'text', 'delta': 'answer'})
    adapter.feed({'type': 'usage', 'usage': {'input_tokens': 4, 'output_tokens': 12}} if cost is None else
                 {'type': 'credits', 'value': cost, 'usage': {'input_tokens': 4, 'output_tokens': 12}})
    final = adapter.end('stop')['usage']
    streamed = stream_usages('anthropic', writer.events)[-1]
    for key in ('credits_used', 'reported_credits_used', 'credits_complete'):
        assert streamed[key] == final[key]


def test_runner_bills_increasing_snapshots_as_one_total_not_repeated_transactions(tmp_path, monkeypatch):
    from test_native_history import close, harness, parsed as parse_request
    from test_native_context_reuse import FIRST, run
    from types import SimpleNamespace
    pool, runner, _ = harness(tmp_path, monkeypatch, [[text('answer'), *[
        {'event': 'metadata', 'usage': {'creditsUsed': cost}} for cost in (.25, .25, .4, .4)],
        {'event': 'end_of_stream'}]])
    paid = []
    runner.credits = SimpleNamespace(record_turn=lambda key, cost: paid.append((key, cost)))
    async def scenario():
        try:
            return await run(runner, parse_request(FIRST, 'chat'))
        finally:
            await close(pool)
    asyncio.run(scenario())
    assert sum(cost for _, cost in paid) == pytest.approx(.4)


@pytest.mark.parametrize('snapshots,expected_cost', [
    ([{'inputTokens': 4, 'outputTokens': 12, 'promptTokensReadFromCache': 100}, {'creditsUsed': .25}], .25),
    ([{'creditsUsed': .25}, {'inputTokens': 4, 'outputTokens': 12, 'promptTokensReadFromCache': 100}], .25),
    ([{'inputTokens': 4, 'outputTokens': 12, 'promptTokensReadFromCache': 100, 'creditsUsed': .25},
      {'inputTokens': 4, 'outputTokens': 12, 'promptTokensReadFromCache': 100, 'creditsUsed': .4}], .4),
])
def test_journal_metrics_count_token_snapshots_once_and_do_not_forget_reported_cost(tmp_path, monkeypatch, snapshots, expected_cost):
    import journal
    from test_native_history import close, harness, parsed as parse_request
    from test_native_context_reuse import FIRST, run
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    pool, runner, _ = harness(tmp_path, monkeypatch, [[text('answer'), *[
        {'event': 'metadata', 'usage': snapshot} for snapshot in snapshots], {'event': 'end_of_stream'}]])
    async def scenario():
        try:
            return await run(runner, parse_request(FIRST))
        finally:
            await close(pool)
    asyncio.run(scenario())
    totals = log.metrics()['totals']
    assert (totals['input_tokens'], totals['output_tokens'], totals['cache_read']) == (4, 12, 100)
    assert totals['credits'] == expected_cost
    rows = [e for e in log.query() if e['kind'] == 'upstream.usage']
    if 'creditsUsed' in snapshots[0]:
        assert not any(row['level'] == 'warn' for row in rows)


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_unknown_truncated_attempt_remains_unknown_after_account_failover(tmp_path, monkeypatch, dialect):
    from types import SimpleNamespace
    from test_native_history import harness, close, parsed as parse_request
    from test_native_context_reuse import FIRST, Backend, run
    pool, runner, sent = harness(tmp_path, monkeypatch, [[], [text('answer'),
        {'event': 'metadata', 'usage': {'inputTokens': 4, 'outputTokens': 12, 'creditsUsed': .25}},
        {'event': 'end_of_stream'}]])
    first = pool.backends[0]
    second = Backend(tmp_path, **first.cfg)
    second.headers = lambda: {}
    second.cfg['provider_resource_name'] = 'other-account'
    pool.backends.append(second)
    unknown, paid = [], []
    runner.credits = SimpleNamespace(record_turn=lambda key, cost: paid.append(cost),
                                     mark_usage_unknown=lambda: unknown.append(True))
    def check(backend):
        backend.unavailable = {'reason': 'exhausted', 'available': 0, 'threshold': 20}
        return True
    runner.accounts = SimpleNamespace(check_balance_now=check)
    request = parse_request(FIRST, dialect)
    async def scenario():
        try:
            return await run(runner, request)
        finally:
            await close(pool)
    events = asyncio.run(scenario())
    assert len(sent) == 2 and unknown == [True]
    adapter = adapters.for_dialect(dialect, request)
    for event in events:
        adapter.feed(event)
    usage = adapter.end('stop')['usage']
    assert usage['credits_used'] is None
    assert usage['reported_credits_used'] == .25 and usage['credits_complete'] is False
