"""A Responses stream that fails after it started ends with response.failed, not only an `error` event.

codex-cli 0.164.0 (rust-v0.164.0) ignores the `error` event (codex-api/src/sse/responses.rs:349), sees the
stream end before response.completed (:576) and asks again up to stream_max_retries = 5 times
(model-provider-info/src/lib.rs:69), a paid request every time, without ever showing the message. A
`response.failed` is classified by error.code (sse/responses_error.rs:26-102, protocol/src/error.rs:413).
"""
import json
import re

import pytest

import adapters
import apierr
import journal
import validate
from test_client_contracts import CFG, _body
from test_error_envelopes import BUDGET_CODES, codes_written_in_the_relay
from test_structured_output import body as structured_body, parse as structured_parse, post, sse_events

# Codex ends the turn at once, without a retry, for these codes (responses_error.rs:44-102, error.rs:413-441);
# server_is_overloaded does too unless the error carries a Retry-After.
CODEX_NO_RETRY = {'context_length_exceeded', 'insufficient_quota', 'credit_balance_exhausted',
                  'organization_spend_limit_exceeded', 'project_spend_limit_exceeded', 'usage_not_included',
                  'cyber_policy', 'bio_policy', 'misalignment_policy_violation', 'invalid_prompt',
                  'flex_unavailable', 'server_is_overloaded'}
# Codex prints the relay's message for these; for the quota codes it prints a text of its own.
CODEX_PRINTS_MESSAGE = {'invalid_prompt', 'cyber_policy', 'bio_policy', 'misalignment_policy_violation'}
# Codex's delay parser (responses_error.rs:141); it is applied to rate_limit_exceeded and slow_down only.
CODEX_DELAY = re.compile(r'(?i)try again in\s*(\d+(?:\.\d+)?)\s*(s|ms|seconds?)')
TRANSIENT = {'upstream_error', 'upstream_auth_error', 'service_unavailable', 'upstream_timeout', 'internal_error',
             'rate_limit_error'}
CODES = codes_written_in_the_relay() | {code: 502 for code in BUDGET_CODES}


def failure_of(code, status, **kwargs):
    return apierr.ApiError(status, code, 'text', **kwargs).responses_error()


@pytest.mark.parametrize('code', sorted(CODES))
def test_each_relay_code_has_an_explicit_entry_and_a_code_codex_handles_as_intended(code):
    assert code in apierr.RESPONSES_ERROR_CODES, 'a new relay code needs a decision here'
    mapped = failure_of(code, CODES[code])['code']
    if code in TRANSIENT:
        assert mapped not in CODEX_NO_RETRY, 'Codex has to ask again after a temporary failure'
    elif code == 'insufficient_credits':
        assert mapped == 'insufficient_quota'
    else:
        assert mapped in CODEX_PRINTS_MESSAGE, 'the failure is final and the user reads why'


@pytest.mark.parametrize('code,status,expected', [
    ('upstream_invalid_output', 502, 'invalid_prompt'), ('insufficient_credits', 402, 'insufficient_quota'),
    ('rate_limit_error', 429, 'rate_limit_exceeded'), ('upstream_error', 502, 'server_error'),
    ('service_unavailable', 503, 'server_error'), ('upstream_timeout', 504, 'server_error'),
    ('invalid_request_error', 400, 'invalid_prompt'), ('unprocessable_entity', 422, 'invalid_prompt'),
    ('prompt_too_large', 413, 'invalid_prompt'), ('upstream_empty_turn', 502, 'invalid_prompt'),
    ('upstream_attempt_limit', 502, 'invalid_prompt'), ('internal_error', 500, 'server_error')])
def test_the_codes_the_review_asked_for(code, status, expected):
    assert failure_of(code, status)['code'] == expected


@pytest.mark.parametrize('status,expected', [(402, 'insufficient_quota'), (429, 'rate_limit_exceeded'),
                                             (400, 'invalid_prompt'), (404, 'invalid_prompt'), (409, 'invalid_prompt'),
                                             (500, 'server_error'), (502, 'server_error'), (504, 'server_error')])
def test_an_unknown_code_follows_its_status(status, expected):
    assert failure_of('some_future_code', status)['code'] == expected


