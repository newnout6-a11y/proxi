"""Effort tiers from the effort study, and how the relay sends a client's level to a Minara model."""
import pytest

import apierr
import minara_catalog
import models
from scripts import probe_minara as probe


def study(**values):
    """{level: tokens | 'refused'} -> study rows, two equal samples per level; default given as default=..."""
    rows = {}
    for level, value in values.items():
        if value == 'refused':
            rows[level] = [{'status': 400, 'detail': 'gpt-6.1-sol does not support reasoning effort "none"'}]
        else:
            rows[level] = [{'status': 200, 'reasoning_tokens': value}, {'status': 200, 'reasoning_tokens': value}]
    return rows


def changed(tiers):
    return {level: sent for level, sent in tiers['map'].items() if sent != level}


def test_working_levels_are_sent_as_asked_and_only_distinct_ones_are_offered():
    # Opus 5.5: the same reasoning from none to xhigh on this question, more at max
    tiers = probe.effort_tiers(study(default=200, none=202, minimal=180, low=189, medium=194, high=186, xhigh=215, max=381))
    assert tiers['levels'] == ['max'] and tiers['control'] == 'mapped'
    assert changed(tiers) == {}  # "high" still goes as high: equal on one question is not equal on a hard task


def test_none_is_offered_and_sends_a_level_that_really_switches_reasoning_off():
    # Sonnet 5.5: its own "none" still reasons, minimal/low/medium do not
    tiers = probe.effort_tiers(study(default=180, none=160, minimal=0, low=0, medium=0, high=170, xhigh=195, max=520))
    assert tiers['levels'] == ['none', 'max']
    assert changed(tiers) == {'none': 'minimal'}


def test_high_levels_that_switch_reasoning_off_go_as_the_strongest_level_below():
    # Opus 4.8: no thinking by default, thinks from low on, xhigh and max came back with no reasoning
    tiers = probe.effort_tiers(study(default=0, none=0, minimal=0, low=190, medium=250, high=260, xhigh=0, max=0))
    assert tiers['levels'] == ['low', 'high']
    assert changed(tiers) == {'xhigh': 'high', 'max': 'high'}
    assert tiers['broken'] == ['xhigh', 'max']


def test_a_model_whose_levels_change_nothing_offers_none_but_still_forwards_them():
    # Gemini 3.5 Flash: the same reasoning at every level
    tiers = probe.effort_tiers(study(default=1300, none=1307, minimal=1200, low=1386, medium=1252, high=1291, xhigh=1111,
                                     max=1170))
    assert tiers['levels'] == [] and tiers['control'] == 'mapped' and changed(tiers) == {}


def test_refused_levels_go_as_the_nearest_working_level():
    # GPT-6.1 Sol refuses none and minimal in words on its direct route
    tiers = probe.effort_tiers(study(default=90, none='refused', minimal='refused', low=60, medium=64, high=90, xhigh=95,
                                     max=130))
    assert tiers['refused'] == ['minimal', 'none']
    assert changed(tiers) == {'none': 'low', 'minimal': 'low'}
    assert tiers['levels'] == ['low', 'max']  # high/xhigh reason as the default does: Default is that choice


def test_a_broken_level_goes_as_nothing_when_the_default_reasons_most():
    # Kimi K2.6: explicit levels cap its thinking, its default and "none" think far more
    tiers = probe.effort_tiers(study(default=6000, none=3400, minimal=1000, low=950, medium=1100, high=300, xhigh=1300,
                                     max=830))
    assert tiers['map']['high'] is None


def test_an_incomplete_study_gives_no_tiers():
    assert probe.effort_tiers(study(default=200, none=200, low=190)) is None


def entry(control, levels, effort_map):
    return {'id': 'minara/x', 'upstream': 'minara', 'upstream_model': 'v/x', 'provider': 'v', 'reasoning': True,
            'label': 'x', 'reasoning_effort_levels': levels, 'effort_map': effort_map, 'reasoning_effort_control': control}


FULL_MAP = {'none': 'minimal', 'minimal': 'minimal', 'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'high',
            'max': 'max'}


def test_resolve_sends_the_mapped_level_and_refuses_unknown_words():
    cfg = {'_minara_models': {'minara/x': entry('mapped', ['none', 'max'], FULL_MAP)}}
    assert models.resolve('minara/x', 'none', cfg=cfg)[1] == 'minimal'
    assert models.resolve('minara/x', 'XHIGH', cfg=cfg)[1] == 'high'
    assert models.resolve('minara/x', 'low', cfg=cfg)[1] == 'low'  # not offered, still sent as asked
    with pytest.raises(apierr.ApiError):
        models.resolve('minara/x', 'ultra', cfg=cfg)


