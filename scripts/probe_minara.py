"""Measure the Minara catalog and regenerate minara_models.json (the relay's Minara model list).

The relay publishes every model this file marks "ok" as minara/<name>; nothing else decides which Minara models exist
or what they can do. Each check is one small paid request on the account whose bearer token is given:

  tools     one auto tool call (get_weather) on every model
  vision    a 256x256 two-colour PNG on every model
  thinking  one streaming request: where the reasoning arrives (reasoning / reasoning_content / reasoning_details),
            whether it streams before the answer, signatures, reasoning tokens
  effort    none..max on one representative of each family and backend; the others inherit that ladder

Usage (PowerShell):
  python scripts/probe_minara.py --token-file <file with one or more Minara bearer tokens> [--budget 150] [--only id ...]
  python scripts/probe_minara.py --regenerate     # rebuild minara_models.json from the saved results, no requests

Raw results are kept in --results (default: %TEMP%/minara-probe-results.json) and reused, so an interrupted run
continues where it stopped; --refresh re-measures. The token is read at run time and never written anywhere.
220 Minara credits are 1 USD of the `usage.cost` the API reports (measured 2026-11-09).
"""
import argparse
import base64
import concurrent.futures as cf
import datetime
import json
import os
import pathlib
import struct
import sys
import tempfile
import threading
import time
import zlib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_FILE = ROOT / "minara_models.json"
BASE = "https://api.minara.ai"
CREDITS_PER_USD = 220
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
THINK_TASK = "What is 17 * 23? Think it through briefly, then reply with the number only."
EFFORT_TASK = ("How many 6-digit positive integers have strictly increasing digits (left to right) and a digit sum "
               "divisible by 3? Reply with the number only.")
# One representative per family and backend; the effort ladder of every other model of the group is inherited.
EFFORT_REPRESENTATIVES = (
    "anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-4.6",
    "anthropic/claude-sonnet-4.5", "openai/gpt-6.1-sol", "openai/gpt-6-luna", "openai/gpt-5.5", "openai/gpt-5",
    "google/gemini-3.5-flash", "x-ai/grok-4.7", "z-ai/glm-5.3-prime", "z-ai/glm-5.2", "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v3.2", "moonshotai/kimi-k2.6")
# OpenAI's pro tiers cost 10-20x their base model per token and share its capabilities; they inherit them.
INHERIT = {"openai/o3-pro": "openai/o3", "openai/gpt-5-pro": "openai/gpt-5", "openai/gpt-5.2-pro": "openai/gpt-5.2",
           "openai/gpt-5.4-pro": "openai/gpt-5.4", "openai/gpt-5.5-pro": "openai/gpt-5.5"}
NOT_CHAT = {"openai/gpt-image-2"}


def excluded(upstream):
    """The owner's choice (2026-11-09): older generations are not published nor measured.

    GPT below 6 (with o3, gpt-chat-latest and gpt-oss), DeepSeek's 3 series (with deepseek-chat and r1), GLM below 5.3."""
    import re
    vendor, _, name = upstream.partition("/")
    if vendor == "openai":
        match = re.match(r"gpt-(\d+)", name)
        return not (match and int(match.group(1)) >= 6)
    if vendor == "deepseek":
        return not re.match(r"deepseek-v([4-9]|\d{2})", name)
    if vendor == "z-ai":
        match = re.match(r"glm-(\d+)(?:\.(\d+))?", name)
        return not match or (int(match.group(1)), int(match.group(2) or 0)) < (5, 3)
    return False


def png(width, height, pixel):
    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height))

    def chunk(tag, data):
        body = struct.pack(">I", len(data)) + tag + data
        return body + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


IMAGE = "data:image/png;base64," + base64.b64encode(
    png(256, 256, lambda x, y: (230, 200, 0) if y < 128 else (0, 160, 60))).decode()


def public_id(upstream):
    """minara/<name>: Claude names as the relay spells them (claude-opus-5-5), the rest without the vendor prefix."""
    vendor, _, name = upstream.partition("/")
    if vendor == "anthropic":
        name = name.replace(".", "-")
    return "minara/" + name


def tokens_from(path):
    text = pathlib.Path(path).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip().startswith("eyJ")]


