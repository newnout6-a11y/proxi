"""Measuring Minara's models: the probes, the effort study and the generator of minara_models.json entries.

scripts/probe_minara.py runs them by hand over the whole catalog; the relay runs the same code for a model Minara adds
later (minara_refresh), so a model the relay publishes by itself is measured exactly as one measured by hand. Each check
is one small paid request on the account whose bearer token the Client holds:

  tools     one auto tool call (get_weather)
  vision    a 256x256 two-colour PNG
  thinking  one streaming request: where the reasoning arrives (reasoning / reasoning_content / reasoning_details),
            whether it streams before the answer, signatures, reasoning tokens
  effort    none..max on one representative of each family and backend; the others inherit that ladder
  channels  no level and each of the seven, on every Claude model Minara also serves on its Anthropic endpoint: which
            channel answers. Minara sends such a chat request to OpenRouter, which caches with the cache_control marks,
            or to Bedrock or Anthropic's own API, which it reaches without them (the whole prompt billed every turn)
  cache     a Claude head written with hour-long marks and read back: whether the route caches at all
  study     every level and no level, a few samples each, on one hard question: which levels change the reasoning

220 Minara credits are 1 USD of Minara's own price list (measured 2026-11-10).
"""
import base64
import datetime
import json
import math
import pathlib
import re
import secrets
import struct
import time
import zlib

CATALOG_FILE = pathlib.Path(__file__).with_name("minara_models.json")
BASE = "https://api.minara.ai"
CREDITS_PER_USD = 220
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
THINK_TASK = "What is 17 * 23? Think it through briefly, then reply with the number only."
EFFORT_TASK = ("How many 6-digit positive integers have strictly increasing digits (left to right) and a digit sum "
               "divisible by 3? Reply with the number only.")
CHANNEL_TASK = "What is 23 * 47? Reply with the number only."
# The channels of /v1/chat/completions where the relay's cache_control marks work (measured 2026-11-10). A chat request
# that Minara sends to Bedrock (msg_bdrk_...) or to Anthropic's own API (msg_011C...) reports cache_creation_input_tokens
# 0 on every repeat: the marks are dropped on the way and the whole prompt is billed each turn.
CACHING_CHANNELS = frozenset({"openrouter"})
CHANNEL_LEVELS = ("default",) + EFFORTS  # "default": no reasoning_effort at all
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
    """Minara's API with one account's bearer token. `requests` is curl_cffi's module unless given (tests pass a fake);
    `wait(seconds)` sleeps between retries and returns True when the caller wants to stop (the relay shutting down)."""

    def __init__(self, token, requests=None, base=BASE, wait=None):
        if requests is None:
            from curl_cffi import requests
        self.requests = requests
        self.token = token
        self.base = base.rstrip("/")
        self.wait = wait or (lambda seconds: time.sleep(seconds) or False)

    def headers(self, accept="application/json"):
        return {"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Accept": accept}

    def get(self, path):
        return self.requests.get(self.base + path, headers=self.headers(), impersonate="chrome131", timeout=60,
                                 allow_redirects=False)

    def credits(self):
        data = self.get("/auth/me").json()
        part = data.get("creditBalanceBreakdown") or {}
        return float(part.get("plan", 0)) + float(part.get("extra", 0))

    def chat(self, body, stream=False, timeout=240):
        for attempt in range(4):
            # (connect, read) on a stream: curl_cffi makes a lone number its low-speed window too (a silent 120 s cut)
            response = self.requests.post(self.base + "/v1/chat/completions", json=body, impersonate="chrome131",
                                          timeout=(30, timeout) if stream else timeout, stream=stream,
                                          allow_redirects=False,
                                          headers=self.headers("text/event-stream" if stream else "application/json"))
            if response.status_code in (500, 502, 503, 524) and attempt < 3:
                text = response.text if not stream else b"".join(response.iter_content()).decode("utf-8", "replace")
                if _channel_trouble(text) or not text.strip() or response.status_code == 524:
                    if self.wait(8 * (attempt + 1)):  # Minara's windows of empty 5xx; unbilled, retry later
                        return response, text
                    continue
                return response, text
            if stream:
                return response, None
            return response, response.text
        return response, ""

    def messages(self, body, timeout=600):
        """One streamed /v1/messages request (Anthropic's shape), retried like chat() on Minara's empty 5xx."""
        headers = dict(self.headers("text/event-stream"), **{"anthropic-version": "2023-06-01"})
        for attempt in range(4):
            response = self.requests.post(self.base + "/v1/messages", json=dict(body, stream=True),
                                          impersonate="chrome131", timeout=(30, timeout), stream=True,
                                          allow_redirects=False, headers=headers)
            if response.status_code in (500, 502, 503, 524) and attempt < 3:
                text = b"".join(response.iter_content()).decode("utf-8", "replace")
                if _channel_trouble(text) or not text.strip() or response.status_code == 524:
                    if self.wait(8 * (attempt + 1)):
                        return response, text
                    continue
                return response, text
            return response, None
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
    # The image reached the model when it names colours of it: GPT-6 Luna called the dark yellow half green ("green,
    # green"), saw it, and was published blind (its images refused with 422). A model that cannot see says so or names none.
    colours = ("yellow", "green", "gold", "olive", "amber", "lime", "mustard", "khaki", "orange")
    blind = any(words in low for words in ("can't see", "cannot see", "no image", "unable to", "don't see", "not able"))
    return {"status": "ok", "vision": not blind and any(colour in low for colour in colours), "answer": answer[:60],
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
    """Each level streamed, at most 3,000 tokens: Cloudflare in front of Minara cuts a non-streamed answer after 100 s
    with a 502 that is still billed (Grok 4.7 at high, xhigh and max on 2026-11-09, part of 123 credits)."""
    ladder = {}
    for level in EFFORTS:
        body = {"model": model, "max_tokens": 3000, "reasoning_effort": level, "stream": True,
                "stream_options": {"include_usage": True}, "messages": [{"role": "user", "content": EFFORT_TASK}]}
        response, text = client.chat(body, stream=True)
        if response.status_code != 200:
            ladder[level] = {"accepted": False, "status": response.status_code, "detail": (text or "")[:160]}
            continue
        usage, buffer = {}, b""
        for piece in response.iter_content():
            buffer += piece
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
                    try:
                        usage = json.loads(line[5:]).get("usage") or usage
                    except ValueError:
                        pass
        tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if tokens is None:
            tokens = ((usage.get("billing_usage") or {}).get("gemini_usage_metadata") or {}).get("thoughtsTokenCount")
        ladder[level] = {"accepted": True, "reasoning_tokens": tokens, "completion_tokens": usage.get("completion_tokens")}
    return {"status": "ok", "ladder": ladder}


def channel_of(ident):
    """The channel behind a reply id: gen- OpenRouter, msg_bdrk_ Bedrock, any other msg_ Anthropic's own API."""
    ident = str(ident or "")
    if ident.startswith("gen-"):
        return "openrouter"
    if ident.startswith("msg_bdrk"):
        return "bedrock"
    if ident.startswith("msg_"):
        return "anthropic"
    return backend_of({"id": ident}) or "unknown"


def probe_channels(client, model):
    """Which channel answers no level and each of the seven, at the max_tokens an agent sends (32,000).

    Minara chooses it from the level and, for Sonnet 4.6 and Opus 4.6, from max_tokens as well (OpenRouter only up to
    about 2,000 tokens), so a small max_tokens would say nothing about an agent's requests (measured 2026-11-10). One
    tiny streamed request per level; a level whose request fails stays unknown."""
    channels = {}
    for level in CHANNEL_LEVELS:
        body = {"model": model, "max_tokens": 32000, "stream": True, "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": CHANNEL_TASK}]}
        if level != "default":
            body["reasoning_effort"] = level
        response, _ = client.chat(body, stream=True)
        if response.status_code != 200:
            continue
        for line in b"".join(response.iter_content()).split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                continue
            try:
                ident = json.loads(line[5:]).get("id")
            except ValueError:
                continue
            if ident:
                channels[level] = channel_of(ident)
                break
    # Only a full measure is used: a level whose request failed is unknown, and a channel rule built on a gap lowered
    # or raised levels that were never measured (review of 2026-11-10).
    return {"status": "ok" if len(channels) == len(CHANNEL_LEVELS) else "partial" if channels else "failed",
            "channels": channels}


