"""Split relay turns from journal.jsonl into context, thinking, tool arguments and text.

Usage: python scripts/turn_breakdown.py [journal.jsonl|export.jsonl] [--last N]
Needs the upstream.thinking_* / tool_start / text_start events (relay after 3b347e6).
"""
import collections
import datetime
import json
import statistics
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load(path):
    with open(path, encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def turns(events):
    table = collections.OrderedDict()
    for event in events:
        turn_id = event.get("turn_id")
        if not turn_id:
            continue
        t = table.setdefault(turn_id, {"args_ms": 0, "calls": 0, "chars": 0})
        data, kind, ts = event.get("data") or {}, event["kind"], event["ts"]
        if kind == "turn.begin":
            t.update(model=data.get("model"), effort=data.get("effort"))
        elif kind == "upstream.request":
            t.setdefault("req", ts)
        elif kind == "upstream.thinking_start":
            t.setdefault("think_start", ts)
        elif kind == "upstream.thinking_end":
            t["thinking_ms"] = t.get("thinking_ms", 0) + (data.get("thinking_ms") or 0)
        elif kind == "upstream.tool_start":
            t.setdefault("first_out", ts)
        elif kind == "upstream.text_start":
            t.setdefault("first_out", ts)
        elif kind == "tool.call":
            t["args_ms"] += data.get("args_ms") or 0
            t["chars"] += data.get("args_chars") or 0
            t["calls"] += 1
        elif kind == "upstream.usage":
            t["cache_read"] = data.get("cache_read")
            t["out_tokens"] = data.get("output_tokens")
        elif kind == "turn.end":
            t["end"], t["reason"] = ts, data.get("reason")
    rows = []
    for turn_id, t in table.items():
        if "req" not in t or "end" not in t or ("think_start" not in t and "first_out" not in t):
            continue
        total = (t["end"] - t["req"]) * 1000
        context = ((t.get("think_start") or t["first_out"]) - t["req"]) * 1000
        thinking = t.get("thinking_ms", 0)
        args = t["args_ms"]
        text = max(0.0, total - context - thinking - args)
        rows.append({"id": turn_id[:8], "at": t["req"], "model": t.get("model"), "effort": t.get("effort"),
                     "context": context, "thinking": thinking, "args": args, "text": text, "total": total,
                     "calls": t["calls"], "chars": t["chars"], "cache_read": t.get("cache_read"),
                     "out": t.get("out_tokens"), "reason": t.get("reason")})
    return rows


def main():
    args = sys.argv[1:]
    last = None
    if "--last" in args:
        last = int(args[args.index("--last") + 1])
        del args[args.index("--last"):args.index("--last") + 2]
    rows = turns(load(args[0] if args else "journal.jsonl"))
    if last:
        rows = rows[-last:]
    if not rows:
        print("No turns with timing events yet: restart the relay on the new code and work a few turns.")
        return
    sec = lambda ms: "%5.1f" % (ms / 1000)
    print("time     turn     effort  context thinking  args   text   total calls  chars   cache_read  out  reason")
    for r in rows:
        print("%s %s %-7s %s   %s  %s %s %s %5d %6d %12s %5s  %s" % (
            datetime.datetime.fromtimestamp(r["at"]).strftime("%H:%M:%S"), r["id"], r["effort"],
            sec(r["context"]), sec(r["thinking"]), sec(r["args"]), sec(r["text"]), sec(r["total"]),
            r["calls"], r["chars"], r["cache_read"], r["out"], r["reason"]))
    total = sum(r["total"] for r in rows)
    print("\n%d turns, %.1f s in total" % (len(rows), total / 1000))
    for part, label in (("context", "обработка контекста"), ("thinking", "мышление"),
                        ("args", "генерация аргументов"), ("text", "текст и прочее")):
        values = [r[part] for r in rows]
        print("  %-22s median %5.1f s   max %5.1f s   share %3.0f%%" % (
            label, statistics.median(values) / 1000, max(values) / 1000, 100 * sum(values) / total))


if __name__ == "__main__":
    main()