def test_auto_sends_nothing_to_minara_and_an_explicit_default_goes_through_the_map():
    mapped = entry('mapped', ['none', 'max'], FULL_MAP)
    assert models.effective_effort(mapped, None, 'auto') is None
    assert models.effective_effort(mapped, None, 'none') == 'minimal'
    retool = models._BY_ID['claude-opus-5-5']
    assert models.effective_effort(retool, None, 'auto') is not None  # Retool's behaviour is unchanged


def test_routes_take_the_studied_map_unless_the_account_lists_its_own_levels(monkeypatch):
    measured = {'v/x': {'id': 'minara/x', 'upstream_model': 'v/x', 'provider': 'v', 'status': 'ok', 'reasoning': True,
                        'reasoning_effort_levels': ['none', 'max'], 'effort_control': 'mapped', 'effort_map': FULL_MAP},
                'v/y': {'id': 'minara/y', 'upstream_model': 'v/y', 'provider': 'v', 'status': 'ok', 'reasoning': True,
                        'reasoning_effort_levels': [], 'effort_control': 'mapped',
                        'effort_map': {level: level for level in models.EFFORT_ORDER if level != 'none'} | {'none': 'none'}}}
    monkeypatch.setattr(minara_catalog, 'MEASURED', measured)
    routes = minara_catalog.build([{'upstream': 'minara', 'bearer_token': 't'}])
    assert routes['minara/x']['reasoning_effort_control'] == 'mapped' and routes['minara/x']['effort_map']['none'] == 'minimal'
    assert routes['minara/y']['reasoning_effort_control'] == 'mapped' and routes['minara/y']['reasoning_effort_levels'] == []
    assert models.resolve('minara/y', 'high', cfg={'_minara_models': routes})[1] == 'high'
    own = minara_catalog.build([{'upstream': 'minara', 'bearer_token': 't',
                                 'minara_models': [{'id': 'v/x', 'reasoning_effort_levels': ['low', 'high']}]}])
    assert own['minara/x']['reasoning_effort_control'] == 'selectable' and own['minara/x']['effort_map'] is None


# -- channels: levels Minara sends to a channel that drops the cache marks -------------------------------------------
OPUS_47 = {'default': 'bedrock', 'none': 'bedrock', 'minimal': 'bedrock', 'low': 'openrouter', 'medium': 'openrouter',
           'high': 'openrouter', 'xhigh': 'bedrock', 'max': 'bedrock'}  # measured 2026-11-10 at max_tokens 32000


def test_every_level_goes_as_the_nearest_one_that_reaches_a_caching_channel():
    levels, mapping, default = probe.cached_levels(OPUS_47)
    assert levels == ['low', 'medium', 'high'] and default == 'high'
    assert mapping == {'none': 'low', 'minimal': 'low', 'low': 'low', 'medium': 'medium', 'high': 'high',
                       'xhigh': 'high', 'max': 'high'}
    assert probe.cached_levels(dict.fromkeys(OPUS_47, 'bedrock')) is None
    assert probe.cached_levels(dict(OPUS_47, default='openrouter'))[2] is None  # its own default caches: send nothing


def _generated(study=None):
    ok = {'status': 'ok', 'tools': True, 'id': 'gen-1', 'served': None}
    thinking = {'status': 'ok', 'thinking': 'end', 'reasoning_tokens': 40}
    results = {
        'anthropic/claude-opus-5.5': {'tools': dict(ok), 'thinking': dict(thinking)},
        'anthropic/claude-opus-4.7': {'tools': dict(ok), 'thinking': dict(thinking),
                                      'channels': {'status': 'ok', 'channels': dict(OPUS_47)}},
        'anthropic/claude-sonnet-4.6': {'tools': dict(ok, id='msg_bdrk_1'), 'thinking': dict(thinking),
                                        'channels': {'status': 'ok', 'channels': dict.fromkeys(OPUS_47, 'bedrock')}},
        'openai/gpt-6.1-sol': {'tools': dict(ok, id='resp_1'), 'thinking': dict(thinking)},
    }
    listing = [{'id': name, 'supported_endpoint_types': ['openai']} for name in results]
    return {entry['upstream_model']: entry for entry in probe.generate(results, listing, {}, study)['models']}


def test_the_generator_takes_a_full_study_and_the_measured_channels():
    full = {level: [{'status': 200, 'reasoning_tokens': 200}] * 2 for level in ('default',) + probe.EFFORTS}
    entries = _generated({'anthropic/claude-opus-5.5': full, 'openai/gpt-6.1-sol': full})
    opus55 = entries['anthropic/claude-opus-5.5']
    assert opus55['effort_control'] == 'mapped' and set(opus55['effort_study']) == {
        'groups', 'broken', 'off', 'refused', 'default_reasoning_tokens', 'question'}
    assert opus55['default_effort'] is None and opus55['channels'] is None
    opus47 = entries['anthropic/claude-opus-4.7']
    assert opus47['status'] == 'ok' and opus47['reasoning_effort_levels'] == ['low', 'medium', 'high']
    assert opus47['effort_map']['none'] == 'low' and opus47['effort_map']['max'] == 'high'
    assert opus47['default_effort'] == 'high' and opus47['effort_effect'] == 'channels'
    # Every level of Sonnet 4.6 reaches Bedrock through the chat endpoint: no cache at all, not published.
    assert entries['anthropic/claude-sonnet-4.6']['status'] == 'uncached'
    # Anthropic refuses a forced tool choice whenever thinking is on: every Claude route asks for the call in words.
    assert [entries[name]['forced_tool_choice'] for name in sorted(entries)] == [False, False, False, True]


