"""Model capability & USD pricing registry backed by models.dev + bundled offline snapshot.

Provides:
  - Offline zero-latency metadata & USD/1M token pricing lookup (`lookup_metadata`)
  - Fuzzy normalization of vendor prefixes, date stamps, tags (`:free`, `:thinking`), and dot/hyphen variants
  - Optional background sync from `https://models.dev/api.json` into `<config_dir>/models_dev.cache.json`
  - Exact USD turn cost calculation (`calculate_cost_usd`) from normalized token usage
"""
from __future__ import annotations

import copy
import json
import math
import os
import pathlib
import re
import tempfile

from drivers import http_pool

MODELS_DEV_URL = "https://models.dev/api.json"
SNAPSHOT_FILE = pathlib.Path(__file__).with_name("models_dev_snapshot.json")
CACHE_NAME = "models_dev.cache.json"

_DATE_SUFFIX = re.compile(r"-\d{8}$")
_TAG_SUFFIX = re.compile(r":[a-z0-9_.-]+$", re.I)
_PREVIEW_SUFFIX = re.compile(r"-(?:preview|latest|exp|beta)(?:-\d{2,4}-\d{2})?$", re.I)

_SHIPPED: dict[str, dict] = {}
_LIVE_OVERLAY: dict[str, dict] = {}


def _load_snapshot() -> dict[str, dict]:
    try:
        raw = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        models = raw.get("models")
        return dict(models) if isinstance(models, dict) else {}
    except (OSError, ValueError):
        return {}


_SHIPPED = _load_snapshot()


def cache_path(config_path: str | os.PathLike | None) -> pathlib.Path | None:
    if not config_path:
        return None
    return pathlib.Path(config_path).resolve().with_name(CACHE_NAME)


def canonical_slug(model_id: str) -> str:
    """Normalize a provider model ID to a canonical lookup slug."""
    s = (model_id or "").strip()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    s = _TAG_SUFFIX.sub("", s)
    s = s.lower()
    s = _DATE_SUFFIX.sub("", s)
    if s.startswith("claude-"):
        s = s.replace(".", "-")
    return s


def infer_family(model_id: str, provider_hint: str | None = None) -> str:
    """Infer model family (`anthropic`, `openai`, `google`, `deepseek`, etc.) for effort & cache rules."""
    low = (model_id or "").lower()
    if "/" in low:
        vendor = low.split("/", 1)[0]
        if vendor in ("anthropic", "claude"):
            return "anthropic"
        if vendor in ("openai",):
            return "openai"
        if vendor in ("google", "gemini", "vertex"):
            return "google"
        if vendor in ("deepseek",):
            return "deepseek"
        if vendor in ("x-ai", "xai"):
            return "xai"
        if vendor in ("moonshot", "moonshotai"):
            return "moonshot"
    slug = canonical_slug(low)
    if slug.startswith("claude-"):
        return "anthropic"
    if slug.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-", "codex-")):
        return "openai"
    if slug.startswith(("gemini-", "gemma-")):
        return "google"
    if slug.startswith("deepseek-"):
        return "deepseek"
    if slug.startswith("grok-"):
        return "xai"
    if slug.startswith(("kimi-", "moonshot-")):
        return "moonshot"
    if slug.startswith(("qwen", "qwq")):
        return "qwen"
    if slug.startswith("glm-"):
        return "zhipu"
    hint = (provider_hint or "").lower()
    if hint in ("anthropic", "openai", "google", "deepseek", "xai", "moonshot"):
        return hint
    return hint or "openai"


