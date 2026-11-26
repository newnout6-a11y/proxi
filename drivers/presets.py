"""Built-in provider presets, token-prefix detection, and OpenCode-style provider schema validation.

Supports both single-credential shorthand (OpenCode style):
    {"id": "openrouter", "preset": "openrouter", "api_key_env": "OPENROUTER_API_KEY"}
    {"id": "ollama", "preset": "ollama"}
and multi-credential pools (CLIProxyAPI / uni-api style):
    {"id": "anthropic", "preset": "anthropic", "routing_strategy": "fill-first",
     "credentials": [{"api_key": "sk-ant-..."}, {"api_key_env": "ANTHROPIC_KEY_2", "proxy_url": "socks5://..."}]}
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import urllib.parse

from drivers import http_pool

DRIVERS = ("openai-chat", "openai-responses", "anthropic-messages", "retool", "minara")
UNIVERSAL_DRIVERS = ("openai-chat", "openai-responses", "anthropic-messages")
AUTH_MODES = ("bearer", "x-api-key", "custom-header", "none")
ROUTING_STRATEGIES = ("fill-first", "weighted", "least-inflight")
CACHE_POLICIES = ("auto", "5m", "1h", "off")

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
HEADER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")

# Built-in 1-click provider presets.
PRESETS: dict[str, dict] = {
    "openai": {
        "name": "OpenAI",
        "driver": "openai-chat",
        "base_url": "https://api.openai.com/v1",
        "auth_mode": "bearer",
        "cache_policy": "auto",
        "balance_endpoint": None,
    },
    "openai-responses": {
        "name": "OpenAI (Responses API)",
        "driver": "openai-responses",
        "base_url": "https://api.openai.com/v1",
        "auth_mode": "bearer",
        "cache_policy": "auto",
        "balance_endpoint": None,
    },
    "anthropic": {
        "name": "Anthropic",
        "driver": "anthropic-messages",
        "base_url": "https://api.anthropic.com/v1",
        "auth_mode": "x-api-key",
        "cache_policy": "auto",
        "balance_endpoint": None,
    },
    "openrouter": {
        "name": "OpenRouter",
        "driver": "openai-chat",
        "base_url": "https://openrouter.ai/api/v1",
        "auth_mode": "bearer",
        "cache_policy": "auto",
        "balance_endpoint": "https://openrouter.ai/api/v1/key",
    },
    "deepseek": {
        "name": "DeepSeek",
        "driver": "openai-chat",
        "base_url": "https://api.deepseek.com",
        "auth_mode": "bearer",
        "cache_policy": "auto",
        "balance_endpoint": "https://api.deepseek.com/user/balance",
    },
    "gemini": {
        "name": "Google Gemini",
        "driver": "openai-chat",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "auth_mode": "bearer",
        "cache_policy": "auto",
        "balance_endpoint": None,
    },
    "groq": {
        "name": "Groq",
        "driver": "openai-chat",
        "base_url": "https://api.groq.com/openai/v1",
        "auth_mode": "bearer",
        "cache_policy": "off",
        "balance_endpoint": None,
    },
    "xai": {
        "name": "xAI (Grok)",
        "driver": "openai-chat",
        "base_url": "https://api.x.ai/v1",
        "auth_mode": "bearer",
        "cache_policy": "auto",
        "balance_endpoint": None,
    },
    "mistral": {
        "name": "Mistral AI",
        "driver": "openai-chat",
        "base_url": "https://api.mistral.ai/v1",
        "auth_mode": "bearer",
        "cache_policy": "off",
        "balance_endpoint": None,
    },
    "together": {
        "name": "Together AI",
        "driver": "openai-chat",
        "base_url": "https://api.together.xyz/v1",
        "auth_mode": "bearer",
        "cache_policy": "off",
        "balance_endpoint": None,
    },
    "fireworks": {
        "name": "Fireworks AI",
        "driver": "openai-chat",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "auth_mode": "bearer",
        "cache_policy": "off",
        "balance_endpoint": None,
    },
    "moonshot": {
        "name": "Moonshot (Kimi)",
        "driver": "openai-chat",
        "base_url": "https://api.moonshot.ai/v1",
        "auth_mode": "bearer",
        "cache_policy": "auto",
        "balance_endpoint": "https://api.moonshot.ai/v1/users/me/balance",
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "driver": "openai-chat",
        "base_url": "https://api.siliconflow.com/v1",
        "auth_mode": "bearer",
        "cache_policy": "off",
        "balance_endpoint": "https://api.siliconflow.com/v1/user/info",
    },
    "ollama": {
        "name": "Ollama (Local)",
        "driver": "openai-chat",
        "base_url": "http://127.0.0.1:11434/v1",
        "auth_mode": "none",
        "cache_policy": "auto",
        "balance_endpoint": None,
    },
    "vllm": {
        "name": "vLLM / SGLang (Local)",
        "driver": "openai-chat",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_mode": "none",
        "cache_policy": "auto",
        "balance_endpoint": None,
    },
    "lmstudio": {
        "name": "LM Studio (Local)",
        "driver": "openai-chat",
        "base_url": "http://127.0.0.1:1234/v1",
        "auth_mode": "none",
        "cache_policy": "off",
        "balance_endpoint": None,
    },
}

# Unambiguous API key prefixes ordered from most specific to least specific.
_KEY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("sk-or-v1-", "openrouter"),
    ("sk-ant-", "anthropic"),
    ("sk-proj-", "openai"),
    ("sk-svcacct-", "openai"),
    ("gsk_", "groq"),
    ("xai-", "xai"),
    ("AIza", "gemini"),
)

_ENDPOINT_SUFFIXES = ("/chat/completions", "/responses", "/messages", "/models")
_RESERVED_PROVIDER_IDS = frozenset({"retool", "minara", "admin", "v1"})


def clean_api_key(raw: str | None) -> str:
    """Strip surrounding whitespace and a single leading 'Bearer ' prefix; validate printable ASCII."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("api_key must be a string")
    val = raw.strip()
    if val.lower().startswith("bearer "):
        val = val[7:].strip()
    if len(val) > 8192:
        raise ValueError("api_key exceeds maximum length")
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in val):
        raise ValueError("api_key must contain printable ASCII characters without spaces")
    return val


