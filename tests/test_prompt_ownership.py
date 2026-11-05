"""Explicit API instructions belong to every client, including unknown IDEs."""
import asyncio
import copy
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


POLICY = 'Роль: редактор.\nПравила: отвечай кратко.\nГраницы: не изменяй файлы без запроса.'


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('tools', [[], None], ids=['text-client', 'tool-client'])
@pytest.mark.parametrize('mode', ['standalone', 'append', 'replace'])
def test_configured_policy_has_real_system_role_and_explicit_client_composition(tmp_path, monkeypatch, dialect, tools, mode):
    _, _, runner = setup(tmp_path, transport='direct', project_context=True, exec_policy='allow_all',
                         cookie_values=[], relay_system_prompt=POLICY, relay_system_prompt_mode=mode)
    monkeypatch.setattr(context, 'build_project_context', lambda *a, **kw: pytest.fail('client owns project context'))
    sent = capture_direct(monkeypatch)
    p = request(FIRST, tools=tools, system='CLIENT_ROLE\nCLIENT_RULES', dialect=dialect)
    p.request_purpose = 'client-tools' if p.tools else 'client-text'
    events = asyncio.run(run(runner, p))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}
    head = {'standalone': p.system, 'append': p.system + '\n\n' + POLICY, 'replace': POLICY}[mode]
    assert sent[0][1]['messages'][0] == {'role': 'system', 'content': [
        {'type': 'text', 'text': head, 'cache': True}]}
    assert p.system == 'CLIENT_ROLE\nCLIENT_RULES'
    assert 'LOCAL EXEC' not in json.dumps(sent[0][1])
    assert POLICY not in str(sent[0][1]['messages'][1:])


@pytest.mark.parametrize('policy', [POLICY, ''])
def test_native_tools_without_client_system_use_configured_role(tmp_path, monkeypatch, policy):
    _, _, runner = setup(tmp_path, transport='direct', project_context=True, exec_policy='allow_all',
                         cookie_values=[], relay_system_prompt=policy)
    monkeypatch.setattr(context, 'build_project_context', lambda *a, **kw: pytest.fail('client owns project context'))
    sent = capture_direct(monkeypatch)
    events = asyncio.run(run(runner, request(FIRST, system='')))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}
    heads = [m for m in sent[0][1]['messages'] if m['role'] == 'system']
    assert heads == ([{'role': 'system', 'content': [{'type': 'text', 'text': policy, 'cache': True}]}] if policy else [])


@pytest.mark.parametrize('mode', ['append', 'replace'])
@pytest.mark.parametrize('purpose', sorted(client_context.SERVICE_KINDS))
def test_user_role_does_not_overwrite_client_service_contracts(tmp_path, monkeypatch, mode, purpose):
    _, _, runner = setup(tmp_path, transport='direct', project_context=True, exec_policy='allow_all',
                         cookie_values=[], relay_system_prompt=POLICY, relay_system_prompt_mode=mode)
    sent = capture_direct(monkeypatch)
    p = request(FIRST, tools=[], system='CLIENT_SERVICE_SYSTEM')
    p.request_purpose = purpose
    asyncio.run(run(runner, p))
    assert sent[0][1]['messages'][0]['content'][0]['text'] == p.system
    assert POLICY not in json.dumps(sent[0][1], ensure_ascii=False)


@pytest.mark.parametrize('mode', ['append', 'replace'])
def test_empty_policy_is_a_deliberate_value_for_client_composition(tmp_path, monkeypatch, mode):
    _, _, runner = setup(tmp_path, transport='direct', project_context=False, exec_policy='off',
                         cookie_values=[], relay_system_prompt='', relay_system_prompt_mode=mode)
    sent = capture_direct(monkeypatch)
    asyncio.run(run(runner, request(FIRST, tools=[], system='CLIENT_SYSTEM')))
    heads = [m for m in sent[0][1]['messages'] if m['role'] == 'system']
    assert heads == ([{'role': 'system', 'content': [{'type': 'text', 'text': 'CLIENT_SYSTEM', 'cache': True}]}]
                     if mode == 'append' else [])