def test_a_relay_error_that_forbids_retries_is_final_whatever_its_code():
    assert failure_of('some_future_code', 503, headers={'x-should-retry': 'false'})['code'] == 'invalid_prompt'
    assert failure_of('some_future_code', 503, headers={'X-Should-Retry': 'true'})['code'] == 'server_error'


def test_the_message_is_kept_and_only_a_rate_limit_with_a_short_wait_gets_a_hint():
    assert failure_of('upstream_error', 502)['message'] == 'text'
    assert failure_of('rate_limit_error', 429)['message'] == 'text'
    assert failure_of('insufficient_credits', 402, retry_after=20)['message'] == 'text'
    assert failure_of('upstream_error', 502, retry_after=20)['message'] == 'text'


@pytest.mark.parametrize('seconds,delay', [(1, 1.0), (20, 20.0), (1.5, 1.5), (1.1, 1.1), (1.52, 1.6), (0.05, 0.1),
                                           (60, 60.0), (59.01, 59.1)])
def test_a_short_wait_becomes_a_hint_in_the_form_codex_parses(seconds, delay):
    message = failure_of('rate_limit_error', 429, retry_after=seconds)['message']
    found = CODEX_DELAY.search(message)
    assert found and float(found.group(1)) == delay and found.group(2) == 's' and message.startswith('text')


@pytest.mark.parametrize('seconds', [60.01, 61, 120, 900, 3600, 0, -5, None, float('inf'), float('nan'), True, '20'])
def test_a_long_or_unusable_wait_gets_no_hint_because_codex_would_sleep_for_all_of_it(seconds):
    # Codex sleeps until the deadline it parsed, with no cap, before each of up to five retries: 900 s each.
    message = failure_of('rate_limit_error', 429, retry_after=seconds)['message']
    assert CODEX_DELAY.search(message) is None and message == 'text'


def test_no_hint_ever_makes_codex_wait_longer_than_the_cap():
    for seconds in (0.001, 0.05, 5, 30, 59.9, 60):
        found = CODEX_DELAY.search(failure_of('rate_limit_error', 429, retry_after=seconds)['message'])
        assert found and float(found.group(1)) <= apierr.RETRY_HINT_MAX


def test_the_real_relay_waits_become_hints_or_not_by_their_length():
    assert CODEX_DELAY.search(apierr.accounts_rate_limited(20, 1, 1).responses_error()['message'])
    assert CODEX_DELAY.search(apierr.accounts_rate_limited(900, 1, 1).responses_error()['message']) is None
    assert CODEX_DELAY.search(apierr.too_many_requests('relay rate limit reached', .05).responses_error()['message'])


# -- the adapter ----------------------------------------------------------------------------------------

class Writer:
    def __init__(self):
        self.events = []

    def event(self, name, payload):
        self.events.append((name, payload))

    def done(self):
        self.events.append(('done', {}))


def started(structured=False):
    writer = Writer()
    parsed = (structured_parse('responses') if structured else validate.parse_responses(_body('responses'), CFG))
    adapter = adapters.for_dialect('responses', parsed, writer)
    adapter.begin()
    return adapter, writer


def failed_of(events):
    return [payload for name, payload in events if name == 'response.failed']


def test_the_adapter_ends_a_failed_stream_with_the_error_and_then_response_failed():
    adapter, writer = started()
    adapter.feed({'type': 'text', 'delta': 'partial'})
    adapter.fail(apierr.upstream_error('Retool connection failed: boom'), {'request_id': 'abc'})
    names = [name for name, _ in writer.events]
    assert names[-2:] == ['error', 'response.failed'] and names[0] == 'response.created'
    created, error, failed = writer.events[0][1], writer.events[-2][1], writer.events[-1][1]
    assert error['code'] == 'server_error' and error['relay_code'] == 'upstream_error'  # the relay's own code stays
    assert error['request_id'] == 'abc' and error['message'] == 'Retool connection failed: boom'
    response = failed['response']
    assert response['id'] == created['response']['id'] and response['status'] == 'failed'
    assert response['object'] == 'response' and response['model'] == created['response']['model']
    assert response['error'] == {'code': 'server_error', 'message': 'Retool connection failed: boom'}
    sequence = [payload['sequence_number'] for _, payload in writer.events]
    assert sequence == list(range(1, len(sequence) + 1))


