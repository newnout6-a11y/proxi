"""Universal model catalog builder, route alias / fallback chain resolver, and load-balancing strategies."""
from __future__ import annotations

import copy
import random
import re
import threading

from accounts import priority as account_priority
from catalog import discovery, models_dev

_SAFE_ROUTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$")
_ROUTE_LOCK = threading.RLock()


def sanitize_public_model_id(provider_id: str, upstream_model: str) -> str:
    """Build `<provider_id>/<model_id>` guaranteed free of `@` (which is reserved for `@profile` virtual models)."""
    clean_model = upstream_model.strip().replace("@", "-")
    if clean_model.lower().startswith(f"{provider_id.lower()}/"):
        clean_model = clean_model[len(provider_id) + 1 :]
    return f"{provider_id}/{clean_model}"


def _manual_descriptors(models_spec) -> tuple[list[str], dict[str, dict]]:
    """Normalize `provider['models']` (either a list of strings/dicts or OpenCode `{id: {name, limit, cost}}` dict)."""
    order: list[str] = []
    overrides: dict[str, dict] = {}
    if isinstance(models_spec, list):
        for item in models_spec:
            if isinstance(item, str) and item.strip():
                mid = item.strip()
                if mid not in overrides:
                    order.append(mid)
                    overrides[mid] = {}
            elif isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                mid = item["id"].strip()
                if mid not in overrides:
                    order.append(mid)
                overrides[mid] = dict(item)
    elif isinstance(models_spec, dict):
        for raw_id, spec in models_spec.items():
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            mid = raw_id.strip()
            order.append(mid)
            if not isinstance(spec, dict):
                overrides[mid] = {}
                continue
            mapped = dict(spec)
            if "name" in spec and "label" not in spec:
                mapped["label"] = spec["name"]
            limit = spec.get("limit")
            if isinstance(limit, dict):
                if limit.get("context") and "context" not in mapped:
                    mapped["context"] = int(limit["context"])
                if limit.get("output") and "max_output" not in mapped:
                    mapped["max_output"] = int(limit["output"])
            cost = spec.get("cost")
            if isinstance(cost, dict) and "pricing" not in mapped and cost.get("input") is not None and cost.get("output") is not None:
                mapped["pricing"] = {
                    "input": float(cost["input"]),
                    "output": float(cost["output"]),
                    "cache_read": float(cost["cache_read"]) if cost.get("cache_read") is not None else None,
                    "cache_write": float(cost["cache_write"]) if cost.get("cache_write") is not None else None,
                }
            if "attachment" in spec and "vision" not in mapped:
                mapped["vision"] = bool(spec["attachment"])
            if "tool_call" in spec and "tools" not in mapped:
                mapped["tools"] = bool(spec["tool_call"])
            overrides[mid] = mapped
    return order, overrides