@pytest.mark.parametrize('value', [False, 1, [], {}, None, 'invalid'])
def test_invalid_policy_mode_fails_before_credentials_are_used(tmp_path, value):
    path = tmp_path / 'config.json'
    original = json.dumps({'relay_system_prompt_mode': value})
    path.write_text(original)
    with pytest.raises(Exception, match='relay_system_prompt_mode must be one of'):
        config.load(str(path))
    assert path.read_text() == original


@pytest.mark.parametrize('mode', ['append', 'replace'])
def test_policy_replacement_cannot_enable_relay_execution_for_a_client(tmp_path, monkeypatch, mode):
    _, _, runner = setup(tmp_path, transport='direct', project_context=False, exec_policy='allow_all',
                         cookie_values=[], relay_system_prompt=POLICY, relay_system_prompt_mode=mode)
    async def stream(*args, **kwargs):
        yield {'type': 'text', 'delta': '<exec_command>synthetic-command</exec_command>'}
        yield {'type': 'end', 'reason': 'stop'}
    monkeypatch.setattr(RelaySession, 'stream', stream)
    monkeypatch.setattr(executor, 'extract_command', lambda *_: pytest.fail('client owns execution'))
    events = asyncio.run(run(runner, request(FIRST, tools=[], system='CLIENT_SYSTEM')))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}


@pytest.mark.parametrize('mode', ['append', 'replace'])
def test_null_policy_selects_default_without_host_capabilities(tmp_path, monkeypatch, mode):
    _, _, runner = setup(tmp_path, transport='direct', project_context=True, exec_policy='allow_all',
                         cookie_values=[], relay_system_prompt=None, relay_system_prompt_mode=mode)
    sent = capture_direct(monkeypatch)
    asyncio.run(run(runner, request(FIRST, tools=[], system='CLIENT_SYSTEM')))
    head = sent[0][1]['messages'][0]['content'][0]['text']
    expected = context.DEFAULT_RELAY_SYSTEM_PROMPT
    assert head == ('CLIENT_SYSTEM\n\n' + expected if mode == 'append' else expected)
    assert 'LOCAL EXEC' not in head and str(tmp_path) not in head


def test_legacy_agent_explicit_system_also_preserves_client_execution_boundary(tmp_path, monkeypatch):
    _, _, runner = setup(tmp_path, transport='agent', project_context=True, exec_policy='allow_all',
                         cookie_values=[], relay_system_prompt=POLICY, relay_system_prompt_mode='append')
    monkeypatch.setattr(context, 'build_project_context', lambda *a, **kw: pytest.fail('client owns context'))
    monkeypatch.setattr(executor, 'extract_command', lambda *_: pytest.fail('client owns execution'))
    sent = []
    async def stream(session, prompt, provider, tool_names=None):
        sent.append(prompt)
        yield {'type': 'text', 'delta': '<exec_command>synthetic-command</exec_command>'}
        yield {'type': 'end', 'reason': 'stop'}
    monkeypatch.setattr(RelaySession, 'stream', stream)
    events = asyncio.run(run(runner, request(FIRST, tools=[], system='CLIENT_SYSTEM')))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}
    assert sent[0].startswith('CLIENT_SYSTEM\n\n' + POLICY)
    assert sent[0].count('CLIENT_SYSTEM') == sent[0].count(POLICY) == 1


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('mode', ['append', 'replace'])
def test_policy_keeps_signed_tool_history_while_unchanged(tmp_path, monkeypatch, dialect, mode):
    import copy
    from test_native_history import harness, close, parsed as parse, append_result, tool_reply, strip_cache
    from test_review_fixes_pr19 import END, text
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), [text('done'), *END]])
    runner.cfg.update(relay_system_prompt=POLICY, relay_system_prompt_mode=mode)
    async def scenario():
        try:
            p = parse(FIRST, dialect)
            messages = append_result(copy.deepcopy(FIRST), p, await run(runner, p), dialect)
            events = await run(runner, parse(messages, dialect))
            assert events[-1] == {'type': 'end', 'reason': 'stop'}
        finally:
            await close(pool)
    asyncio.run(scenario())
    assert len(sent) == 2
    assert strip_cache(sent[1]['messages'][:len(sent[0]['messages'])]) == strip_cache(sent[0]['messages'])
    assert 'opaque-one' in json.dumps(sent[1])
    assert sent[1]['openAICacheKey'] == sent[0]['openAICacheKey']