def cached_levels(channels):
    """(offered levels, the level sent for each of the seven, the level sent when none is asked) of a model whose levels
    Minara sends to channels that do not all cache, or None when no level reaches a caching channel.

    Every level goes as the nearest one that lands on OpenRouter (none and minimal as the lowest, xhigh and max as the
    highest), and no level at all goes as high when the model's own default lands on a channel that drops the marks:
    Opus 4.7 at Default, none, xhigh or max wrote nothing to the cache in any of 4 repeats, at low, medium and high it
    read the whole prefix. Reasoning cannot be switched off on OpenRouter for these models, so none means low."""
    rank = EFFORTS.index
    cached = [level for level in EFFORTS if channels.get(level) in CACHING_CHANNELS]
    if not cached:
        return None
    uncached = {level for level in EFFORTS if level in channels and channels[level] not in CACHING_CHANNELS}

    def nearest(level):
        # ties go up: the level is never sent as one that reasons less than the one between
        return min(cached, key=lambda other: (abs(rank(other) - rank(level)), rank(other) < rank(level)))
    mapping = {level: nearest(level) if level in uncached else level for level in EFFORTS}
    known = "default" in channels and channels["default"] not in CACHING_CHANNELS
    default = ("high" if "high" in cached else max(cached, key=rank)) if known else None
    return [level for level in EFFORTS if level not in uncached], mapping, default


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


TIER_STEP = 1.3  # a level reasons differently from its neighbour when their samples do not overlap and this ratio holds
BROKEN_STEP = 2.0  # a level is sent as another only when a lower one reasons this many times more (or it reasons not at all)
NAMES = ("low", "medium", "high", "max")  # the usual names a group of levels is called by


def _median(values):
    values = sorted(values)
    n = len(values)
    return None if not n else values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def _apart(upper, lower, step=None):
    """True when the samples of `upper` lie wholly above those of `lower` and its median is `step` (TIER_STEP) times
    theirs."""
    step = TIER_STEP if step is None else step
    return bool(upper) and bool(lower) and min(upper) > max(lower) and _median(upper) >= step * max(_median(lower), 1)