class Client:
    def __init__(self, token):
        from curl_cffi import requests
        self.requests = requests
        self.token = token

    def headers(self, accept="application/json"):
        return {"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Accept": accept}

    def get(self, path):
        return self.requests.get(BASE + path, headers=self.headers(), impersonate="chrome131", timeout=60,
                                 allow_redirects=False)

    def credits(self):
        data = self.get("/auth/me").json()
        part = data.get("creditBalanceBreakdown") or {}
        return float(part.get("plan", 0)) + float(part.get("extra", 0))

    def chat(self, body, stream=False, timeout=240):
        for attempt in range(4):
            response = self.requests.post(BASE + "/v1/chat/completions", json=body, impersonate="chrome131",
                                          timeout=timeout, stream=stream, allow_redirects=False,
                                          headers=self.headers("text/event-stream" if stream else "application/json"))
            if response.status_code in (500, 502, 503, 524) and attempt < 3:
                text = response.text if not stream else b"".join(response.iter_content()).decode("utf-8", "replace")
                if _channel_trouble(text) or not text.strip() or response.status_code == 524:
                    time.sleep(8 * (attempt + 1))  # Minara's windows of empty 5xx; unbilled, retry later
                    continue
                return response, text
            if stream:
                return response, None
            return response, response.text
        return response, ""


def _channel_trouble(text):
    """Minara's own routing failures: the model is listed but no channel serves it right now."""
    return any(mark in text for mark in ("channel_selection", "channel selection", "no_enabled_channel",
                                         "available channel", "Unknown or unsupported model"))


def classify_error(status, text):
    if "base_resp" in text:
        return "broken"  # HTTP 200 carrying the upstream's own error (MiniMax: invalid api key)
    if _channel_trouble(text):
        return "unavailable"
    if status == 524:
        return "timeout"
    return "error %s" % status


def probe_tools(client, model):
    body = {"model": model, "max_tokens": 1000, "reasoning_effort": "low", "tools": TOOLS,
            "messages": [{"role": "user", "content": "What's the weather in Paris right now? Use the tool."}]}
    response, text = client.chat(body)
    if response.status_code == 400 and "reasoning" in (text or ""):
        body.pop("reasoning_effort")
        response, text = client.chat(body)
    if response.status_code != 200 or "base_resp" in (text or ""):
        return {"status": classify_error(response.status_code, text or ""), "detail": (text or "")[:200]}
    data = json.loads(text)
    choice = (data.get("choices") or [{}])[0]
    calls = (choice.get("message") or {}).get("tool_calls") or []
    ok = False
    for call in calls:
        fn = call.get("function") or {}
        try:
            ok = ok or (fn.get("name") == "get_weather" and "paris" in json.loads(fn.get("arguments") or "{}").get("city", "").lower())
        except (ValueError, AttributeError):
            pass
    return {"status": "ok", "tools": ok, "finish": choice.get("finish_reason"), "served": data.get("model"),
            "id": str(data.get("id"))[:20], "provider": data.get("provider")}


def probe_vision(client, model):
    body = {"model": model, "max_tokens": 600, "reasoning_effort": "low", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Top half colour and bottom half colour of this image? Two words."},
        {"type": "image_url", "image_url": {"url": IMAGE}}]}]}
    response, text = client.chat(body)
    if response.status_code == 400 and "reasoning" in (text or ""):
        body.pop("reasoning_effort")
        response, text = client.chat(body)
    if response.status_code != 200 or "base_resp" in (text or ""):
        return {"status": classify_error(response.status_code, text or ""), "detail": (text or "")[:200]}
    data = json.loads(text)
    answer = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    low = answer.lower()
    return {"status": "ok", "vision": "yellow" in low and "green" in low, "answer": answer[:60],
            "prompt_tokens": (data.get("usage") or {}).get("prompt_tokens")}


def probe_thinking(client, model):
    body = {"model": model, "max_tokens": 500, "stream": True, "stream_options": {"include_usage": True},
            "reasoning_effort": "medium", "messages": [{"role": "user", "content": THINK_TASK}]}
    started = time.monotonic()
    response, text = client.chat(body, stream=True)
    if response.status_code == 400:
        body.pop("reasoning_effort")
        started = time.monotonic()
        response, text = client.chat(body, stream=True)
    if response.status_code != 200:
        return {"status": classify_error(response.status_code, text or "")}
    fields, details, signature, chunks, first_reason, first_text, usage, error = set(), set(), False, 0, None, None, {}, None
    buffer = b""
    for piece in response.iter_content():
        buffer += piece
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                continue
            try:
                item = json.loads(line[5:])
            except ValueError:
                continue
            now = time.monotonic() - started
            if "base_resp" in item or "error" in item:
                error = "broken"
            usage = item.get("usage") or usage
            for choice in item.get("choices") or []:
                delta = choice.get("delta") or {}
                seen = False
                for key in ("reasoning", "reasoning_content"):
                    if isinstance(delta.get(key), str) and delta[key]:
                        fields.add(key)
                        seen = True
                for detail in delta.get("reasoning_details") or []:
                    fields.add("reasoning_details")
                    details.add(detail.get("type"))
                    signature = signature or bool(detail.get("signature"))
                if seen:
                    chunks += 1
                    first_reason = first_reason if first_reason is not None else now
                if isinstance(delta.get("content"), str) and delta["content"].strip() and first_text is None:
                    first_text = now
    if error:
        return {"status": error}
    tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if tokens is None:
        tokens = ((usage.get("billing_usage") or {}).get("gemini_usage_metadata") or {}).get("thoughtsTokenCount")
    if chunks == 0:
        mode = "hidden" if tokens else "none"
    elif chunks >= 3 and (first_text is None or first_text - first_reason > 0.3):
        mode = "stream"
    else:
        mode = "end"
    return {"status": "ok", "thinking": mode, "fields": sorted(fields), "details": sorted(d for d in details if d),
            "signature": signature, "reasoning_tokens": tokens, "chunks": chunks}


