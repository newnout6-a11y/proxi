"""The level of the request.compatibility event: only what clients send with every request, what changes
neither the reply nor its price and what the response headers already name is routine (debug)."""
import copy
import json

import pytest

import generation
import journal
import validate
from test_client_contracts import CFG, _body
from test_inline_system_compatibility import BY_REFERENCE

TASK_BUDGET = {'task_budget': {'type': 'tokens', 'total': 1000}}
DESKTOP_USER = json.dumps({'device_id': 'D', 'account_uuid': '', 'session_id': 'S'})

# (dialect, model, request fields, level): one row per ignored parameter and per route that ignores it.
LEVELS = [
    # routine: sent with every request by a mass client, changes neither reply nor price, named in the headers
    ('anthropic', 'claude-opus-5-5', {'thinking': {'type': 'adaptive', 'display': 'updates'}}, 'debug'),
    ('anthropic', 'claude-opus-5-5', {'temperature': 1, 'metadata': {'user_id': DESKTOP_USER}}, 'debug'),
    ('chat', 'gpt-6-sol', {'temperature': .2, 'top_p': .9, 'top_k': 5, 'presence_penalty': 0,
                           'frequency_penalty': 0}, 'debug'),
    ('chat', 'gpt-6-sol', {'metadata': {'k': 'v'}}, 'debug'),
    ('responses', 'gpt-6-sol', {'prompt_cache_key': 'K', 'prompt_cache_options': {'mode': 'auto'},
                                'client_metadata': {'x-codex-installation-id': 'I'}}, 'debug'),
    ('responses', 'gpt-6-sol', {'include': ['reasoning.encrypted_content']}, 'debug'),
    ('responses', 'gpt-6-sol', {'include': ['reasoning.encrypted_content', 'reasoning.encrypted_content']}, 'debug'),
    ('chat', 'claude-sonnet-5-5', {'verbosity': 'low'}, 'debug'),
    ('responses', 'claude-opus-5-5', {'text': {'verbosity': 'high'}}, 'debug'),
    ('anthropic', 'claude-haiku-4-5-20251001', {'thinking': {'type': 'enabled', 'budget_tokens': 2048}}, 'debug'),
    ('anthropic', 'claude-haiku-4-5-20251001', {'output_config': {'effort': 'high'}}, 'debug'),
    ('chat', 'claude-haiku-4-5-20251001', {'reasoning_effort': 'high'}, 'debug'),
    ('responses', 'zai-org/GLM-5.3', {'reasoning': {'effort': 'high'}}, 'debug'),
    # material: the client asked for something the route cannot do, or for something that changes cost or reply
    ('chat', 'gpt-6-sol', {'seed': 7}, 'warn'),
    ('responses', 'gpt-6-sol', {'prompt_cache_retention': '24h'}, 'warn'),
    ('responses', 'gpt-6-sol', {'prompt_cache_retention': 'in_memory'}, 'warn'),
    ('anthropic', 'claude-opus-5-5', {'output_config': TASK_BUDGET}, 'warn'),
    ('responses', 'gpt-6-sol', {'include': ['message.output_text.logprobs']}, 'warn'),
    ('responses', 'gpt-6-sol', {'include': ['reasoning.encrypted_content', 'file_search_call.results']}, 'warn'),
    ('responses', 'gpt-6-sol', {'include': ['web_search_call.action.sources']}, 'warn'),
    ('chat', 'gpt-6-sol', {'logprobs': True}, 'warn'),
    ('chat', 'gpt-6-sol', {'logprobs': True, 'top_logprobs': 3}, 'warn'),
    ('chat', 'gpt-6-sol', {'logit_bias': {'50256': -100}}, 'warn'),
    # one material name among routine ones is enough
    ('chat', 'gpt-6-sol', {'temperature': 1, 'metadata': {'k': 'v'}, 'seed': 7}, 'warn'),
    ('anthropic', 'claude-opus-5-5', {'thinking': {'type': 'adaptive', 'display': 'updates'},
                                      'output_config': TASK_BUDGET}, 'warn'),
    ('responses', 'gpt-6-sol', {'include': ['reasoning.encrypted_content'], 'prompt_cache_retention': '24h'}, 'warn'),
    # nothing ignored, nothing to say
    ('chat', 'gpt-6-sol', {}, 'debug'),
]


@pytest.mark.parametrize('dialect,model,extra,level', LEVELS)
def test_each_ignored_parameter_has_its_level(dialect, model, extra, level):
    parsed = getattr(validate, 'parse_' + dialect)(_body(dialect, model=model, **copy.deepcopy(extra)), CFG)
    assert generation.compatibility_level(parsed) == level, parsed.declared_ignored


def test_the_public_names_stay_what_the_headers_list():
    parsed = validate.parse_responses(_body('responses', include=['reasoning.encrypted_content'],
                                            prompt_cache_retention='24h'), CFG)
    assert parsed.declared_ignored == ['prompt_cache_retention', 'include']
    assert generation.compatibility_level(parsed) == 'warn'


def test_inline_tool_changes_are_info_unless_something_material_is_ignored_too():
    messages = [{'role': 'user', 'content': 'hi'}, {'role': 'system', 'content': [copy.deepcopy(BY_REFERENCE)]}]
    quiet = validate.parse_anthropic(_body('anthropic', messages=copy.deepcopy(messages), temperature=1), CFG)
    assert quiet.inline_tool_changes and generation.compatibility_level(quiet) == 'info'
    loud = validate.parse_anthropic(_body('anthropic', messages=copy.deepcopy(messages), seed=1), CFG)
    assert generation.compatibility_level(loud) == 'warn'


