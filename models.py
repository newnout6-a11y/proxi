"""Model catalog, provider routing and reasoning-effort normalisation."""
from apierr import bad_request, not_found

EFFORTS = ("minimal", "low", "medium", "high", "max")

# Aliases let clients use the short names IDEs tend to ship with.
ALIASES = {
    "claude-opus-5.5": "claude-opus-5-5",
    "claude-opus": "claude-opus-5-5",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-haiku": "claude-haiku-4-5-20251001",
    "gpt-5.2-chat": "gpt-5.2",
}

CATALOG = [
    # id, provider, context window, capability flags
    {"id": "claude-opus-5-5", "provider": "anthropic", "context": 200000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-sonnet-4-6", "provider": "anthropic", "context": 200000, "reasoning": True, "tools": True, "vision": True},
    {"id": "claude-haiku-4-5-20251001", "provider": "anthropic", "context": 200000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.2", "provider": "openai", "context": 400000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5.1", "provider": "openai", "context": 400000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-5", "provider": "openai", "context": 400000, "reasoning": True, "tools": True, "vision": True},
    {"id": "gpt-4.1", "provider": "openai", "context": 1000000, "reasoning": False, "tools": True, "vision": True},
]

_BY_ID = {m["id"]: m for m in CATALOG}

# Anthropic thinking budgets, used when a client only sends budget_tokens.
_BUDGET_TO_EFFORT = ((1024, "minimal"), (4096, "low"), (16384, "medium"), (32768, "high"))


def catalog() -> list:
    return CATALOG


def _canonical(name: str) -> str:
    name = (name or "").strip()
    return ALIASES.get(name, name)


def resolve(model, effort=None, *, strict=True, default_model=None):
    """Return (model, provider_resource_name, effort) or raise."""
    name = _canonical(model or default_model or "")
    if not name:
        raise bad_request("'model' is required", param="model")
    entry = _BY_ID.get(name)
    if entry is None:
        if strict:
            raise not_found(f"unknown model '{name}'; see GET /v1/models")
        entry = {"id": name, "provider": "anthropic", "reasoning": True}
    if not isinstance(effort, str) or effort.lower() not in EFFORTS:
        if effort is None:
            effort = None
        else:
            raise bad_request(f"reasoning effort must be one of {', '.join(EFFORTS)}", param="reasoning_effort")
    if effort and not entry.get("reasoning", True):
        effort = None
    return entry, effort


def effort_from_budget(budget) -> str:
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        raise bad_request("thinking.budget_tokens must be an integer", param="thinking.budget_tokens")
    if budget < 1024:
        raise bad_request("thinking.budget_tokens must be >= 1024", param="thinking.budget_tokens")
    for limit, effort in _BUDGET_TO_EFFORT:
        if budget <= limit:
            return effort
    return "max"


def budget_from_effort(effort: str) -> int:
    return {"minimal": 1024, "low": 2048, "medium": 8192, "high": 24576, "max": 32768}.get(effort, 8192)