@pytest.mark.parametrize('change', ['policy', 'mode'])
def test_changed_policy_resets_old_signature_then_resumes_new_history(tmp_path, monkeypatch, change):
    import copy
    from test_native_history import harness, close, parsed as parse, append_result, tool_reply
    from test_review_fixes_pr19 import END, text
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), tool_reply('c2', 'opaque-two'), [text('done'), *END]])
    runner.cfg.update(relay_system_prompt=POLICY, relay_system_prompt_mode='append')
    async def scenario():
        try:
            p = parse(FIRST)
            messages = append_result(copy.deepcopy(FIRST), p, await run(runner, p), 'chat')
            runner.cfg['relay_system_prompt' if change == 'policy' else 'relay_system_prompt_mode'] = (
                'NEW_ROLE_AND_RULES' if change == 'policy' else 'replace')
            p = parse(messages)
            messages = append_result(messages, p, await run(runner, p), 'chat')
            await run(runner, parse(messages))
        finally:
            await close(pool)
    asyncio.run(scenario())
    assert len(sent) == 3
    assert 'opaque-one' not in json.dumps(sent[1])
    assert 'opaque-two' in json.dumps(sent[2])
    assert sent[0]['openAICacheKey'] != sent[1]['openAICacheKey']
    assert sent[1]['openAICacheKey'] == sent[2]['openAICacheKey']


@pytest.mark.parametrize('mode', ['standalone', 'append', 'replace'])
def test_prompt_byte_limit_counts_full_system_policy_before_acquiring_upstream(tmp_path, monkeypatch, mode):
    _, pool, runner = setup(tmp_path, transport='direct', project_context=False, exec_policy='off',
                            cookie_values=[], relay_system_prompt='ж' * 100,
                            relay_system_prompt_mode=mode, max_prompt_bytes=64)
    sent = capture_direct(monkeypatch)
    p = request(FIRST, tools=[], system='' if mode == 'standalone' else 'CLIENT_SYSTEM')
    events = asyncio.run(run(runner, p))
    error = next(e['error'] for e in events if e.get('type') == 'error')
    assert error.status == 413 and error.code == 'prompt_too_large'
    assert not sent and not pool._active and not pool._conversations


@pytest.mark.parametrize('over_by', [0, 1])
def test_policy_budget_accepts_exact_utf8_boundary_without_truncating(tmp_path, monkeypatch, over_by):
    _, _, runner = setup(tmp_path, transport='direct', project_context=False, exec_policy='off',
                         cookie_values=[], relay_system_prompt='Роль: редактор', relay_system_prompt_mode='replace')
    sent = capture_direct(monkeypatch)
    p = request(FIRST, tools=[], system='CLIENT_SYSTEM')
    runner.cfg['max_prompt_bytes'] = len(('Роль: редактор\n' + p.prompt).encode('utf-8')) - over_by
    events = asyncio.run(run(runner, p))
    if over_by:
        assert next(e['error'] for e in events if e.get('type') == 'error').status == 413
        assert not sent
    else:
        assert events[-1] == {'type': 'end', 'reason': 'stop'}
        assert sent[0][1]['messages'][0]['content'][0]['text'] == 'Роль: редактор'


