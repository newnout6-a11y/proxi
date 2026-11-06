"""Retool must not silently strengthen an explicit client tool contract."""
import copy

import pytest

import direct
import validate


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('strict', [False, True])
def test_explicit_tool_strict_mode_survives_normalization_and_upstream_translation(dialect, strict):
    schema = {'type': 'object', 'properties': {'cmd': {'type': 'string'}, 'login': {'type': 'boolean'}},
              'required': ['cmd']}
    tool = {'name': 'exec_command', 'parameters': schema, 'strict': strict}
    body = {'model': 'gpt-6-sol'}
    if dialect == 'responses':
        body.update(input='Read the synthetic fixture.', tools=[{'type': 'function', **tool}])
    else:
        body['messages'] = [{'role': 'user', 'content': 'Read the synthetic fixture.'}]
        if dialect == 'anthropic':
            body.update(max_tokens=128, tools=[{'name': tool['name'], 'input_schema': schema, 'strict': strict}])
        else:
            body['tools'] = [{'type': 'function', 'function': tool}]
    original = copy.deepcopy(body)
    parsed = getattr(validate, 'parse_' + dialect)(body, {'strict_models': True, 'model': 'gpt-6-sol'})
    converted = direct.tools_to_retool(parsed.tools)
    assert converted[0]['strict'] is strict
    assert converted[0]['params']['cmd']['required'] is True
    assert converted[0]['params']['login'] == {'type': 'boolean', 'required': False}
    assert body == original


def test_unspecified_strict_mode_preserves_the_existing_retool_default():
    tools = [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}]
    assert direct.tools_to_retool(tools)[0]['strict'] is True