def effort_tiers(study):
    """What the seven reasoning_effort levels really do on one model, from its effort study
    ({"default" | level: [rows with status and reasoning_tokens]}); None until the default and every level are measured.

    Returns "levels" (what a client is offered), "map" (the level sent upstream for each of the seven) and "control".
    A level that works is sent as asked: one question of medium difficulty shows when levels are broken, not that two
    working levels stay equal on a hard task. Only broken levels are sent as another:
    - a level Minara refuses in words goes as the nearest level that works;
    - a level above medium that reasons clearly less than a lower one (Opus 4.8 and Sonnet 4.6 at xhigh and max: no
      reasoning at all) goes as the strongest group below it (its member nearest below the level), or as nothing when the
      default reasons more than that group;
    - when reasoning can be switched off and the model reasons by default, "none" is offered and sends a level that
      really switches it off (Sonnet 5.5's own "none" still reasons, its "minimal" does not).
    The offered list holds one level per group of levels that reason alike (walking them by median, a level starts a
    new group when its samples lie wholly above those of the group's first level and its median is TIER_STEP times
    theirs, so noise never splits a group and a slow climb cannot chain far-apart levels into one), named in the order
    of their reasoning, without the group that reasons as the default does (Default is that choice). An empty list
    means no level does anything the default does not; the levels a client sends anyway still go upstream."""
    samples, refused = {}, set()
    for level in ("default",) + EFFORTS:
        rows = study.get(level) or []
        values = [row["reasoning_tokens"] for row in rows
                  if row.get("status") == 200 and isinstance(row.get("reasoning_tokens"), (int, float))]
        if values:
            samples[level] = values
        elif any(row.get("status") == 400 and "reasoning effort" in str(row.get("detail", "")).lower() for row in rows):
            refused.add(level)
    measured = [level for level in EFFORTS if level in samples]
    if "default" not in samples or len(measured) + len(refused) < len(EFFORTS):
        return None
    default = samples["default"]
    default_thinks = any(default)
    off = [level for level in measured if not any(samples[level])]
    groups = []
    for level in sorted((level for level in measured if level not in off), key=lambda level: _median(samples[level])):
        # compared with the group's first (weakest) member, so a slow climb cannot chain far-apart levels together
        if groups and not _apart(samples[level], samples[groups[-1][0]]):
            groups[-1].append(level)
        else:
            groups.append([level])
    pooled = [[value for level in group for value in samples[level]] for group in groups]
    strength = [_median(values) for values in pooled]
    group_of = {level: index for index, group in enumerate(groups) for level in group}
    rank = EFFORTS.index

    broken = {}  # level above medium that reasons clearly less than a lower one -> the strongest lower group
    for level in measured:
        # "none" never speaks for a level above it: where it reasons the most (GLM-5.3 Prime: Minara ignores it there),
        # max was judged broken and sent as none, which would switch reasoning off the day Minara honours it.
        lower = [group_of[other] for other in measured
                 if rank(other) < rank(level) and other in group_of and other != "none"]
        best = max(lower, key=lambda index: strength[index], default=None)
        # Broken: no reasoning at all, or at most half the lower group's. Two samples a level are noisy: a gap of 1.3x
        # (Gemini 3.7 Flash at max, 988 and 981 tokens against about 1,100) is no reason to send another level.
        if rank(level) > rank("medium") and best is not None and (
                level in off or _apart(pooled[best], samples[level], BROKEN_STEP)):
            broken[level] = best
    working = [level for level in measured if level not in broken]
    used = sorted({group_of[level] for level in working if level in group_of}, key=lambda index: strength[index])
    names, previous = {}, -1
    for position, index in enumerate(used):
        members = sorted((level for level in groups[index] if level in working), key=rank)
        pool = [level for level in members if level in NAMES] or members
        if position == len(used) - 1:
            choice = max(pool, key=rank)
        else:
            choice = next((level for level in pool if rank(level) > previous), max(pool, key=rank))
        names[index] = choice
        previous = max(previous, rank(choice))
    like_default = {index for index in used
                    if default_thinks and not _apart(default, pooled[index]) and not _apart(pooled[index], default)}
    # The level "none" is sent as must switch reasoning off for real: one at or below medium (xhigh or max that reason
    # not at all are broken levels, which "none" must not rely on staying broken).
    calm = [level for level in off if level not in broken]
    offer_none = bool(calm) and default_thinks
    offered = list(dict.fromkeys(names[index] for index in used if index not in like_default and names[index] != "none"))
    offered.sort(key=rank)
    off_sent = ("none" if "none" in calm else min(calm, key=rank)) if calm else None

    def replacement(level):
        """What a broken level is sent as: nothing when the default reasons more than any lower group (Kimi K2.6), else
        the strongest lower group's member nearest below it, so the reply never costs more than the level asked for."""
        best = broken[level]
        if default_thinks and (_apart(default, pooled[best]) or best in like_default):
            return None  # the default reasons at least as much: send nothing
        members = [other for other in groups[best] if other in working]
        below = [other for other in members if rank(other) <= rank(level)]
        return max(below, key=rank) if below else min(members, key=rank)

    mapping = {}
    for level in EFFORTS:
        if level in broken:
            mapping[level] = replacement(level)
        elif level in refused:
            near = min(working, key=lambda other: (abs(rank(other) - rank(level)), rank(other) > rank(level)))
            mapping[level] = near
        else:
            mapping[level] = level
    if offer_none:
        mapping["none"] = off_sent  # the offered "none" switches reasoning off, whatever the level "none" itself does
    natural = _natural(default, default_thinks, groups, pooled, like_default, working, off_sent)
    return {"levels": (["none"] if offer_none else []) + offered, "map": mapping, "control": "mapped", "natural": natural,
            "default_reasoning_tokens": _median(default), "broken": sorted(broken, key=rank), "refused": sorted(refused),
            "off": off, "groups": [{"levels": group, "median_reasoning_tokens": strength[i]} for i, group in enumerate(groups)]}