def probe_effort(client, model):
    ladder = {}
    for level in EFFORTS:
        body = {"model": model, "max_tokens": 6000, "reasoning_effort": level,
                "messages": [{"role": "user", "content": EFFORT_TASK}]}
        response, text = client.chat(body)
        if response.status_code != 200:
            ladder[level] = {"accepted": False, "status": response.status_code, "detail": (text or "")[:160]}
            continue
        data = json.loads(text)
        usage = data.get("usage") or {}
        tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if tokens is None:
            tokens = ((usage.get("billing_usage") or {}).get("gemini_usage_metadata") or {}).get("thoughtsTokenCount")
        ladder[level] = {"accepted": True, "reasoning_tokens": tokens, "completion_tokens": usage.get("completion_tokens")}
    return {"status": "ok", "ladder": ladder}


def backend_of(probe):
    ident = str(probe.get("id") or "")
    if ident.startswith("gen-"):
        return "openrouter"
    if ident.startswith("resp_"):
        return "openai-responses"
    if ident.startswith("msg_"):
        return "anthropic-native"
    if ident[:3].isdigit():
        return "volcengine-ark"
    return "other" if ident else None


def ladder_levels(ladder):
    """Levels the route takes: every level except the ones upstream refused in so many words.

    Minara's channel failures (5xx, `Unknown or unsupported model`, channel_selection) say nothing about the level: on
    2026-11-09 they hit random rungs of nearly every ladder, while the one real refusal named the levels ("gpt-6.1-sol does
    not support reasoning effort "none"; use low, medium, high, xhigh or max")."""
    taken = []
    for level in EFFORTS:
        rung = ladder.get(level) or {}
        status = rung.get("status") or 0
        if rung.get("accepted") or status >= 500 or _channel_trouble(rung.get("detail") or ""):
            taken.append(level)
    tokens = {(ladder.get(level) or {}).get("reasoning_tokens") for level in taken if (ladder.get(level) or {}).get("accepted")}
    return taken, ("measured" if len(tokens - {None}) > 1 else "unmeasured")


def ladder_reasons(ladder):
    return any(((ladder.get(level) or {}).get("reasoning_tokens") or 0) > 0 for level in EFFORTS)


def generate(results, listing):
    """Build minara_models.json from the listing and the saved measurements."""
    entries = []
    groups = {}
    for upstream in EFFORT_REPRESENTATIVES:
        effort = (results.get(upstream) or {}).get("effort") or {}
        if effort.get("status") == "ok":
            tools = (results.get(upstream) or {}).get("tools") or {}
            groups[(upstream.split("/")[0], backend_of(tools), _family(upstream))] = upstream
    seen = set()
    for item in listing:
        upstream = item["id"]
        if "openai" not in (item.get("supported_endpoint_types") or []) or upstream in NOT_CHAT:
            continue
        source = INHERIT.get(upstream, upstream)
        measured = results.get(source) or {}
        if excluded(upstream):
            name = public_id(upstream)
            seen.add(name)
            entries.append({"id": name, "upstream_model": upstream, "label": _label(upstream),
                            "provider": upstream.split("/")[0], "status": "excluded", "pricing": item.get("pricing")})
            continue
        tools, vision, thinking = (measured.get(key) or {} for key in ("tools", "vision", "thinking"))
        status = "ok"
        for probe in (tools, vision, thinking):
            if probe.get("status") in ("broken",):
                status = "broken"
        if status == "ok" and tools.get("status") != "ok" and thinking.get("status") != "ok":
            status = tools.get("status") or thinking.get("status") or "unmeasured"
        served = tools.get("served") or ""
        if served and _substituted(source, served):  # a pro tier is measured on its base model (INHERIT)
            status = "substituted"
        backend = backend_of(tools)
        rep = measured.get("effort") if (measured.get("effort") or {}).get("status") == "ok" else None
        effort_source = "measured" if rep else None
        if rep is None:
            group = groups.get((upstream.split("/")[0], backend, _family(upstream)))
            rep = ((results.get(group) or {}).get("effort") or {}) if group else None
            effort_source = ("family:" + group) if rep else None
        levels, effect = ladder_levels(rep["ladder"]) if rep else ([], "unmeasured")
        own = (measured.get("effort") or {}).get("ladder") or {}
        # A short question can be answered without thinking (Sonnet 5.5 did): the ladder's harder one shows that it reasons.
        reasoning = (thinking.get("thinking") not in (None, "none") or bool(thinking.get("reasoning_tokens"))
                     or ladder_reasons(own))
        if not reasoning:
            levels = []
        name = public_id(upstream)
        if name in seen:
            raise SystemExit("duplicate public id " + name)
        seen.add(name)
        entry = {
            "id": name, "upstream_model": upstream, "label": _label(upstream), "provider": upstream.split("/")[0],
            "status": status, "backend": backend, "tools": tools.get("tools") if tools.get("status") == "ok" else None,
            "forced_tool_choice": not _mandatory_reasoning(upstream),
            "vision": vision.get("vision") if vision.get("status") == "ok" else None,
            "reasoning": reasoning, "thinking": thinking.get("thinking"), "thinking_signature": bool(thinking.get("signature")),
            "reasoning_effort_levels": levels, "effort_source": effort_source, "effort_effect": effect,
            "cache": "explicit" if upstream.startswith("anthropic/") else "automatic",
            "pricing": item.get("pricing"),
            "measured_from": source if source != upstream else None,
            "checked_at": measured.get("checked_at"),
        }
        if served and source == upstream and served.split("/")[-1] != upstream.split("/")[-1]:
            entry["served_as"] = served
        entries.append(entry)
    entries.sort(key=lambda e: (e["provider"], e["id"]))
    return {"generated_at": datetime.date.today().isoformat(), "source": BASE + "/v1/models",
            "credits_per_usd": CREDITS_PER_USD, "models": entries}