@pytest.mark.parametrize('output_chars', [150, 250, 2000])
@pytest.mark.parametrize('step_cap', [0, 1])
def test_direct_exec_budget_counts_retained_chunks_and_trims_only_new_output(tmp_path, monkeypatch, output_chars, step_cap):
    from test_review_fixes_pr19 import END, response, text
    import direct
    _, _, runner = setup(tmp_path, transport='direct', base='https://fixture.invalid', project_context=False,
                         exec_policy='allow_all', exec_allowlist=[], command_timeout=3, command_output_limit=10000,
                         show_exec_output=False, max_exec_turns=step_cap, cookie_values=[],
                         relay_system_prompt='POLICY', max_prompt_bytes=1500, cache_keepwarm_seconds=0)
    runner.pool.backend.headers = lambda: {}
    bodies, commands = [], []
    def opener(req, **kw):
        bodies.append(json.loads(req.data))
        return response(text('<exec_command>synthetic-command</exec_command>' if len(bodies) == 1 else 'done'), *END)
    monkeypatch.setattr(direct.urllib.request, 'urlopen', opener)
    monkeypatch.setattr(executor, 'run_command', lambda command, *a, **kw: (commands.append(command) or 0, 'ж' * output_chars))
    p = request([{'role': 'user', 'content': 'ж' * 100}], tools=[], system='')
    events = asyncio.run(run(runner, p))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}
    assert len(bodies) == (3 if step_cap else 2) and len(commands) == 1
    for body in bodies:
        parts = [block['text'] for message in body['messages'] for block in message['content']
                 if isinstance(block, dict) and block.get('type') == 'text']
        assert len('\n'.join(parts).encode('utf-8')) <= 1500
        assert parts[0].startswith('POLICY')
        assert any('ж' * 100 in part for part in parts)
    assert 'synthetic-command' in json.dumps(bodies[1])
    assert 'truncated to configured prompt byte limit' in json.dumps(bodies[1])


@pytest.mark.parametrize('system', ['\n\n', ' ', '\t\n'])
def test_blank_client_system_never_takes_the_relay_role_away(tmp_path, monkeypatch, system):
    _, _, runner = setup(tmp_path, transport='direct', project_context=True, exec_policy='allow_all',
                         cookie_values=[], relay_system_prompt=POLICY)
    sent = capture_direct(monkeypatch)
    p = request(FIRST, system='')
    p.system = system  # what two empty effort entries used to leave behind
    events = asyncio.run(run(runner, p))
    assert events[-1] == {'type': 'end', 'reason': 'stop'}
    heads = [m for m in sent[0][1]['messages'] if m['role'] == 'system']
    assert heads == [{'role': 'system', 'content': [{'type': 'text', 'text': POLICY, 'cache': True}]}]


def test_blank_client_system_keeps_project_and_execution_support_of_an_anonymous_relay_task(tmp_path, monkeypatch):
    _, _, runner = setup(tmp_path, transport='direct', project_context=True, exec_policy='allowlist',
                         cookie_values=[], command_timeout=3, command_output_limit=4000, exec_allowlist=[],
                         context_max_bytes=6000, relay_system_prompt='USER_CONFIGURED_RELAY_SYSTEM')
    (tmp_path / 'README.md').write_text('SYNTHETIC_PROJECT_CONTEXT', encoding='utf-8')
    heads = []

    async def stream(session, prompt, provider, tool_names=None):
        heads.append(session._direct['head'])
        yield {'type': 'text', 'delta': 'ok'}
        yield {'type': 'end', 'reason': 'stop'}

    monkeypatch.setattr(RelaySession, 'stream', stream)
    p = request([{'role': 'user', 'content': 'Run my relay task'}], tools=[], system='')
    p.system = '\n\n'
    assert asyncio.run(run(runner, p))[-1] == {'type': 'end', 'reason': 'stop'}
    for expected in ('SYNTHETIC_PROJECT_CONTEXT', 'LOCAL EXEC REMINDER', 'USER_CONFIGURED_RELAY_SYSTEM'):
        assert expected in heads[0]


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_effort_only_entries_leave_the_relay_role_in_place(tmp_path, monkeypatch, dialect):
    _, _, runner = setup(tmp_path, transport='direct', project_context=True, exec_policy='allow_all',
                         cookie_values=[], relay_system_prompt=POLICY)
    sent = capture_direct(monkeypatch)
    effort = {'role': 'system', 'content': [], 'output_config': {'effort': 'low'}}
    p = request(FIRST + [effort, copy.deepcopy(effort)], system='', dialect=dialect)
    assert p.system == '' and p.effort == 'low'
    assert asyncio.run(run(runner, p))[-1] == {'type': 'end', 'reason': 'stop'}
    heads = [m for m in sent[0][1]['messages'] if m['role'] == 'system']
    assert heads == [{'role': 'system', 'content': [{'type': 'text', 'text': POLICY, 'cache': True}]}]


