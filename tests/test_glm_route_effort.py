"""Retool GLM effort: both routes accepted four levels on 2026-11-04, a 2026-11-05 probe found no effect.

The relay therefore accepts any known level, forwards none and names the control as ignored.
"""
import pytest

import models
import validate
from apierr import ApiError

SENT = {'chat': ['reasoning_effort'], 'responses': ['reasoning.effort'], 'anthropic': ['output_config.effort']}


def request(model, dialect, effort):
    data = {'model': model, 'max_tokens': 2048}
    if dialect == 'responses':
        data.update(input='17+25?', reasoning={'effort': effort})
    elif dialect == 'anthropic':
        data.update(messages=[{'role': 'user', 'content': '17+25?'}],
                    thinking={'type': 'adaptive'}, output_config={'effort': effort})
    else:
        data.update(messages=[{'role': 'user', 'content': '17+25?'}], reasoning_effort=effort)
    return data


@pytest.mark.parametrize('model', ['zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'])
@pytest.mark.parametrize('effort', ['low', 'medium', 'high', 'max'])
@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_glm_route_efforts_are_accepted_but_not_forwarded_and_the_control_is_named(model, effort, dialect):
    parsed = getattr(validate, 'parse_' + dialect)(request(model, dialect, effort), {'strict_models': True})
    assert parsed.entry['reasoning'] is True
    assert parsed.effort is None and parsed.requested_effort == effort
    assert parsed.declared_ignored == SENT[dialect]


@pytest.mark.parametrize('model', ['zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'])
@pytest.mark.parametrize('effort', ['none', 'minimal', 'xhigh'])
def test_glm_accepts_levels_other_providers_define_so_clients_never_get_a_400(model, effort):
    assert models.resolve(model, effort)[1] is None
    parsed = validate.parse_chat(request(model, 'chat', effort), {'strict_models': True})
    assert parsed.effort is None and parsed.declared_ignored == ['reasoning_effort']


@pytest.mark.parametrize('model', ['zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'])
@pytest.mark.parametrize('effort', ['extra', 'ultra', ''])
def test_glm_still_rejects_a_value_no_provider_defines(model, effort):
    with pytest.raises(ApiError) as caught:
        models.resolve(model, effort)
    assert caught.value.status == 400 and 'known level' in caught.value.message


@pytest.mark.parametrize('model', ['zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'])
@pytest.mark.parametrize('configured', ['auto', 'max'])
def test_configured_default_never_reaches_a_route_that_ignores_it(model, configured):
    assert models.effective_effort(models.resolve(model)[0], configured=configured) is None


def test_glm_disabled_thinking_cannot_be_honored_and_is_reported():
    data = request('zai-org/GLM-5.3', 'anthropic', None)
    data.update(thinking={'type': 'disabled'})
    data.pop('output_config')
    parsed = validate.parse_anthropic(data, {'strict_models': True})
    assert parsed.effort is None and parsed.declared_ignored == ['thinking.type']


@pytest.mark.parametrize('configured', ['auto', 'max'])
def test_provider_managed_defaults_still_omit_unverified_controls(configured):
    entry = dict(models.resolve('zai-org/GLM-5.3')[0],
                 reasoning_effort_levels=[], default_reasoning_effort=None,
                 reasoning_effort_control='provider-managed')
    assert models.effective_effort(entry, configured=configured) is None


def test_non_reasoning_model_still_omits_configured_effort(monkeypatch):
    entry = dict(models.resolve('zai-org/GLM-5.3-Flash')[0], reasoning=False,
                 id='synthetic-plain', reasoning_effort_levels=[], default_reasoning_effort=None)
    monkeypatch.setitem(models._BY_ID, entry['id'], entry)
    assert models.effective_effort(entry, configured='high') is None
    resolved, effort = models.resolve_tolerant(entry['id'], 'high', allow_downgrade=True)
    assert resolved is entry and effort is None


@pytest.mark.parametrize('payload,names', [
    ({'reasoning_effort': 'high'}, ['reasoning_effort']),
    ({'reasoningEffort': 'high'}, ['reasoningEffort']),
    ({'effort': 'high'}, ['effort']),
    ({'reasoning': {'effort': 'high'}}, ['reasoning.effort']),
    ({'output_config': {'effort': 'high'}}, ['output_config.effort']),
    ({'thinking': {'type': 'enabled', 'budget_tokens': 8192}}, ['thinking.budget_tokens']),
    ({'thinking': {'effort': 'low'}}, ['thinking.effort']),
    ({'providerOptions': {'openai': {'reasoningEffort': 'high'}}}, ['providerOptions.openai.reasoningEffort']),
    ({'extra_body': {'reasoning': {'effort': 'high'}}}, ['extra_body.reasoning.effort']),
    ({'settings': {'effort': 'high'}}, ['settings.effort']),
    ({'messages': [{'role': 'system', 'content': '', 'output_config': {'effort': 'low'}}]},
     ['messages.output_config.effort']),
    ({}, []),
])
def test_every_effort_source_the_validator_reads_is_named_when_ignored(payload, names):
    entry = models.resolve('zai-org/GLM-5.3')[0]
    assert models.ignored_effort_controls(payload, entry) == names
    if names:
        assert validate._effort(dict(payload, model='x')) is not None


def test_effort_values_are_never_echoed_in_the_ignored_names():
    entry = models.resolve('zai-org/GLM-5.3')[0]
    names = models.ignored_effort_controls({'reasoning_effort': 'secret-level-value'}, entry)
    assert 'secret-level-value' not in ''.join(names)
