"""Explicit paid replay of a frozen upstream JSON body with private wire traces."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth import upstream_headers
import config
import direct
import journal
from upstream_attempts import MAX_ATTEMPTS, AttemptBudget
from upstream_refresh import Refresher


def load_config_readonly(path):
    """Load a config for replay without ever touching the original file.

    config.load() may migrate account_id into the file and the Refresher may persist a
    rotated cookie. A throwaway copy absorbs the former; _persist_auth=False stops the latter.
    """
    source = Path(path).resolve()
    with tempfile.TemporaryDirectory(prefix="replay-config-") as folder:
        copy = Path(folder) / source.name
        shutil.copyfile(source, copy)
        cfg = config.load(str(copy))
    cfg.pop("_config_path", None)
    cfg["_persist_auth"] = False
    return cfg


async def replay(cfg, source, output):
    payload = source.read_bytes()
    body = json.loads(payload)
    if not isinstance(body, dict) or body.get("model") != "claude-opus-5-5":
        raise ValueError("this incident replay requires the frozen claude-opus-5-5 body")
    output.mkdir(parents=True, exist_ok=False)
    cfg = dict(cfg, upstream_trace_dir=str(output / "wire"), upstream_error_dir=str(output / "summaries"),
               cache_keepwarm_seconds=0, session_refresh_interval=0, direct_debug=False,
               _persist_auth=False, upstream_empty_retries=MAX_ATTEMPTS - 1)
    journal.JOURNAL.configure(cfg, str(output / "journal.jsonl"))
    backend = SimpleNamespace(cfg=cfg, headers=lambda: upstream_headers(cfg), refresher=Refresher(cfg))
    session = SimpleNamespace(backend=backend, _cancelled=False, _request_tag="exact-replay",
                              _attempt_budget=AttemptBudget(threading.Event()))
    param_types = {tool["name"]: tool.get("params") or {} for tool in body.get("tools", [])}
    counts = {}
    outcome = "unknown"
    started = time.monotonic()
    credits = 0
    with (output / "normalized-events.jsonl").open("w", encoding="utf-8") as events:
        try:
            async for event in direct._stream_body(cfg, session, body, param_types, payload=payload):
                counts[event["type"]] = counts.get(event["type"], 0) + 1
                events.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                events.flush()
                if event["type"] == "credits":
                    credits += event.get("value", 0)
            outcome = session._attempt_budget.last.get("outcome", "unknown")
        except Exception as exc:
            outcome = type(exc).__name__
            events.write(json.dumps({"type": "replay_error", "exception_type": outcome,
                                     "message": str(exc)}, ensure_ascii=False) + "\n")
    sent = list((output / "wire").glob("*/request-body.json"))
    raw_responses = list((output / "wire").glob("*/response-body.ndjson"))
    manifest = {"source_path": str(source.resolve()), "source_bytes": len(payload),
                "source_sha256": hashlib.sha256(payload).hexdigest(), "model": body["model"],
                "messages": len(body.get("messages", [])), "tools": len(body.get("tools", [])),
                "attempts": session._attempt_budget.used,
                "all_attempts_byte_identical": bool(sent) and len(sent) == session._attempt_budget.used and all(p.read_bytes() == payload for p in sent),
                "captured_attempts": len(sent), "raw_responses": len(raw_responses),
                "outcome": outcome, "credits_used": round(credits, 6), "normalized_event_counts": counts,
                "seconds": round(time.monotonic() - started, 3)}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", required=True, type=Path, help="Existing exact upstream JSON capture")
    parser.add_argument("--output", required=True, type=Path, help="New private output directory")
    parser.add_argument("--config", type=Path, default=Path(config.CONFIG_PATH))
    parser.add_argument("--allow-paid", action="store_true", help="Authorize up to five paid POSTs of this exact body")
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("--allow-paid is required: replay can spend credits on up to five attempts")
    if args.output.exists():
        parser.error("--output must be a new directory to avoid overwriting previous evidence")
    cfg = load_config_readonly(args.config)
    asyncio.run(replay(cfg, args.body, args.output))


if __name__ == "__main__":
    main()
