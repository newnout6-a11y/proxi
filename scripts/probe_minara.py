"""Measure the Minara catalog and regenerate minara_models.json (the relay's Minara model list).

The relay publishes every model this file marks "ok" as minara/<name>; nothing else decides which Minara models exist
or what they can do. The checks are minara_probe's (the relay runs the same ones for a model Minara adds later, see
minara_refresh): tools, vision, thinking on every model; the effort ladder on one representative per family; channels on
chat and on /v1/messages for the Claude models Minara also serves on its Anthropic endpoint; a cache check on the other
Claude models. Each check is one or a few small paid requests on the account whose bearer token is given.

Usage (PowerShell):
  python scripts/probe_minara.py --token-file <file with one or more Minara bearer tokens> [--budget 150] [--only id ...]
  python scripts/probe_minara.py --regenerate     # rebuild minara_models.json from the saved results, no requests

Raw results are kept in --results (default: %TEMP%/minara-probe-results.json) and reused, so an interrupted run
continues where it stopped; --refresh re-measures. The token is read at run time and never written anywhere.
220 Minara credits are 1 USD of Minara's own price list (measured 2026-11-10).
"""
import argparse
import concurrent.futures as cf
import datetime
import json
import os
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import minara_probe  # noqa: E402
from minara_probe import (  # noqa: E402,F401 - the names tests and the effort study read from here
    CACHING_CHANNELS, CATALOG_FILE, CHANNEL_LEVELS, CREDITS_PER_USD, EFFORT_REPRESENTATIVES, EFFORTS, INHERIT, NOT_CHAT,
    OUTPUT_CEILING, Client, _family, cached_levels, effort_tiers, excluded, fetch_published, generate, merged_study,
    messages_offer, probe_cache, probe_channels, probe_effort, probe_messages, probe_thinking, probe_tools,
    probe_vision, public_id, tokens_from)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token-file", help="file with Minara bearer tokens, one per line (the richest one is used)")
    parser.add_argument("--results", default=os.path.join(tempfile.gettempdir(), "minara-probe-results.json"))
    parser.add_argument("--budget", type=float, default=150, help="stop starting new checks after this many credits")
    parser.add_argument("--only", nargs="*", help="upstream model ids to check")
    parser.add_argument("--checks", nargs="*", default=["tools", "vision", "thinking", "effort", "channels", "messages",
                                                        "cache"])
    parser.add_argument("--refresh", action="store_true", help="re-measure checks that already have a result")
    parser.add_argument("--regenerate", action="store_true", help="only rebuild minara_models.json")
    parser.add_argument("--limits", action="store_true",
                        help="with --regenerate: fetch OpenRouter's published windows again (free, no key)")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    results_path = pathlib.Path(args.results)
    results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else {}
    listing_path = results_path.with_name("minara-probe-listing.json")
    if not args.regenerate:
        token_list = tokens_from(args.token_file) if args.token_file else [os.environ["MINARA_TOKEN"]]
        clients = sorted((Client(token) for token in token_list), key=lambda c: -c.credits())
        client = clients[0]
        fresh = client.get("/v1/models").json()["data"]
        previous = json.loads(listing_path.read_text(encoding="utf-8")) if listing_path.exists() else []
        if previous and len(fresh) < len(previous) // 2:
            # Minara's /v1/models answered 32 of its 119 models once (2026-11-09) and all of them a minute later.
            print("warning: /v1/models lists %d models, %d before: keeping the union" % (len(fresh), len(previous)))
        merged = {item["id"]: item for item in previous}
        merged.update({item["id"]: item for item in fresh})  # newer prices win; a model missing once is kept
        listing = list(merged.values())
        listing_path.write_text(json.dumps(listing, ensure_ascii=False, indent=1), encoding="utf-8")
        start = client.credits()
        print("credits before: %.2f (%.4f USD)" % (start, start / CREDITS_PER_USD), flush=True)
        lock = threading.Lock()
        stop = threading.Event()
        jobs = []
        for item in listing:
            upstream = item["id"]
            if ("openai" not in (item.get("supported_endpoint_types") or []) or upstream in NOT_CHAT or upstream in INHERIT
                    or excluded(upstream)):
                continue
            if args.only and upstream not in args.only:
                continue
            wanted = set(minara_probe.checks_for(item)) | {"effort"}
            for check in args.checks:
                if check not in wanted or (check == "effort" and upstream not in EFFORT_REPRESENTATIVES):
                    continue
                if not args.refresh and ((results.get(upstream) or {}).get(check) or {}).get("status") == "ok":
                    continue
                jobs.append((upstream, check))
        print("checks to run:", len(jobs), flush=True)
        probes = {"tools": probe_tools, "vision": probe_vision, "thinking": probe_thinking, "effort": probe_effort,
                  "channels": probe_channels, "messages": probe_messages, "cache": probe_cache}

        def run(job):
            upstream, check = job
            if stop.is_set():
                return
            try:
                outcome = probes[check](client, upstream)
            except Exception as exc:  # noqa: BLE001 - one failing model must not end the run
                outcome = {"status": "exception", "detail": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
            with lock:
                record = results.setdefault(upstream, {})
                record[check] = outcome
                record["checked_at"] = datetime.date.today().isoformat()
                results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
                spent = start - client.credits()
                print("%-38s %-8s %s  [spent %.1f credits]" % (upstream, check, json.dumps(outcome, ensure_ascii=False)[:300], spent), flush=True)
                if spent > args.budget:
                    stop.set()

        with cf.ThreadPoolExecutor(args.workers) as pool:
            list(pool.map(run, jobs))
        end = client.credits()
        print("credits after: %.2f, spent %.2f (%.4f USD)" % (end, start - end, (start - end) / CREDITS_PER_USD))
    listing = json.loads(listing_path.read_text(encoding="utf-8"))
    published_path = results_path.with_name("minara-probe-openrouter.json")
    if not args.regenerate or args.limits:
        try:
            published_path.write_text(json.dumps(fetch_published(), ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - limits stay as the last snapshot had them
            print("OpenRouter model list unavailable (%s): keeping the last snapshot" % type(exc).__name__)
    published = json.loads(published_path.read_text(encoding="utf-8")) if published_path.exists() else {}
    study = merged_study(results_path.with_name("minara-effort-study.json"),
                         results_path.with_name("minara-effort-study-hard.json"))
    catalog = generate(results, listing, published, study)
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    counts = {}
    for entry in catalog["models"]:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print("wrote %s: %d models %s" % (CATALOG_FILE.name, len(catalog["models"]), counts))


if __name__ == "__main__":
    main()
