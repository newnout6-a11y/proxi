"""Per-account reporting shared by the relay API (/v1/balance, /v1/usage, /health) and the admin API."""
import concurrent.futures

import console_log
import journal
from accounts import enabled
from session import SessionPool


def account_tag(backend):
    cfg = getattr(backend, "cfg", None) or {}
    return journal.tag(cfg.get("account_id") or cfg.get("page_uuid")) or "account"


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
    account cannot be called healthy.
    """
    rows = account_managers(runtime)
    states = account_states(runtime)
    active = [backend for backend, _ in rows
              if getattr(backend, "unavailable", None) is None and enabled(backend)]
    spare = [] if active else SessionPool._reserve([backend for backend, _ in rows])

    def fetch(row):
        return row[1].get_balance(force_refresh=force_refresh)

    if len(rows) == 1:
        infos = [fetch(rows[0])]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
            infos = list(pool.map(fetch, rows))
    entries = []
    for (backend, manager), info in zip(rows, infos):
        status = states.get(backend) or {}
        state = status.get("state") or ((getattr(backend, "unavailable", None) or {}).get("reason") or "active")
        available = known(info.get("available"))
        entries.append({
            "backend": backend, "manager": manager, "info": info, "state": state,
            "auth_failures": status.get("auth_failures") or 0, "available": available,
            "fresh": available is not None and not info.get("stale"),
            "routable": backend in active or backend in spare, "reserve": backend in spare,
        })
    return entries


def summarize(entries):
    """Readiness figures shared by /health and the admin overview."""
    states = {}
    for entry in entries:
        states[entry["state"]] = states.get(entry["state"], 0) + 1
    return {"ok": any(entry["routable"] and entry["fresh"] for entry in entries),
            "balance_known": any(entry["fresh"] for entry in entries),
            "total": len(entries), "routable": sum(entry["routable"] for entry in entries),
            "reserve": sum(entry["reserve"] for entry in entries), "states": states}


def aggregate_balance(infos):
    """Top-level /v1/balance figures: sums over accounts whose balance is known."""
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
        "session_used": round(sum(known(info.get("session_used")) or 0.0 for info in infos), 4),
        "stale": any(info.get("stale") for info in with_balance),
        "unreported_usage": any(info.get("unreported_usage") for info in infos),
    }


def balance_report(entries):
    accounts = []
    for entry in entries:
        info = entry["info"]
        item = {"account": account_tag(entry["backend"]), "state": entry["state"],
                "available": entry["available"], "monthly_allocation": info.get("monthly_allocation"),
                "reset_date": info.get("reset_date"), "stale": bool(info.get("stale"))}
        error = info.get("error") or info.get("stale_error")
        if error:
            item["error"] = error
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
    for backend, manager in rows:
        usage = manager.usage()
        tag = account_tag(backend)
        items = [dict(item, account=tag) for item in usage.get("recent") or []]
        recent.extend(items)
        used = console_log.number(usage.get("session_used")) or 0.0
        turns = int(usage.get("turns") or 0)
        totals["session_used"] += used
        totals["turns"] += turns
        totals["recent_total"] += console_log.number(usage.get("recent_total")) or sum(
            console_log.number(item.get("credits")) or 0.0 for item in items)
        unreported = unreported or bool(usage.get("unreported_usage"))
        balance = known(usage.get("available"))
        if balance is not None:
            available.append(balance)
        accounts.append({"account": tag, "session_used": round(used, 4), "turns": turns, "available": balance})
    recent.sort(key=lambda item: item.get("ts") or 0)
    return {"session_used": round(totals["session_used"], 4), "turns": totals["turns"],
            "recent": recent[-50:], "recent_total": round(totals["recent_total"], 4),
            "unreported_usage": unreported,
            "available": round(sum(available), 4) if available else None,
            "accounts": accounts}