# The bodies mass clients really send in everyday work. None of them may be a warning on every request.
DESKTOP_RUNNER = {  # Claude Desktop runner 2.1.309 / 2.1.317 (56 captured bodies, all of this shape)
    'model': 'claude-opus-5-5', 'max_tokens': 128000, 'stream': True, 'metadata': {'user_id': DESKTOP_USER},
    'thinking': {'type': 'adaptive', 'display': 'updates'}, 'output_config': {'effort': 'xhigh'},
    'context_management': {'edits': [{'type': 'clear_thinking_20251015', 'keep': 'all'}]},
    'messages': [{'role': 'user', 'content': 'hi'}]}
CLAUDE_CODE = {  # Claude Code CLI: temperature and metadata.user_id on every request
    'model': 'claude-opus-5-5', 'max_tokens': 64000, 'stream': True, 'temperature': 1,
    'metadata': {'user_id': DESKTOP_USER}, 'thinking': {'type': 'adaptive', 'display': 'summarized'},
    'output_config': {'effort': 'high'}, 'messages': [{'role': 'user', 'content': 'hi'}]}
CODEX_0_164 = {  # codex-cli 0.164.0 through `codex exec` on a custom Responses provider (captured live)
    'model': 'gpt-6-sol', 'stream': True, 'tool_choice': 'auto', 'parallel_tool_calls': False,
    'reasoning': {'effort': 'medium', 'context': 'all_turns'}, 'store': False,
    'include': ['reasoning.encrypted_content'], 'prompt_cache_key': '01a1ac30-5259-7582-9dbd-9d9bab7ce419',
    'text': {'verbosity': 'low'},
    'client_metadata': {'thread_id': 'T', 'x-codex-window-id': 'T:0', 'x-codex-installation-id': 'I', 'session_id': 'T'},
    'input': [{'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'rules'}]},
              {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hi'}]}]}
CODEX_STRUCTURED = dict(CODEX_0_164, text={'verbosity': 'low', 'format': {
    'type': 'json_schema', 'strict': True, 'name': 'codex_output_schema',
    'schema': {'type': 'object', 'properties': {'project_name': {'type': 'string'}},
               'required': ['project_name'], 'additionalProperties': False}}})
OPENCODE_CHAT = {  # OpenCode through @ai-sdk/openai-compatible
    'model': 'gpt-6-sol', 'stream': True, 'stream_options': {'include_usage': True}, 'temperature': .7, 'top_p': .9,
    'messages': [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'hi'}]}
AI_SDK_RESPONSES = {  # @ai-sdk/openai Responses on a reasoning model with store: false
    'model': 'gpt-6-sol', 'stream': True, 'store': False, 'include': ['reasoning.encrypted_content'],
    'reasoning': {'effort': 'medium', 'summary': 'detailed'}, 'input': 'hi'}


@pytest.mark.parametrize('path,body', [
    ('/v1/messages', DESKTOP_RUNNER), ('/v1/messages', CLAUDE_CODE), ('/v1/responses', CODEX_0_164),
    ('/v1/responses', CODEX_STRUCTURED), ('/v1/chat/completions', OPENCODE_CHAT),
    ('/v1/responses', AI_SDK_RESPONSES)], ids=['desktop-runner', 'claude-code', 'codex', 'codex-output-schema',
                                              'opencode', 'ai-sdk-responses'])
def test_a_mass_client_is_never_a_warning_on_every_request(relay, monkeypatch, path, body):
    relay.cfg['allow_effort_downgrade'] = True  # Desktop sends xhigh to every model, as the live setting expects
    relay.fake.script = [{'type': 'text', 'delta': '{"project_name":"p"}'}, {'type': 'end', 'reason': 'stop'}]
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    with relay.request(path, dict(body, stream=False)) as response:
        assert response.status == 200
    events = [e for e in log.query() if e['kind'] == 'request.compatibility']
    assert events, 'these requests do carry ignored parameters; the header and the event name them'
    assert {e['level'] for e in events} == {'debug'}, [(e['level'], e['data']['parameters']) for e in events]


def test_the_desktop_runner_display_is_named_in_both_headers_and_logged_quietly(relay, monkeypatch):
    relay.cfg['allow_effort_downgrade'] = True
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    with relay.request('/v1/messages', dict(DESKTOP_RUNNER, stream=False)) as response:
        assert response.headers['x-proxi-thinking-display'] == 'summarized'
        assert response.headers['x-proxi-ignored-parameters'] == 'thinking.display,metadata'
    event = next(e for e in log.query() if e['kind'] == 'request.compatibility')
    assert event['level'] == 'debug' and event['data']['parameters'] == ['thinking.display', 'metadata']


@pytest.mark.parametrize('extra,expected', [({'seed': 7}, 'seed'), ({'prompt_cache_retention': '24h'}, 'prompt_cache_retention'),
                                            ({'include': ['message.output_text.logprobs']}, 'include')])
def test_a_material_parameter_warns_over_http_and_stays_named_in_the_header(relay, monkeypatch, extra, expected):
    log = journal.Journal()
    monkeypatch.setattr(journal, 'JOURNAL', log)
    with relay.request('/v1/responses', dict(CODEX_0_164, stream=False, **extra)) as response:
        assert expected in response.headers['x-proxi-ignored-parameters'].split(',')
    event = next(e for e in log.query() if e['kind'] == 'request.compatibility')
    assert event['level'] == 'warn' and expected in event['data']['parameters']