def normalize_base_url(base_url: str, *, allow_insecure_http: bool = False) -> str:
    """Normalize an API root URL, stripping common endpoint suffixes like /chat/completions or /messages."""
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url is required")
    raw = base_url.strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(raw)
        _ = parts.port
    except ValueError:
        raise ValueError("base_url or port is malformed") from None
    if not parts.hostname or parts.scheme not in ("https", "http"):
        raise ValueError("base_url must start with https:// or http://")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("base_url must not contain credentials, query parameters, or fragments")
    if http_pool.is_blocked_metadata_host(parts.hostname):
        raise ValueError("base_url points to a restricted metadata or link-local address")
    if parts.scheme == "http" and not allow_insecure_http and not http_pool.is_local_or_private_host(parts.hostname):
        raise ValueError("Plain http:// is only allowed for localhost/LAN addresses (or set allow_insecure_http=true)")

    path = parts.path.rstrip("/")
    for suffix in _ENDPOINT_SUFFIXES:
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    # Official hosts where bare root implies /v1
    host_lower = parts.hostname.lower()
    if not path and host_lower in ("api.openai.com", "api.anthropic.com", "api.groq.com", "api.x.ai", "api.mistral.ai"):
        path = "/v1" if host_lower != "api.groq.com" else "/openai/v1"
    elif not path and host_lower == "openrouter.ai":
        path = "/api/v1"
    rebuilt = urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))
    return rebuilt.rstrip("/")


