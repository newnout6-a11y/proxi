"""Per-account reporting shared by the relay API (/v1/balance, /v1/usage, /health) and the admin API."""
import concurrent.futures

import console_log
from minara_catalog import CREDITS_PER_USD
from accounts import UNNAMED, account_tag, enabled
from session import SessionPool

# Balance lookups in flight for one report. Not account_refresh_parallel: that bounds the dispatcher's
# background renewals, while a report is a client waiting on every account's answer.
BALANCE_PARALLEL = 8


def account_managers(runtime):
    """(backend, CreditsManager) per account; the primary account follows runtime.credits."""
    rows = []
    for backend in runtime.accounts.backends:
        manager = runtime.credits if backend is runtime.backend else runtime.accounts.managers.get(backend)
        if manager is not None:
            rows.append((backend, manager))
    return rows or [(runtime.backend, runtime.credits)]


def known(value):
    return console_log.number(value, signed=True)


def account_states(runtime):
    return {backend: row for backend, row in zip(runtime.accounts.backends, runtime.accounts.status())}


def collect(runtime, *, force_refresh=False):
    """One entry per account: balance info, dispatcher state and routing eligibility.

    Routing mirrors SessionPool._next_backend: active accounts first; when none is
    active, exhausted accounts with a positive or unknown balance (the failover
    reserve) are still served. A lone auth_dead account is also tried there as a
    last resort, but it is deliberately reported as not routable: a signed-out
    account cannot be called healthy. A rate_limited account (Retool's hourly
    managed-key limit) is parked until `rate_limited_until` and is never a reserve.
    """
    rows = account_managers(runtime)
    states = account_states(runtime)
    active = [backend for backend, _ in rows
              if getattr(backend, "unavailable", None) is None and enabled(backend)]
    spare = [] if active else SessionPool._reserve([backend for backend, _ in rows])

    def fetch(row):
        backend, manager = row
        snapshot = getattr(manager, "balance_snapshot", None)
        if not enabled(backend) and snapshot is not None:
            # A switched-off account stays off the network (a stale cache would refetch and may
            # renew the session); only an explicit admin balance check reaches Retool.
            info = dict(snapshot())
            info.setdefault("available", None)
            info["stale"] = bool(info.get("stale")) and info["available"] is not None
            return info
        return manager.get_balance(force_refresh=force_refresh)

    if len(rows) == 1:
        infos = [fetch(rows[0])]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(BALANCE_PARALLEL, len(rows))) as pool:
            infos = list(pool.map(fetch, rows))
    entries = []
    for (backend, manager), info in zip(rows, infos):
        status = states.get(backend) or {}
        state = status.get("state") or ((getattr(backend, "unavailable", None) or {}).get("reason") or "active")
        available = known(info.get("available"))
        entries.append({
            "backend": backend, "manager": manager, "info": info, "state": state,
            "auth_failures": status.get("auth_failures") or 0, "available": available,
            "rate_limited_until": status.get("rate_limited_until"),
            "fresh": available is not None and not info.get("stale"),
            "balance_supported": info.get("balance_supported", True),
            "routable": backend in active or backend in spare, "reserve": backend in spare,
        })
    return entries


def serving_entry(entries):
    """The account /health describes: a routable one first, then an enabled one, then the first.

    The first account in config.json is not special: it may be switched off while the pool serves.
    """
    return (next((entry for entry in entries if entry["routable"]), None)
            or next((entry for entry in entries if enabled(entry["backend"])), None) or entries[0])


def summarize(entries):
    """Readiness figures shared by /health and the admin overview."""
    states = {}
    for entry in entries:
        states[entry["state"]] = states.get(entry["state"], 0) + 1
    until = [entry["rate_limited_until"] for entry in entries if entry.get("rate_limited_until")]
    return {"ok": any(entry["routable"] and (entry["fresh"] or not entry.get("balance_supported", True)) for entry in entries),
            "balance_known": any(entry["fresh"] for entry in entries),
            "total": len(entries), "routable": sum(entry["routable"] for entry in entries),
            "reserve": sum(entry["reserve"] for entry in entries), "states": states,
            "rate_limited_until": min(until) if until else None}


