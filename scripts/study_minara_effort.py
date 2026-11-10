"""Effort study on the Minara pool: which reasoning_effort levels change anything, per model family.

Streams every request (Cloudflare cuts a non-streamed reply after 100 s and still bills it), samples each level 2-3
times on one effort-sensitive question, spreads the models over the pool's fullest accounts (read from the relay's
config.json, never printed) and saves after every request, so a stopped run continues where it ended.

  python scripts/study_minara_effort.py [--budget-usd 8] [--only model ...] [--hard] [--samples N]

The result (%TEMP%/minara-effort-study.json, with --hard %TEMP%/minara-effort-study-hard.json) is read by
scripts/probe_minara.py --regenerate, which turns it into the levels and effort maps of minara_models.json
(effort_tiers). The hard question is minara_probe's, the one the relay itself asks a model Minara adds later.
Measured on 2026-11-09/10: about $10 for all 50 models on the first question, $14 on the hard one.
"""
import argparse
import concurrent.futures as cf
import json
import os
import pathlib
import sys
import threading
import time

from curl_cffi import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import minara_probe  # noqa: E402

BASE = "https://api.minara.ai"
CONFIG = pathlib.Path(__file__).resolve().parents[1] / "config.json"
OUT = pathlib.Path(os.environ["TEMP"]) / "minara-effort-study.json"
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
LEVELS = (None,) + EFFORTS  # None: no reasoning_effort at all, the model's own default
QUESTION = "How many positive integers less than 10000 have a digit sum of exactly 19? Reply with the number only."
ANSWER = str(sum(1 for n in range(1, 10000) if sum(map(int, str(n))) == 19))
# model -> output price per million (for the budget guard) and samples per level
MODELS = {
    "anthropic/claude-opus-5.5": 20, "anthropic/claude-sonnet-5.5": 10, "anthropic/claude-haiku-5.5": 2.5,
    "anthropic/claude-fable-5.1": 50, "anthropic/claude-opus-4.8": 25, "anthropic/claude-sonnet-4.6": 15,
    "anthropic/claude-opus-4.5": 25, "anthropic/claude-haiku-4.5": 5,
    "openai/gpt-6.1-sol": 10, "openai/gpt-6-luna": 0.5, "openai/gpt-6.1-terra": 5, "openai/gpt-6-sol-pro": 10,
    "google/gemini-3.5-flash": 9, "google/gemini-4-argon": 10,
    "x-ai/grok-4.7": 6, "x-ai/grok-4.20": 2.5,
    "deepseek/deepseek-v4-pro": 3.2, "deepseek/deepseek-v4.1-flash": 1.2,
    "z-ai/glm-5.3-prime": 8.8, "z-ai/glm-5.3-flash": 0.3,
    "moonshotai/kimi-k3": 8.5, "moonshotai/kimi-k2.6": 4,
    # second batch: every other published model, so none inherits a ladder
    "anthropic/claude-fable-5": 50, "anthropic/claude-opus-4.1": 75, "anthropic/claude-opus-4.6": 25,
    "anthropic/claude-opus-4.7": 25, "anthropic/claude-opus-5": 25, "anthropic/claude-sonnet-4.5": 15,
    "anthropic/claude-sonnet-5": 10,
    "deepseek/deepseek-v4-flash": 0.18, "deepseek/deepseek-v4-flash-vision-exp": 0.66, "deepseek/deepseek-v4-pro-0813": 2.95,
    "deepseek/deepseek-v4.1-flash-1017": 0.12,
    "google/gemini-3.5-flash-lite": 2.5, "google/gemini-3.6-flash": 3.75, "google/gemini-3.7-flash": 1.9,
    "google/gemini-3.8-flash": 3.75,
    "openai/gpt-6-astra": 50, "openai/gpt-6-astra-pro": 50, "openai/gpt-6-luna-pro": 0.5, "openai/gpt-6-sol": 10,
    "openai/gpt-6.1-sol-pro": 10, "openai/gpt-6.1-terra-pro": 5,
    "x-ai/grok-4.20-multi-agent": 2.5, "x-ai/grok-4.3": 2.5, "x-ai/grok-4.5": 6, "x-ai/grok-4.6": 6, "x-ai/grok-build-0.1": 2,
    "z-ai/glm-5.3-flashx": 1.25, "z-ai/glm-5.3-flashx-legal": 1.25,
    # released 2026-11-10
    "anthropic/claude-fable-5.2": 40,
}
MAX_TOKENS = 4000
# --hard: a question that makes the strong models think long enough for working levels to separate (the one above takes
# GPT-6 and Gemini 4 Argon 50-400 reasoning tokens at every level). Divisibility by 11 splits it into two 3-digit sums.
HARD_QUESTION, HARD_ANSWER = minara_probe.HARD_QUESTION, minara_probe.HARD_ANSWER
HARD_OUT = pathlib.Path(os.environ["TEMP"]) / "minara-effort-study-hard.json"
# Measured on chat they answer from a channel without cache; the relay sends them through /v1/messages, whose levels
# come from the Messages API (minara_probe.messages_offer), so their chat study says nothing about it.
HARD_SKIP = {"anthropic/claude-fable-5", "anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6"}


