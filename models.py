"""Audited model metadata; see docs/model-catalog-audit.md for provenance.

Effort capabilities describe the Retool route rather than the provider-wide union.
"""
import collections
import copy
import datetime
import fnmatch

from apierr import ApiError, bad_request, not_found, service_unavailable

# Accepted reasoning effort values per Retool route (bundle plus dated live probes).
EFFORTS_BY_PROVIDER = {
    "openai": ("minimal", "low", "medium", "high", "xhigh", "max", "none"),
    "anthropic": ("low", "medium", "high", "max"),
    "google": ("low", "medium", "high"),
    # Both GLM routes accepted low/medium/high/max on 2026-11-04, but a 2026-11-05 probe
    # found no behavioural difference between them (see _EFFORT_IGNORED below).
    "retool-fast": ("low", "medium", "high", "max"),
}

# Per-model route capabilities. Live 2026-11-03 supersedes the bundle's GPT-6
# xhigh entries: copilotStreamGPT rejects that value with HTTP 422.
_BASE_EFFORTS = ("low", "medium", "high")
_EFFORTS_BY_MODEL = {
    "gpt-5": _BASE_EFFORTS + ("minimal",),
    "gpt-5.1": ("none",) + _BASE_EFFORTS,
    "gpt-5.2": ("none",) + _BASE_EFFORTS,
    "gpt-6-astra": _BASE_EFFORTS + ("max",),
    "gpt-6.1-sol": _BASE_EFFORTS + ("max",),
    "gpt-6.1-terra": ("none",) + _BASE_EFFORTS + ("max",),
    "gpt-6-sol": ("none",) + _BASE_EFFORTS + ("max",),
    "gpt-6-luna": ("none",) + _BASE_EFFORTS + ("max",),
}

# Which config key holds the providerResourceName for each provider.
PROVIDER_RESOURCE_KEY = {
    "openai": "openai_provider_resource_name",
    "anthropic": "provider_resource_name",
    "google": "google_provider_resource_name",
    "retool-fast": "retool_fast_provider_resource_name",
}

ALIASES = {"claude-opus": "claude-opus-5-5", "claude-sonnet": "claude-sonnet-5",
           "claude-sonnet-5.5": "claude-sonnet-5-5",
           "claude-haiku": "claude-haiku-4-5-20251001", "gpt-5.6": "gpt-5.6-sol",
           "claude-opus-5.5": "claude-opus-5-5", "claude-opus-4.8": "claude-opus-4-8",
           "claude-sonnet-4.6": "claude-sonnet-4-6", "gpt-6": "gpt-6-sol",
           "gpt-6.1": "gpt-6.1-sol"}

CATALOG = [
    {"id": "gpt-6.1-sol", "label": "GPT 6.1-sol", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-6.1-terra", "label": "GPT 6.1 Terra", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-6-luna", "label": "GPT 6-luna", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-6-sol", "label": "GPT 6-sol", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-6-astra", "label": "GPT 6-astra", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5", "label": "GPT 5", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5-mini", "label": "GPT 5 Mini", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5-nano", "label": "GPT 5 Nano", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.1", "label": "GPT 5.1", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.2", "label": "GPT 5.2", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.3-codex", "label": "GPT 5.3 Codex", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.4", "label": "GPT 5.4", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.5", "label": "GPT 5.5", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.6-sol", "label": "GPT 5.6 Sol", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.6-terra", "label": "GPT 5.6 Terra", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.6-luna", "label": "GPT 5.6 Luna", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-4.1", "label": "GPT 4.1", "provider": "openai", "context": 1047576, "max_output": 32768, "reasoning": False, "tools": True, "vision": True},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-5", "label": "Claude Opus 5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-7", "label": "Claude Opus 4.7", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-20250514", "label": "Claude Opus 4", "provider": "anthropic", "context": 200000, "max_output": 32000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-1-20250805", "label": "Claude Opus 4.1", "provider": "anthropic", "context": 200000, "max_output": 32000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-5-20251101", "label": "Claude Opus 4.5", "provider": "anthropic", "context": 200000, "max_output": 64000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-6", "label": "Claude Opus 4.6", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-fable-5", "label": "Claude Fable 5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4", "provider": "anthropic", "context": 200000, "max_output": 64000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-sonnet-4-5-20250929", "label": "Claude Sonnet 4.5", "provider": "anthropic", "context": 200000, "max_output": 64000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "provider": "anthropic", "context": 200000, "max_output": 64000, "reasoning": False, "tools": True, "vision": True},
    {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro (Preview)", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash (Preview)", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash", "provider": "google", "context": 1000000, "max_output": 65000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3.1-flash-lite-preview", "label": "Gemini 3.1 Flash-Lite (Preview)", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "zai-org/GLM-5.3", "label": "Retool Power (GLM 5.3)", "provider": "retool-fast", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "zai-org/GLM-5.3-Flash", "label": "Retool Mini (GLM 5.3 Flash)", "provider": "retool-fast", "context": 984576, "max_output": 32768, "reasoning": True, "tools": True, "vision": True},
]

_BY_ID = {m['id']: m for m in CATALOG}

# Explicit registry capabilities, confirmed by live Retool route probes on 2026-11-05
# (one question, reasoningEffort low, verbosity omitted / low / high; answer words):
#   gpt-5 393/155/680, gpt-5.1 345/305/1242, gpt-5.2 398/315/839, gpt-5.4 413/216/570,
#   gpt-5.5 550/264/1349, gpt-5.6-sol 229/155/750, gpt-5.6-terra 389/184/935,
#   gpt-5.6-luna 243/160/831, gpt-6-sol 116/88/141.
# Claude answers HTTP 200 but ignores the field (claude-sonnet-5-5: 492/470/417), so it stays out.
# gpt-5.3-codex is unavailable on the route (_UNSUPPORTED_ROUTES) and was not probed: it keeps
# its registry source. Do not infer verbosity support from a model name or reasoning alone.
_VERBOSITY_MODELS = {'gpt-5', 'gpt-5.1', 'gpt-5.2', 'gpt-5.3-codex', 'gpt-5.4', 'gpt-5.5',
                     'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-6-sol'}
_VERBOSITY_LIVE = _VERBOSITY_MODELS - {'gpt-5.3-codex'}
for _model in CATALOG:
    _model['verbosity_levels'] = ['low', 'medium', 'high'] if _model['id'] in _VERBOSITY_MODELS else []
    if _model['verbosity_levels']:
        _model['verbosity_checked_at'] = '2026-11-05'
        _model['verbosity_source'] = ('live Retool route' if _model['id'] in _VERBOSITY_LIVE else
                                      'https://retool-edge.com/assets_vjs/main-DVkrwaG2.js')

