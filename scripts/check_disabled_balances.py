"""Real balances of the accounts config.json has switched off (enabled: false), asked from each upstream.

    python scripts/check_disabled_balances.py                 # disabled accounts only
    python scripts/check_disabled_balances.py --all           # every account
    python scripts/check_disabled_balances.py --json out.json # write the rows (no secrets) to a new file

Read-only: nothing is renewed and config.json is not touched. Minara accounts are asked GET /auth/me through
accounts.MinaraAccount; Retool accounts GET /api/credits/balance with the stored cookie, and are not refreshed, so a
Retool "auth failed" may only mean an access token the relay would renew itself. The checks use the configured proxy.
Accounts are shown by label only.
"""
import argparse
import json
import math
import os
import pathlib
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts  # noqa: E402
from auth import upstream_headers  # noqa: E402
from drivers import curl_pool  # noqa: E402

LOW_MINARA = 50.0   # credits; a fresh Minara plan is 300
LOW_RETOOL = 5.0
MIN_RETOOL = 20.0   # --groups: a Retool account counts as good above this
MIN_MINARA = 0.0    # ... and a Minara one above this
TRANSPORT_OPTIONS = ("proxy_url", "impersonate", "use_system_proxy", "verify_tls", "ua", "retool_http_backend")


def resolve(record):
    cfg = dict(record)
    env = cfg.get("bearer_token_env")
    if env and not cfg.get("bearer_token"):
        cfg["bearer_token"] = os.environ.get(env, "")
    return cfg


def check_minara(cfg):
    info = accounts.MinaraAccount(cfg).get_balance(force_refresh=True)
    error, value = info.get("error"), info.get("available")
    expires = info.get("expires_at")
    row = {"state": "error", "balance": value, "unit": "minara credits", "error": error, "expires_at": expires}
    if error in ("token_expired",) or (error and "auth" in str(error).lower()):
        row["state"] = "dead"
    elif error:
        row["state"] = "error"
    elif value is None:
        row["state"] = "unknown"
    else:
        row["state"] = "empty" if value <= 0 else "low" if value < LOW_MINARA else "ok"
    return row


def check_retool(cfg):
    row = {"state": "error", "balance": None, "unit": "retool credits", "error": None, "expires_at": None}
    if cfg.get("retool_http_backend") == "urllib" and cfg.get("proxy_url"):
        # The legacy urllib backend ignores per-account proxies. Do not send this cookie by another route.
        row["error"] = "Retool proxy requires retool_http_backend=curl_cffi"
        return row
    try:
        with curl_pool.retool_open(cfg, cfg["base"].rstrip("/") + "/api/credits/balance",
                                  headers=upstream_headers(cfg), timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        row.update(state="dead" if exc.code in (401, 403) else "error", error="HTTP %s" % exc.code)
        return row
    except Exception as exc:  # noqa: BLE001
        row["error"] = type(exc).__name__
        return row
    org = data.get("org") if isinstance(data, dict) else None
    balance = org.get("balance") if isinstance(org, dict) else None
    try:
        balance = math.nan if isinstance(balance, bool) else float(balance)
    except (TypeError, ValueError, OverflowError):
        balance = math.nan
    if not math.isfinite(balance):
        row.update(state="unknown", error="no balance in reply")
        return row
    row["balance"] = round(balance, 4)
    row["state"] = "empty" if balance <= 0 else "low" if balance < LOW_RETOOL else "ok"
    return row


def check(record):
    cfg = resolve(record)
    kind = cfg.get("upstream", "retool")
    base = {"label": cfg.get("label") or cfg.get("account_id") or "?", "upstream": kind,
            "enabled": bool(cfg.get("enabled", True))}
    try:
        base.update(check_minara(cfg) if kind == "minara" else check_retool(cfg))
    except Exception as exc:  # noqa: BLE001
        base.update(state="error", balance=None, unit=None, error=type(exc).__name__, expires_at=None)
    return base


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--all", action="store_true", help="check enabled accounts too")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--groups", action="store_true", help="list good / logged-out / the rest separately")
    parser.add_argument("--json", metavar="FILE", help="write the rows to a new FILE (never overwrite)")
    args = parser.parse_args()
    if args.json and os.path.lexists(args.json):
        parser.error("--json output already exists; choose a new file")

    saved = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))
    records = saved.get("accounts") or []
    transport = {key: saved[key] for key in TRANSPORT_OPTIONS if key in saved}
    todo = [dict(transport, **r) for r in records if args.all or not r.get("enabled", True)]
    print("checking %d of %d accounts ..." % (len(todo), len(records)), flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        rows = list(pool.map(check, todo))

    rows.sort(key=lambda r: (r["upstream"], -(r["balance"] if r["balance"] is not None else -1e9)))
    now = time.time()
    for r in rows:
        left = ""
        if r["expires_at"]:
            days = (r["expires_at"] - now) / 86400
            left = "token %s" % ("EXPIRED" if days <= 0 else "%.1fd" % days)
        balance = "-" if r["balance"] is None else "%.2f" % r["balance"]
        print("%-8s %-22s %-8s %10s  %s %s" % (r["upstream"], r["label"][:22], r["state"], balance,
                                              left, r["error"] or ""))

    print()
    for kind in ("minara", "retool"):
        part = [r for r in rows if r["upstream"] == kind]
        if not part:
            continue
        states = {}
        for r in part:
            states[r["state"]] = states.get(r["state"], 0) + 1
        usable = [r for r in part if r["state"] in ("ok", "low")]
        total = sum(r["balance"] for r in usable)
        print("%s: %d checked, %s; usable %d with %.2f credits in total"
              % (kind, len(part), ", ".join("%s=%d" % kv for kv in sorted(states.items())), len(usable), total))

    if args.groups:
        def show(title, part):
            print("\n== %s: %d ==" % (title, len(part)))
            for r in part:
                print("  %-8s %-22s %10s  %s" % (r["upstream"], r["label"][:22],
                                                 "-" if r["balance"] is None else "%.2f" % r["balance"],
                                                 "on" if r["enabled"] else "off"))
        good = [r for r in rows if r["state"] not in ("dead", "error", "unknown") and r["balance"] is not None and
                (r["balance"] > MIN_RETOOL if r["upstream"] == "retool" else r["balance"] > MIN_MINARA)]
        show("good (retool > %g, minara > %g)" % (MIN_RETOOL, MIN_MINARA), good)
        show("logged out", [r for r in rows if r["state"] == "dead"])
        rest = [r for r in rows if r not in good and r["state"] != "dead"]
        show("the rest (low, empty, unreadable)", rest)

    if args.json:
        report = json.dumps(rows, indent=2, ensure_ascii=False)
        try:
            with pathlib.Path(args.json).open("x", encoding="utf-8") as output:
                output.write(report)
        except FileExistsError:
            parser.error("--json output already exists; choose a new file")
        print("rows written to", args.json)


if __name__ == "__main__":
    main()