def _natural(default, default_thinks, groups, pooled, like_default, working, off_sent):
    """The level that reasons as the model does when no level is sent: what a client that must name a level (Codex) sends
    by default. A model that does not reason by default gets the level that switches reasoning off; one that does, the
    member nearest to medium of the group that reasons like it, or of the group nearest to it when none does."""
    rank = EFFORTS.index
    if not default_thinks:
        return off_sent or "medium"
    candidates = sorted(like_default) or sorted(
        range(len(groups)), key=lambda index: abs(math.log(max(_median(pooled[index]), 1) / max(_median(default), 1))))[:1]
    # Never an extreme for a model that reasons by default: "none" would rely on Minara ignoring it (GLM-5.3 FlashX), and
    # max would make Codex run at its most (and offer Ultra). Then medium, a level every route takes.
    members = [level for index in candidates for level in groups[index]
               if level in working and level not in ("none", "max")]
    if not members:
        return "medium"
    return min(members, key=lambda level: (abs(rank(level) - rank("medium")), rank(level)))


# Studied model that speaks for an unstudied one: its own family's, else the vendor's flagship.
STUDY_FLAGSHIP = {"anthropic": "anthropic/claude-opus-5.5", "openai": "openai/gpt-6.1-sol", "google": "google/gemini-4-argon",
                  "x-ai": "x-ai/grok-4.7", "deepseek": "deepseek/deepseek-v4-pro", "z-ai": "z-ai/glm-5.3-prime",
                  "moonshotai": "moonshotai/kimi-k3"}


def _study_for(upstream, studies):
    if upstream in studies:
        return upstream, "study"
    vendor, family = upstream.split("/")[0], _family(upstream)
    same = [name for name in studies if name.split("/")[0] == vendor and _family(name) == family]
    if same:
        return same[0], "study:" + same[0]
    flagship = STUDY_FLAGSHIP.get(vendor)
    return (flagship, "study:" + flagship) if flagship in studies else (None, None)


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


def generate(results, listing, published=None, study=None):
    """Build minara_models.json from the listing, the saved measurements and OpenRouter's published limits.

    `published` is OpenRouter's public /api/v1/models (id -> entry): the window, the output ceiling and whether the
    model takes reasoning. Minara routes most models through OpenRouter, and its other backends were checked to take
    the same window (WINDOW_CHECKS)."""
    published = published or {}
    studies = {}
    for name, rows in (study or {}).items():
        tiers = effort_tiers(rows)
        if tiers:
            studies[name] = dict(tiers, question=rows.get("_question", "easy"))
    entries = []
    groups = {}
    vendor_ladders = {}
    for upstream in EFFORT_REPRESENTATIVES:
        effort = (results.get(upstream) or {}).get("effort") or {}
        if effort.get("status") == "ok":
            tools = (results.get(upstream) or {}).get("tools") or {}
            groups[(upstream.split("/")[0], backend_of(tools), _family(upstream))] = upstream
            vendor_ladders.setdefault(upstream.split("/")[0], []).append(upstream)
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
        if rep is None and vendor_ladders.get(upstream.split("/")[0]):
            # No ladder of its own nor of its family: the levels every measured model of the vendor took (Minara and
            # OpenRouter read reasoning_effort the same way for all of a vendor's models; GPT-6.1-sol refused none and
            # minimal, so GPT models get low..max).
            names = vendor_ladders[upstream.split("/")[0]]
            sets = [ladder_levels(results[name]["effort"]["ladder"])[0] for name in names]
            levels = [level for level in EFFORTS if all(level in taken for taken in sets)]
            effort_source, effect = "vendor:" + ",".join(names), "inherited"
        own = (measured.get("effort") or {}).get("ladder") or {}
        info = published.get(upstream) or {}
        # A short question can be answered without thinking (Sonnet 5.5 did, Claude 4.x think only when asked): the
        # ladder's harder question and OpenRouter's parameters show that the model reasons.
        reasoning = (thinking.get("thinking") not in (None, "none") or bool(thinking.get("reasoning_tokens"))
                     or ladder_reasons(own) or "reasoning" in (info.get("supported_parameters") or []))
        if not reasoning:
            levels = []
        context, output = _limits(info)
        effort_map, effort_control, effort_detail, natural = None, None, None, None
        studied, study_source = _study_for(upstream, studies)
        if studied is not None and reasoning:
            tiers = studies[studied]
            levels, effort_map, effort_control = tiers["levels"], tiers["map"], tiers["control"]
            effort_source, effect = study_source, "studied"
            effort_detail = {key: tiers[key] for key in ("groups", "broken", "off", "refused", "default_reasoning_tokens")}
            effort_detail["question"] = tiers.get("question", "easy")
            natural = tiers.get("natural")
        channel_probe = measured.get("channels") or {}
        # A partial measure still gives what it measured (cached_levels leaves unknown levels as asked); only a full one
        # can say a model never caches.
        complete = channel_probe.get("status") == "ok"
        channels = channel_probe.get("channels") if channel_probe.get("status") in ("ok", "partial") else None
        default_effort = None
        messages_levels = None
        # The levels /v1/messages reaches a channel with: there they cache where the chat path drops the marks.
        via = {level for level, info in ((measured.get("messages") or {}).get("levels") or {}).items()
               if info.get("caches")}
        if channels:
            routed = cached_levels(channels)
            uncached = {level for level in CHANNEL_LEVELS if level in channels and channels[level] not in CACHING_CHANNELS}
            if routed is None and complete and channels.get("default") not in CACHING_CHANNELS:
                if set(CHANNEL_LEVELS) <= via and reasoning:
                    # No level caches on chat, every one does on /v1/messages: the model goes there whole, each level as
                    # asked, with the levels the Messages API documents for it (no study of that path).
                    messages_levels = list(CHANNEL_LEVELS)
                    levels, effort_map = messages_offer(upstream), {level: level for level in EFFORTS}
                    effort_control, effect, effort_source = "mapped", "messages", "messages:documented"
                elif set(CHANNEL_LEVELS) <= via:
                    messages_levels = list(CHANNEL_LEVELS)
                else:
                    status = "uncached" if status == "ok" else status
            elif routed is not None and reasoning:
                if uncached and uncached <= via:
                    # The levels chat loses go through /v1/messages as asked; Default stays the level that caches on chat
                    # (high: the owner's choice of 2026-11-10), so a client that sends no level is answered as before.
                    messages_levels = [level for level in CHANNEL_LEVELS if level in uncached]
                    chat_levels, _, default_effort = routed
                    offered = set(chat_levels) | (set(messages_offer(upstream)) & {"none", "xhigh", "max"})
                    levels = [level for level in EFFORTS if level in offered]
                    effort_map = {level: level for level in EFFORTS}
                    effort_control, effect = "mapped", "channels+messages"
                    natural = default_effort or natural
                else:
                    # The study's levels are the reasoning of the channel each level reached; the channel decides the cost.
                    levels, effort_map, default_effort = routed
                    effort_control, effect = "mapped", "channels"
                    natural = default_effort or natural
        cache_probe = measured.get("cache") or {}
        if cache_probe.get("status") == "ok" and cache_probe.get("caches") is False and status == "ok":
            status = "uncached"  # a head written with marks was not read back: every turn would pay the whole prompt
        native = native_levels(upstream)
        if reasoning and native:
            # Claude's own levels are offered whatever the study says (the owner, 2026-11-10: Fable 5.2 showed max alone):
            # on the study's question low..high reason alike, on long agent work they differ (tool calls, care, length),
            # and the Messages API takes every one. Only a level sent as itself: one the channels send as another (Opus
            # 4.7's max on a chat path without cache) is not offered as if it were there.
            mapped = effort_map or {}
            extra = [level for level in native if mapped.get(level, level) == level]
            levels = [level for level in EFFORTS if level in set(levels) | set(extra)]
        name = public_id(upstream)
        if name in seen:
            raise SystemExit("duplicate public id " + name)
        seen.add(name)
        entry = {
            "id": name, "upstream_model": upstream, "label": _label(upstream), "provider": upstream.split("/")[0],
            "status": status, "backend": backend, "tools": tools.get("tools") if tools.get("status") == "ok" else None,
            # Anthropic refuses a forced tool choice whenever thinking is on: always on the 5.5 family and Fable 5.1, at
            # any level on the others (OpenRouter turns reasoning_effort into a thinking budget). The relay asks for the
            # call in words on every Claude route, as for Retool's (forced_choice).
            "forced_tool_choice": not upstream.startswith("anthropic/"),
            "vision": vision.get("vision") if vision.get("status") == "ok" else None,
            "reasoning": reasoning, "thinking": thinking.get("thinking"), "thinking_signature": bool(thinking.get("signature")),
            "reasoning_effort_levels": levels, "effort_source": effort_source, "effort_effect": effect,
            "effort_map": effort_map, "effort_control": effort_control, "effort_study": effort_detail,
            "default_effort": default_effort, "natural_effort": natural if reasoning else None, "channels": channels,
            "messages_levels": messages_levels,
            "context": context, "max_output": output,
            "limits_source": ("openrouter /api/v1/models" if context else None),
            "limits_checked": WINDOW_CHECKS.get(upstream),
            "cache": "explicit" if upstream.startswith("anthropic/") else "automatic",
            "pricing": item.get("pricing"),
            "measured_from": source if source != upstream else None,
            "checked_at": measured.get("checked_at"),
        }
        if served and source == upstream and served.split("/")[-1] != upstream.split("/")[-1]:
            entry["served_as"] = served
        entries.append(entry)
    entries.sort(key=lambda e: (e["provider"], e["id"]))
    return {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "source": BASE + "/v1/models", "credits_per_usd": CREDITS_PER_USD, "models": entries}