# Capabilities are dated independently of local resource availability. Values
# marked provisional must not be presented as a verified upstream contract.
_AUDIT_DATE = '2026-10-29'
_RETOOL_SOURCE = 'https://retool-edge.com/assets_vjs/main-CnZ2mbHm.js'
_CLAUDE_DOCS = 'https://platform.claude.com/docs/en/models/'
_LEGACY_CLAUDE = {'claude-opus-4-20250514', 'claude-opus-4-1-20250805',
                  'claude-sonnet-4-20250514'}
# Small and fast models; the default applies where the model publishes selectable levels.
_LOW_DEFAULTS = {'gpt-5-mini', 'gpt-5-nano', 'claude-haiku-4-5-20251001',
                 'gemini-3-flash-preview', 'gemini-3.5-flash',
                 'gemini-3.1-flash-lite-preview', 'gemini-2.5-flash', 'zai-org/GLM-5.3-Flash'}
_HIGH_DEFAULTS = {'gpt-6-astra', 'claude-fable-5'}
# Retool takes a reasoningEffort for these models and changes nothing: no thinking events and no
# ordering of output tokens or latency across levels (scripts/probe_effort_effect.py, 2026-11-05).
# The control is accepted, never forwarded and never published as selectable.
_EFFORT_IGNORED = {'claude-haiku-4-5-20251001', 'zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'}


def _recommended_effort(name, levels):
    if not levels:
        return None
    return 'low' if name in _LOW_DEFAULTS else 'high' if name in _HIGH_DEFAULTS else 'medium'


for _model in CATALOG:
    _default_efforts = (_BASE_EFFORTS if _model['provider'] == 'openai'
                        else EFFORTS_BY_PROVIDER.get(_model['provider'], ()))
    _model['reasoning_effort_levels'] = list(_EFFORTS_BY_MODEL.get(_model['id'], _default_efforts))
    if not _model.get('reasoning') or _model['id'] in _EFFORT_IGNORED:
        _model['reasoning_effort_levels'] = []
    _levels = _model['reasoning_effort_levels']
    _model['default_reasoning_effort'] = _recommended_effort(_model['id'], _levels)
    _model['reasoning_effort_control'] = ('accepted-ignored' if _model['id'] in _EFFORT_IGNORED else
                                          'selectable' if _levels else 'provider-managed')
    _model['metadata_checked_at'] = _AUDIT_DATE
    _model['metadata_source'] = [_RETOOL_SOURCE]
    _model['metadata_status'] = 'verified'
    if _model['provider'] == 'openai':
        # Mini/nano inherit the family limits; their registry entries omit an
        # explicit effort list. Do not claim more than the 2026-11-05 effort probe showed.
        if _model['id'] in {'gpt-5-mini', 'gpt-5-nano'}:
            _model['metadata_status'] = 'partial'
            _model['reasoning_effort_checked_at'] = '2026-11-05'
            _model['metadata_notes'] = ('Effort low/medium/high measurably changes output tokens and latency '
                                        '(live probe 2026-11-05, two runs per level); minimal was not tested. '
                                        'Other limits inherit the Retool GPT family and are not individually verified.')
    elif _model['provider'] == 'anthropic':
        if _model['id'] in _LEGACY_CLAUDE:
            _model['metadata_status'] = 'provisional'
            _model['metadata_source'].append('https://platform.claude.com/docs/en/build-with-claude/claude-on-amazon-bedrock-legacy')
            _model['metadata_notes'] = 'Legacy 200K context; 32K Opus/64K Sonnet output were never verified on a route.'
        else:
            _slug = _model['id'].removeprefix('claude-')
            _slug = _slug.rsplit('-', 1)[0] if _slug[-8:].isdigit() else _slug
            _model['metadata_source'].append(_CLAUDE_DOCS + _slug + '/overview')
    elif _model['id'] == 'gemini-3.5-flash':
        _model['metadata_source'].append('https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5')
        _model['metadata_status'] = 'partial'
        _model['metadata_notes'] = '1M input window and 65K output documented; relay route not configured or live-verified.'
    elif _model['id'] == 'zai-org/GLM-5.3':
        _model['metadata_source'].append('https://docs.z.ai/guides/llm/glm-5.3')
        _model['metadata_status'] = 'partial'
        _model['metadata_notes'] = 'Model thinking is always enabled; relay effort forwarding and route not verified.'
    else:
        _model['metadata_status'] = 'provisional'
        _model['metadata_notes'] = 'Context is the Retool input reservation; full model limits and relay capabilities not independently verified.'

for _name in ('gpt-6-sol', 'gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-luna'):
    _entry = _BY_ID[_name]
    _entry['reasoning_effort_checked_at'] = '2026-11-03'
    _entry['metadata_notes'] = 'Live Retool managed route rejected xhigh with HTTP 422 on 2026-11-03.'
    if _name in ('gpt-6-astra', 'gpt-6-luna'):
        _entry['metadata_status'] = 'partial'
        _entry['metadata_notes'] += ' This account also rejected max as model unavailable; availability is not verified.'

for _name in ('zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash'):
    _entry = _BY_ID[_name]
    _entry['reasoning_effort_checked_at'] = '2026-11-05'
    _entry['metadata_notes'] = ('Live Retool route accepted low/medium/high/max and emitted reasoning at every level '
                                'on 2026-11-04, but a 2026-11-05 probe (eight runs per extreme level of a hard '
                                'counting question) found no consistent change in thinking volume, output tokens or '
                                'latency: effort is accepted, not forwarded and not published. '
                                'Retool image inputs passed two randomized quadrant-color tests per route; '
                                'this does not establish the underlying provider vision implementation. '
                                'Maximum context/output sizes were not stress-tested.')
    _entry['metadata_source'].append('https://docs.z.ai/guides/capabilities/thinking')

_entry = _BY_ID['claude-haiku-4-5-20251001']
_entry['reasoning_effort_checked_at'] = '2026-11-05'
_entry['metadata_notes'] = ('Retool accepts reasoningEffort for this model and ignores it: no thinking events and no '
                            'ordering of output tokens or latency across none/low/medium/high/max (live probe '
                            '2026-11-05), so the route is listed as not reasoning. Anthropic lists "Default effort: '
                            'Not supported" for Haiku 4.5, whose extended thinking is manual (budget_tokens); no '
                            'thinking appeared through this route at any level that was tried.')