def aggregate_balance(infos):
    """Top-level /v1/balance figures: sums over accounts whose balance is known.

    `unknown_accounts` counts the accounts left out; `partial` is true when the sums cover only part of
    the pool (an unknown balance, or a known balance without a monthly allocation), so a lower total is
    never mistaken for the whole pool.

    Only Retool credits are summed here. Minara counts its own credits (220 per US dollar): they go in `minara`, a total
    of their own, and never into Retool's figures."""
    minara = [info for info in infos if info.get("unit") == "minara_credits"]
    infos = [info for info in infos if info.get("unit") != "minara_credits"]
    report = _aggregate(infos)
    if minara:
        known_minara = [info for info in minara if known(info.get("available")) is not None]
        credits = round(sum(known(info["available"]) for info in known_minara), 4) if known_minara else None
        report["minara"] = {"available": credits, "unit": "minara_credits",
                            "usd": round(credits / CREDITS_PER_USD, 4) if credits is not None else None,
                            "accounts_total": len(minara), "unknown_accounts": len(minara) - len(known_minara)}
    return report


def _aggregate(infos):
    with_balance = [info for info in infos if known(info.get("available")) is not None]
    allocation = [value for value in (known(info.get("monthly_allocation")) for info in with_balance)
                  if value is not None]
    total = round(sum(known(info.get("available")) for info in with_balance), 4) if with_balance else None
    return {
        "available": total,
        "monthly_allocation": round(sum(allocation), 4) if allocation else None,
        "used_month": round(sum(info.get("used_month") or 0.0 for info in with_balance), 4) if with_balance else None,
        "reset_date": next((info["reset_date"] for info in with_balance if info.get("reset_date")), None),
        "is_low": any(info.get("is_low") for info in with_balance),
        "session_used": (round(sum(known(info.get("session_used")) or 0.0 for info in infos), 4)
                         if any(info.get("balance_supported", True) for info in infos) else None),
        "stale": any(info.get("stale") for info in with_balance),
        "unreported_usage": any(info.get("unreported_usage") for info in infos),
        "accounts_total": len(infos),
        "unknown_accounts": len(infos) - len(with_balance),
        "partial": bool(with_balance) and (len(with_balance) < len(infos) or len(allocation) < len(with_balance)),
    }


def balance_report(entries):
    accounts = []
    for entry in entries:
        info = entry["info"]
        item = {"account": account_tag(entry["backend"], UNNAMED), "state": entry["state"],
                "available": entry["available"], "upstream": entry["backend"].cfg.get("upstream", "retool"),
                "unit": info.get("unit", "credits"), "balance_supported": entry.get("balance_supported", True),
                "monthly_allocation": info.get("monthly_allocation"),
                "reset_date": info.get("reset_date"), "stale": bool(info.get("stale"))}
        error = info.get("error") or info.get("stale_error")
        if error:
            item["error"] = error
        if entry.get("rate_limited_until"):
            item["rate_limited_until"] = entry["rate_limited_until"]
        accounts.append(item)
    report = aggregate_balance([entry["info"] for entry in entries])
    report["accounts"] = accounts
    if report["available"] is None:
        report["error"] = next((item["error"] for item in accounts if item.get("error")), None)
    return report


def usage_report(runtime, rows=None):
    rows, recent, accounts = rows or account_managers(runtime), [], []
    totals = {"session_used": 0.0, "turns": 0, "recent_total": 0.0}
    available, unreported = [], False
    credit_accounts = 0
    for backend, manager in rows:
        usage = manager.usage()
        tag = account_tag(backend, UNNAMED)
        items = [dict(item, account=tag) for item in usage.get("recent") or []]
        recent.extend(items)
        used = console_log.number(usage.get("session_used")) or 0.0
        turns = int(usage.get("turns") or 0)
        monetary = backend.cfg.get("upstream") == "minara"
        credit_accounts += not monetary
        totals["session_used"] += used if not monetary else 0
        totals["turns"] += turns
        totals["recent_total"] += console_log.number(usage.get("recent_total")) or sum(
            console_log.number(item.get("credits")) or 0.0 for item in items)
        unreported = unreported or bool(usage.get("unreported_usage"))
        balance = known(usage.get("available"))
        if balance is not None:
            available.append(balance)
        accounts.append({"account": tag, "session_used": None if monetary else round(used, 4),
                         "turns": turns, "available": balance,
                         **({"upstream": "minara", **{key: usage[key] for key in (
                             "attempts", "provider_cost", "provider_cost_currency", "provider_cost_complete",
                             "provider_cost_records") if key in usage}} if monetary else {})})
    recent.sort(key=lambda item: item.get("ts") or 0)
    return {"session_used": round(totals["session_used"], 4) if credit_accounts else None, "turns": totals["turns"],
            "recent": recent[-50:], "recent_total": round(totals["recent_total"], 4) if credit_accounts else None,
            "unreported_usage": unreported,
            "available": round(sum(available), 4) if available else None,
            "accounts": accounts}