def test_no_level_sends_the_catalogs_default_where_the_models_own_one_does_not_cache(monkeypatch):
    measured = {'anthropic/claude-opus-4.7': {
        'id': 'minara/claude-opus-4-7', 'upstream_model': 'anthropic/claude-opus-4.7', 'provider': 'anthropic',
        'status': 'ok', 'reasoning': True, 'reasoning_effort_levels': ['low', 'medium', 'high'],
        'effort_control': 'mapped', 'effort_map': probe.cached_levels(OPUS_47)[1], 'default_effort': 'high'}}
    monkeypatch.setattr(minara_catalog, 'MEASURED', measured)
    route = minara_catalog.build([{'upstream': 'minara', 'bearer_token': 't'}])['minara/claude-opus-4-7']
    assert route['default_reasoning_effort'] == 'high' and route['forced_tool_choice'] is False
    assert models.effective_effort(route, None, 'auto') == 'high'
    assert models.effective_effort(route, None, None) == 'high'
    assert models.effective_effort(route, None, 'none') == 'low'  # an explicit configured level goes through the map
    assert models.resolve('minara/claude-opus-4-7', 'max', cfg={'_minara_models': {'minara/claude-opus-4-7': route}})[1] == 'high'
    own = minara_catalog.build([{'upstream': 'minara', 'bearer_token': 't', 'minara_models': [
        {'id': 'anthropic/claude-opus-4.7', 'reasoning_effort_levels': ['low', 'high']}]}])['minara/claude-opus-4-7']
    assert own['default_reasoning_effort'] is None  # an account that lists its own levels sends what it is asked


def test_the_harder_question_speaks_for_a_model_once_every_level_was_measured_on_it(tmp_path):
    full = {level: [{'status': 200, 'reasoning_tokens': 200}] * 2 for level in ('default',) + probe.EFFORTS}
    easy, hard = tmp_path / 'easy.json', tmp_path / 'hard.json'
    easy.write_text(__import__('json').dumps({'v/a': full, 'v/b': full}), encoding='utf-8')
    partial = {'default': full['default'], 'none': full['none']}
    hard.write_text(__import__('json').dumps({'v/a': full, 'v/b': partial}), encoding='utf-8')
    study = probe.merged_study(easy, hard)
    assert study['v/a']['_question'] == 'hard' and study['v/b']['_question'] == 'easy'
    assert probe.effort_tiers(study['v/a']) is not None  # the tag is no level


def test_none_never_speaks_for_a_higher_level_and_a_small_gap_is_noise():
    # GLM-5.3 Prime: Minara ignores none there and it reasons the most; max reasons like the default
    glm = probe.effort_tiers(study(default=1665, none=2620, minimal=420, low=665, medium=606, high=538, xhigh=630, max=1612))
    assert glm['map']['max'] == 'max' and glm['map']['high'] == 'high'
    assert [level for level, sent in glm['map'].items() if sent == 'none'] == ['none']
    # Gemini 3.7 Flash at max: 985 against about 1,100 elsewhere is two samples' noise, not a broken level
    gemini = probe.effort_tiers(study(default=1280, none=1140, minimal=1290, low=1140, medium=980, high=1140, xhigh=1170,
                                      max=985))
    assert gemini['map']['max'] == 'max'
    # DeepSeek V4 Pro at max: a fifth of xhigh's reasoning is broken, sent as xhigh
    deepseek = probe.effort_tiers(study(default=1400, none=0, minimal=0, low=2300, medium=2800, high=3900, xhigh=16000,
                                        max=2900))
    assert deepseek['map']['max'] == 'xhigh'


def test_none_switches_reasoning_off_for_real_and_a_thinking_models_natural_level_is_no_extreme():
    # only xhigh and max reason not at all: those are broken levels, "none" is not offered and never sent as one of them
    tiers = probe.effort_tiers(study(default=500, none=500, minimal=300, low=400, medium=600, high=700, xhigh=0, max=0))
    assert 'none' not in tiers['levels'] and tiers['map']['none'] not in ('xhigh', 'max')
    # like the default: none (Minara ignores it) and max: the natural level is neither
    tiers = probe.effort_tiers(study(default=1200, none=1700, minimal=0, low=370, medium=500, high=500, xhigh=500, max=1700))
    assert tiers['natural'] not in ('none', 'max')