def native_levels(upstream):
    """The effort levels a Claude model takes natively (Anthropic's Messages API), or () for one without effort: the five
    from low to max on Fable, the 5.x models and Opus 4.7 and 4.8; no xhigh on Claude 4.6; none on older ones."""
    name = upstream.split("/", 1)[-1].replace(".", "-")
    if not upstream.startswith("anthropic/"):
        return ()
    if (name.startswith("claude-fable-") or re.fullmatch(r"claude-(opus|sonnet|haiku)-5(-5)?", name)
            or name in ("claude-opus-4-7", "claude-opus-4-8")):
        return ("low", "medium", "high", "xhigh", "max")
    if name in ("claude-opus-4-6", "claude-sonnet-4-6"):
        return ("low", "medium", "high", "max")
    return ()


def messages_offer(upstream):
    """The levels the Messages API documents as distinct for a Claude model: none where thinking can be switched off, no
    xhigh before Claude 4.7, never minimal (it is low there)."""
    name = upstream.split("/", 1)[-1].replace(".", "-")
    can_disable = not (name.startswith("claude-fable-") or re.fullmatch(r"claude-(opus|sonnet|haiku)-5-5", name))
    no_xhigh = name in ("claude-opus-4-6", "claude-sonnet-4-6")
    return [level for level in EFFORTS if level != "minimal" and not (level == "none" and not can_disable)
            and not (level == "xhigh" and no_xhigh)]


