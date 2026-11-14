"""Messages errors use Anthropic's documented types; the OpenAI dialects keep their shapes."""
import json
import urllib.error

import pytest

import apierr
from test_client_contracts import _body


def test_unprocessable_is_an_invalid_request_for_anthropic_clients():
    error = apierr.unprocessable("'stop' is not supported by the Retool route", param='stop')
    assert error.anthropic_envelope() == {'type': 'error', 'error': {
        'type': 'invalid_request_error', 'message': "'stop' is not supported by the Retool route"}}
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


def test_anthropic_types_of_other_relay_codes_are_not_rewritten():
    assert apierr.bad_request('x').anthropic_envelope()['error']['type'] == 'invalid_request_error'
    assert apierr.not_found().anthropic_envelope()['error']['type'] == 'not_found_error'
    assert apierr.too_many_requests().anthropic_envelope()['error']['type'] == 'rate_limit_error'
    assert apierr.upstream_error('x').anthropic_envelope()['error']['type'] == 'upstream_error'


def fetch(relay, path, payload):
    try:
        with relay.request(path, payload) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_messages_route_reports_422_as_an_invalid_request(relay):
    status, body = fetch(relay, '/v1/messages', _body('anthropic', stop_sequences=['END']))
    assert status == 422 and body['type'] == 'error'
    assert body['error']['type'] == 'invalid_request_error'
    assert "'stop_sequences' is not supported" in body['error']['message']
    assert body['request_id'] and 'param' not in body['error']


def test_openai_routes_keep_their_422_shape(relay):
    status, body = fetch(relay, '/v1/chat/completions', _body('chat', stop=['END']))
    assert status == 422
    assert body['error']['type'] == body['error']['code'] == 'unprocessable_entity'
    assert body['error']['param'] == 'stop'
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
