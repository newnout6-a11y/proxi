"""Messages errors use Anthropic's documented types; the OpenAI dialects keep their shapes."""
import json
import pathlib
import re
import urllib.error

import pytest

import apierr
from test_client_contracts import _body


def test_unprocessable_is_an_invalid_request_for_anthropic_clients():
    error = apierr.unprocessable("'stop' is not supported by the Retool route", param='stop')
    assert error.anthropic_envelope() == {'type': 'error', 'error': {
        'type': 'invalid_request_error', 'message': "'stop' is not supported by the Retool route",
        'relay_code': 'unprocessable_entity'}}
    # The relay's own code, status and param stay available to logs and the other dialects.
    assert (error.status, error.code, error.param) == (422, 'unprocessable_entity', 'stop')


@pytest.mark.parametrize('make', [
    lambda: apierr.unprocessable('bad', param='n'), lambda: apierr.bad_request('bad', param='model'),
    lambda: apierr.unauthorized(), lambda: apierr.not_found(), lambda: apierr.too_many_requests(),
    lambda: apierr.upstream_error('bad'), lambda: apierr.upstream_timeout(), lambda: apierr.internal('bad'),
    lambda: apierr.insufficient_credits(5), lambda: apierr.invalid_structured_output('bad'),
])
def test_openai_and_responses_envelopes_are_unchanged(make):
    error = make()
    assert error.openai_envelope() == {'error': {'message': error.message, 'type': error.code,
                                                 'code': error.code, 'param': error.param}}
    assert error.responses_envelope() == {'type': 'error', 'code': error.code, 'message': error.message,
                                          'param': error.param}


def test_documented_anthropic_types_pass_through_unchanged():
    assert apierr.bad_request('x').anthropic_envelope()['error'] == {'type': 'invalid_request_error', 'message': 'x'}
    assert apierr.not_found().anthropic_envelope()['error']['type'] == 'not_found_error'
    assert apierr.too_many_requests().anthropic_envelope()['error']['type'] == 'rate_limit_error'


def test_the_relay_code_stays_visible_where_the_documented_type_replaces_it():
    # Before the translation `upstream_error` and the like were the type itself: a client could tell them apart.
    for make, kind in ((lambda: apierr.upstream_error('x'), 'api_error'), (lambda: apierr.upstream_timeout(), 'timeout_error'),
                       (lambda: apierr.insufficient_credits(5), 'billing_error'),
                       (lambda: apierr.service_unavailable('x'), 'overloaded_error')):
        error = make()
        shown = error.anthropic_envelope()['error']
        assert (shown['type'], shown['relay_code']) == (kind, error.code)
    for make in (lambda: apierr.bad_request('x'), lambda: apierr.not_found(), lambda: apierr.too_many_requests()):
        assert 'relay_code' not in make().anthropic_envelope()['error']  # nothing was replaced


# The closed set of error types platform.claude.com/docs/en/api/errors documents (checked 2026-11-05).
DOCUMENTED_ANTHROPIC_TYPES = {'invalid_request_error', 'authentication_error', 'billing_error', 'permission_error',
                              'not_found_error', 'conflict_error', 'request_too_large', 'rate_limit_error',
                              'api_error', 'timeout_error', 'overloaded_error'}
# The codes AttemptBudget.error and the orchestrator build from a variable.
BUDGET_CODES = {'upstream_attempt_limit', 'upstream_empty_turn', 'upstream_tool_choice_ignored'}
EXPECTED_ANTHROPIC_TYPE = {
    'invalid_request_error': 'invalid_request_error', 'unprocessable_entity': 'invalid_request_error',
    'unsupported_media_type': 'invalid_request_error', 'method_not_allowed': 'invalid_request_error',
    'unauthorized': 'authentication_error', 'forbidden': 'permission_error', 'not_found_error': 'not_found_error',
    'conflict': 'conflict_error', 'payload_too_large': 'request_too_large', 'prompt_too_large': 'request_too_large',
    'rate_limit_error': 'rate_limit_error', 'insufficient_credits': 'billing_error',
    'service_unavailable': 'overloaded_error', 'upstream_timeout': 'timeout_error',
    'upstream_error': 'api_error', 'upstream_auth_error': 'api_error', 'upstream_invalid_output': 'api_error',
    'internal_error': 'api_error', **{code: 'api_error' for code in BUDGET_CODES},
}


def codes_written_in_the_relay():
    """Every `(status, "code"` pair in the relay's own modules, plus the codes the attempt budget builds."""
    root = pathlib.Path(__file__).resolve().parent.parent
    pair = re.compile(r'\(\s*(\d{3})\s*,\s*["\']([a-z_]+)["\']')
    found = {}
    for path in root.glob('*.py'):
        for status, code in pair.findall(path.read_text(encoding='utf-8')):
            found.setdefault(code, int(status))
    return found | {code: 502 for code in BUDGET_CODES}