# Minara's own backends checked against the published window (2026-11-09, prompts of about 943k tokens).
WINDOW_CHECKS = {
    "deepseek/deepseek-v4-flash": "943,533 tokens answered; 1,107,041 refused: maximum 1,048,576 (2026-11-09)",
    "z-ai/glm-5.3-flash": "943,351 tokens answered (2026-11-09)",
}
# OpenRouter lists a provider's whole budget as max_completion_tokens where it states no output ceiling (943,718 of a
# 1,048,576 window). No model writes more than this in one reply; Minara cuts a larger max_tokens silently.
OUTPUT_CEILING = 128000


def _limits(info):
    """(context window, output ceiling) of an OpenRouter model entry, or (None, None)."""
    top = info.get("top_provider") or {}
    context = top.get("context_length") or info.get("context_length")
    output = top.get("max_completion_tokens")
    context = int(context) if isinstance(context, (int, float)) and context > 0 else None
    output = int(output) if isinstance(output, (int, float)) and output > 0 else None
    if output is not None:
        output = min(output, OUTPUT_CEILING)
    elif context is not None:
        output = min(OUTPUT_CEILING, context // 4)
    return context, output


def fetch_published():
    """OpenRouter's public model list: no key, no cost."""
    from curl_cffi import requests
    response = requests.get("https://openrouter.ai/api/v1/models", impersonate="chrome131", timeout=60)
    response.raise_for_status()
    return {item["id"]: item for item in response.json().get("data", []) if isinstance(item, dict) and item.get("id")}


def _family(upstream):
    name = upstream.split("/", 1)[1]
    prefixes = ("claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5", "gpt-6.1", "gpt-6", "gpt-5.6", "gpt-5",
                "gemini", "grok", "glm", "deepseek", "kimi", "claude-opus-4", "claude-sonnet-4", "claude-opus-5",
                "claude-sonnet-5", "claude-fable", "claude-haiku-4", "o3", "gpt-4", "gpt-3", "gpt-oss", "minimax")
    for prefix in sorted(prefixes, key=len, reverse=True):  # gpt-6.1-sol is gpt-6.1 before it is gpt-6
        if name.startswith(prefix):
            return prefix
    return name


def _substituted(upstream, served):
    """True when upstream answered with another model: glm-5.3 served as glm-5-3-flash-260828. A date or version suffix
    (deepseek-v4-pro-260425, glm-5-2-260617) is the same model."""
    want = upstream.split("/")[-1].replace(".", "-").replace("_", "-").lower()
    got = served.split("/")[-1].replace(".", "-").replace("_", "-").lower()
    got = re.sub(r"(-\d{4,8})+$", "", got)
    return got != want and got != re.sub(r"(-\d{4,8})+$", "", want)


def _label(upstream):
    name = upstream.split("/", 1)[1]
    return name.replace("-", " ").replace("claude ", "Claude ").replace("gpt ", "GPT-").title().replace("Gpt", "GPT") + " (Minara)"


def merged_study(easy_path, hard_path):
    """The effort study per model: the harder question's rows where every level was measured on it, else the first one's.

    The first question (digit sum 19 below 10,000) takes GPT-6 and Gemini 4 Argon 50-400 reasoning tokens at every level,
    too little for working levels to separate; the harder one (digit sum 27 and divisible by 11 below 1,000,000) makes
    them think long enough. Each model's rows say which question they answer ("_question")."""
    easy = json.loads(easy_path.read_text(encoding="utf-8")) if easy_path.exists() else {}
    hard = json.loads(hard_path.read_text(encoding="utf-8")) if hard_path.exists() else {}
    study = {name: dict(rows, _question="easy") for name, rows in easy.items()}
    for name, rows in hard.items():
        if effort_tiers(rows) is not None:
            study[name] = dict(rows, _question="hard")
    return study


# The effort study's hard question (scripts/study_minara_effort.py --hard): it makes the strong models think long enough
# for working levels to separate. Its answer, counted by brute force over every n below 10**6.
HARD_QUESTION = ("How many positive integers less than 1,000,000 have a digit sum of exactly 27 and are divisible by 11? "
                 "Reply with the number only.")
HARD_ANSWER = "4050"
STUDY_LEVELS = (None,) + EFFORTS  # None: no reasoning_effort at all, the model's own default
STUDY_MAX_TOKENS = 12000
CACHE_HEAD_SENTENCES = 240  # about 5,800 tokens: above every Claude model's smallest cacheable prefix (4,096)


def _usage_tokens(usage):
    """(uncached input, cache read, cache write, output) of a chat usage block; cache write None when not reported."""
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    read = details.get("cached_tokens") or 0
    write = details.get("cache_write_tokens") if "cache_write_tokens" in details else None
    prompt = usage.get("prompt_tokens") or 0
    return max(0, prompt - read - (write or 0)), read, write, usage.get("completion_tokens") or 0


def _stream_summary(client, body, timeout=600):
    """One streamed chat request: status, reply id, OpenRouter's provider, usage, text and reasoning characters."""
    response, text = client.chat(body, stream=True, timeout=timeout)
    if response.status_code != 200:
        if text is None:
            text = b"".join(response.iter_content()).decode("utf-8", "replace")
        return {"status": response.status_code, "detail": (text or "")[:300]}
    ident, provider, usage, answer, reasoning, buffer = "", None, {}, "", 0, b""
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
            if not isinstance(item, dict):
                continue
            ident = ident or str(item.get("id") or "")
            provider = provider or item.get("provider")
            usage = item.get("usage") or usage
            for choice in item.get("choices") or []:
                delta = choice.get("delta") or {}
                for key in ("reasoning", "reasoning_content"):
                    if isinstance(delta.get(key), str):
                        reasoning += len(delta[key])
                if isinstance(delta.get("content"), str):
                    answer += delta["content"]
    return {"status": 200, "id": ident, "provider": provider, "usage": usage, "text": answer, "reasoning_chars": reasoning}


def probe_cache(client, model, level=None):
    """A Claude head of about 5,800 tokens with hour-long marks, sent twice: the route caches when the second request reads
    what the first wrote. The head is new every time (a nonce), so the first request always writes."""
    head = "Session %s. " % secrets.token_hex(6) + " ".join(
        "Crate %d sits in bay %d and weighs %d kg." % (n, n % 17, 10 + n % 50) for n in range(CACHE_HEAD_SENTENCES))
    mark = {"type": "ephemeral", "ttl": "1h"}
    body = {"model": model, "max_tokens": 32000, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": [{"type": "text", "text": head, "cache_control": mark}]},
                         {"role": "user", "content": [{"type": "text", "text": "Which bay holds crate 40? One sentence.",
                                                       "cache_control": mark}]}]}
    if level:
        body["reasoning_effort"] = level
    # As the relay sends a Claude head (minara.build_body): one key, so OpenRouter keeps both requests on one provider.
    body["prompt_cache_key"] = "proxi-probe-" + secrets.token_hex(12)
    rows = []
    for _ in range(2):
        row = _stream_summary(client, body)
        if row.get("status") != 200:
            return {"status": classify_error(row.get("status") or 0, row.get("detail") or ""), "detail": row.get("detail")}
        rows.append(row)
    _, _, written, _ = _usage_tokens(rows[0]["usage"])
    _, read, _, _ = _usage_tokens(rows[1]["usage"])
    return {"status": "ok", "caches": read > 0, "written": written, "read": read,
            "channels": [channel_of(row["id"]) for row in rows], "providers": [row.get("provider") for row in rows]}


