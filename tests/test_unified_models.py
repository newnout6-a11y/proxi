"""One public model may select distinct account-owned upstream routes."""
import pytest
import models
import config
import validate
from apierr import ApiError


def setup():
    r = {'base': 'https://retool.invalid', 'provider_resource_name': '__managed__'}
    m = {'upstream': 'minara', 'bearer_token': 'synthetic', 'minara_models': [
        {'id': 'anthropic/claude-opus-5.5', 'public_id': 'claude-opus-5-5', 'provider': 'anthropic',
         'tools': True, 'reasoning': True, 'reasoning_effort_levels': ['low'], 'max_output': 128}]}
    cfg = dict(config.DEFAULTS, accounts=[r, m], model='claude-opus-5-5')
    cfg['_minara_models'] = models.build_minara_catalog([r, m])
    return cfg, r, m


def test_unified_catalog_deduplicates_and_keeps_actual_ids():
    cfg, r, m = setup()
    entries = models.catalog(cfg)
    assert sum(e['id'] == 'claude-opus-5-5' for e in entries) == 1
    assert not any(e['id'].startswith('minara/') for e in entries)
    entry, _ = models.resolve('claude-opus-5-5', cfg=cfg)
    assert models.route_for(entry, m)['upstream_model'] == 'anthropic/claude-opus-5.5'
    assert models.route_for(entry, r)['upstream_model'] == 'claude-opus-5-5'
    assert models.resolve('minara/anthropic/claude-opus-5.5', cfg=cfg)[0]['upstream'] == 'minara'
    assert not models.route_block_reason(entry, m)
    assert not models.resource_block_reason(entry, m)


def test_explicit_controls_filter_actual_route_without_mutating_catalog():
    cfg, r, m = setup()
    parsed = validate.parse_chat({'messages': [{'role': 'user', 'content': 'hi'}],
        'reasoning_effort': 'high', 'max_tokens': 256}, cfg)
    assert models.route_block_reason(parsed.entry, m)
    assert models.route_block_reason(parsed.entry, r) is None
    assert '_requirements' not in cfg['_minara_models']['claude-opus-5-5']
    parsed = validate.parse_chat({'messages': [{'role': 'user', 'content': 'hi'}],
        'reasoning_effort': 'low', 'max_tokens': 64}, cfg)
    assert models.route_block_reason(parsed.entry, m) is None


def test_retool_gates_and_substitution_do_not_hide_minara_route():
    cfg, r, m = setup()
    m['minara_models'] = [{'id': 'openai/gpt-6-luna', 'public_id': 'gpt-6-luna', 'provider': 'openai'}]
    cfg['_minara_models'] = models.build_minara_catalog([r, m])
    entry, _ = models.resolve_tolerant('gpt-6-luna', cfg=cfg)
    assert entry['id'] == 'gpt-6-luna'
    assert models.route_block_reason(entry, r)
    assert models.route_block_reason(entry, m) is None


def test_cross_provider_capabilities_are_not_copied_to_minara():
    cfg, r, m = setup()
    entry = cfg['_minara_models']['claude-opus-5-5']
    route = models.route_for(entry, m)
    assert not route.get('vision')
    assert route['max_output'] == 128
    assert route['reasoning_effort_levels'] == ['low']
    assert models.catalog_routable(entry, cfg)


def test_invalid_public_id_rejected():
    _, _, m = setup()
    m['minara_models'][0]['public_id'] = 'bad@profile'
    with pytest.raises(ApiError):
        config.validate_minara_account(m)

@pytest.mark.parametrize('kind', ['chat', 'responses', 'anthropic'])
def test_all_dialects_attach_actual_candidate_constraints(kind):
    cfg, r, m = setup()
    fn = getattr(validate, 'parse_' + kind)
    body = {'input': 'hi'} if kind == 'responses' else {'messages': [{'role': 'user', 'content': 'hi'}]}
    body.update(max_tokens=64, reasoning_effort='low')
    parsed = fn(body, cfg)
    assert not models.route_block_reason(parsed.entry, m)
    parsed.entry['_requirements']['vision'] = True
    assert models.route_block_reason(parsed.entry, m) == 'route does not support requested vision'
    assert not models.route_block_reason(parsed.entry, r)


