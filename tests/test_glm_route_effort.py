"""Retool GLM controls measured on both real GLM routes, 2026-11-04."""
import pytest

import models
import validate
from apierr import ApiError


@pytest.mark.parametrize('model', ['zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'])
@pytest.mark.parametrize('effort', ['low', 'medium', 'high', 'max'])
@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_verified_glm_route_efforts_are_preserved(model, effort, dialect):
    data = {'model': model, 'max_tokens': 2048}
    if dialect == 'responses':
        data.update(input='17+25?', reasoning={'effort': effort})
    elif dialect == 'anthropic':
        data.update(messages=[{'role': 'user', 'content': '17+25?'}],
                    thinking={'type': 'adaptive'}, output_config={'effort': effort})
    else:
        data.update(messages=[{'role': 'user', 'content': '17+25?'}], reasoning_effort=effort)
    parsed = getattr(validate, 'parse_' + dialect)(data, {'strict_models': True})
    assert parsed.effort == effort
    assert parsed.entry['reasoning'] is True


@pytest.mark.parametrize('model', ['zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'])
@pytest.mark.parametrize('effort', ['none', 'minimal', 'xhigh', 'extra'])
def test_glm_does_not_invent_unsupported_levels_or_disable_thinking(model, effort):
    with pytest.raises(ApiError) as caught:
        models.resolve(model, effort)
    assert caught.value.status == 400
    assert 'low, medium, high, max' in caught.value.message


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