_BUDGET_TO_EFFORT = ((1024, 'low'), (8192, 'medium'), (24576, 'high'))

# Anthropic lifecycle (deprecated, retired) in UTC, from
# https://platform.claude.com/docs/en/about-claude/model-deprecations as checked on 2026-11-05.
# "Requests to retired models fail", so a retired model is never routed, with or without BYOK.
_DEPRECATIONS_URL = 'https://platform.claude.com/docs/en/about-claude/model-deprecations'
_LIFECYCLE = {
    'claude-opus-4-20250514': ('2026-04-14', '2026-06-15'),
    'claude-opus-4-1-20250805': ('2026-06-05', '2026-08-05'),
    'claude-sonnet-4-20250514': ('2026-04-14', '2026-06-15'),
    'claude-sonnet-4-5-20250929': ('2026-09-30', '2026-11-30'),
}
for _name, (_deprecated, _retires) in _LIFECYCLE.items():
    _entry = _BY_ID[_name]
    _entry.update(deprecated_at=_deprecated, retires_at=_retires)
    _entry['metadata_source'].append(_DEPRECATIONS_URL)
    if _name in _LEGACY_CLAUDE:
        _entry['metadata_notes'] += ' Retired by Anthropic on %s.' % _retires
_BY_ID['claude-sonnet-4-5-20250929']['metadata_notes'] = (
    'Deprecated by Anthropic on 2026-09-30; retirement is scheduled for 2026-11-30, '
    'when the relay stops publishing this model.')

# Dated route facts, separate from model limits and per-account resource access.
_RETOOL_RETIRED = ("Retool's managed key answers 400 'not available right now' (App Builder) and 422 "
                   "'not available when using Retool's managed key for Assist' (live check 2026-11-05); "
                   "a retired model does not come back with your own key.")
_UNSUPPORTED_ROUTES = {
    'gpt-5.3-codex': 'Retool Assist uses an upstream Chat endpoint this model rejects; Responses is required.',
    'claude-opus-4-20250514': 'Retired by Anthropic on 2026-06-15. ' + _RETOOL_RETIRED,
    'claude-opus-4-1-20250805': 'Retired by Anthropic on 2026-08-05. ' + _RETOOL_RETIRED,
    'claude-sonnet-4-20250514': ('Retired by Anthropic on 2026-06-15; '
                                 'Retool Assist returns provider 404 not_found for this model.'),
    'gpt-6-luna': 'Unavailable on the Retool React route; Assist rejects this model.',
}
# A model a client names that no account serves (an unsupported route), and the model the relay serves in its place (the
# owner's choice). Codex's memory jobs ask for gpt-6-luna by name; Retool refused it (404, request
# 423d2c23bbc8 of 2026-11-06), so Codex never made a memory. A request for one is served by its substitute and says so in
# the journal (request.model_substituted); /v1/models and the Codex catalog still list only what is served.
SUBSTITUTES = {'gpt-6-luna': 'gpt-6.1-terra'}
_BYOK_ROUTES = {'gpt-6-astra'}
_RETENTION_ROUTES = {'claude-fable-5'}
_VERIFIED_ROUTES = {
    'gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-sol', 'gpt-5', 'gpt-5-mini', 'gpt-5-nano',
    'gpt-5.1', 'gpt-5.2', 'gpt-5.4', 'gpt-5.5', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-4.1',
    'claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-opus-4-7',
    'claude-opus-4-5-20251101', 'claude-opus-4-6', 'claude-sonnet-5-5', 'claude-sonnet-5',
    'claude-sonnet-4-5-20250929', 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001',
    'zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash',
}
# Every route fact above came from a live request. The Gemini entries were never probed.
_PROBED_ROUTES = _VERIFIED_ROUTES | set(_UNSUPPORTED_ROUTES) | _BYOK_ROUTES | _RETENTION_ROUTES
for _model in CATALOG:
    _name = _model['id']
    if _name in _PROBED_ROUTES:
        _model['route_checked_at'] = '2026-11-05'
    if _name in _UNSUPPORTED_ROUTES:
        _model.update(route_status='unsupported', route_reason=_UNSUPPORTED_ROUTES[_name])
    elif _name in _BYOK_ROUTES or _name in _RETENTION_ROUTES:
        _model.update(route_status='gated', route_reason=('Requires BYOK or enabled Anthropic resource data retention.'
                      if _name in _RETENTION_ROUTES else 'Requires a suitable BYOK provider resource.'))
    elif _model['provider'] == 'google':
        _model.update(route_status='unverified', route_reason='Requires Google BYOK; no configured route was tested.')
    elif _name in _VERIFIED_ROUTES:
        _model.update(route_status='verified', route_reason='Synthetic questions answered on Retool direct/Assist route.')
    else:
        _model.update(route_status='unverified', route_reason='No successful route probe is recorded.')
for _name in ('gpt-6.1-terra', 'gpt-4.1'):
    _BY_ID[_name].update(metadata_checked_at='2026-11-05',
                        metadata_source=['https://retool-edge.com/assets_vjs/main-DVkrwaG2.js'])
_BY_ID['claude-opus-5']['route_reason'] = 'Low effort answered; other small-output probes sometimes returned thinking only.'
_BY_ID['claude-sonnet-4-5-20250929']['route_reason'] = (
    'Synthetic questions answered on Retool direct/Assist route. Anthropic retires this model on '
    '2026-11-30; the relay stops publishing it on that date.')

# Input limits Retool applies per route (COPILOT_MODEL_INPUT_CONTEXT_SIZE_MAP, see
# docs/model-catalog-audit.md). A model missing here has no verified route limit, so discovery
# publishes no max_input_tokens for it.
_ROUTE_INPUT_TOKENS = {
    'claude-opus-4-6': 872000, 'claude-opus-4-7': 872000, 'claude-opus-4-8': 872000, 'claude-opus-5': 872000,
    'claude-opus-5-5': 872000, 'claude-fable-5': 872000, 'claude-sonnet-4-6': 936000,
}
for _name, _limit in _ROUTE_INPUT_TOKENS.items():
    _BY_ID[_name]['route_input_tokens'] = _limit


def _is_claude_route(model):
    """A catalog id of the Anthropic provider; a name the catalog lacks (strict_models off) counts when it says claude."""
    entry = _BY_ID.get(model)
    return entry['provider'] == 'anthropic' if entry else str(model or '').startswith('claude')