def _messages_summary(client, body):
    """One streamed /v1/messages request: status, reply id, usage, thinking characters, stop reason."""
    response, text = client.messages(body)
    if response.status_code != 200:
        if text is None:
            text = b"".join(response.iter_content()).decode("utf-8", "replace")
        return {"status": response.status_code, "detail": (text or "")[:300]}
    ident, usage, thinking, stop, buffer = "", {}, 0, None, b""
    for piece in response.iter_content():
        buffer += piece
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                item = json.loads(line[5:])
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "message_start":
                message = item.get("message") or {}
                ident = str(message.get("id") or "")
                usage.update(message.get("usage") or {})
            elif kind == "content_block_delta":
                delta = item.get("delta") or {}
                if delta.get("type") == "thinking_delta":
                    thinking += len(delta.get("thinking") or "")
            elif kind == "message_delta":
                usage.update({k: v for k, v in (item.get("usage") or {}).items() if v is not None})
                stop = (item.get("delta") or {}).get("stop_reason")
            elif kind == "error":
                return {"status": "stream_error", "detail": json.dumps(item)[:300]}
    return {"status": 200, "id": ident, "usage": usage, "thinking_chars": thinking, "stop": stop}


def _messages_cache(client, model, level):
    """probe_cache on /v1/messages: the head written with hour-long marks, then read back, at one level."""
    import minara
    head = "Session %s. " % secrets.token_hex(6) + " ".join(
        "Crate %d sits in bay %d and weighs %d kg." % (n, n % 17, 10 + n % 50) for n in range(CACHE_HEAD_SENTENCES))
    mark = {"type": "ephemeral", "ttl": "1h"}
    chat = {"model": model, "max_tokens": 32000,
            "messages": [{"role": "system", "content": [{"type": "text", "text": head, "cache_control": mark}]},
                         {"role": "user", "content": [{"type": "text", "text": "Which bay holds crate 40? One sentence.",
                                                       "cache_control": mark}]}]}
    if level != "default":
        chat["reasoning_effort"] = level
    body = minara.messages_body(chat)
    rows = []
    for _ in range(2):
        row = _messages_summary(client, body)
        if row.get("status") != 200:
            return {"status": classify_error(row.get("status") or 0, row.get("detail") or ""), "detail": row.get("detail")}
        rows.append(row)
    written = rows[0]["usage"].get("cache_creation_input_tokens") or 0
    read = rows[1]["usage"].get("cache_read_input_tokens") or 0
    return {"status": "ok", "caches": read > 0, "written": written, "read": read, "level": level,
            "channels": [channel_of(row["id"]) for row in rows]}


def probe_messages(client, model):
    """Every level through Minara's /v1/messages, written as the relay writes it (minara.messages_body): which channel
    answers, whether the reasoning shows, and whether that channel reads what it wrote (one pair of requests per channel).

    The relay sends a Claude level there when the chat path loses its cache marks (Bedrock, Anthropic's API)."""
    import minara
    levels = {}
    for level in CHANNEL_LEVELS:
        chat = {"model": model, "max_tokens": 32000, "messages": [{"role": "user", "content": CHANNEL_TASK}]}
        if level != "default":
            chat["reasoning_effort"] = level
        row = _messages_summary(client, minara.messages_body(chat))
        info = {"status": row.get("status")}
        if row.get("status") == 200:
            info.update(channel=channel_of(row.get("id")), thinking_chars=row.get("thinking_chars"),
                        output_tokens=(row.get("usage") or {}).get("output_tokens"))
        else:
            info["detail"] = row.get("detail")
        levels[level] = info
    caching = {}
    for level in CHANNEL_LEVELS:
        info = levels[level]
        if info.get("status") != 200 or info.get("channel") in caching:
            continue
        caching[info["channel"]] = _messages_cache(client, model, level)
    for info in levels.values():
        cache = caching.get(info.get("channel")) or {}
        info["caches"] = cache.get("caches") if info.get("status") == 200 and cache.get("status") == "ok" else None
    answered = [level for level in CHANNEL_LEVELS if levels[level].get("status") == 200]
    return {"status": "ok" if len(answered) == len(CHANNEL_LEVELS) else "partial" if answered else "failed",
            "levels": levels, "caching": caching}