def build_single_provider_entry(provider_meta: dict, upstream_model: str, discovered_info: dict | None = None, override: dict | None = None) -> dict:
    """Merge models.dev metadata + live `/models` discovery + user overrides into a canonical relay catalog entry."""
    pid = provider_meta["id"]
    pname = provider_meta.get("name") or pid
    driver = provider_meta["driver"]
    enriched = models_dev.lookup_metadata(upstream_model, provider_meta.get("preset"))
    disc = discovered_info or {}
    ovr = override or {}

    family = str(ovr.get("provider") or enriched.get("family") or "openai")
    public_id = sanitize_public_model_id(pid, str(ovr.get("public_id") or upstream_model))
    base_label = str(ovr.get("label") or disc.get("label") or enriched.get("label") or upstream_model)
    full_label = base_label if base_label.lower().startswith(pname.lower()) else f"{pname}: {base_label}"

    context = ovr.get("context") or disc.get("context") or enriched.get("context")
    max_output = ovr.get("max_output") or disc.get("max_output") or enriched.get("max_output")

    vision = ovr["vision"] if "vision" in ovr else (disc["vision"] if "vision" in disc else enriched.get("vision", True))
    pdf = ovr["pdf"] if "pdf" in ovr else (disc["pdf"] if "pdf" in disc else enriched.get("pdf", True))
    tools = ovr["tools"] if "tools" in ovr else (disc["tools"] if "tools" in disc else enriched.get("tools", True))
    reasoning = ovr["reasoning"] if "reasoning" in ovr else (disc["reasoning"] if "reasoning" in disc else enriched.get("reasoning", True))

    levels = list(ovr.get("reasoning_effort_levels") if "reasoning_effort_levels" in ovr else enriched.get("reasoning_effort_levels") or [])
    if not reasoning:
        levels = []
    effort_control = "accepted-ignored" if not reasoning else ("selectable" if levels else "passthrough")
    default_effort = ovr.get("default_reasoning_effort") or ("medium" if "medium" in levels else (levels[0] if levels else None))

    pricing = ovr.get("pricing") if "pricing" in ovr else (disc.get("pricing") or enriched.get("pricing"))

    # Anthropic Messages API (and OpenRouter anthropic/*) rejects forced tool_choice (`any` / `tool`) when thinking is active
    default_forced_choice = not (driver == "anthropic-messages" or family == "anthropic")
    forced_tool_choice = bool(ovr.get("forced_tool_choice", default_forced_choice))

    return {
        "id": public_id,
        "label": full_label,
        "upstream": driver,
        "driver": driver,
        "provider_id": pid,
        "provider_name": pname,
        "upstream_model": upstream_model,
        "provider": family,
        "context": int(context) if context else None,
        "max_output": int(max_output) if max_output else None,
        "vision": bool(vision),
        "pdf": bool(pdf),
        "tools": bool(tools),
        "reasoning": bool(reasoning),
        "verbosity_levels": [],
        "reasoning_effort_levels": levels,
        "default_reasoning_effort": default_effort,
        "reasoning_effort_control": effort_control,
        "forced_tool_choice": forced_tool_choice,
        "pricing": copy.deepcopy(pricing) if isinstance(pricing, dict) else None,
        "cache_policy": provider_meta.get("cache_policy", "auto"),
        "route_status": "verified",
        "metadata_status": "enriched" if pricing else "discovered",
    }


def build_provider_catalog(cfg: dict, discovered_map: dict[str, list[dict]] | None = None) -> dict[str, dict]:
    """Build `<provider_id>/<model_id>` catalog entries for all configured `providers[]`."""
    providers_meta = cfg.get("_providers_meta") or {}
    if not providers_meta:
        return {}
    if discovered_map is None:
        discovered_map = cfg.get("_discovered_provider_models")
        if discovered_map is None:
            discovered_map = discovery.load_overlay(discovery.overlay_path(cfg.get("_config_path")))

    models_dev.apply_cache_file(models_dev.cache_path(cfg.get("_config_path")))

    catalog_entries: dict[str, dict] = {}
    for pid, meta in providers_meta.items():
        manual_order, manual_overrides = _manual_descriptors(meta.get("models"))
        disc_list = (discovered_map or {}).get(pid) or []
        disc_by_id = {item["id"]: item for item in disc_list if isinstance(item, dict) and isinstance(item.get("id"), str)}

        whitelist = meta.get("model_whitelist")
        blacklist = meta.get("model_blacklist")

        ordered_ids: list[str] = []
        for mid in manual_order:
            if mid not in ordered_ids:
                ordered_ids.append(mid)
        for mid in disc_by_id:
            if mid not in ordered_ids and discovery.matches_filters(mid, whitelist, blacklist):
                ordered_ids.append(mid)

        for mid in ordered_ids:
            entry = build_single_provider_entry(meta, mid, disc_by_id.get(mid), manual_overrides.get(mid))
            catalog_entries[entry["id"]] = entry
    return catalog_entries