def refuses_forced_tool_choice(model):
    """True when Retool answers 400 to tool_choice any/required/named for the route: every Claude model, with and
    without thinking, at every effort (live probes of 2026-11-06). GPT and GLM take both natively. The relay asks for
    the call in words instead (forced_choice)."""
    return _is_claude_route(model)


def ignores_output_cap(model):
    """True when the route takes maxCompletionTokens and does not keep it: a cap of 300 returned 5,466 tokens and
    end_turn from Claude (live probes of 2026-11-06). GPT and GLM keep it. The relay cuts the reply itself
    (adapters.OutputCapAdapter)."""
    return _is_claude_route(model)


def _today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def _access_hint(entry, cfg):
    """How an operator unlocks a BYOK/retention model: the config keys and the account entry to edit."""
    provider = entry['provider']
    key = PROVIDER_RESOURCE_KEY[provider]
    resource = cfg.get(key)
    flags = '"byok": true' + (' or "data_retention_enabled": true' if entry['id'] in _RETENTION_ROUTES else '')
    declared = ('provider_access.%s = {"resource_name": "<value of %s>", %s}' % (provider, key, flags))
    access = cfg.get('provider_access')
    access = access.get(provider) if isinstance(access, dict) else None
    if not resource:
        return ("Set %s to your own resource in this account's entry of config.json, then declare it with %s."
                % (key, declared))
    if resource == '__managed__':
        if entry['id'] in _RETENTION_ROUTES:
            return ('If Retool shows data retention enabled for Anthropic models, add provider_access.%s = '
                    '{"resource_name": "__managed__", "data_retention_enabled": true} to this account\'s entry of '
                    'config.json; a "__managed__" resource never counts as BYOK.' % provider)
        return ('%s is "__managed__": Retool-managed keys never count as BYOK. Use your own resource there and '
                'declare it with %s.' % (key, declared))
    if isinstance(access, dict) and access.get('resource_name') not in (None, resource):
        return ('provider_access.%s.resource_name does not match %s; they must be identical. Add %s to this '
                "account's entry of config.json." % (provider, key, declared))
    return ("Add %s to this account's entry of config.json (a top-level provider_access counts only "
            "when there is no accounts[])." % declared)


def route_block_reason(entry, cfg):
    name = entry['id']
    if entry['provider'] == 'google' and cfg.get('google_provider_resource_name') == '__managed__':
        return 'Google requires a real BYOK resource; managed Google is not supported.'
    if name in _UNSUPPORTED_ROUTES:
        return _UNSUPPORTED_ROUTES[name]
    retires = entry.get('retires_at')
    if retires and retires <= _today().isoformat():
        return 'Retired by Anthropic on %s; requests to retired models fail.' % retires
    if name not in _BYOK_ROUTES | _RETENTION_ROUTES:
        return None
    provider = entry['provider']
    access = cfg.get('provider_access')
    access = access.get(provider) if isinstance(access, dict) else None
    resource = cfg.get(PROVIDER_RESOURCE_KEY[provider])
    if not isinstance(access, dict) or not resource or access.get('resource_name') != resource:
        access = {}
    byok = access.get('byok') is True and resource != '__managed__'
    retention = access.get('data_retention_enabled') is True
    if byok or (name in _RETENTION_ROUTES and retention):
        return None
    return '%s %s' % (entry.get('route_reason') or 'Required provider access is not configured.',
                      _access_hint(entry, cfg))


def resource_block_reason(entry, cfg):
    """Why this account has no provider resource for the model, or None."""
    key = PROVIDER_RESOURCE_KEY.get(entry['provider'])
    if not key:
        return "provider '%s' is not routed" % entry['provider']
    if not cfg.get(key):
        kind = ('a real Google BYOK resource' if entry['provider'] == 'google'
                else "a resource name, or '__managed__'")
        return "needs config key '%s' in the account entry (%s)" % (key, kind)
    return None


def model_unavailable(entry, reasons):
    """A settled refusal: the model exists, but no account can serve it as configured.

    404 is how both API families say a model is not served here; neither SDK retries it.
    """
    return not_found("model '%s' is not available: %s" % (entry['id'], '; '.join(dict.fromkeys(reasons))))


def require_route(entry, cfg):
    reason = route_block_reason(entry, cfg)
    if reason:
        raise service_unavailable(f"model '{entry['id']}' is unavailable on this account: {reason}")

def catalog():
    return CATALOG


def account_config(root, record):
    """One account's effective config, built exactly as SessionPool builds its backends.

    Resources are inherited from the root only within the same tenant; the pool controls
    (enabled, priority, label, provider_access) belong to the account record alone.
    """
    merged = {key: value for key, value in root.items() if key != 'accounts'}
    if str(record.get('base', root.get('base', ''))).rstrip('/') != str(root.get('base', '')).rstrip('/'):
        for key in PROVIDER_RESOURCE_KEY.values():
            if key not in record:
                merged.pop(key, None)
    merged.update(record)
    merged.update(enabled=record.get('enabled', True), priority=record.get('priority', 0),
                  label=record.get('label', ''), provider_access=record.get('provider_access', {}))
    return merged


def pool_configs(cfg, backends=None):
    """Configs of every account of the pool, switched-off ones included: the live backends, or the account
    records when there are none yet."""
    if backends is not None:
        return [backend.cfg for backend in backends]
    records = cfg.get('accounts')
    return ([account_config(cfg, record) for record in records if isinstance(record, dict)]
            if isinstance(records, list) and records else [cfg])


def serving(items, cfg, shared_main=True):
    """The accounts a request can be routed to, switched-off ones included.

    This is the one list that discovery, SessionPool.model_refusal/unavailable_error and acquire() read. In
    shared mode a request without a context scope (`shared_main`) runs on the shared session, which only the
    pool's first account serves; every other request may use any account. `items` are backends or configs.
    """
    items = list(items)
    return items[:1] if shared_main and cfg.get('session_mode') == 'shared' else items


def discovery_configs(cfg, backends):
    """Configs of the enabled accounts discovery is judged against."""
    return [item for item in serving(pool_configs(cfg, backends), cfg) if item.get('enabled', True)]


def pool_configured(configs):
    """False for a pool in which no account has any provider resource; `configs` are all the pool's accounts.

    Such a pool is shown in full and its requests explain the missing resource themselves (see refusal_reasons).
    Switched-off accounts and accounts that do not serve a given request count too: whether the pool has
    resources does not depend on who is enabled right now or which path a request takes.
    """
    return any(item.get(key) for item in configs for key in PROVIDER_RESOURCE_KEY.values())


