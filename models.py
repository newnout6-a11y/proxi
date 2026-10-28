"""Model catalog mirrored from the Retool copilot bundle (probes/gen_models.py).

Regenerate with: python probes/gen_models.py && python probes/gen_models_py.py
"""
from apierr import bad_request, not_found, service_unavailable

# Accepted reasoning effort values per provider (from the same bundle).
EFFORTS_BY_PROVIDER = {
    "openai": ("minimal", "low", "medium", "high", "xhigh", "max", "none"),
    "anthropic": ("low", "medium", "high", "max"),
    "google": ("low", "medium", "high"),
}

# Which config key holds the providerResourceName for each provider.
PROVIDER_RESOURCE_KEY = {
    "openai": "openai_provider_resource_name",
    "anthropic": "provider_resource_name",
    "google": "google_provider_resource_name",
    "retool-fast": "retool_fast_provider_resource_name",
}

ALIASES = {"claude-opus": "claude-opus-5-5", "claude-sonnet": "claude-sonnet-5",
           "claude-haiku": "claude-haiku-4-5-20251001", "gpt-5.6": "gpt-5.6-sol",
           "claude-opus-5.5": "claude-opus-5-5", "claude-opus-4.8": "claude-opus-4-8",
           "claude-sonnet-4.6": "claude-sonnet-4-6", "gpt-6": "gpt-6-sol"}

CATALOG = [
    {"id": "gpt-6.1-sol", "label": "GPT 6.1-sol", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-6-luna", "label": "GPT 6-luna", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-6-sol", "label": "GPT 6-sol", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-6-astra", "label": "GPT 6-astra", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5", "label": "GPT 5", "provider": "openai", "context": 272000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5-mini", "label": "GPT 5 Mini", "provider": "openai", "context": 272000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5-nano", "label": "GPT 5 Nano", "provider": "openai", "context": 272000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.1", "label": "GPT 5.1", "provider": "openai", "context": 272000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.2", "label": "GPT 5.2", "provider": "openai", "context": 272000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.3-codex", "label": "GPT 5.3 Codex", "provider": "openai", "context": 272000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.4", "label": "GPT 5.4", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.5", "label": "GPT 5.5", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.6-sol", "label": "GPT 5.6 Sol", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.6-terra", "label": "GPT 5.6 Terra", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.6-luna", "label": "GPT 5.6 Luna", "provider": "openai", "context": 400000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-5", "label": "Claude Opus 5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-7", "label": "Claude Opus 4.7", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-20250514", "label": "Claude Opus 4", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-1-20250805", "label": "Claude Opus 4.1", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-5-20251101", "label": "Claude Opus 4.5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-opus-4-6", "label": "Claude Opus 4.6", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-fable-5", "label": "Claude Fable 5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-sonnet-4-5-20250929", "label": "Claude Sonnet 4.5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "provider": "anthropic", "context": 1000000, "max_output": 128000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro (Preview)", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash (Preview)", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-3.1-flash-lite-preview", "label": "Gemini 3.1 Flash-Lite (Preview)", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "google", "context": 983040, "max_output": 65536, "reasoning": True, "tools": True, "vision": True},
    {"id": "zai-org/GLM-5.3", "label": "Retool Power", "provider": "retool-fast", "context": 984576, "max_output": 32768, "reasoning": False, "tools": True, "vision": True},
    {"id": "zai-org/GLM-5.3-Flash", "label": "Retool Mini", "provider": "retool-fast", "context": 984576, "max_output": 32768, "reasoning": False, "tools": True, "vision": True},
]

_BY_ID = {m['id']: m for m in CATALOG}

_BUDGET_TO_EFFORT = ((1024, 'low'), (8192, 'medium'), (24576, 'high'))

def catalog():
    return CATALOG


def _canonical(name):
    name = (name or '').strip()
    return ALIASES.get(name, name)


def resolve(model, effort=None, *, strict=True, default_model=None):
    name = _canonical(model or default_model or '')
    if not name:
        raise bad_request("'model' is required", param='model')
    entry = _BY_ID.get(name)
    if entry is None:
        if strict:
            raise not_found(f"unknown model '{name}'; see GET /v1/models")
        entry = {'id': name, 'provider': 'anthropic', 'reasoning': True, 'label': name, 'context': 200000}
    if effort is not None:
        allowed = EFFORTS_BY_PROVIDER.get(entry['provider'], ())
        if not isinstance(effort, str) or effort.lower() not in allowed:
            raise bad_request(
                f"reasoning effort for {entry['provider']} must be one of {', '.join(allowed) or 'none'}",
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
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        raise bad_request('thinking.budget_tokens must be an integer', param='thinking.budget_tokens')
    if budget < 1024:
        raise bad_request('thinking.budget_tokens must be >= 1024', param='thinking.budget_tokens')
    for limit, effort in _BUDGET_TO_EFFORT:
        if budget <= limit:
            return effort
    return 'max'


def budget_from_effort(effort):
    return {'low': 1024, 'medium': 8192, 'high': 24576, 'max': 32768}.get(effort, 8192)
