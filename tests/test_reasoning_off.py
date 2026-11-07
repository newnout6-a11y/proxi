"""Reasoning off on a route whose levels have no `none` (Opus 5.5, Sonnet 5.5, GPT-5.5): with
allow_effort_downgrade the lowest supported level is used and disclosed like any other replaced level;
without it the 400 names the setting."""
import asyncio

import pytest

import apierr
import journal
import validate
from test_client_contracts import CFG, _body
from test_native_context_reuse import run, setup
from test_service_requests import capture_direct

# (model, the lowest level its route accepts)
NO_NONE = [('claude-opus-5-5', 'low'), ('claude-sonnet-5-5', 'low'), ('gpt-5.5', 'low'), ('gpt-5', 'minimal')]
HAS_NONE = ['gpt-6-sol', 'gpt-5.2', 'gpt-5.1', 'gpt-6.1-terra']
# How a client asks for reasoning to be off.
ASKS = [('anthropic', {'thinking': {'type': 'disabled'}}), ('anthropic', {'output_config': {'effort': 'none'}}),
        ('chat', {'reasoning_effort': 'none'}), ('chat', {'thinking': {'type': 'disabled'}}),
        ('responses', {'reasoning': {'effort': 'none'}})]


def parse(dialect, model, extra, **cfg):
    return getattr(validate, 'parse_' + dialect)(_body(dialect, model=model, **extra), dict(CFG, **cfg))


@pytest.mark.parametrize('dialect,extra', ASKS)
@pytest.mark.parametrize('model,lowest', NO_NONE)
def test_with_the_setting_reasoning_off_becomes_the_lowest_supported_level(model, lowest, dialect, extra):
    parsed = parse(dialect, model, extra, allow_effort_downgrade=True)
    assert (parsed.requested_effort, parsed.effort) == ('none', lowest)


@pytest.mark.parametrize('dialect,extra', ASKS)
@pytest.mark.parametrize('model,lowest', NO_NONE)
def test_without_the_setting_it_stays_a_400_and_names_the_setting(model, lowest, dialect, extra):
    with pytest.raises(apierr.ApiError) as error:
        parse(dialect, model, extra)
    message = str(error.value)
    assert (error.value.status, error.value.param) == (400, 'reasoning_effort')
    assert 'must be one of' in message and 'allow_effort_downgrade' in message
    assert 'turn reasoning off' in message and 'lowest level, %s' % lowest in message


@pytest.mark.parametrize('dialect,extra', ASKS)
@pytest.mark.parametrize('model', HAS_NONE)
def test_a_route_that_has_none_honours_it_with_or_without_the_setting(model, dialect, extra):
    for cfg in ({}, {'allow_effort_downgrade': True}):
        parsed = parse(dialect, model, extra, **cfg)
        assert (parsed.requested_effort, parsed.effort) == ('none', 'none')


def test_the_hint_names_the_level_that_would_replace_an_unsupported_one():
    with pytest.raises(apierr.ApiError) as error:
        parse('chat', 'claude-opus-5-5', {'reasoning_effort': 'minimal'})
    assert 'allow_effort_downgrade' in str(error.value) and 'use low instead' in str(error.value)
    assert 'turn reasoning off' not in str(error.value)


@pytest.mark.parametrize('value', ['extra', 'huge', 'LOUD'])  # not ultra: Codex's name for max (test_codex_ultra.py)
def test_no_hint_where_the_setting_cannot_help(value):
    with pytest.raises(apierr.ApiError) as error:
        parse('chat', 'claude-opus-5-5', {'reasoning_effort': value})
    assert error.value.status == 400 and 'allow_effort_downgrade' not in str(error.value)


@pytest.mark.parametrize('model,lowest', NO_NONE[:2])
def test_http_names_the_setting_without_it_and_serves_with_it(relay, model, lowest):
    payload = _body('anthropic', model=model, thinking={'type': 'disabled'})
    status, body = relay.json_any('/v1/messages', payload)
    assert status == 400 and 'allow_effort_downgrade' in body['error']['message'] and not relay.fake.seen
    relay.cfg['allow_effort_downgrade'] = True
    status, body = relay.json_any('/v1/messages', payload)
    assert status == 200
    assert (relay.fake.seen[-1].requested_effort, relay.fake.seen[-1].effort) == ('none', lowest)


@pytest.mark.parametrize('model,lowest,resolution', [('claude-opus-5-5', 'low', 'compatibility'),
                                                     ('claude-sonnet-5-5', 'low', 'compatibility'),
                                                     ('gpt-6-sol', 'none', 'exact')])
def test_the_replacement_is_disclosed_in_the_journal_and_the_upstream_gets_the_actual_level(
        tmp_path, monkeypatch, model, lowest, resolution):
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    _, _, runner = setup(tmp_path, transport='direct', project_context=False, cookie_values=[],
                         provider_resource_name='test-anthropic', openai_provider_resource_name='test-openai')
    sent = capture_direct(monkeypatch)
    parsed = parse('anthropic', model, {'thinking': {'type': 'disabled'}}, allow_effort_downgrade=True)
    events = asyncio.run(run(runner, parsed))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}
    assert sent[0][1]['reasoningEffort'] == lowest
    begun = next(e for e in log.query() if e['kind'] == 'turn.begin')['data']
    assert (begun['requested_effort'], begun['effective_effort'], begun['effort_resolution']) == (
        'none', lowest, resolution)