def refusal_reasons(entry, configs, configured=True):
    """Why each account refuses the model for good, in order; None for an account that may serve it.

    Route facts always count. A missing provider resource counts only when `configured`: callers pass
    pool_configured(...) so that an empty pool leaves the explanation to the request, and acquire() passes True.
    """
    return [route_block_reason(entry, item) or (resource_block_reason(entry, item) if configured else None)
            for item in configs]


def catalog_visibility(entry, cfg, backends=None):
    """(published, reason): discovery uses enabled tenants' resources, as the actual pool does.

    Resource configuration is separate from model availability or account health:
    a temporarily empty balance does not erase an installed model's metadata.
    """
    pool = pool_configs(cfg, backends)
    configs = [item for item in serving(pool, cfg) if item.get('enabled', True)]
    if not configs:
        return False, 'no enabled account'
    refusals = [route_block_reason(entry, item) for item in configs]
    eligible = [item for item, reason in zip(configs, refusals) if not reason]
    if not eligible:
        return False, '; '.join(dict.fromkeys(refusals))
    if cfg.get('show_unrouted_models'):
        return True, None
    if not pool_configured(pool):
        return True, None  # Unconfigured setup still exposes metadata; requests explain missing resources.
    key = PROVIDER_RESOURCE_KEY.get(entry['provider'])
    if key and any(item.get(key) for item in eligible):
        return True, None
    return False, 'no enabled account sets %s' % key


def catalog_routable(entry, cfg, backends=None):
    return catalog_visibility(entry, cfg, backends)[0]


def default_model_warning(cfg, backends=None):
    """Why requests without 'model' would be refused, or None. A hidden default gives no other hint."""
    name = cfg.get('model')
    try:
        entry = resolve(name, strict=cfg.get('strict_models', True))[0]
    except ApiError:
        return "default model '%s' is not in the catalog; requests without 'model' are refused" % name
    published, reason = catalog_visibility(entry, cfg, backends)
    if published:
        return None
    return "default model '%s' is not published (%s); requests without 'model' are refused" % (entry['id'], reason)


def desktop_metadata(entry, entries):
    """Additional Anthropic Desktop discovery fields; IDs stay canonical.

    Desktop reads supports_1m, or max_input_tokens >= 1M, as "1M confirmed" and adds a separate 1M
    picker entry, so both follow the verified route input limit and never the model's public window.
    """
    result = {'display_name': entry['label'], 'description': 'Retool relay: ' + entry['label']}
    limit = entry.get('route_input_tokens')
    if limit:
        result['max_input_tokens'] = limit
        if limit >= 1000000:
            result['supports_1m'] = True
    if entry['provider'] == 'anthropic':
        family = entry['id'].removeprefix('claude-').split('-', 1)[0]
        if family not in {'opus', 'sonnet', 'haiku', 'fable', 'mythos'}:  # Desktop's documented tier enum
            return result
        result['anthropic_family_tier'] = family
        first = next(item['id'] for item in entries
                     if item['provider'] == 'anthropic' and item['id'].startswith('claude-' + family + '-'))
        result['is_family_default'] = entry['id'] == first
    return result


_CODEX_INSTRUCTIONS = (
    "You are a coding assistant working in the user's project. Follow the "
    "user's instructions and project guidance. Use the tools provided by "
    "the client to inspect files, make requested changes, and run relevant "
    "checks. Base reports on observed tool results and state any limitations."
)
# Codex ModelInfo flags a prompts document may switch per model (prompt_policy.CODEX_OVERRIDE_KEYS minus the text).
# They are Codex's own usage guidance for skills, plugins and apps; the relay publishes all three as False.
_CODEX_USAGE_FLAGS = ('include_skills_usage_instructions', 'include_plugin_usage_instructions',
                      'include_apps_usage_instructions')


def _publishes(setting, model_id):
    """Whether a profile's publish_as_model (False | True | glob list) lists this catalog model."""
    if setting is True:
        return True
    globs = [setting] if isinstance(setting, str) else setting if isinstance(setting, (list, tuple)) else ()
    folded = model_id.lower()
    return any(isinstance(glob, str) and fnmatch.fnmatchcase(folded, glob.lower()) for glob in globs)


def virtual_entries(entries, snapshot):
    """Catalog entries of the virtual models <model>@<profile> a prompts snapshot publishes.

    A profile publishes through publish_as_model: True lists every model of `entries`, a glob list the
    models whose id matches. Each entry is a copy of its base with the id <model>@<profile>, a label that
    names the profile and a `virtual` marker ({"base", "profile"}); the base entries are not repeated.
    Without a snapshot (no prompts file) there is nothing to publish and the catalogs stay as they were.
    """
    if snapshot is None:
        return []
    bases = [entry for entry in entries if 'virtual' not in entry]
    published = []
    for profile in snapshot.profile_names():
        if not _profile_pattern().fullmatch(profile):
            continue  # the id would not parse back
        setting = snapshot.profile(profile).get('publish_as_model')
        for entry in bases:
            if _publishes(setting, entry['id']):
                item = copy.deepcopy(entry)
                item.update(id=entry['id'] + VIRTUAL_SEPARATOR + profile,
                            label='%s (profile %s)' % (entry.get('label', entry['id']), profile),
                            virtual={'base': entry['id'], 'profile': profile})
                published.append(item)
    return published


def virtual_fields(entry):
    """The /v1/models fields naming the base model and the profile of a virtual entry; {} for any other."""
    virtual = entry.get('virtual')
    return {'base_model': virtual['base'], 'prompt_profile': virtual['profile']} if virtual else {}


def _codex_text(snapshot, model):
    """base_instructions of a model: the document's codex_base, else the relay's own text (an empty one is kept)."""
    text = snapshot.codex_base(model) if snapshot is not None else None
    return _CODEX_INSTRUCTIONS if text is None else text


def _codex_overrides(snapshot, model):
    """codex.models overrides of a model; a value of the wrong type is ignored (the document check is prompt_policy's)."""
    found = snapshot.codex_overrides(model) if snapshot is not None else None
    found = found if isinstance(found, dict) else {}
    persistent = found.get('persistent_instructions')
    return {'persistent_instructions': persistent if isinstance(persistent, str) else '',
            **{flag: found[flag] if isinstance(found.get(flag), bool) else False for flag in _CODEX_USAGE_FLAGS}}


