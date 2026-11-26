"""Admin CRUD and live runtime hot-reload operations for universal providers, routes, client keys, and probes."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import queue
import re
import secrets
import threading
import time
import urllib.parse

import account_report
import apierr
import auth
import client_context
import client_sync
from catalog import discovery, router
from drivers import curl_pool, http_pool, presets
import journal
import models
import validate

ROUTE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,79}$")
ROUTE_STRATEGIES = ("priority", "weighted", "least-inflight")
SECRET_HEADER_NAMES = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
# A model id a provider lists by hand: no spaces, quotes, braces, brackets or commas. The edit form split an OpenCode models
# object at its commas, and the pieces ('{"deepseek-chat":{"name":"DeepSeek Chat"', ...) were saved as ids (audit A14-01).
MODEL_ID_PATTERN = re.compile(r"[^\s\"'{}\[\],]{1,200}")
# What a key preview shows: a mask, and the last four characters of a key long enough that they say nothing of the rest.
KEY_MASK = "••••••••"
PREVIEW_MIN_CHARS = 24
CLIENT_KEY_PREFIX = "sk-proxi-"  # auth.generate_client_key: the relay's own prefix is public
# The userinfo of a proxy_url in an answer: a stand-in, which a PATCH that sends it back unchanged leaves alone.
PROXY_USERINFO_MASK = "•••"
# How long the admin waits for a catalog refresh before it answers "still running" (a refresh reads /models of every
# provider in turn, and models.dev).
DISCOVERY_WAIT = 25.0
_DISCOVERY_LOCK = threading.Lock()


def mask_key_preview(raw_key: str | None) -> str | None:
    """A key as the admin API shows it: at most its last four characters, and only for a key of PREVIEW_MIN_CHARS or more.

    The preview showed the first six and the last four characters of any key longer than 12: ten of a thirteen-character
    key (audits A07-09, A14-12)."""
    if not isinstance(raw_key, str) or not raw_key.strip():
        return None
    val = raw_key.strip()
    return "…" + val[-4:] if len(val) >= PREVIEW_MIN_CHARS else KEY_MASK


def client_key_preview(raw_key: str | None) -> str | None:
    """A client key as the admin shows it: a key the relay made keeps its public prefix (sk-proxi-) and its last four
    characters; one set by hand is shown like a provider's key. auth.list_public showed the first nine and the last four
    characters of any key over 16: thirteen of a seventeen-character key set by hand."""
    if isinstance(raw_key, str) and raw_key.startswith(CLIENT_KEY_PREFIX) and len(raw_key) >= PREVIEW_MIN_CHARS:
        return CLIENT_KEY_PREFIX + "…" + raw_key[-4:]
    return mask_key_preview(raw_key)


def public_proxy_url(url):
    """A proxy_url without its user name and password: http_pool accepts socks5://user:pass@host:port, and the admin API
    returned it as it was (audits A07-09, A11-19)."""
    if not isinstance(url, str) or "@" not in url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return PROXY_USERINFO_MASK
    if parts.username is None and parts.password is None:
        return url
    return urllib.parse.urlunsplit((parts.scheme, PROXY_USERINFO_MASK + "@" + parts.netloc.rpartition("@")[2],
                                    parts.path, parts.query, parts.fragment))


def _proxy_password(url):
    try:
        return urllib.parse.urlsplit(url).password if isinstance(url, str) and "@" in url else None
    except ValueError:
        return None


def _proxy_secrets(url) -> list[str]:
    """Extract non-empty username and password from a proxy_url."""
    if not isinstance(url, str) or "@" not in url:
        return []
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return []
    found: list[str] = []
    for raw in (parts.username, parts.password):
        if isinstance(raw, str) and raw:
            if raw not in found:
                found.append(raw)
            if "%" in raw:
                unquoted = urllib.parse.unquote(raw)
                if unquoted and unquoted not in found:
                    found.append(unquoted)
    return found


def _admin_lock(admin_obj):
    return getattr(admin_obj, "lock", None) or contextlib.nullcontext()


def collect_extra_secrets(saved: dict | None) -> list[str]:
    """Gather API keys and virtual client keys from saved config so `Admin._safe` masks them everywhere."""
    if not isinstance(saved, dict):
        return []
    found: list[str] = []

    def _add(val):
        if isinstance(val, str):
            s = val.strip()
            if len(s) >= 8 and s not in found:
                found.append(s)

    def _add_proxy(url):
        for secret in _proxy_secrets(url):
            _add(secret)

    for prov in saved.get("providers") or []:
        if not isinstance(prov, dict):
            continue
        _add(prov.get("api_key"))
        _add(prov.get("bearer_token"))
        _add_proxy(prov.get("proxy_url"))
        opts = prov.get("options") if isinstance(prov.get("options"), dict) else {}
        _add(opts.get("apiKey"))
        env_name = prov.get("api_key_env") or prov.get("bearer_token_env")
        if isinstance(env_name, str) and env_name:
            _add(os.environ.get(env_name))
        for hdrs in (prov.get("headers"), opts.get("headers")):
            if isinstance(hdrs, dict):
                for hk, hv in hdrs.items():
                    if isinstance(hk, str) and hk.lower().strip() in SECRET_HEADER_NAMES:
                        _add(hv)
                        if isinstance(hv, str) and hv.lower().startswith("bearer "):
                            _add(hv[7:].strip())
        for cred in prov.get("credentials") or []:
            if isinstance(cred, dict):
                _add(cred.get("api_key"))
                _add(cred.get("bearer_token"))
                _add_proxy(cred.get("proxy_url"))
                c_env = cred.get("api_key_env") or cred.get("bearer_token_env")
                if isinstance(c_env, str) and c_env:
                    _add(os.environ.get(c_env))

    for ck in saved.get("client_keys") or []:
        if isinstance(ck, dict):
            _add(ck.get("key"))

    _add_proxy(saved.get("proxy_url"))
    for acc in saved.get("accounts") or []:
        if isinstance(acc, dict):
            _add_proxy(acc.get("proxy_url"))

    return found


def presets_catalog() -> list[dict]:
    items = []
    for pid, spec in presets.PRESETS.items():
        items.append({
            "id": pid,
            "name": spec["name"],
            "driver": spec["driver"],
            "base_url": spec["base_url"],
            "auth_mode": spec["auth_mode"],
            "cache_policy": spec["cache_policy"],
            "balance_supported": bool(spec.get("balance_endpoint")),
        })
    return items


def _sanitize_saved_provider(raw: dict, meta: dict) -> dict:
    """Build a clean provider dict for `config.json` that keeps `api_key_env` separate from resolved env values."""
    clean = copy.deepcopy(raw)
    clean.pop("discover_now", None)
    clean["id"] = meta["id"]
    clean["name"] = meta["name"]
    if meta.get("preset") and "preset" in raw:
        clean["preset"] = meta["preset"]
    clean["driver"] = meta["driver"]
    clean["base_url"] = meta["base_url"]
    clean["auth_mode"] = meta["auth_mode"]
    if meta.get("auth_header_name"):
        clean["auth_header_name"] = meta["auth_header_name"]
    else:
        clean.pop("auth_header_name", None)
    clean["routing_strategy"] = meta["routing_strategy"]
    clean["cache_policy"] = meta["cache_policy"]
    clean["enabled"] = bool(meta.get("enabled", True))
    clean["auto_discover"] = bool(meta.get("auto_discover", True))
    if meta.get("headers"):
        clean["headers"] = copy.deepcopy(meta["headers"])
    elif "headers" in clean and not clean["headers"]:
        clean.pop("headers", None)
    if meta.get("body_overrides"):
        clean["body_overrides"] = copy.deepcopy(meta["body_overrides"])
    elif "body_overrides" in clean and not clean["body_overrides"]:
        clean.pop("body_overrides", None)
    if meta.get("proxy_url"):
        clean["proxy_url"] = meta["proxy_url"]
    elif "proxy_url" in clean and not clean["proxy_url"]:
        clean.pop("proxy_url", None)
    if clean["auth_mode"] == "none":
        for k in ("api_key", "bearer_token", "api_key_env", "bearer_token_env", "credentials"):
            clean.pop(k, None)
        _drop_options_api_key(clean)
    return clean


def _public_provider(raw: dict, meta: dict, admin_obj, entries_by_pid: dict, usage_by_acc: dict) -> dict:
    pid = meta["id"]
    discovered = list((admin_obj.cfg.get("_discovered_provider_models") or {}).get(pid) or [])
    manual_raw = meta.get("models")
    if isinstance(manual_raw, dict):
        manual_ids = list(manual_raw.keys())
    elif isinstance(manual_raw, list):
        manual_ids = [m["id"] if isinstance(m, dict) else str(m) for m in manual_raw if (isinstance(m, dict) and m.get("id")) or isinstance(m, str)]
    else:
        manual_ids = []

    published = [
        mid for mid, entry in (admin_obj.cfg.get("_provider_models") or {}).items()
        if entry.get("provider_id") == pid
    ]

    prov_entries = entries_by_pid.get(pid) or []
    routable_cnt = sum(1 for e in prov_entries if e.get("routable"))
    avail_vals = [e["available"] for e in prov_entries if isinstance(e.get("available"), (int, float))]
    bal_supported = any((e.get("info") or {}).get("balance_supported") for e in prov_entries)
    turns = 0
    cost_usd = 0.0
    errors = []
    states: dict[str, int] = {}
    for e in prov_entries:
        st = e.get("state") or "active"
        states[st] = states.get(st, 0) + 1
        b = e["backend"]
        u = usage_by_acc.get(id(b)) or {}
        turns += int(u.get("turns") or 0)
        if isinstance(u.get("provider_cost"), (int, float)):
            cost_usd += float(u["provider_cost"])
        err = (e.get("info") or {}).get("error") or (e.get("info") or {}).get("stale_error")
        if err:
            errors.append(str(err)[:180])

    creds_public = []
    raw_creds = raw.get("credentials")
    if isinstance(raw_creds, list) and raw_creds:
        for idx, c in enumerate(raw_creds):
            if not isinstance(c, dict):
                continue
            creds_public.append({
                "index": idx + 1,
                "label": c.get("label") or f"{meta['name']} #{idx + 1}",
                "api_key_set": bool(c.get("api_key") or c.get("bearer_token")),
                "api_key_preview": mask_key_preview(c.get("api_key") or c.get("bearer_token")),
                "api_key_env": c.get("api_key_env") or c.get("bearer_token_env"),
                "enabled": c.get("enabled", True) is not False,
                "priority": c.get("priority", 0),
                "weight": c.get("weight", 1),
                "proxy_url": public_proxy_url(c.get("proxy_url")),
            })
    else:
        top_key = raw.get("api_key") or raw.get("bearer_token") or ((raw.get("options") or {}).get("apiKey") if isinstance(raw.get("options"), dict) else None)
        top_env = raw.get("api_key_env") or raw.get("bearer_token_env")
        if meta["auth_mode"] != "none" or top_key or top_env:
            creds_public.append({
                "index": 1,
                "label": raw.get("label") or meta["name"],
                "api_key_set": bool(top_key),
                "api_key_preview": mask_key_preview(top_key),
                "api_key_env": top_env,
                "enabled": meta.get("enabled", True) is not False,
                "priority": raw.get("priority", 0),
                "weight": raw.get("weight", 1),
                "proxy_url": public_proxy_url(meta.get("proxy_url")),
            })

    refresher = getattr(admin_obj.runtime, "provider_catalog", None)
    last_refresh = (getattr(refresher, "last_refresh", None) or {}).get(pid)

    return {
        "id": pid,
        "name": meta["name"],
        "preset": meta.get("preset"),
        "driver": meta["driver"],
        "base_url": meta["base_url"],
        "auth_mode": meta["auth_mode"],
        "auth_header_name": meta.get("auth_header_name"),
        "routing_strategy": meta["routing_strategy"],
        "cache_policy": meta["cache_policy"],
        "enabled": bool(meta.get("enabled", True)),
        "priority": raw.get("priority", 0) if type(raw.get("priority")) is int else 0,
        "allow_insecure_http": bool(meta.get("allow_insecure_http", False)),
        "auto_discover": bool(meta.get("auto_discover", True)),
        "proxy_url": public_proxy_url(meta.get("proxy_url")),
        "headers_keys": sorted((meta.get("headers") or {}).keys()),
        "body_overrides": meta.get("body_overrides") or {},
        "models": meta.get("models") or [],
        "manual_models_count": len(manual_ids),
        "discovered_models_count": len(discovered),
        "published_models_count": len(published),
        "published_models": published[:60],
        "model_whitelist": meta.get("model_whitelist"),
        "model_blacklist": meta.get("model_blacklist"),
        "credentials_count": len(creds_public) if meta["auth_mode"] != "none" else 1,
        "credentials": creds_public,
        "routable": routable_cnt > 0,
        "backends_total": len(prov_entries),
        "backends_routable": routable_cnt,
        "states": states,
        "balance_supported": bal_supported,
        "available_usd": round(sum(avail_vals), 4) if avail_vals else None,
        "turns": turns,
        "cost_usd": round(cost_usd, 6),
        "last_refresh": last_refresh,
        "error": errors[0] if errors else None,
    }


def _collect_provider_telemetry(runtime) -> tuple[dict[str, list[dict]], dict[int, dict]]:
    entries = account_report.collect(runtime)
    usage = account_report.usage_report(runtime, [(e["backend"], e["manager"]) for e in entries])
    by_pid: dict[str, list[dict]] = {}
    usage_by_acc: dict[int, dict] = {}
    for entry, per_acc in zip(entries, usage.get("accounts") or []):
        b = entry["backend"]
        usage_by_acc[id(b)] = per_acc
        pid = (getattr(b, "cfg", None) or {}).get("provider_id")
        if pid:
            by_pid.setdefault(pid, []).append(entry)
    return by_pid, usage_by_acc


def list_providers(admin_obj) -> dict:
    saved = admin_obj._saved() if admin_obj.cfg.get("_config_path") else {}
    raw_list = saved.get("providers") if isinstance(saved.get("providers"), list) else (admin_obj.cfg.get("providers") or [])
    metas = admin_obj.cfg.get("_providers_meta") or {}
    by_pid, usage_by_acc = _collect_provider_telemetry(admin_obj.runtime)

    out = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id") or "").strip().lower()
        meta = metas.get(pid)
        if meta is None:
            try:
                meta, _, _ = presets.expand_provider_to_accounts(copy.deepcopy(raw), "provider")
            except ValueError:
                continue
        out.append(_public_provider(raw, meta, admin_obj, by_pid, usage_by_acc))

    return admin_obj._safe({
        "providers": out,
        "presets": presets_catalog(),
        "drivers": list(presets.UNIVERSAL_DRIVERS),
        "auth_modes": list(presets.AUTH_MODES),
        "routing_strategies": list(presets.ROUTING_STRATEGIES),
        "cache_policies": list(presets.CACHE_POLICIES),
    })


def _rebuild_universal_catalogs(cfg: dict) -> None:
    overlay = cfg.get("_discovered_provider_models")
    if overlay is None:
        overlay = discovery.load_overlay(discovery.overlay_path(cfg.get("_config_path")))
        cfg["_discovered_provider_models"] = overlay
    cfg["_provider_models"] = router.build_provider_catalog(cfg, overlay)
    cfg["_route_aliases"] = router.build_route_aliases(cfg, models)


def _replace_provider_backends_live(admin_obj, provider_id: str, new_records: list[dict]) -> bool:
    runtime = admin_obj.runtime
    pool = getattr(runtime, "pool", None)
    dispatcher = getattr(runtime, "accounts", None)
    if pool is None or dispatcher is None or not hasattr(pool, "add_account"):
        return False

    # 1. Remove any existing live backends for this provider_id
    existing = [b for b in list(getattr(pool, "backends", None) or []) if (getattr(b, "cfg", None) or {}).get("provider_id") == provider_id]
    for b in existing:
        b.cfg["enabled"] = False
        http_pool.close_account_clients(
            b.cfg.get("account_id") or b.cfg.get("provider_id") or curl_pool.account_scope(b.cfg)
        )
        try:
            pool.remove_account(b)
        except Exception:
            pass
        try:
            dispatcher.remove_account(b)
        except Exception:
            pass

    # 2. Adopt new credential backends
    added_backends = []
    try:
        for rec in new_records:
            b = pool.add_account(copy.deepcopy(rec))
            mgr_fn = getattr(runtime, "account_manager", None)
            dispatcher.add_account(b, mgr_fn(b) if mgr_fn is not None else None)
            added_backends.append(b)
    except Exception:
        for b in added_backends:
            try:
                dispatcher.remove_account(b)
                pool.remove_account(b)
            except Exception:
                pass
        return False
    return True


def _check_models(models_spec) -> None:
    """Refuse a hand-written model list whose entries cannot be model ids (a list of ids or of {"id": ...} objects, or an
    OpenCode object keyed by id): a list of pieces of JSON was saved as is, and every model of the provider went 404."""
    if models_spec is None:
        return
    if isinstance(models_spec, dict):
        ids = list(models_spec)
        bad = [key for key, value in models_spec.items() if not (value is None or isinstance(value, dict))]
    elif isinstance(models_spec, list):
        ids = [item.get("id") if isinstance(item, dict) else item for item in models_spec]
        bad = []
    else:
        raise apierr.bad_request("'models' must be a list of model ids or an object keyed by model id", param="models")
    bad += [item for item in ids if not (isinstance(item, str) and MODEL_ID_PATTERN.fullmatch(item))]
    if bad:
        shown = ", ".join(repr(item)[:60] for item in bad[:3])
        raise apierr.bad_request("'models' holds entries that are not model ids (no spaces, quotes, braces or commas): "
                                 + shown, param="models")


def _merge_headers(saved, changes):
    """A PATCH's headers by name over the saved ones (a name is matched whatever its case); null removes one.

    The admin never shows a saved header's value, so the form cannot send the whole set back: the object replaced them all,
    and adding X-New dropped HTTP-Referer and X-Title (audit A14-03)."""
    merged = dict(saved) if isinstance(saved, dict) else {}
    for name, value in changes.items():
        if isinstance(name, str):
            for old in [key for key in merged if isinstance(key, str) and key.lower() == name.lower()]:
                merged.pop(old)
        if value is not None:
            merged[name] = value
    return merged


def _drop_options_api_key(provider: dict) -> None:
    opts = provider.get("options")
    if isinstance(opts, dict) and "apiKey" in opts:
        opts.pop("apiKey", None)
        if not opts:
            provider.pop("options", None)


def _preset_matches_base_url(preset_id: str | None, base_url: str | None) -> bool:
    if not preset_id or preset_id == "custom" or preset_id not in presets.PRESETS:
        return False
    if not isinstance(base_url, str) or not base_url.strip():
        return True
    if presets.detect_preset_from_url(base_url) == preset_id:
        return True
    spec_url = presets.PRESETS[preset_id].get("base_url") or ""
    try:
        new_parts = urllib.parse.urlsplit(base_url.strip())
        spec_parts = urllib.parse.urlsplit(spec_url)
        new_host = (new_parts.hostname or "").lower()
        spec_host = (spec_parts.hostname or "").lower()
        if not new_host or not spec_host:
            return False
        if new_host == spec_host and (new_parts.port or None) == (spec_parts.port or None):
            return True
        if preset_id in ("moonshot", "siliconflow") and new_host == f"api.{preset_id}.cn":
            return True
        return False
    except ValueError:
        return False


def _merge_credentials_patch(existing: dict, raw_creds: list) -> list:
    existing_creds = existing.get("credentials") if isinstance(existing.get("credentials"), list) else []
    by_id = {}
    for prev in existing_creds:
        if isinstance(prev, dict):
            pid = prev.get("id") or prev.get("account_id")
            if pid:
                by_id[pid] = prev
    merged_list = []
    for idx, item in enumerate(raw_creds):
        if not isinstance(item, dict):
            merged_list.append(item)
            continue
        cred = copy.deepcopy(item)
        cid = cred.get("id") or cred.get("account_id")
        prev = by_id.get(cid) if cid in by_id else None
        if prev is None and isinstance(cred.get("index"), int) and 1 <= cred["index"] <= len(existing_creds):
            prev = existing_creds[cred["index"] - 1]
        elif prev is None and idx < len(existing_creds):
            prev = existing_creds[idx]
        if not isinstance(prev, dict):
            prev = None

        raw_proxy = cred.get("proxy_url")
        if isinstance(raw_proxy, str):
            prev_proxy = prev.get("proxy_url") if prev else None
            if prev_proxy and raw_proxy != prev_proxy and raw_proxy == public_proxy_url(prev_proxy):
                cred["proxy_url"] = prev_proxy
            elif not prev_proxy and existing.get("proxy_url") and raw_proxy == public_proxy_url(existing.get("proxy_url")):
                cred["proxy_url"] = existing["proxy_url"]
            elif PROXY_USERINFO_MASK + "@" in raw_proxy or raw_proxy == PROXY_USERINFO_MASK:
                matched_proxy = next(
                    (c.get("proxy_url") for c in existing_creds
                     if isinstance(c, dict) and c.get("proxy_url") and public_proxy_url(c.get("proxy_url")) == raw_proxy),
                    None,
                )
                if matched_proxy:
                    cred["proxy_url"] = matched_proxy

        has_env = bool((isinstance(cred.get("api_key_env"), str) and cred["api_key_env"].strip())
                       or (isinstance(cred.get("bearer_token_env"), str) and cred["bearer_token_env"].strip()))
        prev_key = None
        if prev is not None:
            prev_key = prev.get("api_key") or prev.get("bearer_token")
        elif idx == 0 and not existing_creds:
            opts = existing.get("options") if isinstance(existing.get("options"), dict) else {}
            prev_key = existing.get("api_key") or existing.get("bearer_token") or opts.get("apiKey")

        if has_env:
            cred.pop("api_key", None)
            cred.pop("bearer_token", None)
        else:
            for k_field in ("api_key", "bearer_token"):
                if k_field in cred:
                    val = cred[k_field]
                    is_blank = val is None or (isinstance(val, str) and not val.strip())
                    is_masked = isinstance(val, str) and prev_key and val in (KEY_MASK, mask_key_preview(prev_key))
                    if (is_blank or is_masked) and prev_key:
                        cred[k_field] = prev_key
                    elif is_blank:
                        cred.pop(k_field, None)
            if "api_key" not in cred and "bearer_token" not in cred:
                if prev_key:
                    cred["api_key"] = prev_key
                elif prev is not None:
                    for env_field in ("api_key_env", "bearer_token_env"):
                        if prev.get(env_field) and env_field not in cred:
                            cred[env_field] = prev[env_field]
        merged_list.append(cred)
    return merged_list


def _apply_custom_preset_override(source: dict, meta: dict, records: list[dict]) -> None:
    if str((source or {}).get("preset") or "").strip().lower() != "custom":
        return
    endpoint = source.get("balance_endpoint") if "balance_endpoint" in source else None
    meta["preset"] = "custom"
    meta["balance_endpoint"] = endpoint
    for rec in records:
        rec["preset"] = "custom"
        rec["balance_endpoint"] = endpoint


def _merge_provider_patch(existing: dict, data: dict) -> dict:
    """The saved provider with a PATCH (or a probe of the saved provider) over it.

    An empty api_key keeps the stored credentials; `headers` are merged by name (null removes one, `"headers": null` all
    of them); a proxy_url sent back as the admin showed it (its user name and password masked) keeps the saved one; any
    other field replaces the saved value, so `"body_overrides": {}` clears them."""
    merged = copy.deepcopy(existing)
    for k, v in data.items():
        if k in ("id", "discover_now", "provider_id"):
            continue
        if k in ("api_key", "bearer_token"):
            prev_key = existing.get("api_key") or existing.get("bearer_token") or (
                (existing.get("options") or {}).get("apiKey") if isinstance(existing.get("options"), dict) else None
            )
            if v is None or (isinstance(v, str) and not v.strip()):
                # Empty key on edit preserves existing stored credentials unless switching to keyless
                continue
            if isinstance(v, str) and prev_key and v in (KEY_MASK, mask_key_preview(prev_key)):
                continue
        if k == "headers" and (v is None or isinstance(v, dict)):
            headers = None if v is None else _merge_headers(existing.get("headers"), v)
            if headers:
                merged["headers"] = headers
            else:
                merged.pop("headers", None)
            continue
        if (k == "proxy_url" and isinstance(v, str) and v != existing.get("proxy_url")
                and v == public_proxy_url(existing.get("proxy_url"))):
            continue
        if k == "credentials" and isinstance(v, list) and v:
            merged[k] = _merge_credentials_patch(existing, v)
            continue
        if k == "preset" and (v is None or (isinstance(v, str) and v.strip().lower() in ("", "custom"))):
            merged["preset"] = "custom"
            if "balance_endpoint" not in data:
                merged.pop("balance_endpoint", None)
            continue
        merged[k] = v

    # When base_url moves off a preset host without naming a new preset, clear stale preset/balance_endpoint.
    if "preset" not in data and ("base_url" in data or "base" in data):
        old_preset = str(existing.get("preset") or "").strip().lower()
        new_base = data.get("base_url") if "base_url" in data else data.get("base")
        if old_preset and old_preset != "custom" and not _preset_matches_base_url(old_preset, new_base):
            merged["preset"] = "custom"
            if "balance_endpoint" not in data:
                merged.pop("balance_endpoint", None)

    # Mutual exclusivity when switching from api_key_env <-> api_key or credentials
    if (isinstance(data.get("api_key"), str) and data["api_key"].strip()) or (
        isinstance(data.get("bearer_token"), str) and data["bearer_token"].strip()
    ):
        merged.pop("api_key_env", None)
        merged.pop("bearer_token_env", None)
        merged.pop("credentials", None)
        _drop_options_api_key(merged)
    elif (isinstance(data.get("api_key_env"), str) and data["api_key_env"].strip()) or (
        isinstance(data.get("bearer_token_env"), str) and data["bearer_token_env"].strip()
    ):
        merged.pop("api_key", None)
        merged.pop("bearer_token", None)
        merged.pop("credentials", None)
        _drop_options_api_key(merged)
    elif isinstance(data.get("credentials"), list) and data["credentials"]:
        merged.pop("api_key", None)
        merged.pop("bearer_token", None)
        merged.pop("api_key_env", None)
        merged.pop("bearer_token_env", None)
        _drop_options_api_key(merged)
    if merged.get("auth_mode") == "none":
        for k in ("api_key", "bearer_token", "api_key_env", "bearer_token_env", "credentials"):
            merged.pop(k, None)
        _drop_options_api_key(merged)
    return merged


def _serves_without(admin_obj, saved: dict, pid: str) -> bool:
    """Whether the relay keeps something to answer with when provider `pid` stops: an enabled account or another enabled
    provider. The last enabled account cannot be switched off or removed; the last provider of a relay without accounts
    could, and nothing was left to answer (audit A14-13)."""
    accounts = [record for record in admin_obj._records(saved) if record.get("enabled", True) is not False]
    others = [p for p in saved.get("providers") or [] if isinstance(p, dict) and p.get("enabled", True) is not False
              and str(p.get("id") or "").strip().lower() != pid]
    return bool(accounts or others)


def create_provider(admin_obj, data: dict) -> dict:
    if not isinstance(data, dict):
        raise apierr.bad_request("Request body must be a JSON object")
    _check_models(data.get("models"))
    discover_now = bool(data.get("discover_now", data.get("auto_discover", True)))
    try:
        meta, records, secrets_found = presets.expand_provider_to_accounts(copy.deepcopy(data), "provider")
    except ValueError as exc:
        raise apierr.bad_request(str(exc))
    _apply_custom_preset_override(data, meta, records)

    pid = meta["id"]
    clean = _sanitize_saved_provider(data, meta)

    with _admin_lock(admin_obj):
        with admin_obj._transaction() as (path, saved):
            prov_list = saved.get("providers")
            if not isinstance(prov_list, list):
                prov_list = saved["providers"] = []
            if any(isinstance(p, dict) and str(p.get("id") or "").strip().lower() == pid for p in prov_list):
                raise apierr.conflict(f"Provider '{pid}' already exists")
            prov_list.append(clean)
            admin_obj._write(path, saved)
            admin_obj.cfg["providers"] = copy.deepcopy(prov_list)
        admin_obj._mask_tokens(secrets_found)
        metas = admin_obj.cfg.setdefault("_providers_meta", {})
        metas[pid] = meta
        live = _replace_provider_backends_live(admin_obj, pid, records)
        _rebuild_universal_catalogs(admin_obj.cfg)

    refresher = getattr(admin_obj.runtime, "provider_catalog", None)
    disc_res = None
    if refresher is not None:
        refresher.start()
        if discover_now and live and meta.get("enabled", True):
            try:
                disc_res = refresher.refresh_now(pid, timeout=12.0)
            except Exception as exc:  # noqa: BLE001
                disc_res = {"providers": {pid: {"ok": False, "error": admin_obj._safe(str(exc))[:200]}}}

    journal.emit("admin.provider", f"Подключён провайдер {meta['name']} ({pid})", "success",
                 provider=pid, driver=meta["driver"], live=live)
    by_pid, usage_by_acc = _collect_provider_telemetry(admin_obj.runtime)
    return admin_obj._safe({
        "provider": _public_provider(clean, meta, admin_obj, by_pid, usage_by_acc),
        "discovery": disc_res,
        "restart_required": not live,
    })


def update_provider(admin_obj, provider_id: str, data: dict) -> dict:
    if not isinstance(data, dict) or not data:
        raise apierr.bad_request("Expected provider fields to update")
    pid = provider_id.strip().lower()
    discover_requested = data.get("discover_now")
    if "models" in data:
        _check_models(data["models"])

    with _admin_lock(admin_obj):
        with admin_obj._transaction() as (path, saved):
            prov_list = saved.get("providers") if isinstance(saved.get("providers"), list) else []
            idx = next((i for i, p in enumerate(prov_list) if isinstance(p, dict) and str(p.get("id") or "").strip().lower() == pid), None)
            if idx is None:
                raise apierr.not_found(f"Unknown provider '{pid}'")
            existing = copy.deepcopy(prov_list[idx])
            if (data.get("enabled") is False and existing.get("enabled", True) is not False
                    and not _serves_without(admin_obj, saved, pid)):
                raise apierr.conflict("The last enabled provider cannot be switched off: nothing else would answer")
            merged = _merge_provider_patch(existing, data)

            merged["id"] = pid
            try:
                meta, records, secrets_found = presets.expand_provider_to_accounts(copy.deepcopy(merged), f"providers[{pid}]")
            except ValueError as exc:
                raise apierr.bad_request(str(exc))
            _apply_custom_preset_override(merged, meta, records)

            clean = _sanitize_saved_provider(merged, meta)
            admin_obj._mask_tokens(secrets_found)
            prov_list[idx] = clean
            admin_obj._write(path, saved)
            admin_obj.cfg["providers"] = copy.deepcopy(prov_list)

        metas = admin_obj.cfg.setdefault("_providers_meta", {})
        old_meta = metas.get(pid) or {}
        metas[pid] = meta
        live = _replace_provider_backends_live(admin_obj, pid, records)
        _rebuild_universal_catalogs(admin_obj.cfg)

    conn_changed = (
        old_meta.get("base_url") != meta["base_url"]
        or old_meta.get("driver") != meta["driver"]
        or bool(data.get("api_key") or data.get("api_key_env") or data.get("credentials"))
    )
    should_discover = bool(discover_requested) or (conn_changed and meta.get("auto_discover", True) and meta.get("enabled", True))
    disc_res = None
    refresher = getattr(admin_obj.runtime, "provider_catalog", None)
    if should_discover and refresher is not None and live:
        try:
            disc_res = refresher.refresh_now(pid, timeout=12.0)
        except Exception as exc:  # noqa: BLE001
            disc_res = {"providers": {pid: {"ok": False, "error": admin_obj._safe(str(exc))[:200]}}}

    journal.emit("admin.provider", f"Обновлён провайдер {meta['name']} ({pid})", "info",
                 provider=pid, changed=sorted(set(data) - {"api_key", "credentials"}), live=live)
    by_pid, usage_by_acc = _collect_provider_telemetry(admin_obj.runtime)
    return admin_obj._safe({
        "provider": _public_provider(clean, meta, admin_obj, by_pid, usage_by_acc),
        "discovery": disc_res,
        "restart_required": not live,
    })


def delete_provider(admin_obj, provider_id: str) -> dict:
    pid = provider_id.strip().lower()
    with _admin_lock(admin_obj):
        with admin_obj._transaction() as (path, saved):
            prov_list = saved.get("providers") if isinstance(saved.get("providers"), list) else []
            remaining = [p for p in prov_list if not (isinstance(p, dict) and str(p.get("id") or "").strip().lower() == pid)]
            if len(remaining) == len(prov_list):
                raise apierr.not_found(f"Unknown provider '{pid}'")
            removed = [p for p in prov_list if p not in remaining]
            if (any(isinstance(p, dict) and p.get("enabled", True) is not False for p in removed)
                    and not _serves_without(admin_obj, saved, pid)):
                raise apierr.conflict("The last enabled provider cannot be removed: nothing else would answer")
            saved["providers"] = remaining
            admin_obj._write(path, saved)
            admin_obj.cfg["providers"] = copy.deepcopy(remaining)

        (admin_obj.cfg.get("_providers_meta") or {}).pop(pid, None)
        overlay = admin_obj.cfg.get("_discovered_provider_models")
        if isinstance(overlay, dict) and pid in overlay:
            overlay.pop(pid, None)
            ov_path = discovery.overlay_path(admin_obj.cfg.get("_config_path"))
            if ov_path is not None:
                discovery.save_overlay(ov_path, overlay)

        _replace_provider_backends_live(admin_obj, pid, [])
        _rebuild_universal_catalogs(admin_obj.cfg)
    journal.emit("admin.provider", f"Удалён провайдер {pid}", "warn", provider=pid)
    return {"ok": True, "restart_required": False}


def test_provider_connection(admin_obj, data: dict) -> dict:
    """Dry-run connection check + live `/models` discovery preview."""
    if not isinstance(data, dict):
        raise apierr.bad_request("Request body must be a JSON object")
    candidate = copy.deepcopy(data)
    editing_id = str(candidate.pop("provider_id", "") or "").strip().lower()
    existing_id = editing_id or str(candidate.get("id") or "").strip().lower()
    # The saved provider is the base of a probe of it, whatever key field the form sends: with api_key_env in the form it
    # was left out, and the check ran without its base_url, headers and flags (audit A14-03). A new provider's form that
    # only shares an id with a saved one is merged when it brings no key of its own, as before.
    if existing_id and (editing_id or not (candidate.get("api_key") or candidate.get("api_key_env")
                                           or candidate.get("credentials"))):
        saved = admin_obj._saved() if admin_obj.cfg.get("_config_path") else {}
        found_saved = next(
            (p for p in (saved.get("providers") or []) if isinstance(p, dict) and str(p.get("id") or "").strip().lower() == existing_id),
            None,
        )
        if found_saved:
            candidate = _merge_provider_patch(found_saved, candidate)

    candidate.setdefault("id", existing_id or "probe")
    try:
        meta, records, secrets_found = presets.expand_provider_to_accounts(candidate, "provider_test")
    except ValueError as exc:
        raise apierr.bad_request(str(exc))
    _apply_custom_preset_override(candidate, meta, records)

    admin_obj._mask_tokens(secrets_found)
    t0 = time.monotonic()
    try:
        coro = discovery.discover_provider_models(records[0], timeout=12.0)
        if getattr(admin_obj.runtime, "loop", None) is not None:
            found = admin_obj.runtime.submit(coro).result(timeout=15.0)
        else:
            found = asyncio.run(coro)
        latency_ms = round((time.monotonic() - t0) * 1000)
        return admin_obj._safe({
            "ok": True,
            "latency_ms": latency_ms,
            "driver": meta["driver"],
            "base_url": meta["base_url"],
            "preset": meta.get("preset"),
            "models_count": len(found),
            "models": found[:100],
        })
    except Exception as exc:  # noqa: BLE001
        latency_ms = round((time.monotonic() - t0) * 1000)
        return admin_obj._safe({
            "ok": False,
            "latency_ms": latency_ms,
            "driver": meta["driver"],
            "base_url": meta["base_url"],
            "preset": meta.get("preset"),
            "error": str(exc)[:240],
        })


def discover_provider(admin_obj, provider_id: str | None = None) -> dict:
    """Read /models again (one provider, or every provider and models.dev); at most DISCOVERY_WAIT seconds.

    A refresh still running then is answered `pending`: it goes on and fills the catalog, and a second one is not started
    beside it. A bare TimeoutError used to come out of here: a 500 and a red http.exception, while the refresh went on and
    the next click started another (audit A07-07)."""
    refresher = getattr(admin_obj.runtime, "provider_catalog", None)
    if refresher is None:
        raise apierr.service_unavailable("Provider catalog refresher is unavailable")
    pid = provider_id.strip().lower() if provider_id else None
    runtime = admin_obj.runtime
    if getattr(runtime, "loop", None) is None or not callable(getattr(runtime, "submit", None)):
        res = refresher.refresh_now(pid, timeout=DISCOVERY_WAIT)
        return admin_obj._safe({"ok": True, **res})
    with _DISCOVERY_LOCK:
        running = getattr(admin_obj, "_catalog_refresh", None)
        if running is not None and not running.done():
            return {"ok": False, "pending": True,
                    "error": "Обновление каталога ещё идёт: его результат появится в каталоге сам"}
        running = admin_obj._catalog_refresh = runtime.submit(refresher.refresh_async(pid))
    try:
        res = running.result(timeout=DISCOVERY_WAIT)
    except TimeoutError:
        return {"ok": False, "pending": True,
                "error": "Обновление каталога идёт дольше %d с: оно продолжается, результат появится в каталоге сам"
                         % DISCOVERY_WAIT}
    except Exception as exc:  # noqa: BLE001 - a failed refresh is an answer, not a server error
        return admin_obj._safe({"ok": False, "error": (str(exc) or type(exc).__name__)[:240]})
    return admin_obj._safe({"ok": True, **res})


def ping_model(admin_obj, data: dict) -> dict:
    """Run a 16-token live health probe against any routable model or route alias."""
    if not isinstance(data, dict):
        raise apierr.bad_request("Request body must be a JSON object")
    model_id = str(data.get("model") or "").strip()
    if not model_id or len(model_id) > 160:
        raise apierr.bad_request("'model' must be a non-empty model id", param="model")

    t0 = time.monotonic()
    token = client_context.bind({"request_id": "admin-ping", "request_purpose": "connection-probe"})
    try:
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            "max_tokens": 16,
            "stream": False,
        }
        parsed = validate.parse_chat(body, admin_obj.cfg)
        parsed.conversation_key = None
        parsed.parent_conversation_key = None
        parsed.request_id = "admin-ping"
        parsed.dialect = "chat"
        parsed.request_purpose = "connection-probe"
        parsed.client_task = None
        parsed.thinking_placeholder = ""
        events_q: queue.Queue = queue.Queue()
        holder: dict = {
            "reason": "stop",
            "session": None,
            "cancel_event": threading.Event(),
            "_request_started_monotonic": t0,
        }
        fut = admin_obj.runtime.submit_turn(parsed, events_q, holder)

        ttft_ms = None
        reply_parts: list[str] = []
        cost_usd = None
        deadline = t0 + 25.0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                holder["cancel_event"].set()
                fut.cancel()
                raise TimeoutError("probe timed out after 25s")
            try:
                ev = events_q.get(timeout=min(0.25, left))
            except queue.Empty:
                if fut.done() and events_q.empty():
                    break
                continue
            if ev is None:
                break
            etype = ev.get("type")
            if etype == "error":
                raise ev["error"]
            if etype in ("text", "delta", "thinking") and ttft_ms is None:
                ttft_ms = round((time.monotonic() - t0) * 1000)
            if etype in ("text", "delta"):
                chunk = ev.get("delta") if "delta" in ev else ev.get("text")
                if chunk:
                    reply_parts.append(str(chunk))
            if etype == "usage" and isinstance(ev.get("cost_usd"), (int, float)):
                cost_usd = round(float(ev["cost_usd"]), 6)

        fut.result(timeout=2.0)
        latency_ms = round((time.monotonic() - t0) * 1000)
        return admin_obj._safe({
            "ok": True,
            "model": parsed.model,
            "latency_ms": latency_ms,
            "ttft_ms": ttft_ms if ttft_ms is not None else latency_ms,
            "reply": "".join(reply_parts).strip()[:120] or "(empty)",
            "cost_usd": cost_usd,
        })
    except Exception as exc:  # noqa: BLE001
        latency_ms = round((time.monotonic() - t0) * 1000)
        msg = exc.message if isinstance(exc, apierr.ApiError) else str(exc)
        return admin_obj._safe({
            "ok": False,
            "model": model_id,
            "latency_ms": latency_ms,
            "error": str(msg)[:240],
        })
    finally:
        client_context.reset(token)


# -- Routes / Model Aliases (`routes[]`) --------------------------------------

def _validate_route(data: dict, fixed_id: str | None = None) -> dict:
    if not isinstance(data, dict):
        raise apierr.bad_request("Route must be an object")
    rid = fixed_id or str(data.get("id") or data.get("alias") or "").strip()
    if not rid or not ROUTE_ID_PATTERN.fullmatch(rid):
        raise apierr.bad_request("'id' must be 1..80 ASCII letters, digits, '.', '_', '-', ':' or '/'", param="id")
    targets = data.get("targets") or data.get("models")
    if not isinstance(targets, list) or not targets:
        raise apierr.bad_request("'targets' must be a non-empty list of model IDs", param="targets")
    clean_targets = []
    for t in targets:
        if not isinstance(t, str) or not t.strip() or len(t.strip()) > 160:
            raise apierr.bad_request("Each target in 'targets' must be a non-empty model ID string", param="targets")
        s = t.strip()
        if s not in clean_targets:
            clean_targets.append(s)
    if not clean_targets:
        raise apierr.bad_request("'targets' must contain at least one model ID", param="targets")
    strategy = str(data.get("strategy") or "priority").strip().lower()
    if strategy not in ROUTE_STRATEGIES:
        raise apierr.bad_request(f"'strategy' must be one of {', '.join(ROUTE_STRATEGIES)}", param="strategy")
    label = str(data.get("label") or rid).strip()[:80]
    return {"id": rid, "label": label, "targets": clean_targets, "strategy": strategy}


def _enrich_route(route: dict, admin_obj) -> dict:
    all_models = {entry["id"]: entry for entry in models.catalog(admin_obj.cfg)}
    target_status = []
    for tid in route.get("targets") or []:
        entry = all_models.get(tid) or models.universal_entry(tid, admin_obj.cfg)
        routable = bool(entry and admin_obj._routable(entry))
        target_status.append({"id": tid, "known": entry is not None, "routable": routable})
    return {
        **route,
        "routable": any(t["routable"] for t in target_status),
        "target_status": target_status,
    }


def list_routes(admin_obj) -> dict:
    saved = admin_obj._saved() if admin_obj.cfg.get("_config_path") else {}
    raw_routes = saved.get("routes") if isinstance(saved.get("routes"), list) else (admin_obj.cfg.get("routes") or [])
    items = [_enrich_route(r, admin_obj) for r in raw_routes if isinstance(r, dict) and r.get("id")]
    return admin_obj._safe({"routes": items, "strategies": list(ROUTE_STRATEGIES)})


def create_route(admin_obj, data: dict) -> dict:
    clean = _validate_route(data)
    rid = clean["id"]
    with _admin_lock(admin_obj):
        with admin_obj._transaction(refreshers=()) as (path, saved):
            routes = saved.get("routes")
            if not isinstance(routes, list):
                routes = saved["routes"] = []
            if any(isinstance(r, dict) and str(r.get("id") or "") == rid for r in routes):
                raise apierr.conflict(f"Route alias '{rid}' already exists")
            routes.append(clean)
            admin_obj._write(path, saved)
            admin_obj.cfg["routes"] = copy.deepcopy(routes)
        admin_obj.cfg["_route_aliases"] = router.build_route_aliases(admin_obj.cfg, models)
    journal.emit("admin.route", f"Создан маршрут-алиас {rid} -> {', '.join(clean['targets'][:3])}", "success", route=rid)
    return admin_obj._safe({"route": _enrich_route(clean, admin_obj)})


def update_route(admin_obj, route_id: str, data: dict) -> dict:
    if not isinstance(data, dict) or not data:
        raise apierr.bad_request("Expected route fields to update")
    with _admin_lock(admin_obj):
        with admin_obj._transaction(refreshers=()) as (path, saved):
            routes = saved.get("routes") if isinstance(saved.get("routes"), list) else []
            idx = next((i for i, r in enumerate(routes) if isinstance(r, dict) and str(r.get("id") or "") == route_id), None)
            if idx is None:
                raise apierr.not_found(f"Unknown route '{route_id}'")
            merged = {**routes[idx], **data, "id": route_id}
            clean = _validate_route(merged, fixed_id=route_id)
            routes[idx] = clean
            admin_obj._write(path, saved)
            admin_obj.cfg["routes"] = copy.deepcopy(routes)
        admin_obj.cfg["_route_aliases"] = router.build_route_aliases(admin_obj.cfg, models)
    journal.emit("admin.route", f"Обновлён маршрут-алиас {route_id}", "info", route=route_id)
    return admin_obj._safe({"route": _enrich_route(clean, admin_obj)})


def delete_route(admin_obj, route_id: str) -> dict:
    with _admin_lock(admin_obj):
        with admin_obj._transaction(refreshers=()) as (path, saved):
            routes = saved.get("routes") if isinstance(saved.get("routes"), list) else []
            remaining = [r for r in routes if not (isinstance(r, dict) and str(r.get("id") or "") == route_id)]
            if len(remaining) == len(routes):
                raise apierr.not_found(f"Unknown route '{route_id}'")
            saved["routes"] = remaining
            admin_obj._write(path, saved)
            admin_obj.cfg["routes"] = copy.deepcopy(remaining)
        admin_obj.cfg["_route_aliases"] = router.build_route_aliases(admin_obj.cfg, models)
    journal.emit("admin.route", f"Удалён маршрут-алиас {route_id}", "warn", route=route_id)
    return {"ok": True}


# -- Virtual Client Keys (`client_keys[]` / AccessKeys) -----------------------

def _slug_key_id(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")[:28]
    suffix = secrets.token_hex(3)
    return f"{slug}-{suffix}" if slug else f"key-{suffix}"


def _public_client_keys(admin_obj, items: list[dict]) -> list[dict]:
    """The client keys as the admin shows them, each preview made from the key itself (client_key_preview)."""
    raw = {str(entry.get("id")): entry.get("key") for entry in admin_obj.cfg.get("client_keys") or []
           if isinstance(entry, dict)}
    return [dict(item, key_preview=client_key_preview(raw.get(str(item.get("id"))))) for item in items]


def list_client_keys(admin_obj) -> dict:
    mgr = getattr(admin_obj.runtime, "client_keys", None)
    items = mgr.list_public() if mgr is not None else []
    return admin_obj._safe({"client_keys": _public_client_keys(admin_obj, items)})


def create_client_key(admin_obj, data: dict) -> dict:
    if not isinstance(data, dict):
        raise apierr.bad_request("Request body must be a JSON object")
    label = str(data.get("label") or "").strip()
    if not label:
        raise apierr.bad_request("'label' is required", param="label")
    kid = str(data.get("id") or "").strip() or _slug_key_id(label)
    raw_key = str(data.get("key") or "").strip() or auth.generate_client_key()
    candidate = {
        "id": kid,
        "label": label,
        "key": raw_key,
        "enabled": data["enabled"] if "enabled" in data else True,
        "allowed_models": data.get("allowed_models"),
        "rpm_limit": data.get("rpm_limit"),
        "max_budget_usd": data.get("max_budget_usd"),
        "max_request_minara": data.get("max_request_minara"),
        "max_day_minara": data.get("max_day_minara"),
        "allow_cost_confirm": data["allow_cost_confirm"] if "allow_cost_confirm" in data else False,
    }
    try:
        clean = auth.validate_client_key_entry(candidate, "client_key")
    except ValueError as exc:
        raise apierr.bad_request(str(exc))

    with _admin_lock(admin_obj):
        with admin_obj._transaction(refreshers=()) as (path, saved):
            ck_list = saved.get("client_keys")
            if not isinstance(ck_list, list):
                ck_list = saved["client_keys"] = []
            if any(isinstance(k, dict) and str(k.get("id") or "") == clean["id"] for k in ck_list):
                raise apierr.conflict(f"Client key '{clean['id']}' already exists")
            ck_list.append(clean)
            admin_obj._write(path, saved)
            admin_obj.cfg["client_keys"] = copy.deepcopy(ck_list)

        admin_obj._mask_tokens([raw_key])
        admin_obj.runtime.client_keys.reload(admin_obj.cfg["client_keys"])
        public = next((item for item in _public_client_keys(admin_obj, admin_obj.runtime.client_keys.list_public())
                       if item["id"] == clean["id"]), None)
    journal.emit("admin.client_key", f"Создан клиентский ключ {clean['label']} ({clean['id']})", "success", key_id=clean["id"])
    # Return `raw_key` ONCE (outside _safe) so the operator can copy it on creation!
    resp = admin_obj._safe({"client_key": public})
    resp["raw_key"] = raw_key
    return resp


def update_client_key(admin_obj, key_id: str, data: dict) -> dict:
    if not isinstance(data, dict) or not data:
        raise apierr.bad_request("Expected client key fields to update")
    rotated_raw = None
    with _admin_lock(admin_obj):
        with admin_obj._transaction(refreshers=()) as (path, saved):
            ck_list = saved.get("client_keys") if isinstance(saved.get("client_keys"), list) else []
            idx = next((i for i, k in enumerate(ck_list) if isinstance(k, dict) and str(k.get("id") or "") == key_id), None)
            if idx is None:
                raise apierr.not_found(f"Unknown client key '{key_id}'")
            merged = copy.deepcopy(ck_list[idx])
            for field in (
                "label", "enabled", "allowed_models", "rpm_limit", "max_budget_usd",
                "max_request_minara", "max_day_minara", "allow_cost_confirm",
            ):
                if field in data:
                    merged[field] = data[field]
            if data.get("rotate_key") is True:
                rotated_raw = auth.generate_client_key()
                merged["key"] = rotated_raw
            merged["id"] = key_id
            try:
                clean = auth.validate_client_key_entry(merged, f"client_keys[{key_id}]")
            except ValueError as exc:
                raise apierr.bad_request(str(exc))
            ck_list[idx] = clean
            admin_obj._write(path, saved)
            admin_obj.cfg["client_keys"] = copy.deepcopy(ck_list)

        if rotated_raw:
            admin_obj._mask_tokens([rotated_raw])
        admin_obj.runtime.client_keys.reload(admin_obj.cfg["client_keys"])
        public = next((item for item in _public_client_keys(admin_obj, admin_obj.runtime.client_keys.list_public())
                       if item["id"] == key_id), None)
    journal.emit("admin.client_key", f"Обновлён клиентский ключ {clean['label']} ({key_id})", "info", key_id=key_id)
    resp = admin_obj._safe({"client_key": public})
    if rotated_raw:
        resp["raw_key"] = rotated_raw
    return resp


def delete_client_key(admin_obj, key_id: str) -> dict:
    with _admin_lock(admin_obj):
        with admin_obj._transaction(refreshers=()) as (path, saved):
            ck_list = saved.get("client_keys") if isinstance(saved.get("client_keys"), list) else []
            remaining = [k for k in ck_list if not (isinstance(k, dict) and str(k.get("id") or "") == key_id)]
            if len(remaining) == len(ck_list):
                raise apierr.not_found(f"Unknown client key '{key_id}'")
            saved["client_keys"] = remaining
            admin_obj._write(path, saved)
            admin_obj.cfg["client_keys"] = copy.deepcopy(remaining)

        admin_obj.runtime.client_keys.reload(admin_obj.cfg["client_keys"])
    journal.emit("admin.client_key", f"Удалён клиентский ключ {key_id}", "warn", key_id=key_id)
    return {"ok": True}


# -- OpenCode 1-click Sync ----------------------------------------------------

def sync_opencode_now(admin_obj) -> dict:
    """Write the relay's models into OpenCode's settings file and the DeepSeek Harness profiles now (the relay does it
    after each catalog look by itself), and say what was done.

    The button ran a sync with a lock of its own beside the relay's, so both could write OpenCode's file at once, and its
    "ok" read OpenCode's summary alone: a failed Harness profile, or a list already current (nothing written), was
    reported as "Список моделей обновлён" (audit A14-07). It now waits for the relay's own sync under way, its answer is
    ok only when every file was synced, and `written` / `changed` say whether a file was rewritten."""
    path = client_sync.settings_path(admin_obj.cfg)
    if path is None or not path.is_file():
        return {"ok": False, "reason": "Файл настроек OpenCode (opencode.jsonc / opencode.json) не найден"}
    syncer = getattr(admin_obj.runtime, "client_sync", None)
    runner = getattr(syncer, "_run", None)
    forced_cfg = {**admin_obj.cfg, "opencode_sync": True}
    manual_sync = client_sync.OpenCodeSync(forced_cfg, runner=runner)
    shared_lock = getattr(syncer, "_lock", None)
    if shared_lock is not None:
        manual_sync._lock = shared_lock  # one write at a time with the relay's own sync after a catalog look
    res = manual_sync("admin")
    if syncer is not None:
        syncer.last = manual_sync.last
        syncer.last_harness = manual_sync.last_harness
    if not res:
        return {"ok": False, "reason": "Синхронизация не выполнена (нет записываемого config.json)"}
    harness = [item for item in res.get("harness") or [] if isinstance(item, dict)]
    failed = bool(res.get("error")) or any(item.get("error") for item in harness)
    written = bool(res.get("written")) or any(item.get("written") for item in harness)
    changed = bool(res.get("changed")) or any(item.get("changed") for item in harness)
    return admin_obj._safe({"ok": not failed, "written": written, "changed": changed, "result": res})