def _target_lookup(cfg: dict, model_catalog_module):
    """The function that finds a route target by the name routes[] gives it, in the catalogs `cfg` has right now."""
    provider_entries = dict(cfg.get("_provider_models") or {})
    cat_lock = getattr(model_catalog_module, "_CATALOG_LOCK", None)
    if cat_lock is not None:
        with cat_lock:
            minara_entries = dict(cfg.get("_minara_models") or {})
            raw_by_id = getattr(model_catalog_module, "_BY_ID", None)
            retool_by_id = dict(raw_by_id) if raw_by_id is not None else {m["id"]: m for m in model_catalog_module.CATALOG}
            aliases = dict(model_catalog_module.ALIASES)
    else:
        minara_entries = dict(cfg.get("_minara_models") or {})
        raw_by_id = getattr(model_catalog_module, "_BY_ID", None)
        retool_by_id = dict(raw_by_id) if raw_by_id is not None else {m["id"]: m for m in list(model_catalog_module.CATALOG)}
        aliases = dict(model_catalog_module.ALIASES)

    def lookup_target(target_name: str) -> dict | None:
        t = (target_name or "").strip()
        if not t:
            return None
        if t in provider_entries:
            return provider_entries[t]
        if t in minara_entries:
            return minara_entries[t]
        if t in retool_by_id:
            return retool_by_id[t]
        canon = aliases.get(t)
        if canon and canon in retool_by_id:
            return retool_by_id[canon]
        # Allow matching `<provider_id>/<upstream_model>` when upstream_model has vendor prefix
        for entry in provider_entries.values():
            if entry.get("provider_id") and entry.get("upstream_model") and f"{entry['provider_id']}/{entry['upstream_model']}" == t:
                return entry
        if t.lower().startswith("minara/") and hasattr(model_catalog_module, "minara_entry"):
            # A Minara model named by its upstream id (minara/anthropic/claude-opus-5.5), as a request may name it.
            return model_catalog_module.minara_entry(t, cfg)
        return None

    return lookup_target


def build_route_aliases(cfg: dict, model_catalog_module) -> dict[str, dict]:
    """Build user-configured model aliases & fallback groups from `cfg['routes']`.

    What cannot be used is said, not dropped in silence (audit A08-04): `cfg['_route_warnings']` names each target that is
    no known model (a typo, another route, a Minara model no account serves yet) and each route left with none, and
    config.load shows them with its other warnings. `cfg['_route_sources']` keeps the catalog entries the targets were
    found as, so that refresh_route_aliases builds the routes again once a catalog changed."""
    with _ROUTE_LOCK:
        return _build_route_aliases_locked(cfg, model_catalog_module)


def _build_route_aliases_locked(cfg: dict, model_catalog_module) -> dict[str, dict]:
    routes_raw = cfg.get("routes")
    warnings: list[str] = []
    sources: list[tuple[str, dict | None]] = []
    if not isinstance(routes_raw, list) or not routes_raw:
        cfg["_route_warnings"] = warnings
        cfg["_route_sources"] = sources
        return {}

    lookup_target = _target_lookup(cfg, model_catalog_module)

    aliases: dict[str, dict] = {}
    for spec in routes_raw:
        if not isinstance(spec, dict):
            continue
        if spec.get("enabled", True) is False:
            continue
        route_id = str(spec.get("id") or spec.get("alias") or "").strip()
        if not route_id or "@" in route_id or not _SAFE_ROUTE_ID.fullmatch(route_id):
            warnings.append("routes[]: route id %r is not a valid model name; the route is not published" % (route_id,))
            continue
        raw_targets = spec.get("targets")
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        if not isinstance(raw_targets, list) or not raw_targets:
            warnings.append("routes[]: route '%s' has no targets; it is not published" % route_id)
            continue
        resolved_targets = []
        for t in raw_targets:
            if isinstance(t, str):
                found = lookup_target(t)
                sources.append((t, found))
                if found is None:
                    warnings.append("routes[]: route '%s': target '%s' is no known model (a typo, another route, or a "
                                    "model no account serves yet); it is left out" % (route_id, t))
                elif found not in resolved_targets:
                    resolved_targets.append(found)
        if not resolved_targets:
            warnings.append("routes[]: route '%s' has no known target; it is not published" % route_id)
            continue
        primary = copy.deepcopy(resolved_targets[0])
        primary["id"] = route_id
        primary["label"] = str(spec.get("label") or f"{route_id} (→ {resolved_targets[0]['id']})")[:80]
        primary["is_route_alias"] = True
        primary["route_strategy"] = str(spec.get("strategy") or "priority").lower()
        primary["route_targets"] = [copy.deepcopy(t) for t in resolved_targets]
        # Intersection or permissive union of capabilities: keep primary's context/effort, allow vision/tools if primary supports them
        aliases[route_id] = primary
    cfg["_route_warnings"] = warnings
    cfg["_route_sources"] = sources
    return aliases