def codex_catalog(entries=None, snapshot=None):
    """Codex-native ModelsResponse with direct client tools for this gateway.

    The OpenAI-compatible list is insufficient for Codex to discover a custom
    model's tool mode. Keep this separate so SDK model-list clients are unchanged.

    With `snapshot` (the current prompt_policy.Snapshot) the document steers what Codex is told:
    codex_base(model) is the model's instruction text, sent both as the legacy base_instructions and as
    model_messages.instructions_template (Codex 0.164 reads the template and takes base_instructions only
    when it is absent); codex_overrides(model) sets model_messages.persistent_instructions, which Codex
    uses only at its "persistent" reasoning effort (an empty string, the default here, turns Codex's own
    persistent-mode text off), and the include_*_usage_instructions flags that add Codex's own how-to text
    for skills, plugins and apps. The models the document publishes as <model>@<profile> follow the
    others as copies of their base (same text and overrides): Codex looks a model up by its exact slug in
    a provider catalog, so a virtual model needs its own entry. Without a snapshot the catalog is unchanged.
    """
    listed = list(CATALOG if entries is None else entries)
    result = []
    for index, entry in enumerate(listed + virtual_entries(listed, snapshot)):
        model = entry['virtual']['base'] if 'virtual' in entry else entry['id']
        instructions = _codex_text(snapshot, model)
        overrides = _codex_overrides(snapshot, model)
        efforts = entry.get('reasoning_effort_levels', [])
        default = entry.get('default_reasoning_effort')
        if default not in efforts:
            default = 'medium' if 'medium' in efforts else (efforts[0] if efforts else None)
        levels = [{'effort': effort, 'description': effort.title() + ' reasoning effort'} for effort in efforts]
        # Codex's Ultra: maximum reasoning plus proactive multi-agent delegation, which Codex runs itself. It sends max
        # for it (openai/codex df1199f, 7f135e1) and grants the delegation only to a model whose entry says
        # multi_agent_version v2, so a route that takes max takes Ultra as well.
        delegation = 'max' in efforts
        if delegation:
            levels.append({'effort': 'ultra', 'description': 'Maximum reasoning with automatic task delegation'})
        result.append({
            'slug': entry['id'], 'display_name': entry.get('label', entry['id']),
            'description': 'Retool relay: ' + entry.get('label', entry['id']),
            'default_reasoning_level': default,
            'supported_reasoning_levels': levels,
            'shell_type': 'unified_exec', 'visibility': 'list', 'supported_in_api': True,
            'priority': index, 'additional_speed_tiers': [], 'service_tiers': [],
            'default_service_tier': None, 'availability_nux': None, 'upgrade': None,
            'base_instructions': instructions,
            'model_messages': {'instructions_template': instructions,
                               'persistent_instructions': overrides['persistent_instructions']},
            'include_skills_usage_instructions': overrides['include_skills_usage_instructions'],
            'include_plugin_usage_instructions': overrides['include_plugin_usage_instructions'],
            'include_apps_usage_instructions': overrides['include_apps_usage_instructions'],
            'supports_reasoning_summary_parameter': bool(entry.get('reasoning')),
            'default_reasoning_summary': 'detailed' if entry.get('reasoning') else 'none',
            'support_verbosity': bool(entry.get('verbosity_levels')), 'default_verbosity': None,
            'apply_patch_tool_type': 'freeform',
            # Codex before openai/codex 86b1123 requires this field and sends parallel_tool_calls=false
            # without it, which the relay rejects; newer versions ignore the unknown key.
            'supports_parallel_tool_calls': True,
            'truncation_policy': {'mode': 'tokens', 'limit': 10000},
            'context_window': entry.get('context', 128000),
            'effective_context_window_percent': 95,
            'experimental_supported_tools': [],
            'input_modalities': ['text', 'image'] if entry.get('vision') else ['text'],
            'supports_search_tool': False, 'supports_experimental_context': False,
            'use_responses_lite': False, 'supports_reasoning_effort_updates': False,
            # Codex refuses its node_repl MCP server (mcp__node_repl__js: "node_repl is unavailable for this model") for
            # a model whose entry disables it; OpenAI's own catalog leaves it on for every model, and so does this one.
            'node_repl_disabled': False, 'tool_mode': 'direct',
        })
        if delegation:
            result[-1]['multi_agent_version'] = 'v2'
    return {'models': result}


def _canonical(name):
    name = (name or '').strip()
    return ALIASES.get(name, name)


def substitute_for(name):
    """The id of the model that serves a client's model name in its place, or None.

    `name` is the base of the client's name (no @profile, no effort suffix). Only a model of an unsupported route has a
    substitute (SUBSTITUTES): a model some account can serve is never replaced."""
    named = _canonical(name)
    if '/' in named and named not in _BY_ID:
        named = _canonical(named.split('/', 1)[1])  # a provider prefix ("openai/gpt-6-luna") is read as resolve() reads it
    served = SUBSTITUTES.get(named)
    return served if served in _BY_ID and named in _UNSUPPORTED_ROUTES else None


EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_EFFORT_RANK = {name: index for index, name in enumerate(EFFORT_ORDER)}

# A client model name may select a prompt profile: <model>@<profile>[#effort | :effort] (plan 4.3 item 7).
# '@' is reserved for that, so no catalog id and no alias may contain one (tests/test_prompt_models_parse.py).
VIRTUAL_SEPARATOR = '@'
# What parse_model_name makes of a name: the base model as the client wrote it (aliases not yet resolved,
# suffixes cut off), the @profile or None, and the #effort / :effort suffix or None.
ModelName = collections.namedtuple('ModelName', 'base profile effort')


def _profile_pattern():
    """prompt_policy.PROFILE_NAME, the one rule for profile names (a profile name is also this suffix)."""
    import prompt_policy  # late: prompt_policy may import this module
    return prompt_policy.PROFILE_NAME


def _names_a_catalog_model(base):
    """Whether `base`, what a client name has before its '@', is a catalog id or alias, as resolve() reads names.

    An effort level written after it is ignored here: claude-opus-5-5#high@mine is a malformed virtual name (the
    effort goes after the profile), not the name of some other model."""
    for separator in ('#', ':'):
        if separator in base:
            head, tail = base.split(separator, 1)
            if tail.strip().lower() in EFFORT_ORDER:
                base = head.strip()
            break
    name = _canonical(base)
    return name in _BY_ID or ('/' in name and _canonical(name.split('/', 1)[1]) in _BY_ID)


