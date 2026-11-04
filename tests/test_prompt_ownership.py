"""Explicit API instructions belong to every client, including unknown IDEs."""
import asyncio
import json

import pytest

import client_context
import context
import config
import executor
from session import RelaySession
from test_native_context_reuse import FIRST, request, run, setup
from test_service_requests import capture_direct


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_unknown_toolless_client_keeps_exact_system_and_never_loads_host_context(tmp_path, monkeypatch, dialect):
    _, _, runner = setup(tmp_path, transport='direct', project_context=True,
                         exec_policy='allow_all', cookie_values=[],
                         relay_system_prompt='RELAY_OVERRIDE_MUST_NOT_REACH_CLIENT')
    monkeypatch.setattr(context, 'build_project_context', lambda *a, **kw: pytest.fail('client owns the project'))
    sent = capture_direct(monkeypatch)
    parsed = request(FIRST, tools=[], system='AIDER_CLIENT_SYSTEM\nPreserve this exact text.', dialect=dialect)
    parsed.request_purpose = 'generation'
    token = client_context.bind({'client': 'aider'})
    try:
        events = asyncio.run(run(runner, parsed))
    finally:
        client_context.reset(token)
    assert not any(event['type'] == 'error' for event in events)
    assert sent[0][1]['messages'][0] == {'role': 'system', 'content': [
        {'type': 'text', 'text': parsed.system, 'cache': True}]}
    assert len(sent) == 1


def test_unknown_client_text_cannot_become_a_relay_shell_command(tmp_path, monkeypatch):
    _, _, runner = setup(tmp_path, transport='direct', project_context=False,
                         exec_policy='allow_all', cookie_values=[])
    async def stream(*args, **kwargs):
        yield {'type': 'text', 'delta': '<exec_command>synthetic-client-example</exec_command>'}
        yield {'type': 'end', 'reason': 'stop'}
    monkeypatch.setattr(RelaySession, 'stream', stream)
    monkeypatch.setattr(executor, 'extract_command', lambda *_: pytest.fail('client text must not be interpreted as relay execution'))
    parsed = request(FIRST, tools=[], system='Client owns all execution.', dialect='chat')
    parsed.request_purpose = 'generation'
    events = asyncio.run(run(runner, parsed))
    assert any(event.get('delta') == '<exec_command>synthetic-client-example</exec_command>' for event in events)
    assert not any(event['type'] == 'exec' for event in events)


def test_standalone_system_prompt_is_customizable_without_claiming_unobserved_results():
    head = context.build_system_instructions('', enable_context=False, enable_local_exec=False,
                                             system_prompt='CUSTOM_RELAY_POLICY')
    assert head == 'CUSTOM_RELAY_POLICY'
    default = context.build_system_instructions('', enable_context=False, enable_local_exec=False)
    assert 'tool' in default.lower() and 'evidence' in default.lower()
    assert 'execution works' not in default


def test_custom_empty_prompt_and_disabled_execution_do_not_reappear():
    assert context.build_system_instructions('', enable_context=False, enable_local_exec=False,
                                             system_prompt='') == ''


@pytest.mark.parametrize('value', [False, 1, [], {}])
def test_invalid_configured_prompt_fails_before_credentials_are_used(tmp_path, value):
    path = tmp_path / 'config.json'
    original = json.dumps({'relay_system_prompt': value})
    path.write_text(original)
    with pytest.raises(Exception, match='relay_system_prompt must be a string or null'):
        config.load(str(path))
    assert path.read_text() == original