def test_both_events_carry_what_strict_responses_parsers_require():
    adapter, writer = started()
    adapter.fail(apierr.insufficient_credits(5))
    failed = failed_of(writer.events)[0]
    # ai-sdk's schema (openai-responses-api.ts) wants sequence_number and response.error.message on
    # response.failed and sequence_number, code, message on the flat error; the openai SDK reads response.*.
    assert failed['type'] == 'response.failed' and isinstance(failed['sequence_number'], int)
    assert failed['response']['error']['code'] == 'insufficient_quota'
    assert isinstance(failed['response']['error']['message'], str) and failed['response']['output'] == []
    error = next(payload for name, payload in writer.events if name == 'error')
    assert error['type'] == 'error' and isinstance(error['sequence_number'], int)
    assert (error['code'], error['message']) == (failed['response']['error']['code'], failed['response']['error']['message'])


def test_the_hint_reaches_both_events():
    adapter, writer = started()
    adapter.fail(apierr.accounts_rate_limited(20, 1, 1))
    error, failed = writer.events[-2][1], writer.events[-1][1]
    assert error['message'] == failed['response']['error']['message'] and CODEX_DELAY.search(error['message'])
    assert error['code'] == 'rate_limit_exceeded' and error['relay_code'] == 'rate_limit_error'


def test_a_failure_before_response_created_still_announces_the_id_it_fails():
    writer = Writer()
    adapter = adapters.for_dialect('responses', validate.parse_responses(_body('responses'), CFG), writer)
    adapter.fail(apierr.upstream_timeout())
    assert [name for name, _ in writer.events] == ['response.created', 'error', 'response.failed']
    assert writer.events[0][1]['response']['id'] == writer.events[-1][1]['response']['id']


def test_a_structured_adapter_fails_through_its_wrapper_and_leaks_nothing():
    adapter, writer = started(structured=True)
    adapter.feed({'type': 'text', 'delta': '{"selected_memories":"PRIVATE"}'})
    with pytest.raises(apierr.ApiError) as raised:
        adapter.end('stop')
    adapter.fail(raised.value)
    failed = failed_of(writer.events)[0]['response']
    assert failed['error']['code'] == 'invalid_prompt' and 'upstream reply does not satisfy' in failed['error']['message']
    assert 'PRIVATE' not in json.dumps(writer.events) and failed['id'] == writer.events[0][1]['response']['id']


def test_the_envelope_the_other_paths_use_is_unchanged():
    error = apierr.upstream_error('boom')
    assert error.responses_envelope() == {'type': 'error', 'code': 'upstream_error', 'message': 'boom', 'param': None}


# -- over HTTP ------------------------------------------------------------------------------------------

def stream_of(relay, script, **extra):
    relay.fake.script = script
    status, headers, raw = post(relay, 'responses', dict(_body('responses'), stream=True, **extra))
    return status, raw, sse_events(raw)


@pytest.mark.parametrize('make,code,hint', [
    (lambda: apierr.upstream_error('Retool connection failed: boom'), 'server_error', False),
    (lambda: apierr.upstream_timeout(), 'server_error', False),
    (lambda: apierr.service_unavailable('shutting down'), 'server_error', False),
    (lambda: apierr.insufficient_credits(5), 'insufficient_quota', False),
    (lambda: apierr.accounts_rate_limited(20, 1, 1), 'rate_limit_exceeded', True),
    (lambda: apierr.accounts_rate_limited(900, 1, 1), 'rate_limit_exceeded', False),
    (lambda: apierr.unprocessable('late rejection', param='x'), 'invalid_prompt', False),
    (lambda: apierr.ApiError(413, 'prompt_too_large', 'prompt is too large'), 'invalid_prompt', False),
])
def test_http_a_failure_after_the_start_ends_with_response_failed_and_the_mapped_code(relay, make, code, hint):
    status, raw, events = stream_of(relay, [{'type': 'text', 'delta': 'partial'}, make()])
    assert status == 200
    names = [name for name, _ in events]
    assert names[0] == 'response.created' and names[-2:] == ['error', 'response.failed']
    assert raw.rstrip().endswith('data: [DONE]')
    named = dict(events)
    failed, created = named['response.failed']['response'], named['response.created']['response']
    assert failed['id'] == created['id'] and failed['status'] == 'failed' and failed['error']['code'] == code
    assert bool(CODEX_DELAY.search(failed['error']['message'])) is hint
    assert named['error']['code'] == code and named['error']['relay_code'] == make().code
    assert named['error']['request_id'] and named['error']['message'] == failed['error']['message']