def pool_tokens():
    saved = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    tokens = [a["bearer_token"] for a in saved["accounts"] if a.get("upstream") == "minara" and a.get("bearer_token")]
    rich = []
    for token in tokens:
        try:
            r = requests.get(BASE + "/auth/me", headers={"Authorization": "Bearer " + token}, impersonate="chrome131", timeout=30)
            parts = r.json().get("creditBalanceBreakdown") or {}
            credits = float(parts.get("plan", 0)) + float(parts.get("extra", 0))
        except Exception:  # noqa: BLE001
            continue
        if credits >= 300:
            rich.append((credits, token))
    rich.sort(reverse=True)
    return [token for _, token in rich]


def one(token, model, level):
    body = {"model": model, "max_tokens": MAX_TOKENS, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": QUESTION}]}
    if level is not None:
        body["reasoning_effort"] = level
    for attempt in range(4):
        started = time.monotonic()
        try:
            r = requests.post(BASE + "/v1/chat/completions", json=body, impersonate="chrome131", stream=True, timeout=300,
                              headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                       "Accept": "text/event-stream"})
        except Exception as exc:  # noqa: BLE001
            if attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            return {"status": "exception", "detail": type(exc).__name__}
        if r.status_code != 200:
            text = b"".join(r.iter_content()).decode("utf-8", "replace")[:300]
            channel = (not text.strip()) or any(m in text for m in ("channel", "Unknown or unsupported model", "error-502"))
            if channel and r.status_code in (400, 500, 502, 503, 524) and attempt < 3:
                time.sleep(15 * (attempt + 1))
                continue
            return {"status": r.status_code, "detail": text}
        usage, text, reasoning_chars, buf = {}, "", 0, b""
        try:
            for piece in r.iter_content():
                buf += piece
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                        continue
                    try:
                        item = json.loads(line[5:])
                    except ValueError:
                        continue
                    usage = item.get("usage") or usage
                    for choice in item.get("choices") or []:
                        delta = choice.get("delta") or {}
                        for key in ("reasoning", "reasoning_content"):
                            if isinstance(delta.get(key), str):
                                reasoning_chars += len(delta[key])
                        if isinstance(delta.get("content"), str):
                            text += delta["content"]
        except Exception as exc:  # noqa: BLE001 - a stream cut mid-way (curl 92): the sample is taken again
            if attempt < 3:
                time.sleep(15 * (attempt + 1))
                continue
            return {"status": "stream_error", "detail": type(exc).__name__}
        tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if tokens is None:
            tokens = ((usage.get("billing_usage") or {}).get("gemini_usage_metadata") or {}).get("thoughtsTokenCount")
        return {"status": 200, "reasoning_tokens": tokens, "completion_tokens": usage.get("completion_tokens"),
                "reasoning_chars": reasoning_chars, "secs": round(time.monotonic() - started, 1),
                "correct": ANSWER in text.replace(",", ""), "cost": usage.get("cost")}
    return {"status": "gave_up"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-usd", type=float, default=8.0)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--hard", action="store_true", help="the harder question, 2 samples, into %%TEMP%%/minara-effort-study-hard.json")
    parser.add_argument("--samples", type=int, default=0, help="samples per level (default 2 with --hard, 2-3 without)")
    args = parser.parse_args()
    global QUESTION, ANSWER, OUT, MAX_TOKENS
    if args.hard:
        QUESTION, ANSWER, OUT, MAX_TOKENS = HARD_QUESTION, HARD_ANSWER, HARD_OUT, 12000
    results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    lock = threading.Lock()
    spent = [0.0]
    tokens = pool_tokens()
    models = [m for m in MODELS if (not args.only or m in args.only) and not (args.hard and m in HARD_SKIP)]
    print("answer", ANSWER, "| rich accounts", len(tokens), "| models", len(models), flush=True)

    def study(index_model):
        index, model = index_model
        token = tokens[index % len(tokens)]
        samples = args.samples or (2 if args.hard else 3 if MODELS[model] <= 10 else 2)
        for level in LEVELS:
            key = "default" if level is None else level
            for sample in range(samples):
                with lock:
                    done = results.setdefault(model, {}).setdefault(key, [])
                    if len(done) > sample:
                        continue
                    if spent[0] > args.budget_usd:
                        return
                row = one(token, model, level)
                estimate = ((row.get("completion_tokens") or 0) * MODELS[model] + 30 * MODELS[model] / 5) / 1e6
                with lock:
                    spent[0] += row.get("cost") or estimate
                    results[model][key].append(row)
                    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
                    print("%-30s %-8s #%d %s  [~$%.2f]" % (model, key, sample + 1, json.dumps(row)[:150], spent[0]), flush=True)

    with cf.ThreadPoolExecutor(10) as pool:
        list(pool.map(study, enumerate(models)))
    print("done, ~$%.2f" % spent[0])


if __name__ == "__main__":
    main()
