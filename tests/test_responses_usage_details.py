"""Responses usage names cache writes next to cache reads, as strict clients require."""
import json
from types import SimpleNamespace

import pytest

import adapters


class Writer:
    def __init__(self):
        self.events = []

    def event(self, name, payload):
        self.events.append((name, payload))

    def done(self):
        pass


def adapter(writer=None):
    parsed = SimpleNamespace(model='gpt-6-sol', system='', prompt='question', show_thinking=True,
                             wants_usage=True, tools=[])
    return adapters.for_dialect('responses', parsed, writer)


def report(result, **usage):
    result.feed({'type': 'usage_start'})
    result.feed({'type': 'text', 'delta': 'answer'})
    result.feed({'type': 'credits', 'value': .5, 'usage': {'input_tokens': 10, 'output_tokens': 100, **usage}})


def codex_view(usage):
    """The required parts of codex-rs ResponseCompletedUsage: integers, details optional."""
    for key in ('input_tokens', 'output_tokens', 'total_tokens'):
        assert type(usage[key]) is int, key
    details = usage.get('input_tokens_details')
    cached = written = 0
    if details is not None:
        assert type(details['cached_tokens']) is int
        written = details.get('cache_write_tokens', 0)
        assert type(written) is int
        cached = details['cached_tokens']
    if usage.get('output_tokens_details') is not None:
        assert type(usage['output_tokens_details']['reasoning_tokens']) is int
    return cached, written


def python_sdk_view(usage):
    """openai-python 2.45+ InputTokensDetails: both counters are required fields."""
    details = usage['input_tokens_details']
    assert type(details['cached_tokens']) is int and type(details['cache_write_tokens']) is int
    return details


def test_non_stream_usage_carries_cache_write_tokens_in_both_detail_objects():
    result = adapter()
    report(result, cache_read=15000, cache_write=2500)
    usage = result.end('stop')['usage']
    assert usage['input_tokens_details'] == {'cached_tokens': 15000, 'cache_write_tokens': 2500}
    assert usage['input_token_details'] == {'cached_tokens': 15000, 'cache_write_tokens': 2500}
    assert codex_view(usage) == (15000, 2500)
    assert python_sdk_view(usage) == {'cached_tokens': 15000, 'cache_write_tokens': 2500}


def test_streamed_completed_event_carries_the_same_usage():
    writer = Writer()
    result = adapter(writer)
    result.begin()
    report(result, cache_read=40, cache_write=60)
    result.end('stop')
    completed = next(payload for name, payload in writer.events if name == 'response.completed')
    assert completed['response']['usage']['input_tokens_details'] == {'cached_tokens': 40, 'cache_write_tokens': 60}
    assert codex_view(completed['response']['usage']) == (40, 60)


@pytest.mark.parametrize('usage', [{}, {'cache_read': 7}, {'cache_write': 9}, {'cache_read': 0, 'cache_write': 0}])
def test_unreported_counters_are_real_zeros_never_null_or_missing(usage):
    result = adapter()
    report(result, **usage)
    details = python_sdk_view(result.end('stop')['usage'])
    assert details == {'cached_tokens': usage.get('cache_read', 0), 'cache_write_tokens': usage.get('cache_write', 0)}


def test_an_estimated_request_without_any_upstream_usage_still_has_integer_counters():
    result = adapter()
    result.feed({'type': 'text', 'delta': 'answer'})
    usage = result.end('stop')['usage']
    assert python_sdk_view(usage) == {'cached_tokens': 0, 'cache_write_tokens': 0}
    assert codex_view(usage) == (0, 0) and usage['estimated'] is True


def test_reasoning_details_stay_optional_and_valid_when_cache_writes_are_reported():
    result = adapter()
    report(result, cache_write=5, reasoning_tokens=11)
    usage = result.end('stop')['usage']
    assert usage['output_tokens_details'] == {'reasoning_tokens': 11} and codex_view(usage) == (0, 5)
    unknown = adapter()
    report(unknown, cache_write=5)
    assert 'output_tokens_details' not in unknown.end('stop')['usage']
    json.dumps(usage)