def test_every_relay_code_is_translated_to_a_documented_anthropic_type():
    written = codes_written_in_the_relay()
    assert set(written) <= set(EXPECTED_ANTHROPIC_TYPE), 'a new relay code needs an Anthropic type'
    for code, status in written.items():
        error = apierr.ApiError(status, code, 'text')
        assert error.anthropic_envelope()['error']['type'] == EXPECTED_ANTHROPIC_TYPE[code], code
        assert error.code == code  # the relay's own code stays for logs and the OpenAI dialects
    assert set(EXPECTED_ANTHROPIC_TYPE.values()) <= DOCUMENTED_ANTHROPIC_TYPES
    assert apierr.ANTHROPIC_ERROR_TYPES == DOCUMENTED_ANTHROPIC_TYPES


@pytest.mark.parametrize('status,expected', [
    (400, 'invalid_request_error'), (401, 'authentication_error'), (402, 'billing_error'),
    (403, 'permission_error'), (404, 'not_found_error'), (409, 'conflict_error'), (413, 'request_too_large'),
    (418, 'invalid_request_error'), (422, 'invalid_request_error'), (429, 'rate_limit_error'),
    (500, 'api_error'), (502, 'api_error'), (503, 'overloaded_error'), (504, 'timeout_error'),
    (529, 'overloaded_error'), (599, 'api_error')])
def test_an_unknown_code_is_translated_by_its_status(status, expected):
    error = apierr.ApiError(status, 'some_future_code', 'text')
    assert error.anthropic_envelope()['error']['type'] == expected
    assert expected in DOCUMENTED_ANTHROPIC_TYPES


def test_the_attempt_budget_codes_are_the_ones_the_table_lists():
    from upstream_attempts import AttemptBudget
    budget = AttemptBudget()
    assert budget.error().code == 'upstream_empty_turn'
    assert budget.error('upstream_attempt_limit', 'x').code == 'upstream_attempt_limit'
    assert {budget.error().code, budget._limit_error().code} <= BUDGET_CODES


def fetch(relay, path, payload):
    try:
        with relay.request(path, payload) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_messages_route_reports_422_as_an_invalid_request(relay):
    status, body = fetch(relay, '/v1/messages', _body('anthropic', n=2))
    assert status == 422 and body['type'] == 'error'
    assert body['error']['type'] == 'invalid_request_error'
    assert "'n' is not supported" in body['error']['message']
    assert body['request_id'] and 'param' not in body['error']


def test_openai_routes_keep_their_422_shape(relay):
    status, body = fetch(relay, '/v1/chat/completions', _body('chat', n=2))
    assert status == 422
    assert body['error']['type'] == body['error']['code'] == 'unprocessable_entity'
    assert body['error']['param'] == 'n'
    status, body = fetch(relay, '/v1/responses', _body('responses', store=True))
    assert status == 422
    assert body['error']['type'] == body['error']['code'] == 'unprocessable_entity'
    assert body['error']['param'] == 'store'


def test_streamed_messages_error_event_uses_the_same_type(relay):
    relay.fake.script = [apierr.unprocessable('late rejection', param='x')]
    status, raw = relay.sse('/v1/messages', _body('anthropic', stream=True))
    assert status == 200 and 'event: error' in raw
    payload = json.loads(next(line[6:] for line in raw.splitlines() if line.startswith('data: {"type":"error"')
                              or line.startswith('data: {"type": "error"')))
    assert payload['error']['type'] == 'invalid_request_error'


@pytest.mark.parametrize('make,status,expected', [
    (lambda: apierr.upstream_timeout(), 504, 'timeout_error'),
    (lambda: apierr.insufficient_credits(5), 402, 'billing_error'),
    (lambda: apierr.service_unavailable('shutting down'), 503, 'overloaded_error'),
    (lambda: apierr.upstream_error('connection failed'), 502, 'api_error'),
    (lambda: apierr.internal('bug'), 500, 'api_error'),
])
def test_messages_json_and_stream_errors_carry_the_documented_type(relay, make, status, expected):
    relay.fake.script = [make()]
    code, body = fetch(relay, '/v1/messages', _body('anthropic'))
    assert code == status and body['type'] == 'error' and body['error']['type'] == expected
    assert body['error']['relay_code'] == make().code
    relay.fake.script = [make()]
    _, raw = relay.sse('/v1/messages', _body('anthropic', stream=True))
    payload = json.loads(next(line[6:] for line in raw.splitlines() if line.startswith('data: {"type"')
                              and '"error"' in line[:30]))
    assert payload['error']['type'] == expected and payload['error']['relay_code'] == make().code
    # The OpenAI dialects keep the relay's own code.
    relay.fake.script = [make()]
    code, body = fetch(relay, '/v1/chat/completions', _body('chat'))
    assert code == status and body['error']['code'] == make().code