def parse_model_name(name):
    """Take a client model name apart: <base>[@<profile>][#effort | :effort] -> ModelName(base, profile, effort).

    The effort follows the profile (claude-opus-5-5@mine#high) and is cut as it always was: at the first
    '#', else the first ':', and only when what follows is a known level; any other tail stays in the name.
    Whitespace around the parts is ignored. A name without '@' comes out exactly as it did before profiles
    existed. A malformed profile part, or an effort written before it, is a 400 on param "model".

    '@' separates a profile only after a model of the catalog (an id, an alias, either with a provider prefix). In any
    other name it belongs to the model id, as it did before profiles existed: claude-sonnet-4-5@20250929 (a Vertex
    style id) is one unknown model, which strict_models refuses and strict_models: false passes on as it is.
    """
    original = (name or '').strip()
    shown = original[:100]
    text, effort = original, None
    for separator in ('#', ':'):
        if separator in text:
            head, tail = text.split(separator, 1)
            candidate = tail.strip().lower()
            if candidate in EFFORT_ORDER:
                effort, text = candidate, head.strip()
            break
    base, at, profile = text.partition(VIRTUAL_SEPARATOR)
    if not at:
        return ModelName(text, None, effort)
    base, profile = base.strip(), profile.strip()
    if base and not _names_a_catalog_model(base):
        return ModelName(text, None, effort)
    if VIRTUAL_SEPARATOR in profile:
        raise bad_request("model '%s' has more than one '@'; write <model>@<profile>[#effort]" % shown, param='model')
    for separator in ('#', ':'):
        if separator in profile:
            tail = profile.split(separator, 1)[1].strip()
            raise bad_request("model '%s': '%s' after the profile is not a reasoning effort (%s)"
                              % (shown, tail[:40], ', '.join(EFFORT_ORDER)), param='model')
    if not base:
        raise bad_request("model '%s' names no model before '@'" % shown, param='model')
    if not profile:
        raise bad_request("model '%s' names no profile after '@'" % shown, param='model')
    if not _profile_pattern().fullmatch(profile):
        raise bad_request("model '%s': '%s' is not a profile name; use letters, digits, '.', '_' and '-', starting "
                          "with a letter or digit, at most 64 characters" % (shown, profile[:40]), param='model')
    for separator in ('#', ':'):
        if separator in base:
            head, tail = base.split(separator, 1)
            if tail.strip().lower() in EFFORT_ORDER:
                raise bad_request("model '%s': the effort goes after the profile: %s@%s%s%s"
                                  % (shown, head.strip(), profile, separator, tail.strip()), param='model')
            break
    return ModelName(base, profile, effort)


def split_virtual(name):
    """(base, profile) of <model>@<profile>[#effort]; the profile is None for a plain name, the effort is dropped."""
    parsed = parse_model_name(name)
    return parsed.base, parsed.profile


def canonical_name(name):
    """The catalog id of a client model name, its @profile kept: 'claude-opus@mine#high' -> 'claude-opus-5-5@mine'.

    ApiError for an unknown model, as resolve(); the effort suffix is dropped.
    """
    parsed = parse_model_name(name)
    entry = resolve(parsed.base)[0]
    return entry['id'] + (VIRTUAL_SEPARATOR + parsed.profile if parsed.profile else '')


def nearest_effort(levels, requested):
    """Closest level the route accepts; ties resolve downwards."""
    wanted = _EFFORT_RANK.get(str(requested or "").lower(), _EFFORT_RANK["medium"])
    return min(levels, key=lambda level: (abs(_EFFORT_RANK.get(level, wanted) - wanted),
                                          _EFFORT_RANK.get(level, wanted)))


def _effort_controls_sent(data):
    """(client-facing name, value) for every effort or thinking control, where validate._effort reads it."""
    found = []

    def look(prefix, container):
        if not isinstance(container, dict):
            return
        for key in ('reasoning_effort', 'reasoningEffort', 'effort'):
            if container.get(key) is not None:
                found.append((prefix + key, container[key]))
        reasoning = container.get('reasoning')
        if isinstance(reasoning, dict) and reasoning.get('effort') is not None:
            found.append((prefix + 'reasoning.effort', reasoning['effort']))

    look('', data)
    for key in ('settings', 'options', 'body', 'extra_body'):
        look(key + '.', data.get(key))
    provider_options = data.get('providerOptions')
    look('providerOptions.openai.', provider_options.get('openai') if isinstance(provider_options, dict) else None)
    output = data.get('output_config')
    if isinstance(output, dict) and output.get('effort') is not None:
        found.append(('output_config.effort', output['effort']))
    thinking = data.get('thinking')
    if isinstance(thinking, dict):
        for key in ('effort', 'budget_tokens'):
            if thinking.get(key) is not None:
                found.append(('thinking.' + key, thinking[key]))
    messages = data.get('messages', data.get('input'))
    for message in messages if isinstance(messages, list) else []:
        inline = message.get('output_config') if isinstance(message, dict) else None
        if (isinstance(message, dict) and message.get('role') in ('system', 'developer')
                and isinstance(inline, dict) and inline.get('effort') is not None):
            found.append(('messages.output_config.effort', inline['effort']))
            break
    return found


def ignored_effort_controls(data, entry):
    """Names of the controls a route with reasoning_effort_control "accepted-ignored" drops.

    Retool takes these and changes nothing, so they are accepted (never a 400, which would break
    clients that send one level to every model), not forwarded, and reported by name only.
    """
    reasons = bool(entry.get('reasoning', True))
    # "none" asks for no reasoning: a route that never reasons already complies.
    names = [name for name, value in _effort_controls_sent(data)
             if reasons or str(value).lower() != 'none']
    thinking = data.get('thinking')
    kind = str(thinking.get('type') or '').lower() if isinstance(thinking, dict) else ''
    if kind and (kind != 'disabled') != reasons:
        names.append('thinking.type')  # an on/off switch this route cannot honour
    return names


def effective_effort(entry, effort=None, configured=None):
    """A global default is a preference; explicit client controls were validated by resolve."""
    if effort is not None or not configured or not entry.get("reasoning", True):
        return effort
    configured = configured.lower() if isinstance(configured, str) else configured
    if configured != "auto" and configured not in EFFORT_ORDER:
        raise bad_request("invalid configured reasoning effort", param="reasoning_effort")
    levels = entry.get("reasoning_effort_levels", EFFORTS_BY_PROVIDER.get(entry["provider"], ()))
    if entry.get("reasoning_effort_control") in ("provider-managed", "accepted-ignored") or not levels:
        return None
    if configured == "auto":
        preferred = entry.get("default_reasoning_effort")
        return preferred if preferred in levels else ("medium" if "medium" in levels else levels[0])
    return configured if configured in levels else nearest_effort(levels, configured)