def _family(upstream):
    name = upstream.split("/", 1)[1]
    for prefix in ("claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5", "gpt-6", "gpt-5.6", "gpt-5", "gemini",
                   "grok", "glm", "deepseek", "kimi", "claude-opus-4", "claude-sonnet-4", "claude-opus-5", "claude-sonnet-5",
                   "claude-fable", "claude-haiku-4", "o3", "gpt-4", "gpt-3", "gpt-oss", "minimax"):
        if name.startswith(prefix):
            return prefix
    return name


def _mandatory_reasoning(upstream):
    # Measured on 2026-11-09: Opus 5.5 rejects tool_choice "required" and a named tool ("not supported for this model")
    # because its reasoning cannot be disabled. The 5.5 family shares adaptive, mandatory reasoning.
    return upstream in ("anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-5.5")


def _substituted(upstream, served):
    """True when upstream answered with another model: glm-5.3 served as glm-5-3-flash-260828. A date or version suffix
    (deepseek-v4-pro-260425, glm-5-2-260617) is the same model."""
    import re
    want = upstream.split("/")[-1].replace(".", "-").replace("_", "-").lower()
    got = served.split("/")[-1].replace(".", "-").replace("_", "-").lower()
    got = re.sub(r"(-\d{4,8})+$", "", got)
    return got != want and got != re.sub(r"(-\d{4,8})+$", "", want)


def _label(upstream):
    name = upstream.split("/", 1)[1]
    return name.replace("-", " ").replace("claude ", "Claude ").replace("gpt ", "GPT-").title().replace("Gpt", "GPT") + " (Minara)"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token-file", help="file with Minara bearer tokens, one per line (the richest one is used)")
    parser.add_argument("--results", default=os.path.join(tempfile.gettempdir(), "minara-probe-results.json"))
    parser.add_argument("--budget", type=float, default=150, help="stop starting new checks after this many credits")
    parser.add_argument("--only", nargs="*", help="upstream model ids to check")
    parser.add_argument("--checks", nargs="*", default=["tools", "vision", "thinking", "effort"])
    parser.add_argument("--refresh", action="store_true", help="re-measure checks that already have a result")
    parser.add_argument("--regenerate", action="store_true", help="only rebuild minara_models.json")
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
            for check in args.checks:
                if check == "effort" and upstream not in EFFORT_REPRESENTATIVES:
                    continue
                if not args.refresh and ((results.get(upstream) or {}).get(check) or {}).get("status") == "ok":
                    continue
                jobs.append((upstream, check))
        print("checks to run:", len(jobs), flush=True)
        probes = {"tools": probe_tools, "vision": probe_vision, "thinking": probe_thinking, "effort": probe_effort}

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
    catalog = generate(results, listing)
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    counts = {}
    for entry in catalog["models"]:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print("wrote %s: %d models %s" % (CATALOG_FILE.name, len(catalog["models"]), counts))


if __name__ == "__main__":
    main()