def refresh_route_aliases(cfg: dict, model_catalog_module) -> dict[str, dict]:
    """`cfg['_route_aliases']`, built again first when a target's catalog entry changed since the last build.

    A route's targets are copies of the catalog entries of the moment it was built (config.load, an admin edit), and the
    Minara and Retool catalogs refresh themselves while the relay runs: a target kept its old price and effort levels until
    a restart, a model published later never became a target, and one Minara withdrew stayed one (audits A08-04, A12-10).
    This compares the entries each target name finds now with those it found (a few dict lookups) on every read of the
    routes (models.route_alias_catalog); what a rebuild newly cannot use goes to the journal."""
    with _ROUTE_LOCK:
        aliases = cfg.get("_route_aliases") or {}
        sources = list(cfg.get("_route_sources") or ())
        if not sources:
            return aliases
        lookup_target = _target_lookup(cfg, model_catalog_module)
        if all(lookup_target(name) is found for name, found in sources):
            return aliases
        before = set(cfg.get("_route_warnings") or ())
        aliases = _build_route_aliases_locked(cfg, model_catalog_module)
        cfg["_route_aliases"] = aliases
        fresh = [text for text in cfg.get("_route_warnings") or () if text not in before]
    if fresh:
        import console_log
        import journal

        for text in fresh:
            journal.emit("catalog.route_target", "Маршрут после обновления каталога: %s" % text, "warn")
            console_log.write("warning: " + text)
    return aliases


def select_candidate_backend(candidates: list, all_backends: list, counts: dict, inflight_counts: dict, rr_index: int,
                             strategy: str | None = None):
    """Select one backend among `candidates` honoring `fill-first`, `weighted`, or `least-inflight` strategy.

    `strategy` is a route alias's own (routes[].strategy, SessionPool._next_backend); without one the accounts' key-pool
    strategy (routing_strategy) decides."""
    if len(candidates) == 1:
        return candidates[0]

    # Top operator priority tier always wins first (so backup providers at priority=10 only fire when priority=0 is unavailable!)
    top_prio = min(account_priority(b) for b in candidates)
    top_tier = [b for b in candidates if account_priority(b) == top_prio]
    if len(top_tier) == 1:
        return top_tier[0]

    strategies = ({strategy} if strategy else
                  {str((getattr(b, "cfg", None) or {}).get("routing_strategy") or "").lower() for b in top_tier})
    if "least-inflight" in strategies:
        return min(
            top_tier,
            key=lambda b: (
                inflight_counts.get(b, 0),
                counts.get(b, 0),
                (all_backends.index(b) - rr_index) % max(1, len(all_backends)) if b in all_backends else 0,
            ),
        )
    if "weighted" in strategies:
        weights = [max(0.01, float((getattr(b, "cfg", None) or {}).get("weight", 1.0) or 1.0)) for b in top_tier]
        return random.choices(top_tier, weights=weights, k=1)[0]

    # Default / fill-first (when priorities are equal, least loaded + round-robin)
    return min(
        top_tier,
        key=lambda b: (
            counts.get(b, 0),
            (all_backends.index(b) - rr_index) % max(1, len(all_backends)) if b in all_backends else 0,
        ),
    )
