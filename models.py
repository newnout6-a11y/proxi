"""Audited model metadata; see docs/model-catalog-audit.md for provenance.

Effort capabilities describe the Retool route rather than the provider-wide union.
"""
from apierr import ApiError, bad_request, not_found, service_unavailable

# Accepted reasoning effort values per provider (from the same bundle).
EFFORTS_BY_PROVIDER = {
    "openai": ("minimal", "low", "medium", "high", "xhigh", "max", "none"),
    "anthropic": ("low", "medium", "high", "max"),
    "google": ("low", "medium", "high"),
}

# Per-model route capabilities from the Retool bundle. Provider-wide unions
# advertise levels that individual models reject (for example GPT-5.5 xhigh).
_BASE_EFFORTS = ("low", "medium", "high")
_EFFORTS_BY_MODEL = {
    "gpt-5": _BASE_EFFORTS + ("minimal",),
    "gpt-5.1": ("none",) + _BASE_EFFORTS,
    "gpt-5.2": ("none",) + _BASE_EFFORTS,
    "gpt-6-astra": _BASE_EFFORTS + ("xhigh", "max"),
    "gpt-6.1-sol": _BASE_EFFORTS + ("xhigh", "max"),
    "gpt-6-sol": ("none",) + _BASE_EFFORTS + ("xhigh", "max"),
    "gpt-6-luna": ("none",) + _BASE_EFFORTS + ("xhigh", "max"),
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
           "claude-sonnet-4.6": "claude-sonnet-4-6", "gpt-6": "gpt-6-sol"}

