"""Strict Responses consumers must not receive empty token-detail objects."""
import pytest

import adapters
from test_usage_consistency import Writer, parsed, stream_usages


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('reasoning', [None, -1, True, 13])
def test_unknown_or_invalid_reasoning_omits_the_optional_details_object(stream, reasoning):
    writer = Writer() if stream else None
    adapter = adapters.ResponsesAdapter(parsed('', 'question'), writer)
    if writer:
        adapter.begin()
    adapter.feed({'type': 'thinking', 'delta': 'This visible summary is not a token count.'})
    adapter.feed({'type': 'text', 'delta': 'answer'})
    usage = {'input_tokens': 4, 'output_tokens': 12}
    if reasoning is not None:
        usage['reasoning_tokens'] = reasoning
    adapter.feed({'type': 'usage', 'usage': usage})
    final = adapter.end('stop')['usage']
    for actual in [final, *(stream_usages('responses', writer.events) if writer else [])]:
        assert actual['output_tokens'] == 12
        # Codex 0.164.0 accepts an absent optional object, but {} fails parsing
        # ResponseCompleted and triggers a repeat of the already billed turn.
        assert 'output_tokens_details' not in actual
        assert 'output_token_details' not in actual


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('reasoning', [0, 7, 12])
def test_explicit_reasoning_counter_remains_a_complete_details_object(stream, reasoning):
    writer = Writer() if stream else None
    adapter = adapters.ResponsesAdapter(parsed('', 'question'), writer)
    if writer:
        adapter.begin()
    adapter.feed({'type': 'text', 'delta': 'answer'})
    adapter.feed({'type': 'usage', 'usage': {'input_tokens': 4, 'output_tokens': 12,
                                          'reasoning_tokens': reasoning}})
    final = adapter.end('stop')['usage']
    for actual in [final, *(stream_usages('responses', writer.events) if writer else [])]:
        assert actual['output_tokens_details'] == {'reasoning_tokens': reasoning}
        assert actual['output_token_details'] == {'reasoning_tokens': reasoning}


def test_new_attempt_clears_the_previous_details_object_without_inventing_zero():
    adapter = adapters.ResponsesAdapter(parsed('', 'question'))
    adapter.feed({'type': 'usage', 'usage': {'output_tokens': 12, 'reasoning_tokens': 7}})
    adapter.feed({'type': 'usage_start'})
    adapter.feed({'type': 'usage', 'usage': {'output_tokens': 5}})
    actual = adapter.end('stop')['usage']
    assert actual['output_tokens'] == 5
    assert 'output_tokens_details' not in actual