def detect_preset_from_url(base_url: str | None) -> str | None:
    """Best-effort preset match by normalized base_url host or well-known local port."""
    if not base_url or not isinstance(base_url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(base_url.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if http_pool.is_local_or_private_host(host):
        if port == 11434:
            return "ollama"
        if port == 1234:
            return "lmstudio"
        if port == 8000:
            return "vllm"
    host_map = {
        "api.openai.com": "openai",
        "api.anthropic.com": "anthropic",
        "openrouter.ai": "openrouter",
        "api.deepseek.com": "deepseek",
        "generativelanguage.googleapis.com": "gemini",
        "api.groq.com": "groq",
        "api.x.ai": "xai",
        "api.mistral.ai": "mistral",
        "api.together.xyz": "together",
        "api.fireworks.ai": "fireworks",
        "api.moonshot.ai": "moonshot",
        "api.siliconflow.com": "siliconflow",
    }
    return host_map.get(host)


def identify_connection(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    preset: str | None = None,
    driver: str | None = None,
    auth_mode: str | None = None,
    allow_insecure_http: bool = False,
) -> dict:
    """Resolve preset, base_url, driver, and auth_mode from any combination of key, preset, or URL."""
    cleaned_key = clean_api_key(api_key) if api_key else ""
    chosen_preset = preset.strip().lower() if isinstance(preset, str) and preset.strip() else None
    if chosen_preset == "custom":
        chosen_preset = None
    if chosen_preset and chosen_preset not in PRESETS:
        raise ValueError(f"Unknown preset '{chosen_preset}'")

    if not chosen_preset and cleaned_key:
        for prefix, matched in _KEY_PREFIXES:
            if cleaned_key.startswith(prefix) and len(cleaned_key) >= len(prefix) + 8:
                chosen_preset = matched
                break

    if not chosen_preset and base_url:
        chosen_preset = detect_preset_from_url(base_url)

    spec = PRESETS.get(chosen_preset or "", {})
    raw_base = (base_url or "").strip() or spec.get("base_url") or ""
    if not raw_base:
        raise ValueError("base_url is required when the provider cannot be inferred from the API key or preset")

    norm_base = normalize_base_url(raw_base, allow_insecure_http=allow_insecure_http)

    resolved_driver = (driver or "").strip().lower() or ""
    if resolved_driver in ("", "auto"):
        if isinstance(base_url, str) and base_url.strip().rstrip("/").lower().endswith("/responses"):
            resolved_driver = "openai-responses"
        elif isinstance(base_url, str) and base_url.strip().rstrip("/").lower().endswith("/messages"):
            resolved_driver = "anthropic-messages"
        else:
            resolved_driver = spec.get("driver") or "openai-chat"
    if resolved_driver not in UNIVERSAL_DRIVERS:
        raise ValueError(f"driver must be one of {', '.join(UNIVERSAL_DRIVERS)}")

    resolved_auth = (auth_mode or "").strip().lower() or ""
    if resolved_auth in ("", "auto"):
        if spec.get("auth_mode"):
            resolved_auth = spec["auth_mode"]
        elif not cleaned_key and http_pool.is_local_or_private_host(urllib.parse.urlsplit(norm_base).hostname):
            resolved_auth = "none"
        else:
            resolved_auth = "x-api-key" if resolved_driver == "anthropic-messages" else "bearer"
    if resolved_auth not in AUTH_MODES:
        raise ValueError(f"auth_mode must be one of {', '.join(AUTH_MODES)}")

    default_id = chosen_preset or re.sub(r"[^a-z0-9_.-]+", "-", (urllib.parse.urlsplit(norm_base).hostname or "custom").lower()).strip("-") or "custom"
    return {
        "preset": chosen_preset or "custom",
        "suggested_id": default_id[:48],
        "name": spec.get("name") or urllib.parse.urlsplit(norm_base).hostname or "Custom Provider",
        "driver": resolved_driver,
        "base_url": norm_base,
        "auth_mode": resolved_auth,
        "cache_policy": spec.get("cache_policy", "auto"),
        "balance_endpoint": spec.get("balance_endpoint"),
    }


def _validate_headers(headers, where: str) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise ValueError(f"{where}: headers must be an object")
    cleaned: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not HEADER_NAME_PATTERN.fullmatch(key):
            raise ValueError(f"{where}: invalid HTTP header name '{key}'")
        if not isinstance(value, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError(f"{where}: header '{key}' value must be a single-line string")
        cleaned[key] = value
    return cleaned


def _validate_body_overrides(overrides, where: str) -> dict:
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ValueError(f"{where}: body_overrides must be an object")
    remove = overrides.get("__remove__")
    if remove is not None and (not isinstance(remove, list) or not all(isinstance(k, str) and k for k in remove)):
        raise ValueError(f"{where}: body_overrides.__remove__ must be a list of field names")
    return copy.deepcopy(overrides)


def _derived_credential_id(provider_id: str, index: int) -> str:
    digest = hashlib.sha256(f"provider:{provider_id}:{index}".encode("utf-8")).hexdigest()[:16]
    return f"{provider_id}-{digest}"


def expand_provider_to_accounts(provider: dict, where: str = "provider") -> tuple[dict, list[dict], list[str]]:
    """Validate one provider record (on a copy) and expand its credentials into normalized backend records.

    Returns `(normalized_provider_meta, backend_records, secret_values)`.
    Disk-safe: callers pass a copy so resolved env secrets never overwrite `api_key_env` on disk.
    """
    if not isinstance(provider, dict):
        raise ValueError(f"{where} must be an object")

    raw_id = str(provider.get("id") or "").strip().lower()
    allow_insecure = provider.get("allow_insecure_http") is True
    if "enabled" in provider and type(provider["enabled"]) is not bool:
        raise ValueError(f"{where}: 'enabled' must be a boolean")
    provider_enabled = provider.get("enabled", True)

    # Support OpenCode-style nested `options: { baseURL, apiKey, headers }` alongside flat fields.
    options = provider.get("options") if isinstance(provider.get("options"), dict) else {}
    raw_base = provider.get("base_url") or provider.get("base") or options.get("baseURL") or options.get("baseUrl")
    top_key = provider.get("api_key") or provider.get("bearer_token") or options.get("apiKey")
    top_env = provider.get("api_key_env") or provider.get("bearer_token_env")
    if top_env is not None and (not isinstance(top_env, str) or not ENV_PATTERN.fullmatch(top_env)):
        raise ValueError(f"{where}: api_key_env must be a valid environment variable name")
    resolved_top_key = (os.environ.get(top_env, "") if top_env else "") or (top_key or "")

    conn = identify_connection(
        api_key=resolved_top_key if isinstance(resolved_top_key, str) else None,
        base_url=raw_base,
        preset=provider.get("preset"),
        driver=provider.get("driver") or provider.get("api_protocol"),
        auth_mode=provider.get("auth_mode"),
        allow_insecure_http=allow_insecure,
    )

    provider_id = raw_id or conn["suggested_id"]
    if not ID_PATTERN.fullmatch(provider_id) or provider_id in _RESERVED_PROVIDER_IDS:
        raise ValueError(f"{where}: invalid or reserved provider id '{provider_id}'")

    auth_header_name = provider.get("auth_header_name")
    if conn["auth_mode"] == "custom-header":
        if not isinstance(auth_header_name, str) or not HEADER_NAME_PATTERN.fullmatch(auth_header_name):
            raise ValueError(f"{where}: auth_header_name is required when auth_mode is 'custom-header'")
    else:
        auth_header_name = None

    routing_strategy = str(provider.get("routing_strategy") or "fill-first").strip().lower()
    if routing_strategy not in ROUTING_STRATEGIES:
        raise ValueError(f"{where}: routing_strategy must be one of {', '.join(ROUTING_STRATEGIES)}")

    cache_policy = str(provider.get("cache_policy") or conn["cache_policy"]).strip().lower()
    if cache_policy not in CACHE_POLICIES:
        raise ValueError(f"{where}: cache_policy must be one of {', '.join(CACHE_POLICIES)}")

    headers = {
        **_validate_headers(options.get("headers"), f"{where}.options"),
        **_validate_headers(provider.get("headers"), where),
    }
    body_overrides = _validate_body_overrides(provider.get("body_overrides"), where)
    provider_proxy = http_pool.validate_proxy_url(provider.get("proxy_url"))

    models_spec = provider.get("models")
    if models_spec is not None and not isinstance(models_spec, (list, dict)):
        raise ValueError(f"{where}: models must be a list or object")
    whitelist = provider.get("model_whitelist")
    if whitelist is not None and (not isinstance(whitelist, list) or not all(isinstance(x, str) for x in whitelist)):
        raise ValueError(f"{where}: model_whitelist must be a list of strings")
    blacklist = provider.get("model_blacklist")
    if blacklist is not None and (not isinstance(blacklist, list) or not all(isinstance(x, str) for x in blacklist)):
        raise ValueError(f"{where}: model_blacklist must be a list of strings")

    raw_creds = provider.get("credentials")
    if raw_creds is None:
        raw_creds = [
            {
                k: provider[k]
                for k in ("account_id", "label", "priority", "weight", "enabled", "proxy_url")
                if k in provider
            }
            | ({"api_key": top_key} if top_key else {})
            | ({"api_key_env": top_env} if top_env else {})
        ]
    elif not isinstance(raw_creds, list) or not raw_creds:
        raise ValueError(f"{where}: credentials must be a non-empty list when provided")

    meta = {
        "id": provider_id,
        "name": str(provider.get("name") or conn["name"]).strip()[:80],
        "preset": conn["preset"],
        "driver": conn["driver"],
        "base_url": conn["base_url"],
        "auth_mode": conn["auth_mode"],
        "auth_header_name": auth_header_name,
        "routing_strategy": routing_strategy,
        "cache_policy": cache_policy,
        "enabled": provider_enabled,
        "allow_insecure_http": allow_insecure,
        "headers": headers,
        "body_overrides": body_overrides,
        "proxy_url": provider_proxy,
        "balance_endpoint": provider.get("balance_endpoint", conn["balance_endpoint"]),
        "auto_discover": bool(provider.get("auto_discover", True)),
        "models": copy.deepcopy(models_spec) if models_spec is not None else [],
        "model_whitelist": list(whitelist) if whitelist is not None else None,
        "model_blacklist": list(blacklist) if blacklist is not None else None,
    }

    provider_priority = provider.get("priority", 0)
    if type(provider_priority) is not int or not 0 <= provider_priority <= 99:
        raise ValueError(f"{where}: 'priority' must be an integer from 0 to 99")

    records: list[dict] = []
    secrets_found: list[str] = []

    def _add_proxy_secret(raw_proxy: str | None) -> None:
        if isinstance(raw_proxy, str) and "@" in raw_proxy:
            try:
                pw = urllib.parse.urlsplit(raw_proxy).password
            except ValueError:
                pw = None
            if pw and pw not in secrets_found:
                secrets_found.append(pw)

    _add_proxy_secret(provider_proxy)
    for idx, cred in enumerate(raw_creds):
        cwhere = f"{where}.credentials[{idx}]"
        if not isinstance(cred, dict):
            raise ValueError(f"{cwhere} must be an object")
        c_env = cred.get("api_key_env") or cred.get("bearer_token_env")
        if c_env is not None and (not isinstance(c_env, str) or not ENV_PATTERN.fullmatch(c_env)):
            raise ValueError(f"{cwhere}: api_key_env must be a valid environment variable name")
        c_raw = (os.environ.get(c_env, "") if c_env else "") or cred.get("api_key") or cred.get("bearer_token") or ""
        token = clean_api_key(c_raw) if c_raw else ""
        if conn["auth_mode"] != "none" and not token:
            hint = f" (env {c_env} is empty)" if c_env else ""
            raise ValueError(f"{cwhere}: missing API key{hint}")
        if token and len(token) >= 6 and token not in secrets_found:
            secrets_found.append(token)

        cred_enabled = cred.get("enabled", True)
        if type(cred_enabled) is not bool:
            raise ValueError(f"{cwhere}: 'enabled' must be a boolean")
        base_priority = cred.get("priority")
        if base_priority is None:
            # In fill-first mode, credentials without an explicit priority step in sequence
            # from the provider's priority so traffic stays pinned to the first healthy credential!
            base_priority = min(99, provider_priority + idx) if routing_strategy == "fill-first" else provider_priority
        elif type(base_priority) is not int or not 0 <= base_priority <= 99:
            raise ValueError(f"{cwhere}: 'priority' must be an integer from 0 to 99")

        weight = cred.get("weight", 1)
        if type(weight) not in (int, float) or weight <= 0:
            raise ValueError(f"{cwhere}: 'weight' must be a positive number")

        label = cred.get("label") or (meta["name"] if len(raw_creds) == 1 else f"{meta['name']} #{idx + 1}")
        if not isinstance(label, str) or len(label) > 40:
            label = str(label)[:40]

        cred_proxy = http_pool.validate_proxy_url(cred.get("proxy_url")) or provider_proxy
        _add_proxy_secret(cred_proxy)
        acc_id = cred.get("id") or cred.get("account_id") or _derived_credential_id(provider_id, idx)
        if not isinstance(acc_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", acc_id):
            raise ValueError(f"{cwhere}: invalid credential id '{acc_id}'")

        records.append({
            "account_id": acc_id,
            "upstream": conn["driver"],
            "provider_id": provider_id,
            "provider_name": meta["name"],
            "preset": conn["preset"],
            "driver": conn["driver"],
            "base": conn["base_url"],
            "base_url": conn["base_url"],
            "auth_mode": conn["auth_mode"],
            "auth_header_name": auth_header_name,
            "api_key": token,
            "bearer_token": token,
            "api_key_env": c_env,
            "headers": copy.deepcopy(headers),
            "body_overrides": copy.deepcopy(body_overrides),
            "proxy_url": cred_proxy,
            "allow_insecure_http": allow_insecure,
            "cache_policy": cache_policy,
            "routing_strategy": routing_strategy,
            "balance_endpoint": meta["balance_endpoint"],
            "enabled": bool(provider_enabled and cred_enabled),
            "priority": base_priority,
            "weight": float(weight),
            "label": label,
            "_provider_meta": meta,
        })

    return meta, records, secrets_found