def run_heads(tmp_path, monkeypatch, policy, mode, tools, system):
    tmp_path.mkdir()
    _, _, runner = setup(tmp_path, transport='direct', project_context=False, exec_policy='off',
                         cookie_values=[], relay_system_prompt=policy, relay_system_prompt_mode=mode)
    sent = capture_direct(monkeypatch)
    asyncio.run(run(runner, request(FIRST, tools=tools, system=system)))
    return [m for m in sent[0][1]['messages'] if m['role'] == 'system']


@pytest.mark.parametrize('blank', ['   ', ' \n\t ', ' \u00a0 '])
@pytest.mark.parametrize('system', ['', 'CLIENT_SYSTEM'])
@pytest.mark.parametrize('tools', [[], None], ids=['text-client', 'tool-client'])
@pytest.mark.parametrize('mode', ['standalone', 'append', 'replace'])
def test_blank_policy_behaves_exactly_like_the_empty_policy(tmp_path, monkeypatch, mode, tools, system, blank):
    empty = run_heads(tmp_path / 'empty', monkeypatch, '', mode, tools, system)
    assert run_heads(tmp_path / 'blank', monkeypatch, blank, mode, tools, system) == empty
    assert all(block['text'].strip() for message in empty for block in message['content'])


def test_blank_policy_is_accepted_by_the_loader_and_means_no_role(tmp_path, monkeypatch):
    for name in ('RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF', 'RETOOL_PAGE_UUID'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('RELAY_API_KEY', 'synthetic-key')
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'base': 'https://a.invalid', 'page_uuid': 'page-a', 'ua': 'test',
                                'cookie': 'accessToken=synthetic; xsrfToken=x', 'xsrf': 'x',
                                'workspace_root': str(tmp_path), 'relay_system_prompt': ' \n\t '}), encoding='utf-8')
    loaded = config.load(str(path))
    # The stored text is kept: the admin compares the file with the running value.
    assert loaded['relay_system_prompt'] == ' \n\t '
    assert context.relay_policy(loaded['relay_system_prompt']) == ''
    assert context.relay_policy('') == '' and context.relay_policy(None) == context.DEFAULT_RELAY_SYSTEM_PROMPT
    assert context.relay_policy(POLICY) == POLICY
    assert context.build_system_instructions('', enable_context=False, enable_local_exec=False,
                                             system_prompt=loaded['relay_system_prompt']) == ''


def test_client_instructions_section_is_gone_from_the_prompt_builder():
    import inspect
    import orchestrator
    assert '[CLIENT INSTRUCTIONS]' not in inspect.getsource(orchestrator)


def test_replace_mode_is_live_for_ordinary_requests_but_never_for_the_sdk_title_contract(tmp_path, monkeypatch):
    import validate
    from test_desktop_service_protocol import CFG, title_body
    _, _, runner = setup(tmp_path, transport='direct', relay_system_prompt_mode='replace',
                         relay_system_prompt='UNRELATED RELAY POLICY', cookie_values=[])
    assert runner.cfg['relay_system_prompt_mode'] == 'replace'
    heads = []

    async def stream(session, prompt, provider, tool_names=None):
        heads.append(session._direct['head'])
        yield {'type': 'text', 'delta': '{"title":"Cache accounting"}'}
        yield {'type': 'end', 'reason': 'stop'}

    monkeypatch.setattr(RelaySession, 'stream', stream)
    asyncio.run(run(runner, validate.parse_anthropic(title_body(), CFG)))
    asyncio.run(run(runner, request(FIRST, tools=[], system='ORDINARY_CLIENT_SYSTEM')))
    assert client_context.SDK_TITLE_PREFIX in heads[0] and 'UNRELATED RELAY POLICY' not in heads[0]
    assert heads[1] == 'UNRELATED RELAY POLICY'