def _setting_hint(effort, allowed):
    """Names the setting that would replace a known level this route lacks; nothing for any other value."""
    level = effort.lower() if isinstance(effort, str) else ''
    if level not in EFFORT_ORDER:
        return ''  # a level no provider defines is never guessed, so the setting could not help
    enable = 'Set allow_effort_downgrade to true in config.json or the admin settings to '
    if not allowed:
        return '. ' + enable + 'drop the control instead'
    nearest = nearest_effort(allowed, level)
    if level == 'none':
        return ('; this model cannot turn reasoning off (thinking disabled and effort none ask for that). '
                + enable + 'use its lowest level, %s, instead' % nearest)
    return '. ' + enable + 'use the nearest level, %s, instead' % nearest


def resolve_tolerant(model, effort=None, *, strict=True, default_model=None, allow_downgrade=False):
    """Resolve exactly; explicit compatibility permits known-level mapping.

    Compatibility mapping is opt-in. Clients copy one effort across models, so e.g. xhigh reaches
    Opus, which only understands low/medium/high/max. A value no provider
    defines at all ("extra", "ultra", typos) is still rejected with a 400:
    clamping it to medium would silently downgrade a client that asked for
    more thinking (verified live 2026-11-01: the route answers 422 to such
    values, so there is nothing to clamp towards). Models whose route ignores
    effort (reasoning_effort_control "accepted-ignored") take every known level
    and forward none.

    The name may select a prompt profile, <model>@<profile>[#effort]: the profile is not the catalog's
    business, so it is dropped here (validate keeps it in Parsed.virtual_profile, parse_model_name).

    A model that no account serves but that has a substitute (SUBSTITUTES) comes back as the substitute. Its effort
    is validated as the model the client asked for, so a level that model rejects is still a 400, and then moved to the
    nearest level the substitute takes, when it lacks one (gpt-6-luna and gpt-6.1-terra take the same levels).
    """
    entry, level = _resolve_requested(model, effort, strict=strict, default_model=default_model,
                                      allow_downgrade=allow_downgrade)
    served = substitute_for(entry['id'])
    if served:
        entry = _BY_ID[served]
        levels = entry.get('reasoning_effort_levels') or []
        if level is not None and level not in levels:
            level = nearest_effort(levels, level) if levels else None
    return entry, level


def _resolve_requested(model, effort, *, strict, default_model, allow_downgrade):
    parsed = parse_model_name(model or default_model)
    extracted_effort = effort if effort is not None else parsed.effort
    base_name = parsed.base
    try:
        return resolve(base_name, extracted_effort, strict=strict, default_model=default_model)
    except ApiError as error:
        if not allow_downgrade:
            raise
        if error.status != 400:
            raise
        if extracted_effort is None or str(extracted_effort).lower() not in EFFORT_ORDER:
            raise  # not a level any provider defines: surface the 400, never guess
        entry, _ = resolve(base_name, None, strict=strict, default_model=default_model)
        levels = entry.get("reasoning_effort_levels") or []
        if not levels or not entry.get("reasoning", True):
            print("[effort] %s -> None (reasoning not supported) for %s" % (extracted_effort, entry["id"]), flush=True)
            return entry, None
        clamped = nearest_effort(levels, extracted_effort)
        print("[effort] %s -> %s for %s" % (extracted_effort, clamped, entry["id"]), flush=True)
        return entry, clamped


def resolve(model, effort=None, *, strict=True, default_model=None):
    parsed = parse_model_name(model or default_model)
    if effort is None:
        effort = parsed.effort
    name = _canonical(parsed.base)
    if not name:
        raise bad_request("'model' is required", param='model')
    entry = _BY_ID.get(name)
    if entry is None and '/' in name:
        stripped = _canonical(name.split('/', 1)[1])
        if stripped in _BY_ID:
            name = stripped
            entry = _BY_ID.get(name)
    if entry is None:
        if strict:
            raise not_found(f"unknown model '{name}'; see GET /v1/models")
        entry = {'id': name, 'provider': 'anthropic', 'reasoning': True, 'label': name,
                 'context': 200000, 'max_output': 128000}
    if effort is not None:
        if not entry.get('reasoning', True) and isinstance(effort, str) and effort.lower() == 'none':
            return entry, None  # An explicit disabled control is harmless; never forward it.
        if entry.get('reasoning_effort_control') == 'accepted-ignored':
            # The route takes any level some provider defines and changes nothing: accept it, forward nothing.
            if isinstance(effort, str) and effort.lower() in EFFORT_ORDER:
                return entry, None
            raise bad_request(
                f"reasoning effort for {name} must be a known level ({', '.join(EFFORT_ORDER)}); the route ignores it",
                param='reasoning_effort')
        allowed = entry.get('reasoning_effort_levels', EFFORTS_BY_PROVIDER.get(entry['provider'], ()))
        if not isinstance(effort, str) or effort.lower() not in allowed:
            raise bad_request(
                f"reasoning effort for {name} must be one of {', '.join(allowed) or 'no supported levels'}"
                + _setting_hint(effort, allowed),
                param='reasoning_effort')
        effort = effort.lower()
        if not entry.get('reasoning', True):
            effort = None
    return entry, effort


def provider_resource(cfg, entry):
    key = PROVIDER_RESOURCE_KEY.get(entry['provider'])
    if not key:
        raise service_unavailable(f"provider '{entry['provider']}' is not routed")
    value = cfg.get(key)
    if value == "__managed__":
        # Per-account escape hatch: the tenant's own managed AI serves the model
        # without an explicit provider resource (verified live: opus streams with
        # the field omitted). build_body omits providerResourceName when None.
        return None
    if not value:
        raise service_unavailable(
            f"model '{entry['id']}' needs config key '{key}' (provider resource is not configured)")
    return value


def effort_from_budget(budget):
    if isinstance(budget, str) and budget.strip().isdigit():
        budget = int(budget)
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise bad_request('thinking.budget_tokens must be an integer', param='thinking.budget_tokens')
    if budget < 1024:
        raise bad_request('thinking.budget_tokens must be >= 1024', param='thinking.budget_tokens')
    for limit, effort in _BUDGET_TO_EFFORT:
        if budget <= limit:
            return effort
    return 'max'


def budget_from_effort(effort):
    return {'low': 1024, 'medium': 8192, 'high': 24576, 'max': 32768}.get(effort, 8192)