CATALOG = [
    {"id": "gpt-6.1-sol", "label": "GPT 6.1-sol", "provider": "openai", "context": 1050000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
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
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "provider": "anthropic", "context": 200000, "max_output": 64000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro (Preview)", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash (Preview)", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash", "provider": "google", "context": 1000000, "max_output": 65000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3.1-flash-lite-preview", "label": "Gemini 3.1 Flash-Lite (Preview)", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "zai-org/GLM-5.3", "label": "Retool Power", "provider": "retool-fast", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": False},
    {"id": "zai-org/GLM-5.3-Flash", "label": "Retool Mini", "provider": "retool-fast", "context": 984576, "max_output": 32768, "reasoning": False, "tools": True, "vision": True},
]

_BY_ID = {m['id']: m for m in CATALOG}

# Capabilities are dated independently of local resource availability. Values
# marked provisional must not be presented as a verified upstream contract.
_AUDIT_DATE = '2026-10-29'
_RETOOL_SOURCE = 'https://retool-edge.com/assets_vjs/main-CnZ2mbHm.js'
_CLAUDE_DOCS = 'https://platform.claude.com/docs/en/models/'
_LEGACY_CLAUDE = {'claude-opus-4-20250514', 'claude-opus-4-1-20250805',
                  'claude-sonnet-4-20250514'}
_LOW_DEFAULTS = {'gpt-5-mini', 'gpt-5-nano', 'claude-haiku-4-5-20251001',
                 'gemini-3-flash-preview', 'gemini-3.5-flash',
                 'gemini-3.1-flash-lite-preview', 'gemini-2.5-flash'}
_HIGH_DEFAULTS = {'gpt-6-astra', 'claude-fable-5'}

for _model in CATALOG:
    _default_efforts = (_BASE_EFFORTS if _model['provider'] == 'openai'
                        else EFFORTS_BY_PROVIDER.get(_model['provider'], ()))
    _model['reasoning_effort_levels'] = list(_EFFORTS_BY_MODEL.get(_model['id'], _default_efforts))
    if not _model.get('reasoning'):
        _model['reasoning_effort_levels'] = []
    _levels = _model['reasoning_effort_levels']
    _model['default_reasoning_effort'] = (
        ('low' if _model['id'] in _LOW_DEFAULTS else
         'high' if _model['id'] in _HIGH_DEFAULTS else 'medium') if _levels else None)
    _model['reasoning_effort_control'] = 'selectable' if _levels else 'provider-managed'
    _model['metadata_checked_at'] = _AUDIT_DATE
    _model['metadata_source'] = [_RETOOL_SOURCE]
    _model['metadata_status'] = 'verified'
    if _model['provider'] == 'openai':
        # Mini/nano inherit the family levels; their registry entries omit an
        # explicit list. Do not claim those controls were individually tested.
        if _model['id'] in {'gpt-5-mini', 'gpt-5-nano'}:
            _model['metadata_status'] = 'partial'
            _model['metadata_notes'] = 'Effort levels inherit the Retool GPT family; not individually verified.'
    elif _model['provider'] == 'anthropic':
        if _model['id'] in _LEGACY_CLAUDE:
            _model['metadata_status'] = 'provisional'
            _model['metadata_source'].append('https://platform.claude.com/docs/en/build-with-claude/claude-on-amazon-bedrock-legacy')
            _model['metadata_notes'] = 'Legacy 200K context; conservative 32K Opus/64K Sonnet output pending route verification.'
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

_BUDGET_TO_EFFORT = ((1024, 'low'), (8192, 'medium'), (24576, 'high'))

def catalog():
    return CATALOG


def codex_catalog(entries=None):
    """Codex-native ModelsResponse with direct client tools for this gateway.

    The OpenAI-compatible list is insufficient for Codex to discover a custom
    model's tool mode. Keep this separate so SDK model-list clients are unchanged.
    """
    instructions = (
        "You are a coding assistant working in the user's project. Follow the "
        "user's instructions and project guidance. Use the tools provided by "
        "the client to inspect files, make requested changes, and run relevant "
        "checks. Base reports on observed tool results and state any limitations."
    )
    result = []
    for index, entry in enumerate(CATALOG if entries is None else entries):
        efforts = entry.get('reasoning_effort_levels', [])
        default = entry.get('default_reasoning_effort')
        if default not in efforts:
            default = 'medium' if 'medium' in efforts else (efforts[0] if efforts else None)
        result.append({
            'slug': entry['id'], 'display_name': entry.get('label', entry['id']),
            'description': 'Retool relay: ' + entry.get('label', entry['id']),
            'default_reasoning_level': default,
            'supported_reasoning_levels': [{'effort': effort, 'description': effort.title() + ' reasoning effort'}
                                           for effort in efforts],
            'shell_type': 'unified_exec', 'visibility': 'list', 'supported_in_api': True,
            'priority': index, 'additional_speed_tiers': [], 'service_tiers': [],
            'default_service_tier': None, 'availability_nux': None, 'upgrade': None,
            'base_instructions': instructions,
            'model_messages': {'instructions_template': instructions, 'persistent_instructions': ''},
            'include_skills_usage_instructions': False, 'include_plugin_usage_instructions': False,
            'include_apps_usage_instructions': False,
            'supports_reasoning_summary_parameter': bool(entry.get('reasoning')),
            'default_reasoning_summary': 'detailed' if entry.get('reasoning') else 'none',
            'support_verbosity': False, 'default_verbosity': None,
            'apply_patch_tool_type': 'freeform',
            'truncation_policy': {'mode': 'tokens', 'limit': 10000},
            'context_window': entry.get('context', 128000),
            'effective_context_window_percent': 95,
            'experimental_supported_tools': [], 'input_modalities': ['text'],
            'supports_search_tool': False, 'supports_experimental_context': False,
            'use_responses_lite': False, 'supports_reasoning_effort_updates': False,
            'node_repl_disabled': True, 'tool_mode': 'direct',
        })
    return {'models': result}


def _canonical(name):
    name = (name or '').strip()
    return ALIASES.get(name, name)


EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_EFFORT_RANK = {name: index for index, name in enumerate(EFFORT_ORDER)}


def nearest_effort(levels, requested):
    """Closest level the route accepts; ties resolve downwards."""
    wanted = _EFFORT_RANK.get(str(requested or "").lower(), _EFFORT_RANK["medium"])
    return min(levels, key=lambda level: (abs(_EFFORT_RANK.get(level, wanted) - wanted),
                                          _EFFORT_RANK.get(level, wanted)))


def resolve_tolerant(model, effort=None, *, strict=True, default_model=None):
    """Like resolve(), but an unsupported effort is clamped instead of rejected.

    Clients copy one effort across models, so e.g. xhigh legitimately reaches
    Haiku, which only understands low/medium/high/max.
    """
    try:
        return resolve(model, effort, strict=strict, default_model=default_model)
    except ApiError as error:
        entry, _ = resolve(model, None, strict=strict, default_model=default_model)
        levels = entry.get("reasoning_effort_levels") or []
        if error.status != 400 or not levels or not entry.get("reasoning", True):
            raise
        clamped = nearest_effort(levels, effort)
        print("[effort] %s -> %s for %s" % (effort, clamped, entry["id"]), flush=True)
        return entry, clamped


def resolve(model, effort=None, *, strict=True, default_model=None):
    name = _canonical(model or default_model or '')
    if not name:
        raise bad_request("'model' is required", param='model')
    entry = _BY_ID.get(name)
    if entry is None:
        if strict:
            print("[unknown-model]", repr(model), '->', repr(name), flush=True)
            raise not_found(f"unknown model '{name}'; see GET /v1/models")
        entry = {'id': name, 'provider': 'anthropic', 'reasoning': True, 'label': name, 'context': 200000}
    if effort is not None:
        allowed = entry.get('reasoning_effort_levels', EFFORTS_BY_PROVIDER.get(entry['provider'], ()))
        if not isinstance(effort, str) or effort.lower() not in allowed:
            raise bad_request(
                f"reasoning effort for {name} must be one of {', '.join(allowed) or 'no supported levels'}",
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