def test_http_an_invalid_structured_reply_is_final_for_codex_and_costs_one_request(relay):
    relay.fake.script = [{'type': 'text', 'delta': '{"selected_memories":"PRIVATE"}'},
                         {'type': 'credits', 'value': .5}, {'type': 'end', 'reason': 'stop'}]
    status, headers, raw = post(relay, 'responses', structured_body('responses', stream=True))
    failed = dict(sse_events(raw))['response.failed']['response']
    assert failed['error']['code'] == 'invalid_prompt'
    assert 'upstream reply does not satisfy' in failed['error']['message']
    assert 'PRIVATE' not in raw and relay.credits.used == [('turn', .5)]


def test_http_the_internal_error_path_ends_with_response_failed_too(relay, monkeypatch):
    def explode(self, delta):
        raise RuntimeError('bug')  # a defect in the handler thread, not an ApiError

    monkeypatch.setattr(adapters.ResponsesAdapter, '_text', explode)
    relay.fake.script = [{'type': 'text', 'delta': 'x'}, {'type': 'end', 'reason': 'stop'}]
    status, headers, raw = post(relay, 'responses', dict(_body('responses'), stream=True))
    events = sse_events(raw)
    assert [name for name, _ in events][-2:] == ['error', 'response.failed']
    assert dict(events)['response.failed']['response']['error']['code'] == 'server_error'
    assert dict(events)['error']['relay_code'] == 'internal_error'


def test_http_a_completed_stream_is_untouched(relay):
    status, raw, events = stream_of(relay, [{'type': 'text', 'delta': 'ok'}, {'type': 'end', 'reason': 'stop'}])
    names = [name for name, _ in events]
    assert 'response.failed' not in names and 'error' not in names and names[-1] == 'response.completed'


def test_http_a_timed_out_structured_turn_fails_with_a_retryable_code(relay):
    relay.fake.script = [{'type': 'text', 'delta': '{"selected_memories":['}, {'type': 'end', 'reason': 'timeout'}]
    status, headers, raw = post(relay, 'responses', structured_body('responses', stream=True))
    assert dict(sse_events(raw))['response.failed']['response']['error']['code'] == 'server_error'


def test_http_errors_before_the_stream_starts_stay_plain_json(relay):
    status, headers, raw = post(relay, 'responses', dict(_body('responses'), stream=True, store=True))
    assert status == 422 and 'response.failed' not in raw and json.loads(raw)['error']['code'] == 'unprocessable_entity'


@pytest.mark.parametrize('dialect', ['chat', 'anthropic'])
def test_http_chat_and_messages_streams_keep_their_error_event_alone(relay, dialect):
    relay.fake.script = [{'type': 'text', 'delta': 'partial'}, apierr.upstream_error('boom')]
    status, headers, raw = post(relay, dialect, dict(_body(dialect), stream=True))
    events = sse_events(raw)
    names = [name for name, _ in events]
    assert status == 200 and names.count('error') == 1 and names[-1] == 'error' and 'response.failed' not in raw
    assert raw.rstrip().endswith('data: [DONE]')
    payload = dict(events)['error']
    assert 'sequence_number' not in payload
    if dialect == 'chat':
        assert payload['error']['code'] == 'upstream_error' and 'relay_code' not in json.dumps(payload)
    else:  # the Messages type is the documented one (translated by the error table), nothing else changed
        assert payload['error'] == {'type': 'api_error', 'message': 'boom', 'relay_code': 'upstream_error'}


def test_the_journal_still_records_a_failed_responses_stream_as_an_error(relay):
    relay.fake.script = [apierr.upstream_error('boom')]
    post(relay, 'responses', dict(_body('responses'), stream=True))
    failed = [e for e in journal.JOURNAL.query() if e['kind'] == 'http.request'
              and e['data']['path'] == '/v1/responses' and e['data'].get('status') == 502]
    assert failed and all(e['level'] == 'error' for e in failed)
    assert any(e['kind'] == 'http.error' and e['data']['code'] == 'upstream_error' for e in journal.JOURNAL.query())