def parse_models_dev_payload(payload: dict) -> dict[str, dict]:
    """Convert raw `https://models.dev/api.json` provider map into our flat normalized metadata table."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict] = {}
    for prov_key, prov_obj in payload.items():
        if not isinstance(prov_obj, dict):
            continue
        models_map = prov_obj.get("models")
        if not isinstance(models_map, dict):
            continue
        for raw_id, item in models_map.items():
            if not isinstance(item, dict):
                continue
            limit = item.get("limit") if isinstance(item.get("limit"), dict) else {}
            cost = item.get("cost") if isinstance(item.get("cost"), dict) else {}
            modalities = item.get("modalities") if isinstance(item.get("modalities"), dict) else {}
            in_mods = modalities.get("input") if isinstance(modalities.get("input"), list) else []

            context = int(limit.get("context") or 0) or None
            max_output = int(limit.get("output") or 0) or None
            vision = "image" in in_mods or bool(item.get("attachment"))
            pdf = "pdf" in in_mods
            tools = bool(item.get("tool_call", True))
            reasoning = bool(item.get("reasoning", False))

            pricing = None
            if cost.get("input") is not None and cost.get("output") is not None:
                try:
                    pricing = {
                        "input": float(cost["input"]),
                        "output": float(cost["output"]),
                        "cache_read": float(cost["cache_read"]) if cost.get("cache_read") is not None else None,
                        "cache_write": float(cost["cache_write"]) if cost.get("cache_write") is not None else None,
                    }
                except (TypeError, ValueError):
                    pricing = None

            slug = canonical_slug(str(item.get("id") or raw_id))
            if not slug:
                continue
            family = infer_family(raw_id, str(prov_key))
            # Prefer canonical first-party provider pricing (anthropic, openai, google, deepseek) over resellers
            is_primary_vendor = str(prov_key).lower() in ("anthropic", "openai", "google", "deepseek", "xai", "moonshotai")
            if slug in out and not is_primary_vendor:
                continue
            out[slug] = {
                "label": str(item.get("name") or raw_id),
                "family": family,
                "context": context,
                "max_output": max_output,
                "vision": vision,
                "pdf": pdf,
                "tools": tools,
                "reasoning": reasoning,
                "reasoning_effort_levels": _SHIPPED.get(slug, {}).get("reasoning_effort_levels", []),
                "pricing": pricing,
            }
    return out


def apply_cache_file(path: str | os.PathLike | None) -> int:
    """Load cached models.dev entries into `_LIVE_OVERLAY`; returns count loaded."""
    if not path:
        return 0
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        return 0
    _LIVE_OVERLAY.update({k: v for k, v in models.items() if isinstance(v, dict)})
    return len(models)


async def refresh_models_dev_async(cfg: dict) -> int:
    """Fetch `https://models.dev/api.json` asynchronously via `http_pool` and persist `models_dev.cache.json`."""
    if not cfg.get("models_dev_sync", True):
        return 0
    url = str(cfg.get("models_dev_url") or MODELS_DEV_URL)
    http_pool.validate_endpoint_url(url)
    client = http_pool.get_async_client(cfg)
    resp = await client.get(url, timeout=http_pool.make_timeout(cfg, read_timeout=25.0))
    if resp.status_code != 200:
        return 0
    parsed = parse_models_dev_payload(resp.json())
    if not parsed:
        return 0
    _LIVE_OVERLAY.update(parsed)
    target = cache_path(cfg.get("_config_path"))
    if target is not None:
        try:
            fd, tmp = tempfile.mkstemp(prefix=".models_dev_", suffix=".tmp", dir=str(target.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"source": url, "models": parsed}, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
        except OSError:
            pass
    return len(parsed)


def _match_entry(model_id: str) -> dict | None:
    slug = canonical_slug(model_id)
    if not slug:
        return None
    merged_table = {**_SHIPPED, **_LIVE_OVERLAY}
    if slug in merged_table:
        return merged_table[slug]
    stripped = _PREVIEW_SUFFIX.sub("", slug)
    if stripped in merged_table:
        return merged_table[stripped]
    # Prefix match for versioned variants (e.g. gpt-5.4-turbo -> gpt-5.4, claude-sonnet-4-6-thinking -> claude-sonnet-4-6)
    best_key = ""
    for key in merged_table:
        if slug.startswith(key + "-") and len(key) > len(best_key):
            best_key = key
    return merged_table[best_key] if best_key else None


def lookup_metadata(model_id: str, provider_hint: str | None = None) -> dict:
    """Return enriched capability & pricing metadata for `model_id`, falling back to permissive defaults."""
    matched = _match_entry(model_id)
    family = (matched or {}).get("family") or infer_family(model_id, provider_hint)
    is_free_variant = str(model_id or "").strip().lower().endswith(":free")

    if matched is not None:
        result = copy.deepcopy(matched)
        if is_free_variant:
            result["pricing"] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
        return result

    # Unknown / local model (Ollama, custom vLLM, fine-tune): permissive defaults so vision/tools/reasoning work
    default_levels = (
        ["low", "medium", "high", "max"]
        if family == "anthropic"
        else ["low", "medium", "high"]
        if family in ("openai", "google")
        else []
    )
    return {
        "label": str(model_id).rsplit("/", 1)[-1] if "/" in str(model_id) else str(model_id),
        "family": family,
        "context": None,
        "max_output": None,
        "vision": True,
        "pdf": family in ("anthropic", "google", "openai"),
        "tools": True,
        "reasoning": True,
        "reasoning_effort_levels": default_levels,
        "pricing": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0} if is_free_variant else None,
    }


def calculate_cost_usd(entry_or_pricing: dict | None, usage: dict | None) -> float | None:
    """Compute USD turn cost from normalized `usage` and per-1M `pricing` when upstream omitted `cost_usd`.

    In this codebase's normalized `usage` dict:
      - `input_tokens` = TOTAL input tokens (uncached + cache_read + cache_creation)
      - `cache_read_input_tokens` (or `cached_tokens`) = cached read tokens
      - `cache_creation_input_tokens` = cache write tokens
      - `output_tokens` = completion tokens (including reasoning tokens on OpenAI/Anthropic)
    """
    if not isinstance(usage, dict):
        return None
    explicit = usage.get("cost_usd")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool) and math.isfinite(explicit) and explicit >= 0:
        return round(float(explicit), 10)

    pricing = None
    if isinstance(entry_or_pricing, dict):
        pricing = entry_or_pricing.get("pricing") if "pricing" in entry_or_pricing else entry_or_pricing
    if not isinstance(pricing, dict) or pricing.get("input") is None or pricing.get("output") is None:
        return None

    try:
        in_rate = float(pricing["input"])
        out_rate = float(pricing["output"])
        read_rate = float(pricing["cache_read"]) if pricing.get("cache_read") is not None else in_rate
        write_rate = float(pricing["cache_write"]) if pricing.get("cache_write") is not None else in_rate
    except (TypeError, ValueError):
        return None

    total_in = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    if total_in == 0 and out_tok == 0 and not usage:
        return None
    cache_read = int(usage.get("cache_read_input_tokens") or usage.get("cached_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    uncached_in = max(0, total_in - cache_read - cache_write)

    cost = (
        uncached_in * in_rate
        + cache_read * read_rate
        + cache_write * write_rate
        + out_tok * out_rate
    ) / 1_000_000.0
    return round(cost, 10) if math.isfinite(cost) else None
