"""Live `/models` discovery, filtering, overlay persistence (`provider_models.auto.json`), and background refresher."""
from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import math
import os
import pathlib
import tempfile
import threading

import console_log
from drivers import http_pool
import journal

AUTO_NAME = "provider_models.auto.json"
MAX_ANTHROPIC_PAGES = 5

_NON_CHAT_PATTERNS = (
    "*embed*",
    "text-embedding-*",
    "tts-*",
    "whisper*",
    "dall-e*",
    "gpt-image-*",
    "omni-moderation*",
    "text-moderation*",
    "*rerank*",
    "babbage-*",
    "davinci-*",
)


def overlay_path(config_path: str | os.PathLike | None) -> pathlib.Path | None:
    if not config_path:
        return None
    return pathlib.Path(config_path).resolve().with_name(AUTO_NAME)


def load_overlay(path: str | os.PathLike | None) -> dict[str, list[dict]]:
    """Read `provider_models.auto.json` -> `{provider_id: [discovered_model_dict, ...]}`."""
    if not path:
        return {}
    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    providers = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(providers, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for pid, items in providers.items():
        if isinstance(pid, str) and isinstance(items, list):
            out[pid] = [dict(x) for x in items if isinstance(x, dict) and isinstance(x.get("id"), str)]
    return out


def save_overlay(path: str | os.PathLike | None, providers_map: dict[str, list[dict]]) -> bool:
    if not path:
        return False
    target = pathlib.Path(path)
    payload = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "providers": providers_map,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".prov_models_", suffix=".tmp", dir=str(target.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return True
    except OSError:
        return False


def build_auth_headers(cfg: dict) -> dict[str, str]:
    """Build HTTP headers for upstream requests (discovery, turns, balance) from normalized account/provider cfg."""
    headers: dict[str, str] = {"Accept": "application/json"}
    auth_mode = str(cfg.get("auth_mode") or "bearer").lower()
    token = str(cfg.get("api_key") or cfg.get("bearer_token") or "").strip()
    driver = str(cfg.get("driver") or cfg.get("upstream") or "openai-chat")

    if auth_mode == "bearer" and token:
        headers["Authorization"] = f"Bearer {token}"
    elif auth_mode == "x-api-key" and token:
        headers["x-api-key"] = token
    elif auth_mode == "custom-header" and token and cfg.get("auth_header_name"):
        headers[str(cfg["auth_header_name"])] = token

    if driver == "anthropic-messages":
        headers.setdefault("anthropic-version", "2023-06-01")

    extra = cfg.get("headers") or cfg.get("custom_headers")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(k, str) and isinstance(v, str):
                headers[k] = v
    return headers


def is_likely_generation_model(model_id: str, raw_item: dict | None = None) -> bool:
    """Filter out pure embeddings, TTS, ASR, moderation, and image-only models."""
    low = (model_id or "").strip().lower()
    if not low:
        return False
    if any(fnmatch.fnmatchcase(low, pat) for pat in _NON_CHAT_PATTERNS):
        return False
    if isinstance(raw_item, dict):
        arch = raw_item.get("architecture")
        if isinstance(arch, dict):
            out_mods = arch.get("output_modalities")
            if isinstance(out_mods, list) and out_mods and "text" not in [str(x).lower() for x in out_mods]:
                return False
    return True


def matches_filters(model_id: str, whitelist: list[str] | None, blacklist: list[str] | None) -> bool:
    low = (model_id or "").strip().lower()
    if whitelist:
        if not any(fnmatch.fnmatchcase(low, pat.strip().lower()) for pat in whitelist if pat.strip()):
            return False
    if blacklist:
        if any(fnmatch.fnmatchcase(low, pat.strip().lower()) for pat in blacklist if pat.strip()):
            return False
    return True


def _per_million_from_per_token(val) -> float | None:
    if val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num < 0:
        return None
    return round(num * 1_000_000.0, 6)


def normalize_discovered_item(raw: dict) -> dict | None:
    """Extract normalized metadata from OpenAI, OpenRouter, LiteLLM, Anthropic, or Ollama `/models` entries."""
    if not isinstance(raw, dict):
        return None
    mid = raw.get("id") or raw.get("name")
    if not isinstance(mid, str) or not mid.strip():
        return None
    mid = mid.strip()
    if not is_likely_generation_model(mid, raw):
        return None

    out: dict = {"id": mid}
    label = raw.get("display_name") or raw.get("name")
    if isinstance(label, str) and label.strip() and label.strip() != mid:
        out["label"] = label.strip()

    # Context & max output (OpenRouter, LiteLLM, vLLM, Anthropic)
    ctx = (
        raw.get("context_length")
        or raw.get("context_window")
        or raw.get("max_input_tokens")
        or raw.get("input_token_limit")
    )
    top_prov = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
    if not ctx and top_prov.get("context_length"):
        ctx = top_prov.get("context_length")
    max_out = (
        raw.get("max_completion_tokens")
        or raw.get("max_output_tokens")
        or raw.get("output_token_limit")
        or top_prov.get("max_completion_tokens")
    )
    if isinstance(ctx, int) and ctx > 0:
        out["context"] = ctx
    if isinstance(max_out, int) and max_out > 0:
        out["max_output"] = max_out

    # Modalities (OpenRouter architecture)
    arch = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    in_mods = arch.get("input_modalities")
    if isinstance(in_mods, list):
        lowered_mods = {str(m).lower() for m in in_mods}
        out["vision"] = "image" in lowered_mods
        if "file" in lowered_mods or "pdf" in lowered_mods:
            out["pdf"] = True

    # Supported parameters (OpenRouter)
    sup_params = raw.get("supported_parameters")
    if isinstance(sup_params, list):
        param_set = {str(p).lower() for p in sup_params}
        out["tools"] = "tools" in param_set or "tool_choice" in param_set
        if "reasoning" in param_set or "include_reasoning" in param_set:
            out["reasoning"] = True

    # Pricing (OpenRouter returns USD per single token as strings in `pricing: {prompt, completion, input_cache_read, input_cache_write}`)
    pricing_raw = raw.get("pricing")
    if isinstance(pricing_raw, dict) and ("prompt" in pricing_raw or "input" in pricing_raw):
        if "prompt" in pricing_raw:
            p_in = _per_million_from_per_token(pricing_raw.get("prompt"))
            p_out = _per_million_from_per_token(pricing_raw.get("completion"))
            p_read = _per_million_from_per_token(pricing_raw.get("input_cache_read"))
            p_write = _per_million_from_per_token(pricing_raw.get("input_cache_write"))
        else:
            try:
                p_in = float(pricing_raw["input"])
                p_out = float(pricing_raw["output"])
                p_read = float(pricing_raw["cache_read"]) if pricing_raw.get("cache_read") is not None else None
                p_write = float(pricing_raw["cache_write"]) if pricing_raw.get("cache_write") is not None else None
            except (KeyError, TypeError, ValueError):
                p_in = p_out = p_read = p_write = None
        if p_in is not None and p_out is not None:
            out["pricing"] = {
                "input": p_in,
                "output": p_out,
                "cache_read": p_read,
                "cache_write": p_write,
            }
    return out


async def discover_provider_models(cfg: dict, *, timeout: float | None = None) -> list[dict]:
    """Query `GET <base_url>/models` for a universal provider/account config and return normalized descriptors."""
    base = str(cfg.get("base_url") or cfg.get("base") or "").rstrip("/")
    allow_insecure = bool(cfg.get("allow_insecure_http", False))
    url = http_pool.validate_endpoint_url(f"{base}/models", allow_insecure_http=allow_insecure)
    headers = build_auth_headers(cfg)
    client = http_pool.get_async_client(cfg)
    req_timeout = http_pool.make_timeout(cfg, read_timeout=float(timeout or cfg.get("upstream_timeout") or 20.0))

    driver = str(cfg.get("driver") or cfg.get("upstream") or "openai-chat")
    whitelist = cfg.get("model_whitelist")
    blacklist = cfg.get("model_blacklist")
    meta = cfg.get("_provider_meta")
    if isinstance(meta, dict):
        if whitelist is None:
            whitelist = meta.get("model_whitelist")
        if blacklist is None:
            blacklist = meta.get("model_blacklist")

    raw_items: list[dict] = []
    after_id: str | None = None
    max_pages = MAX_ANTHROPIC_PAGES if driver == "anthropic-messages" else 1

    for _ in range(max_pages):
        params = {"limit": 100, "after_id": after_id} if (driver == "anthropic-messages" and after_id) else None
        resp = await client.get(url, headers=headers, params=params, timeout=req_timeout)
        if resp.status_code >= 400:
            detail = ""
            try:
                err_json = resp.json()
                if isinstance(err_json, dict):
                    err_obj = err_json.get("error")
                    detail = err_obj.get("message", "") if isinstance(err_obj, dict) else str(err_obj or "")
            except ValueError:
                detail = resp.text[:200]
            import apierr
            raise apierr.upstream_error(
                f"GET {url} returned HTTP {resp.status_code}" + (f": {detail[:200]}" if detail else "")
            )
        payload = resp.json()
        if isinstance(payload, list):
            page_items = payload
            raw_items.extend(x for x in page_items if isinstance(x, dict))
            break
        if not isinstance(payload, dict):
            break
        page_items = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("models")
        if isinstance(page_items, list):
            raw_items.extend(x for x in page_items if isinstance(x, dict))
        if driver == "anthropic-messages" and payload.get("has_more") and payload.get("last_id"):
            after_id = str(payload["last_id"])
        else:
            break

    seen: set[str] = set()
    discovered: list[dict] = []
    for raw in raw_items:
        norm = normalize_discovered_item(raw)
        if norm is None:
            continue
        mid = norm["id"]
        if mid in seen or not matches_filters(mid, whitelist, blacklist):
            continue
        seen.add(mid)
        discovered.append(norm)
    return discovered


class UniversalCatalogRefresher:
    """Background & on-demand model discovery for `providers[]` + `models.dev` enrichment."""

    def __init__(self, cfg: dict, runtime=None):
        self.cfg = cfg
        self.runtime = runtime
        self.after_look = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_refresh: dict[str, dict] = {}

    def _universal_provider_backends(self, provider_id: str | None = None) -> dict[str, dict]:
        """Pick one enabled credential config per universal provider_id."""
        chosen: dict[str, dict] = {}
        pool = getattr(getattr(self.runtime, "pool", None), "backends", None)
        configs = [b.cfg for b in pool] if pool is not None else (self.cfg.get("accounts") or [])
        for acc_cfg in configs:
            if not isinstance(acc_cfg, dict) or not acc_cfg.get("enabled", True):
                continue
            pid = acc_cfg.get("provider_id")
            if not pid or (provider_id and pid != provider_id):
                continue
            meta = acc_cfg.get("_provider_meta") or (self.cfg.get("_providers_meta") or {}).get(pid) or {}
            if not meta.get("auto_discover", True) and not provider_id:
                continue
            if pid not in chosen:
                chosen[pid] = acc_cfg
        return chosen

    async def refresh_async(self, provider_id: str | None = None) -> dict:
        from catalog import models_dev, router
        import models as model_catalog

        if provider_id is None and self.cfg.get("models_dev_sync", True):
            try:
                await models_dev.refresh_models_dev_async(self.cfg)
            except Exception:
                pass

        targets = self._universal_provider_backends(provider_id)
        path = overlay_path(self.cfg.get("_config_path"))
        current_overlay = load_overlay(path)
        results: dict[str, dict] = {}
        changed = False

        for pid, acc_cfg in targets.items():
            try:
                found = await discover_provider_models(acc_cfg)
                results[pid] = {"ok": True, "count": len(found)}
                if current_overlay.get(pid) != found:
                    current_overlay[pid] = found
                    changed = True
            except Exception as exc:  # noqa: BLE001
                msg = journal.scrub(str(exc))[:240]
                results[pid] = {"ok": False, "error": msg}
                journal.emit(
                    "catalog.provider_refresh_failed",
                    f"Не удалось обновить список моделей провайдера {pid}: {msg}",
                    "warn",
                    provider=pid,
                    error=msg,
                )

        if changed and path is not None:
            save_overlay(path, current_overlay)

        # Rebuild runtime catalogs in place
        self.cfg["_discovered_provider_models"] = current_overlay
        self.cfg["_provider_models"] = router.build_provider_catalog(self.cfg, current_overlay)
        self.cfg["_route_aliases"] = router.build_route_aliases(self.cfg, model_catalog)
        with self._lock:
            self.last_refresh.update(results)

        if changed and callable(self.after_look):
            try:
                await asyncio.to_thread(self.after_look, "providers")
            except Exception:
                pass
        if changed:
            total = len(self.cfg["_provider_models"])
            console_log.write(f"[catalog] universal providers: {total} published models across {len(current_overlay)} provider(s)")
        return {"providers": results, "changed": changed, "total_models": len(self.cfg.get("_provider_models") or {})}

    def refresh_now(self, provider_id: str | None = None, timeout: float = 30.0) -> dict:
        """Run discovery synchronously via the runtime's asyncio loop (or a temporary loop in standalone tests)."""
        if self.runtime is not None and getattr(self.runtime, "loop", None) is not None:
            fut = self.runtime.submit(self.refresh_async(provider_id))
            return fut.result(timeout=timeout)
        return asyncio.run(self.refresh_async(provider_id))

    def start(self):
        hours = float(self.cfg.get("catalog_refresh_hours", 6) or 0)
        if hours <= 0 or self._thread is not None:
            return
        if not (self.cfg.get("_providers_meta") or self._universal_provider_backends()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(hours,), name="provider-catalog-refresh", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _loop(self, hours: float):
        interval = max(60.0, hours * 3600.0)
        # Initial non-blocking startup discovery shortly after boot
        if not self._stop.wait(1.0):
            try:
                self.refresh_now(timeout=45.0)
            except Exception:
                pass
        while not self._stop.wait(interval):
            try:
                self.refresh_now(timeout=45.0)
            except Exception:
                pass