def study_row(client, model, level):
    """One sample of the effort study: the hard question at `level` (None: no level), streamed."""
    body = {"model": model, "max_tokens": STUDY_MAX_TOKENS, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": HARD_QUESTION}]}
    if level is not None:
        body["reasoning_effort"] = level
    started = time.monotonic()
    try:
        row = _stream_summary(client, body)
    except Exception as exc:  # noqa: BLE001 - a stream cut mid-way: the sample is missing, the study goes on
        return {"status": "stream_error", "detail": type(exc).__name__}
    if row.get("status") != 200:
        return {"status": row.get("status"), "detail": row.get("detail")}
    usage = row.get("usage") or {}
    tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if tokens is None:
        tokens = ((usage.get("billing_usage") or {}).get("gemini_usage_metadata") or {}).get("thoughtsTokenCount")
    return {"status": 200, "reasoning_tokens": tokens, "completion_tokens": usage.get("completion_tokens"),
            "reasoning_chars": row.get("reasoning_chars"), "secs": round(time.monotonic() - started, 1),
            "correct": HARD_ANSWER in (row.get("text") or "").replace(",", ""), "cost": usage.get("cost")}


def study_effort(client, model, samples=2, stop=None, budget=None):
    """The effort study of one model on the hard question: {"default" | level: [rows]}, "_question": "hard".

    With a `budget` (credits), the first request's cost decides: when the whole study would cost more, it stops there and
    returns None (models reason from 300 to 27,000 tokens on this question, so no price list says it in advance)."""
    rows = {"_question": "hard"}
    before = client.credits() if budget is not None else None
    for level in STUDY_LEVELS:
        key = "default" if level is None else level
        for _ in range(samples):
            if stop is not None and stop():
                return rows
            rows.setdefault(key, []).append(study_row(client, model, level))
            if before is not None:
                client.wait(1.5)  # Minara debits right after the reply
                first = max(0.0, before - client.credits())
                before = None
                if first * len(STUDY_LEVELS) * samples > budget:
                    return None
    return rows


def _credits(pricing, input_tokens=0, output_tokens=0, read=0, write=0):
    import minara  # the relay's own price reader (flat and tiered prices)
    cost = minara.estimated_cost(pricing, {"input_tokens": input_tokens, "output_tokens": output_tokens,
                                           "cache_read": read, "cache_write": write})
    return None if cost is None else cost * CREDITS_PER_USD


def checks_for(item):
    """The checks a model of the listing is measured with. A Claude model Minara also serves on its Anthropic endpoint gets
    its channels on chat and on /v1/messages (which tell where it caches); any other Claude model one cache check."""
    checks = ["tools", "vision", "thinking"]
    if "anthropic" in (item.get("supported_endpoint_types") or []):
        checks += ["channels", "messages"]
    elif item["id"].startswith("anthropic/"):
        checks.append("cache")
    return checks + ["study"]


def measure_estimate(pricing, checks, samples=2):
    """A generous estimate of what measuring one model costs, in Minara credits (None: the price cannot be read).

    Measured on 2026-11-10, Claude Fable 5.2 ($8/$40 per million): 1.5 credits for tools, vision and thinking, about 30 for
    the cache check with a 13k-token head, 70 for a study of three samples a level; this estimate is about twice that."""
    base = _credits(pricing, 3 * 300, 3 * 800)
    if base is None:
        return None
    total = base
    if "channels" in checks:
        total += _credits(pricing, 8 * 40, 8 * 400) or 0
    if "messages" in checks:
        total += (_credits(pricing, 8 * 40, 8 * 400) or 0) + 2 * (_credits(pricing, 0, 400, read=6000, write=6000) or 0)
    if "cache" in checks:
        total += _credits(pricing, 0, 2 * 200, read=6000, write=6000) or 0
    if "study" in checks:
        total += _credits(pricing, 8 * samples * 60, 8 * samples * 2000) or 0
    return round(total, 2)


def measure_model(client, item, checks=None, samples=2, stop=None, study_budget=None):
    """Every check of one listed model, in order: (results for generate(), the effort study's rows or None).

    `stop()` returning True ends the measurement between two requests (the relay stopping, a budget spent); the study is
    left out when its first request shows it would cost more than `study_budget` credits."""
    upstream = item["id"]
    checks = list(checks or checks_for(item))
    probes = {"tools": probe_tools, "vision": probe_vision, "thinking": probe_thinking, "channels": probe_channels,
              "messages": probe_messages, "cache": probe_cache}
    record = {}
    for check in checks:
        if check == "study" or (stop is not None and stop()):
            continue
        try:
            record[check] = probes[check](client, upstream)
        except Exception as exc:  # noqa: BLE001 - one failing check must not end the measurement
            record[check] = {"status": "exception", "detail": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
        if check == "tools" and record[check].get("status") in ("broken", "unavailable"):
            break  # errors inside a 200 (MiniMax) or no channel at all: the other checks would say the same
    record["checked_at"] = datetime.date.today().isoformat()
    study = None
    usable = (record.get("tools") or {}).get("status") == "ok" or (record.get("thinking") or {}).get("status") == "ok"
    if "study" in checks and usable and not (stop is not None and stop()):
        study = study_effort(client, upstream, samples=samples, stop=stop, budget=study_budget)
    return record, study