def test_actual_route_verbosity_and_cap_are_eligibility_not_ignored_hints():
    cfg, r, m = setup()
    parsed = validate.parse_chat({'messages': [{'role': 'user', 'content': 'hi'}], 'verbosity': 'high'}, cfg)
    assert models.route_block_reason(parsed.entry, m) == 'route does not support requested verbosity'


def test_substitute_used_only_when_no_enabled_actual_desired_route():
    cfg, r, m = setup()
    m['minara_models'] = [{'id': 'openai/gpt-6-luna', 'public_id': 'gpt-6-luna', 'provider': 'openai'}]
    cfg['_minara_models'] = models.build_minara_catalog([r, m])
    m['enabled'] = False
    assert models.resolve_tolerant('gpt-6-luna', cfg=cfg)[0]['id'] != 'gpt-6-luna'


def test_provider_prefix_uses_same_unified_route_and_account_candidates():
    cfg, _, m = setup()
    entry, _ = models.resolve('anthropic/claude-opus-5-5', cfg=cfg)
    assert entry['upstream'] == 'unified' and entry['id'] == 'claude-opus-5-5'
    assert models.route_for(entry, m)['upstream_model'] == 'anthropic/claude-opus-5.5'


def test_concrete_minara_route_never_gets_retool_substitute_or_effort_downgrade():
    cfg, r, m = setup()
    m['minara_models'] = [{'id': 'openai/gpt-6-luna', 'public_id': 'gpt-6-luna', 'provider': 'openai',
                          'reasoning': True, 'reasoning_effort_levels': ['xhigh']}]
    cfg['_minara_models'] = models.build_minara_catalog([r, m])
    entry, effort = models.resolve_tolerant('gpt-6-luna', 'xhigh', cfg=cfg)
    route = models.route_for(entry, m)
    resolved, bound_effort = models.resolve_tolerant('gpt-6-luna', effort,
        cfg=dict(cfg, _minara_models={'gpt-6-luna': route}))
    assert resolved['upstream'] == 'minara' and resolved['id'] == 'gpt-6-luna'
    assert bound_effort == 'xhigh'


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_nonvisual_metadata_and_tool_schema_do_not_filter_text_route(dialect):
    cfg, _, m = setup()
    body = {'input': 'hi'} if dialect == 'responses' else {'messages': [{'role': 'user', 'content': 'hi'}]}
    body.update(max_tokens=64, metadata={'type': 'image'})
    schema = {'type': 'object', 'properties': {'example': {'type': 'string', 'default': {'type': 'image'}}}}
    body['tools'] = ([{'name': 'echo', 'input_schema': schema}] if dialect == 'anthropic' else
                     [{'type': 'function', 'name': 'echo', 'parameters': schema}] if dialect == 'responses' else
                     [{'type': 'function', 'function': {'name': 'echo', 'parameters': schema}}])
    parsed = getattr(validate, 'parse_' + dialect)(body, cfg)
    assert parsed.entry['_requirements']['vision'] is False
    assert models.route_block_reason(parsed.entry, m) is None


@pytest.mark.parametrize('choice', ['required', {'type': 'function', 'function': {'name': 'echo'}}])
def test_explicit_forced_tool_route_capability_filters_minara(choice):
    cfg, r, m = setup()
    m['minara_models'][0]['forced_tool_choice'] = False
    cfg['_minara_models'] = models.build_minara_catalog([r, m])
    parsed = validate.parse_chat({'messages': [{'role': 'user', 'content': 'hi'}],
        'tools': [{'type': 'function', 'function': {'name': 'echo', 'parameters': {'type': 'object'}}}],
        'tool_choice': choice}, cfg)
    assert models.route_for(parsed.entry, m)['forced_tool_choice'] is False
    assert models.route_block_reason(parsed.entry, m) == 'route does not support requested forced tool choice'
    assert models.route_block_reason(parsed.entry, r) is None


def test_auto_tools_allowed_when_native_forced_choice_disabled():
    cfg, _, m = setup()
    m['minara_models'][0]['forced_tool_choice'] = False
    cfg['_minara_models'] = models.build_minara_catalog([m])
    parsed = validate.parse_chat({'messages': [{'role': 'user', 'content': 'hi'}],
        'tools': [{'type': 'function', 'function': {'name': 'echo', 'parameters': {'type': 'object'}}}]}, cfg)
    assert not models.route_block_reason(parsed.entry, m)
