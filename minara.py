"""Minara's OpenAI-compatible SSE transport, isolated from Retool state and billing.

No automatic generation retries: a disconnected request may already have been billed.
"""
import asyncio
import contextlib
import copy
import datetime
import functools
import hashlib
import json
import math
import os
import queue
import re
import threading
import time
import zoneinfo

import apierr
import client_context
import forced_choice
import journal
import redact

BASE = "https://api.minara.ai/v1"
BILLING_PREFIX = "x-anthropic-billing-header:"
HEAD_WARM = 3300.0  # seconds a Claude head used by a request stays readable without a leader: under the marks' hour
SILENCE_LIMIT = 600.0  # seconds a reply may send nothing (a model that reasons before it writes) before curl cuts it  # Claude Code's attribution line (prompt_policy.BILLING_PREFIX)
MAX_EVENT_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 128


def _bounded_json(text, **kwargs):
    """Enforce the same nesting budget across Python JSON implementations.

    Python 3.14 may parse deep input without RecursionError. Check brackets
    outside quoted strings before decoding, without constructing a deep tree.
    The JSON decoder remains responsible for syntax validation.
    """
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError('JSON nesting exceeds the limit')
        elif char in ']}':
            depth -= 1
    value = json.loads(text, **kwargs)
    pending = [value]
    while pending:
        part = pending.pop()
        if isinstance(part, str):
            # Escaped unpaired surrogates are legal decoder input, but cannot
            # reach UTF-8 headers, client writers or tool-fragment byte limits.
            try:
                part.encode('utf-8')
            except UnicodeEncodeError:
                raise ValueError('JSON contains invalid Unicode') from None
        elif isinstance(part, dict):
            pending.extend(part.keys())
            pending.extend(part.values())
        elif isinstance(part, list):
            pending.extend(part)
    return value


def _worker_slots(cfg):
    """One physical request budget per runtime loop, shared by every account.

    A canceled lease may finish before curl receives headers. Its slot belongs
    to the physical worker until that worker and response cleanup really exit.
    Admission itself is polled asynchronously: no queued thread or paid POST.
    """
    loop = asyncio.get_running_loop()
    slots = getattr(loop, '_minara_worker_slots', None)
    if slots is None:
        slots = threading.BoundedSemaphore(max(1, int(cfg.get('max_workers', 32))))
        loop._minara_worker_slots = slots
    return slots


def token_of(cfg):
    name = cfg.get("bearer_token_env")
    return (os.environ.get(name, "") if name else "") or cfg.get("bearer_token", "")


def _content(blocks):
    if isinstance(blocks, str):
        return blocks
    result = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            result.append({"type": "text", "text": block.get("text", "")})
        elif kind == "image":
            data = block.get("imageData", "")
            url = data if data.startswith(("https://", "http://", "data:")) else (
                "data:%s;base64,%s" % (block.get("mimeType", "image/png"), data))
            result.append({"type": "image_url", "image_url": {"url": url}})
        elif kind == "image_url":
            result.append(copy.deepcopy(block))
    return result


def _message(message):
    role = message.get("role", "user")
    value = message.get("content")
    if role == "tool" and isinstance(value, dict):
        result = value.get("toolResult", "")
        if (isinstance(result, list) and result
                and all(isinstance(block, dict) and block.get("type") == "text" for block in result)):
            # Text blocks (MCP results, Claude Code's Agent and ToolSearch results): their text, as a model reads them
            # natively. json.dumps showed it the escaped blocks ('[{"type": "text", "text": "line \"one\"\n..."}]'): exact
            # text lost for edits, and every result paid for the wrapper and the escapes (audit, 2026-11-10).
            result = "\n".join(block.get("text") or "" for block in result)
        elif not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)
        return {"role": "tool", "tool_call_id": value.get("toolCallId"), "content": result}
    result = {"role": role, "content": _content(value)}
    if role == "assistant":
        calls = []
        for block in value if isinstance(value, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_call":
                calls.append({"id": block["toolCallId"], "type": "function", "function": {
                    "name": block["toolName"], "arguments": json.dumps(block.get("toolInput", {}), ensure_ascii=False)}})
        if calls:
            result["tool_calls"] = calls
            result["content"] = result["content"] or None
    return result


def _messages(history):
    """Keep tool IDs paired; expose canonical image results as visual input.

    OpenAI tool messages are text-only. Queue visual blocks until the entire
    contiguous group of tool results is answered, then send one user message.
    Ordinary JSON tool values are never mistaken for native content blocks.
    """
    images = []
    known = {'text', 'image', 'image_url'}
    for message in history:
        role = message.get('role')
        if role != 'tool' and images:
            yield {'role': 'user', 'content': images}
            images = []
        value = message.get('content')
        result = value.get('toolResult') if role == 'tool' and isinstance(value, dict) else None
        native = (isinstance(result, list) and bool(result)
                  and all(isinstance(block, dict) and block.get('type') in known for block in result)
                  and any(block['type'] in ('image', 'image_url') for block in result))
        if not native:
            yield _message(message)
            continue
        call_id = value.get('toolCallId')
        texts = [block.get('text', '') for block in result if block['type'] == 'text']
        visual = _content([block for block in result if block['type'] in ('image', 'image_url')])
        yield {'role': 'tool', 'tool_call_id': call_id,
               'content': '\n'.join(texts) + ('\n' if texts else '') + 'Image result follows as visual input.'}
        images.append({'type': 'text', 'text': 'Images returned by tool call ' + str(call_id) + ':'})
        images.extend(visual)
    if images:
        yield {'role': 'user', 'content': images}


def _mark(message, ttl):
    """Put a cache breakpoint on the last part of a message; False when the message has nothing to carry one."""
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return False
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list) or not content:
        return False
    last = content[-1]
    if not isinstance(last, dict) or last.get("type") != "text" or not last.get("text"):
        return False
    content = [*content[:-1], dict(last, cache_control=dict({"type": "ephemeral"}, **({"ttl": ttl} if ttl else {})))]
    message["content"] = content
    return True


def _cache_marks(messages, ttl, mark_tail=True):
    """Anthropic caches only up to explicit breakpoints (measured on Minara: none, no cache; a top-level mark is ignored).

    At most four: the system head (with the tools, which come before it), the last message, and the end of the turn
    before it, so that a request continues the prefix the previous one wrote even when a batch of tool results adds
    more than the twenty blocks upstream looks back from a breakpoint. A message whose last part cannot carry a mark
    (an assistant reply of only calls, an image) passes it to the message before."""
    marks = 0
    if messages and messages[0].get("role") == "system" and _mark(messages[0], ttl):
        marks += 1
    last_user = [i for i, message in enumerate(messages) if message.get("role") in ("user", "tool")]
    wanted = []
    if last_user:
        if mark_tail:
            wanted.append(last_user[-1])
        elif len(last_user) > 1:
            # A side request (a client's compaction, a one-shot job) is never continued: its own last message, the
            # instruction, would be written for an hour and never read (2,053 and 2,142 tokens of two Claude Code
            # compactions, 2026-11-10). The message before it, the end of the dialog's own history (a mid-turn
            # compaction's latest tool results), is marked instead: everything up to there is read. Not on /v1/messages,
            # where a reply that writes nothing costs the whole prompt at the input price (_stream marks the tail there).
            wanted.append(last_user[-2])
        # The end of the previous turn: the last user/tool message before the latest assistant reply.
        latest_assistant = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=None)
        if latest_assistant is not None:
            before = [i for i in last_user if i < latest_assistant]
            if before:
                wanted.append(before[-1])
    lowest = 1 if messages and messages[0].get("role") == "system" else 0  # no head: the first message can carry one
    for index in wanted:
        if marks >= 4:
            break
        for candidate in range(index, lowest - 1, -1):
            if _mark(messages[candidate], ttl):
                marks += 1
                break


def build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks=None, session=None,
               instruction=None, mark_tail=True):
    """Translate canonical client history, not signed Retool replies, to OpenAI roles.

    `instruction` is a relay-only user turn after the last message (the forced tool choice asked in words): it gets no
    cache mark and is never part of a stored history, so the next request finds the prefix the cache holds."""
    model = model_provider.get("upstream_model") or model_provider.get("model", "")
    if model.startswith("minara/"):
        model = model[len("minara/"):]
    first, separator, rest = (head or "").partition("\n")
    if first.startswith(BILLING_PREFIX) and separator:
        # Claude Code's attribution line (x-anthropic-billing-header: cc_version=...; cch=<per-conversation>;) goes
        # nowhere on Minara. Glued to the instructions, it made Minara drop the whole system prompt: 25 prompt tokens, the
        # model knew none of the rules (Opus 5.5 and 4.7, 2026-11-10), so Claude Code ran without its system prompt on
        # minara/claude-*. Without it the instructions arrive, and every conversation of a client has the same head: its
        # fingerprint no longer splits the cache, so subagents read the head one of them wrote.
        head = rest
    messages = [{"role": "system", "content": head}] if head else []
    canonical = getattr(session, "_native_client_messages", None)
    if canonical is not None and getattr(session, "_native_allow_cold_replay", True):
        messages.extend(_messages(canonical))
    elif chunks:
        for part in chunks:
            if isinstance(part, dict) and part.get("role"):
                messages.append(_message(part))
            else:
                messages.append({"role": "user", "content": _content(part) if isinstance(part, list) else str(part)})
    elif prompt:
        messages.append({"role": "user", "content": prompt})
    # No reasoning_details go back with the replies: Minara does not pass them on. Sent or not, the prompt has the same
    # tokens and reads the same cache, in a tool loop as after it, on Opus 5.5, Opus 4.7, Sonnet 5.5, GPT-6.1-sol,
    # Gemini 4 Argon and 3.6 Flash, Grok 4.7, Kimi K3, DeepSeek V4 Pro and GLM-5.3 Prime (measured 2026-11-10), and every
    # one of them continues a tool call without them. Kept, they cost memory and a larger body on every request.
    if model.startswith("anthropic/") and cfg.get("direct_cache", True):
        ttl = cfg.get("minara_cache_ttl", "1h")
        # An hour costs Minara's accounts nothing more than five minutes (it bills every write at its list price), and a
        # subagent that waits on a long command still reads its own history when it comes back.
        _cache_marks(messages, ttl if ttl in ("1h", "5m") else None, mark_tail)
    if instruction:
        messages.append({"role": "user", "content": instruction})
    body = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if tools:
        declared = []
        for tool in tools:
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                # Only the standard fields: the relay marks a Codex custom tool inside its function (x-client-type,
                # x-format with the whole grammar), which no upstream should see.
                fn = {key: copy.deepcopy(value) for key, value in tool["function"].items()
                      if key in ("name", "description", "parameters", "strict")}
                declared.append({"type": "function", "function": fn})
            else:
                fn = {key: copy.deepcopy(tool[key]) for key in ("name", "description", "parameters", "strict") if key in tool}
                if "parameters" not in fn:
                    fn["parameters"] = copy.deepcopy(tool.get("input_schema", {"type": "object", "properties": {}}))
                declared.append({"type": "function", "function": fn})
        body["tools"] = declared
    if tool_choice:
        body["tool_choice"] = (tool_choice if tool_choice in ("auto", "none", "required") else
                               {"type": "function", "function": {"name": tool_choice}}) if isinstance(tool_choice, str) else copy.deepcopy(tool_choice)
    effort = model_provider.get("reasoningEffort") or model_provider.get("reasoning_effort") or model_provider.get("effort")
    if effort and effort != "auto":
        body["reasoning_effort"] = effort
    conversation = getattr(session, "_native_conversation_key", None) if session is not None else None
    if model.startswith("openai/") and conversation:
        # OpenAI routes a request with a cache key to where the same key's prefix is: through Minara, GPT-6.1 Sol read
        # its cached prefix in 6 of 7 follow-ups with one and 1 of 7 without (2026-11-10). One key per conversation;
        # a hash, so nothing of the client's identifiers goes upstream.
        body["prompt_cache_key"] = "proxi-" + hashlib.sha256(
            json.dumps(conversation, default=str).encode("utf-8")).hexdigest()[:32]
    elif model.startswith("anthropic/"):
        # OpenRouter spreads some Claude models over providers with caches of their own (Fable 5.2: Anthropic, Google
        # Vertex, Azure): without a key a follow-up read the head in 3 of 5 requests and each new provider wrote it again
        # (12.8 credits a 5.8k head), with one all 6 went to one provider and every follow-up read it (2026-11-10). The key
        # is the head's (model, instructions, tools), not the conversation's: a client's conversations and subagents share
        # their head, so they share the provider that holds it too, and a level change keeps it.
        head_part = messages[0].get("content") if messages and messages[0].get("role") == "system" else None
        seed = [model, head_part, body.get("tools")] if head_part else [model, conversation]
        if head_part or conversation:
            body["prompt_cache_key"] = "proxi-" + hashlib.sha256(json.dumps(
                seed, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return body


def sse_data(chunks, max_bytes=MAX_EVENT_BYTES):
    """Parse SSE byte chunks with bounded lines/events, including split UTF-8/CRLF."""
    buffer, data, size = b"", [], 0
    for chunk in chunks:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if len(line) > max_bytes:
                raise apierr.upstream_error("Minara SSE event exceeds the size limit")
            if not line:
                if data:
                    yield "\n".join(data)
                data, size = [], 0
            elif line.startswith(b"data:"):
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                size += len(value)
                if size > max_bytes:
                    raise apierr.upstream_error("Minara SSE event exceeds the size limit")
                try:
                    data.append(value.decode("utf-8"))
                except UnicodeError:
                    raise apierr.upstream_error("Minara SSE contains invalid UTF-8") from None
        if len(buffer) > max_bytes:
            raise apierr.upstream_error("Minara SSE line exceeds the size limit")
    if buffer.strip() or data:
        raise apierr.upstream_error("Minara SSE ended inside an event")


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _modalities(value):
    """The audio and image tokens inside an OpenAI-shaped usage's prompt_tokens and completion_tokens: a price expression
    that bills them apart (new-api's ai, img, ao, img_o) takes them out of p and c. Only the price reads them."""
    found = {}
    for field, source, target in (("prompt_tokens_details", "audio_tokens", "audio_input_tokens"),
                                   ("prompt_tokens_details", "image_tokens", "image_input_tokens"),
                                   ("completion_tokens_details", "audio_tokens", "audio_output_tokens"),
                                   ("completion_tokens_details", "image_tokens", "image_output_tokens")):
        details = value.get(field)
        number = _count(details.get(source)) if isinstance(details, dict) else None
        if number:
            found[target] = number
    return found


def normalize_usage(value, pricing=None):
    result = {}
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
        number = _count(value.get(source))
        if number is not None:
            result[target] = number
    for field, source, target in (("prompt_tokens_details", "cached_tokens", "cache_read"),
                                   ("prompt_tokens_details", "cache_write_tokens", "cache_write"),
                                   ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens")):
        details = value.get(field)
        number = _count(details.get(source)) if isinstance(details, dict) else None
        if number is not None:
            result[target] = number
    details = value.get("prompt_tokens_details")
    if isinstance(details, dict):
        # OpenRouter always reports cache_write_tokens (0 when nothing was written); Minara's Bedrock and Anthropic paths
        # leave the field out: their answer never caches (measured 2026-11-10). The orchestrator says so in the journal.
        result["cache_write_reported"] = "cache_write_tokens" in details
    # OpenAI prompt_tokens includes cached tokens; relay input_tokens excludes
    # them. Adapters and journal add the canonical read/write counters back.
    if 'input_tokens' in result:
        cached = result.get('cache_read', 0) + result.get('cache_write', 0)
        if cached <= result['input_tokens']:
            result['input_tokens'] -= cached
        else:
            # Contradictory details must not inflate an authoritative total.
            result.pop('cache_read', None)
            result.pop('cache_write', None)
    cost = value.get("cost")
    reported = cost if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0 else None
    # Minara debits its own price list (its /v1/models pricing), 220 credits per dollar of it, not the cost upstream
    # reports: a cache write is billed at its cache_write price (1.25x input) whether its TTL is 5 minutes or an hour,
    # Sonnet 5.5's reads at $0.2/M. OpenRouter's usage.cost bills an hour-long write at 2x input: an Opus 5.5 write of
    # 17,139 tokens reported 30.19 credits and was debited 18.88, the list price; balances over whole runs matched the list
    # to 0.0002 credits (end-to-end runs of 2026-11-10). So the list prices every reply it can read; what upstream reported
    # stays as upstream_cost, for diagnosis.
    estimate = (estimated_cost(pricing, dict(result, **_modalities(value)))
                if pricing is not None and 'input_tokens' in result else None)
    if estimate is not None:
        result.update(provider_cost=estimate, provider_cost_currency="USD", provider_cost_estimated=True)
        if reported is not None:
            result["upstream_cost"] = reported
    elif reported is not None and not (reported == 0 and result.get("output_tokens")):
        result["provider_cost"] = reported  # NOT Retool credits.
        currency = value.get("currency")
        if isinstance(currency, str) and 1 <= len(currency) <= 16 and currency.isascii() and currency.isalpha():
            result["provider_cost_currency"] = currency.upper()
        else:
            result["provider_cost_currency"] = "USD"
    return result


def estimated_cost(pricing, usage):
    """USD for one reply at the catalog price (per million tokens): flat, or Minara's price expression; None when the
    price cannot be read (a price per request, an expression this reader does not know): price_problem says why."""
    return _priced(pricing, usage)[0]


# A reply with every counter the price expressions read: price_problem prices it to find out whether a price can be read.
_PROBE_USAGE = {"input_tokens": 1000, "output_tokens": 1000, "cache_read": 1000, "cache_write": 1000}


def price_problem(pricing):
    """Why the relay cannot price the replies of a model with this catalog price, in a few Russian words; None when it
    can. A model whose price cannot be read has every turn unpriced, and the catalog refresh does not measure it."""
    return _priced(pricing, _PROBE_USAGE)[1]


def write_cost(pricing, usage):
    """USD the cache write of one reply cost at the catalog price (for an expression, at the tier the whole prompt falls
    in), None when it cannot be told: the [WRITE COSTLY] warning (credits.write_cost) reads it."""
    written = usage.get("cache_write") if isinstance(usage, dict) else None
    if type(written) not in (int, float) or not math.isfinite(written) or written <= 0 or not isinstance(pricing, dict):
        return None
    if pricing.get("quota_type") not in (0, None):
        return None
    if pricing.get("billing_mode") == "tiered_expr":
        try:
            formula = _formula(pricing.get("billing_expr"))
            names = _token_names(formula.used, usage)
            # Only the write is priced: the other counters are 0, the tier is still chosen by the whole prompt (len).
            alone = dict.fromkeys(names, 0.0)
            alone.update(len=names["len"], image_count=names["image_count"])
            alone["cc" if "cc" in formula.used else "p"] = float(written)  # an unpriced write is billed as input
            total = formula.value(alone) * float(pricing.get("group_ratio") or 1) / 1e6
        except (ValueError, TypeError, ArithmeticError, RecursionError):
            return None
    elif pricing.get("billing_mode"):
        return None
    else:
        try:
            total = written * float(pricing.get("cache_write", pricing["prompt"]) or 0) / 1e6
        except (KeyError, TypeError, ValueError):
            return None
    return round(total, 10) if math.isfinite(total) and total > 0 else None


def _priced(pricing, usage):
    """(USD or None, None or the reason the price cannot be read)."""
    if not isinstance(pricing, dict):
        return None, "в каталоге нет цены"
    if pricing.get("quota_type") not in (0, None):
        return None, "цена за запрос (quota_type %s), а не за токены" % pricing.get("quota_type")
    if pricing.get("billing_mode") == "tiered_expr":
        return _tiered_cost(pricing, usage)
    if pricing.get("billing_mode"):
        return None, "незнакомый способ расчёта %s" % str(pricing.get("billing_mode"))[:40]
    try:
        rates = {key: float(pricing[key]) for key in ("prompt", "completion")}
        read = float(pricing.get("cache_read", pricing["prompt"]) or 0)
        write = float(pricing.get("cache_write", pricing["prompt"]) or 0)
    except (KeyError, TypeError, ValueError):
        return None, "нет цены за токены (prompt, completion)"
    total = (usage.get("input_tokens", 0) * rates["prompt"] + usage.get("output_tokens", 0) * rates["completion"]
             + usage.get("cache_read", 0) * read + usage.get("cache_write", 0) * write) / 1e6
    if not math.isfinite(total) or total < 0:
        return None, "цена за токены не число"
    return round(total, 10), None


def _tiered_cost(pricing, usage):
    """Minara's price expression (new-api's billing expression, its `tiered_expr` mode) for one reply: USD per million
    tokens, e.g. "len > 272000 ? (p * 4 + c * 15 + cr * 0.2 + cc * 5) : (p * 2 + c * 10 + cr * 0.1 + cc * 2.5)" (GPT-6.1
    Sol) or "p * 2 + c * 10 + cr * 0.1 + cc * 0.1875 + ai * 2" (Gemini 4 Carbon, whose `ai` is audio input). Read by a
    small parser, never eval(); with writes at cc it matched Minara's debits. Returns (USD or None, None or the reason)."""
    expression = pricing.get("billing_expr")
    if not isinstance(expression, str) or not expression.strip():
        return None, "в прайсе нет формулы цены"
    try:
        formula = _formula(expression)
        value = formula.value(_token_names(formula.used, usage))
        ratio = float(pricing.get("group_ratio") or 1)
    except ValueError as error:
        return None, "формула цены не читается: %s" % error
    except (TypeError, ArithmeticError, RecursionError) as error:
        return None, "формула цены не считается: %s" % type(error).__name__
    total = value * ratio / 1e6
    if not math.isfinite(total) or total < 0:
        return None, "формула цены дала не цену (%s)" % total
    return round(total, 10), None


# The variables of new-api's price expressions for a chat reply (its pkg/billingexpr/expr.md and
# service/tiered_settle.go, read on 2026-11-13): token counts, plus image_count (images a request makes, 1 for chat).
TOKEN_VARIABLES = frozenset({"p", "c", "len", "cr", "cc", "cc1h", "cc12h", "img", "img_cr", "img_o", "ai", "ao",
                             "image_count"})
# Its functions. tier(name, value) names the tier the reply fell in and is worth `value`; fixed(usd) is a price per
# request; param() and header() read the request, which the price is never given here: they read as absent (a rule such
# as `|||when(header("anthropic-beta") has "fast-mode") * 6` does not apply). The clock functions take a time zone.
_FUNCTIONS = {"tier": (2, 2), "fixed": (1, 1), "max": (1, 64), "min": (1, 64), "abs": (1, 1), "ceil": (1, 1),
              "floor": (1, 1), "has": (2, 2), "param": (1, 1), "header": (1, 1), "hour": (0, 1), "minute": (0, 1),
              "weekday": (0, 1), "month": (0, 1), "day": (0, 1), "when": (1, 1)}
_CONSTANTS = {"true": True, "false": False, "nil": None, "null": None}
_EXPRESSION_CHARS = 20_000
_EXPRESSION_DEPTH = 60
_EXPRESSION_TOKENS = re.compile(r"""
    (?P<space>\s+)
  | (?P<number>(?:\d[\d_]*(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<name>[A-Za-z_]\w*)
  | (?P<op>&&|\|\||==|!=|>=|<=|[-+*/%()?:<>!,])
""", re.VERBOSE)


def _token_names(used, usage):
    """The values of an expression's variables for one reply, as new-api settles it (BuildTieredTokenParams).

    `p` is the input the expression does not price apart: a cache read, a cache write, image or audio input it prices
    with its own variable is left out of p, one it does not price stays in p at the input price (Grok 4.7 has no cc).
    `c` likewise for audio and image output. `len` is the whole prompt and only chooses a tier. Minara bills every
    cache write at cc, an hour-long one too (Haiku 5.5's 1h writes were debited at cc's $0.125/M, not cc1h's $0.2/M), so
    cc1h and cc12h stay 0. The relay's input_tokens already leaves the cache read and write out."""
    def count(key):
        value = usage.get(key) if isinstance(usage, dict) else None
        return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else 0.0

    read, written = count("cache_read"), count("cache_write")
    whole = count("input_tokens") + read + written
    audio_in, image_in = count("audio_input_tokens"), count("image_input_tokens")
    audio_out, image_out = count("audio_output_tokens"), count("image_output_tokens")
    prompt = whole - sum(tokens for name, tokens in (("cr", read), ("cc", written), ("img", image_in), ("ai", audio_in))
                         if name in used)
    output = count("output_tokens") - sum(tokens for name, tokens in (("ao", audio_out), ("img_o", image_out))
                                          if name in used)
    return {"p": max(0.0, prompt), "c": max(0.0, output), "len": whole, "cr": read, "cc": written, "cc1h": 0.0,
            "cc12h": 0.0, "img": image_in, "img_cr": 0.0, "img_o": image_out, "ai": audio_in, "ao": audio_out,
            "image_count": 1.0}


@functools.lru_cache(maxsize=256)
def _formula(expression):
    """The parsed expression (cached: every reply of a model prices the same text); ValueError when it cannot be read."""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("пустая формула")
    if len(expression) > _EXPRESSION_CHARS:
        raise ValueError("формула длиннее %d знаков" % _EXPRESSION_CHARS)
    text = expression.strip()
    version = re.match(r"(v\d+)\s*:(?!:)", text)
    if version:
        if version[1] != "v1":
            raise ValueError("версия формулы %s, известна только v1" % version[1])
        text = text[version.end():]
    parts = text.split("|||")
    base = _ExpressionParser(parts[0]).parse()
    rules = []
    for part in parts[1:]:
        rule = _ExpressionParser(part).parse()
        # `when(condition) * factor`: the factor applies when the condition holds, else the rule is worth 1.
        if rule[0] == "*" and rule[1][0] == "call" and rule[1][1] == "when":
            rules.append((rule[1][2][0], rule[2]))
        else:
            rules.append((None, rule))
    return _Formula(base, tuple(rules))


class _Formula:
    __slots__ = ("base", "rules", "used")

    def __init__(self, base, rules):
        self.base, self.rules = base, rules
        used = set()
        for node in (base, *(item for rule in rules for item in rule if item is not None)):
            _variables(node, used)
        self.used = frozenset(used)

    def value(self, names):
        total = _numeric(_evaluate(self.base, names))
        for condition, factor in self.rules:
            if condition is None or _truth(_evaluate(condition, names)):
                total *= _numeric(_evaluate(factor, names))
        return total


def _variables(node, found):
    if node[0] == "var":
        found.add(node[1])
    elif node[0] == "call":
        for argument in node[2]:
            _variables(argument, found)
    elif node[0] not in ("num", "str", "const"):
        for child in node[1:]:
            if isinstance(child, tuple):
                _variables(child, found)


class _ExpressionParser:
    """ternary := or ["?" ternary ":" ternary]; or := and (("||" | "or") and)*; and := comparison (("&&" | "and")
    comparison)*; comparison := sum [(">" | "<" | ">=" | "<=" | "==" | "!=" | "has" | "contains") sum];
    sum := product (("+" | "-") product)*; product := unary (("*" | "/" | "%") unary)*;
    unary := ("-" | "+" | "!" | "not") unary | atom; atom := number | string | true | false | nil | name |
    function "(" [ternary ("," ternary)*] ")" | "(" ternary ")". Unknown names and functions are a ValueError that names
    them; nothing is evaluated here."""

    def __init__(self, text):
        self.tokens = []
        at = 0
        while at < len(text):
            match = _EXPRESSION_TOKENS.match(text, at)
            if match is None:
                raise ValueError("непонятный знак «%s»" % text[at])
            if match.lastgroup != "space":
                self.tokens.append((match.lastgroup, match[0]))
            at = match.end()
        self.at = 0
        self.depth = 0

    def parse(self):
        if not self.tokens:
            raise ValueError("пустая формула")
        node = self._ternary()
        if self.at != len(self.tokens):
            raise ValueError("лишнее «%s»" % self.tokens[self.at][1])
        return node

    def _peek(self):
        return self.tokens[self.at][1] if self.at < len(self.tokens) else None

    def _take(self, expected=None):
        token = self._peek()
        if token is None:
            raise ValueError("формула оборвалась")
        if expected is not None and token != expected:
            raise ValueError("ждали «%s», а там «%s»" % (expected, token))
        self.at += 1
        return self.tokens[self.at - 1]

    def _ternary(self):
        self.depth += 1
        if self.depth > _EXPRESSION_DEPTH:
            raise ValueError("слишком глубокая вложенность")
        node = self._or()
        if self._peek() == "?":
            self._take()
            yes = self._ternary()
            self._take(":")
            node = ("if", node, yes, self._ternary())
        self.depth -= 1
        return node

    def _or(self):
        node = self._and()
        while self._peek() in ("||", "or"):
            self._take()
            node = ("or", node, self._and())
        return node

    def _and(self):
        node = self._comparison()
        while self._peek() in ("&&", "and"):
            self._take()
            node = ("and", node, self._comparison())
        return node

    def _comparison(self):
        node = self._sum()
        if self._peek() in (">", "<", ">=", "<=", "==", "!=", "has", "contains"):
            operator = self._take()[1]
            node = ("cmp", operator, node, self._sum())
        return node

    def _sum(self):
        node = self._product()
        while self._peek() in ("+", "-"):
            node = (self._take()[1], node, self._product())
        return node

    def _product(self):
        node = self._unary()
        while self._peek() in ("*", "/", "%"):
            node = (self._take()[1], node, self._unary())
        return node

    def _unary(self):
        if self._peek() in ("-", "+", "!", "not"):
            operator = self._take()[1]
            self.depth += 1
            if self.depth > _EXPRESSION_DEPTH:
                raise ValueError("слишком глубокая вложенность")
            node = self._unary()
            self.depth -= 1
            return node if operator == "+" else ("neg" if operator == "-" else "not", node)
        return self._atom()

    def _atom(self):
        kind, token = self._take()
        if token == "(":
            node = self._ternary()
            self._take(")")
            return node
        if kind == "number":
            return ("num", float(token.replace("_", "")))
        if kind == "string":
            return ("str", re.sub(r"\\(.)", r"\1", token[1:-1]))
        if kind != "name":
            raise ValueError("ждали число или имя, а там «%s»" % token)
        if token in _CONSTANTS:
            return ("const", _CONSTANTS[token])
        if self._peek() == "(":
            if token not in _FUNCTIONS:
                raise ValueError("неизвестная функция %s" % token)
            self._take("(")
            arguments = []
            if self._peek() != ")":
                arguments.append(self._ternary())
                while self._peek() == ",":
                    self._take()
                    arguments.append(self._ternary())
            self._take(")")
            fewest, most = _FUNCTIONS[token]
            if not fewest <= len(arguments) <= most:
                raise ValueError("у %s() не то число аргументов" % token)
            return ("call", token, tuple(arguments))
        if token not in TOKEN_VARIABLES:
            raise ValueError("неизвестная переменная %s" % token)
        return ("var", token)


def _truth(value):
    return bool(value)


def _numeric(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if type(value) in (int, float):
        return float(value)
    raise ValueError("в расчёт попало не число (%s)" % ("пусто" if value is None else type(value).__name__))


def _clock(arguments, names):
    zone = _evaluate(arguments[0], names) if arguments else "UTC"
    if not isinstance(zone, str):
        raise ValueError("часовой пояс не строка")
    try:
        return datetime.datetime.now(zoneinfo.ZoneInfo(zone or "UTC"))
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValueError("неизвестный часовой пояс %s" % zone[:40]) from None


def _evaluate(node, names):
    kind = node[0]
    if kind in ("num", "str", "const"):
        return node[1]
    if kind == "var":
        return names[node[1]]
    if kind == "if":
        return _evaluate(node[2] if _truth(_evaluate(node[1], names)) else node[3], names)
    if kind == "or":
        return _truth(_evaluate(node[1], names)) or _truth(_evaluate(node[2], names))
    if kind == "and":
        return _truth(_evaluate(node[1], names)) and _truth(_evaluate(node[2], names))
    if kind == "not":
        return not _truth(_evaluate(node[1], names))
    if kind == "neg":
        return -_numeric(_evaluate(node[1], names))
    if kind == "cmp":
        left, right = _evaluate(node[2], names), _evaluate(node[3], names)
        operator = node[1]
        if operator in ("has", "contains"):
            return isinstance(left, str) and isinstance(right, str) and right in left
        if operator in ("==", "!="):
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                left, right = float(left), float(right)
            return (left == right) == (operator == "==")
        if left is None or right is None:
            return False  # an absent request field is no number
        if isinstance(left, str) != isinstance(right, str):
            raise ValueError("сравнение строки с числом")
        return {">": left > right, "<": left < right, ">=": left >= right, "<=": left <= right}[operator]
    if kind in ("+", "-", "*", "/", "%"):
        left, right = _numeric(_evaluate(node[1], names)), _numeric(_evaluate(node[2], names))
        if kind == "+":
            return left + right
        if kind == "-":
            return left - right
        if kind == "*":
            return left * right
        if right == 0:
            raise ValueError("деление на ноль")
        return left / right if kind == "/" else math.fmod(left, right)
    if kind == "call":
        name, arguments = node[1], node[2]
        if name == "tier":
            return _evaluate(arguments[1], names)
        if name == "fixed":
            return _numeric(_evaluate(arguments[0], names)) * 1e6  # USD per request, in the expression's per-million unit
        if name in ("max", "min"):
            values = [_numeric(_evaluate(argument, names)) for argument in arguments]
            return max(values) if name == "max" else min(values)
        if name in ("abs", "ceil", "floor"):
            value = _numeric(_evaluate(arguments[0], names))
            return abs(value) if name == "abs" else float(math.ceil(value) if name == "ceil" else math.floor(value))
        if name == "has":
            text, part = _evaluate(arguments[0], names), _evaluate(arguments[1], names)
            return isinstance(text, str) and isinstance(part, str) and part in text
        if name == "param":
            return None
        if name == "header":
            return ""
        if name == "when":
            return 1.0 if _truth(_evaluate(arguments[0], names)) else 0.0
        moment = _clock(arguments, names)
        return float({"hour": moment.hour, "minute": moment.minute, "weekday": moment.isoweekday() % 7,
                      "month": moment.month, "day": moment.day}[name])
    raise ValueError("непонятный узел формулы")


class EventParser:
    def __init__(self, max_bytes=MAX_EVENT_BYTES, pricing=None):
        self.pricing = pricing
        self.calls = {}
        self.finish = None
        self.done = False
        self.thinking = False
        self.max_bytes = max_bytes
        self.text = ""
        # reasoning_details by index, as upstream streams them: text/summary/data pieces, then the signature.
        self.details = {}
        self.signed = False
        self.reply_id = ""  # the first chunk's id: the channel that answered (channel_of)
        self.provider = None  # OpenRouter's provider behind it (Anthropic, Google, Azure, ...): each has its own cache

    def reasoning_details(self):
        """The reasoning_details of the reply, assembled (the signature the client gets comes from them)."""
        result = []
        for index in sorted(self.details):
            item = {key: value for key, value in self.details[index].items() if value not in (None, "")}
            if item.get("type"):
                result.append(item)
        return result

    def reply_key(self):
        """The key the reply is known by when the client sends it back: its tool call ids, else a hash of its text."""
        calls = [self.calls[index] for index in sorted(self.calls)]
        if calls:
            return ("calls",) + tuple(call["id"] for call in calls)
        if self.text.strip():
            return ("text", hashlib.sha256(self.text.encode("utf-8")).hexdigest())
        return None

    def _details(self, parts):
        """Fold streamed reasoning_details; the text of the ones that carry visible reasoning (when `reasoning` is absent)."""
        visible = []
        for part in parts:
            if not isinstance(part, dict):
                raise apierr.upstream_error("Minara returned invalid reasoning details")
            index = part.get("index", 0)
            if type(index) is not int or not 0 <= index < 64:
                raise apierr.upstream_error("Minara returned an invalid reasoning index")
            item = self.details.setdefault(index, {})
            for key, value in part.items():
                if key == "index" or value is None:
                    continue
                if key in ("text", "summary", "data") and isinstance(value, str):
                    item[key] = item.get(key, "") + value
                    if key in ("text", "summary"):
                        visible.append(value)
                elif key == "signature" and isinstance(value, str):
                    item[key] = item.get(key, "") + value
                else:
                    item[key] = value
                if sum(len(str(v)) for v in item.values()) > self.max_bytes:
                    raise apierr.upstream_error("Minara reasoning exceeds the size limit")
        return "".join(visible)

    def feed(self, data):
        if self.done:
            raise apierr.upstream_error("Minara sent data after DONE")
        if data == "[DONE]":
            if self.finish is None:
                raise apierr.upstream_error("Minara stream ended without a finish reason")
            events = []
            for call in self.calls.values():
                if not call["id"] or not call["name"]:
                    raise apierr.upstream_error("Minara returned an incomplete tool call")
                event = {"type": "tool_call", "id": call["id"], "name": call["name"], "arguments": call["arguments"] or "{}"}
                try:
                    def invalid_number(value):
                        raise ValueError("nonfinite argument")
                    def finite_float(value):
                        number = float(value)
                        return number if math.isfinite(number) else invalid_number(value)
                    parsed = _bounded_json(event["arguments"], parse_constant=invalid_number, parse_float=finite_float)
                    if not isinstance(parsed, dict):
                        raise ValueError("tool arguments must be an object")
                except (ValueError, RecursionError):
                    event["invalid"] = True
                events.append(event)
            self.done = True
            events.append({"type": "end", "reason": self.finish})
            return events
        try:
            item = _bounded_json(data)
        except (ValueError, RecursionError):
            raise apierr.upstream_error("Minara returned malformed SSE JSON") from None
        if not isinstance(item, dict) or "error" in item:
            raise apierr.upstream_error("Minara returned a stream error")
        failed = _upstream_failure(item)
        if failed:
            raise apierr.upstream_error("Minara's upstream failed: " + failed)
        events = []
        if not self.reply_id and isinstance(item.get("id"), str):
            self.reply_id = item["id"]
        if self.provider is None and isinstance(item.get("provider"), str) and len(item["provider"]) <= 80:
            self.provider = item["provider"]
        if isinstance(item.get("usage"), dict):
            usage = normalize_usage(item["usage"], self.pricing)
            usage["channel_path"] = "chat"
            if self.reply_id:
                usage["channel"] = channel_of(self.reply_id)
            # OpenRouter's provider when it names one, else the provider the channel itself is (Anthropic, Bedrock).
            provider = provider_of(usage.get("channel"), self.provider)
            if provider:
                usage["provider"] = provider
            events.append({"type": "usage", "usage": usage})
        choices = item.get("choices", [])
        if not isinstance(choices, list):
            raise apierr.upstream_error("Minara returned invalid choices")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            finish = choice.get("finish_reason")
            if finish:
                if finish not in ("stop", "length", "tool_calls", "content_filter", "function_call"):
                    raise apierr.upstream_error("Minara returned an unknown finish reason")
                normalized_finish = "tool_calls" if finish == "function_call" else finish
                if self.finish is not None and self.finish != normalized_finish:
                    raise apierr.upstream_error("Minara returned conflicting finish reasons")
                self.finish = normalized_finish
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise apierr.upstream_error("Minara returned an invalid delta")
            details = delta.get("reasoning_details")
            if details is not None and not isinstance(details, list):
                raise apierr.upstream_error("Minara returned invalid reasoning details")
            detail_text = self._details(details or [])
            # OpenRouter sends the same reasoning in `reasoning` and in reasoning_details: show it once.
            reasoning = delta.get("reasoning_content") or delta.get("reasoning") or detail_text
            if isinstance(reasoning, str) and reasoning:
                if not self.thinking:
                    events.append({"type": "thinking_start"})
                    self.thinking = True
                events.append({"type": "thinking", "delta": reasoning})
            if not self.signed:
                signature = "".join(item.get("signature", "") for item in self.details.values()
                                    if item.get("format", "").startswith("anthropic"))
                if signature and any(part.get("signature") for part in details or [] if isinstance(part, dict)):
                    # Claude's signature arrives once its thinking is complete: Messages clients get a real signed block.
                    self.signed = True
                    if not self.thinking:
                        events.append({"type": "thinking_start"})
                        self.thinking = True
                    events.append({"type": "thinking_signature", "signature": signature})
            text = delta.get("content")
            if isinstance(text, str) and text:
                self.text += text
                events.append({"type": "text", "delta": text})
            calls = delta.get("tool_calls") or []
            if not isinstance(calls, list):
                raise apierr.upstream_error("Minara returned invalid tool calls")
            for part in calls:
                if not isinstance(part, dict) or type(part.get("index")) is not int or not 0 <= part["index"] < 128:
                    raise apierr.upstream_error("Minara returned an invalid tool index")
                call = self.calls.setdefault(part["index"], {"id": "", "name": "", "arguments": ""})
                fn = part.get("function") or {}
                if not isinstance(fn, dict):
                    raise apierr.upstream_error("Minara returned an invalid tool function")
                for target, value in (("id", part.get("id")), ("name", fn.get("name")), ("arguments", fn.get("arguments"))):
                    if value is not None:
                        if not isinstance(value, str):
                            raise apierr.upstream_error("Minara returned an invalid tool fragment")
                        call[target] += value
                        if len(call[target].encode("utf-8")) > self.max_bytes:
                            raise apierr.upstream_error("Minara tool call exceeds the size limit")
                if call["id"] and call["name"]:
                    events.append({"type": "tool_call", **call, "partial": True})
        return events


def _error_body(response, limit=65536):
    """The start of an error answer's body, to tell Minara's channel failures from real refusals."""
    data = b""
    try:
        for piece in response.iter_content():
            data += piece if isinstance(piece, bytes) else str(piece).encode("utf-8")
            if len(data) >= limit:
                break
    except Exception:
        pass
    return data[:limit]


def _upstream_failure(item):
    """The error an upstream put inside a 200 chunk (MiniMax through Minara: base_resp "invalid api key"), or None."""
    status = item.get("base_resp")
    if isinstance(status, dict) and status.get("status_code") not in (None, 0):
        return str(status.get("status_msg") or status.get("status_code"))[:200]
    return None


# Minara's own routing failures: the model is in /v1/models, but no channel serves it right now (measured 2026-11-09:
# windows of minutes in which a model or every OpenRouter model answers 500/503 like this, unbilled).
_CHANNEL_MARKS = (b"channel_selection", b"channel_unavailable", b"no_enabled_channel", b"available channel",
                  b"Unknown or unsupported model",
                  # OpenRouter's provider down: Opus 4.7 answered every request so for ten minutes, the same requests were
                  # answered before and after (2026-11-10 01:05-01:15)
                  b"The request could not be processed")
CHANNEL_RETRY_AFTER = 30


def _channel_trouble(status, text):
    if status in (500, 502, 503, 504, 524) and not text.strip():
        return True
    return status in (400, 404, 500, 502, 503) and any(mark in text for mark in _CHANNEL_MARKS)


def _http_error(status, headers, text=b""):
    """The error a Minara answer of `status` becomes, marked as upstream's (the journal logged them as the relay's)."""
    error = _status_error(status, headers, text)
    error.upstream_status = status
    error.unbilled = True  # answered instead of a stream: nothing was generated, so another account may take it
    return error


def _status_error(status, headers, text=b""):
    if status in (401, 403):
        # The code is the one every account path knows (auth_dead, failover); the words are Minara's, not Retool's.
        error = apierr.ApiError(502, "upstream_auth_error", "Minara rejected the account's token (HTTP %s): it expired or "
                                "was revoked; the request moves to another account, the account needs a new token" % status)
        error.upstream_status = status
        return error
    if status == 429:
        return apierr.too_many_requests("Minara rate limit", apierr.retry_after_seconds(headers.get("retry-after")) or 2)
    if status == 402:
        return apierr.ApiError(402, "insufficient_credits", "Minara account has insufficient balance")
    if _channel_trouble(status, text):
        error = apierr.service_unavailable("Minara has no working channel for this model right now; retry later")
        error.retry_after = CHANNEL_RETRY_AFTER
        return error
    if status in (400, 404, 413, 422):
        # Minara's own words go to the client: Claude Code compacts on "prompt is too long", and a bare "HTTP 400" told
        # nobody what to change (audit, 2026-11-10). Cut, on one line, and redacted like everything the relay prints.
        detail = _upstream_message(text)
        error = apierr.bad_request("Minara rejected the upstream request (HTTP %s)%s" % (
            status, ": " + detail if detail else ""))
        if status == 413:
            error.status = 413
        return error
    return apierr.upstream_error("Minara upstream HTTP %s" % status)


def _innermost(message):
    """OpenRouter's provider error inside Minara's ("Provider returned error ({"raw":"{\"type\":\"error\",...
    \"message\":\"Could not process image\"...")): the innermost message, which says what to change; the
    message as it is when there is none."""
    found = message
    for _ in range(3):
        start, end = found.find("{"), found.rfind("}")
        if start < 0 or end <= start:
            break
        try:
            data = json.loads(found[start:end + 1])
        except (ValueError, RecursionError):
            break
        inner = None
        if isinstance(data, dict):
            error = data.get("error")
            inner = (error.get("message") if isinstance(error, dict) else None) or data.get("message") or data.get("raw")
        if not isinstance(inner, str) or not inner.strip():
            break
        found = inner
    return " ".join(found.split()) if found is not message else message


def _upstream_message(text, limit=400):
    """The message of an upstream error body (its error.message, else the start of the text), or ""."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    message = ""
    try:
        data = json.loads(text)
        error = data.get("error") if isinstance(data, dict) else None
        message = error.get("message") if isinstance(error, dict) else error if isinstance(error, str) else ""
        message = message or (data.get("message") if isinstance(data, dict) else "") or ""
    except (ValueError, AttributeError, RecursionError):
        message = text if "<html" not in text[:200].lower() else ""
    message = _innermost(" ".join(str(message).split()))
    if len(message) > limit:
        message = message[:limit - 1] + "…"
    return redact.redact(message) if message else ""


def _forced_requirement(model_provider, tools, tool_choice):
    """The call a request forces on a route that refuses tool_choice required/named, or None.

    Anthropic refuses tool_choice any/tool whenever thinking is on: Opus, Sonnet and Haiku 5.5 answer 400 "tool_choice:
    type tool and any are not supported for this model" through Minara (measured 2026-11-09), and every other Claude model
    does once a level turns thinking on. The relay asks for the call in words on every Claude route (minara_models.json
    forced_tool_choice false), as for Retool's Claude routes (forced_choice)."""
    if not tools or not model_provider.get("refuses_forced_tool_choice"):
        return None
    names = frozenset(name for name in map(forced_choice.tool_name, tools) if name)
    if tool_choice == "required":
        return forced_choice.Requirement(None, names)
    if isinstance(tool_choice, dict):
        named = (tool_choice.get("function") or {}).get("name") or tool_choice.get("name")
    elif isinstance(tool_choice, str) and tool_choice not in ("auto", "none"):
        named = tool_choice
    else:
        return None
    return forced_choice.Requirement(named, names) if named in names else None


async def iter_events(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens, chunks=None):
    required = _forced_requirement(model_provider, tools, tool_choice)
    context = client_context.current()
    if (required is None and tools and context.get("client_task") == client_context.COMPACTION
            and context.get("compaction_tools") == "restored"):
        # The compaction of a Codex chat, whose tools the relay declared again to read the chat's cache: Codex cannot take
        # a call in reply to it (the summary becomes "(no summary available)"), so a reply with one is not passed on.
        async with contextlib.aclosing(_summary_events(cfg, session, model_provider, prompt, head, tools, tool_choice,
                                                       max_tokens, chunks)) as events:
            async for event in events:
                yield event
        return
    if required is None:
        async with contextlib.aclosing(_stream(cfg, session, model_provider, prompt, head, tools, tool_choice,
                                               max_tokens, chunks)) as events:
            async for event in events:
                yield event
        return
    # The reply is released once it holds the call; an attempt without it is asked again once, more firmly, and nothing of
    # it reaches the client (its cost still counts). A second miss is the error a route that ignores a choice earns.
    for attempt in (1, 2):
        gate = forced_choice.Gate(required)
        end = None
        async with contextlib.aclosing(_stream(cfg, session, model_provider, prompt, head, tools, "auto", max_tokens,
                                               chunks, forced_choice.instruction(required, firm=attempt > 1))) as events:
            async for event in events:
                if event.get("type") == "end":
                    end = event
                    continue
                for ready in gate.feed(event):
                    yield ready
        if end is None:
            return
        reason = end.get("reason")
        # A refusal, or a cap spent on reasoning alone, repeats for the same body: both end the turn as they would under a
        # native choice (the orchestrator takes both as final), with what the attempt showed but never a call it held
        # (under a named choice that could hand the client a call to another tool). Asking again only paid twice.
        terminal = reason == "content_filter" or (reason == "length" and not gate.text and not gate.called)
        if gate.complied or reason == "cancelled" or terminal:
            if terminal and not gate.complied:
                for held in gate.held:
                    if held.get("type") != "tool_call":
                        yield held
            yield end
            return
        journal.emit("turn.forced_choice_missed",
                     "Модель не вызвала требуемый инструмент%s" % (": повтор с усиленной просьбой" if attempt == 1 else ""),
                     "warn", model=model_provider.get("model"), tool=required.named, called=gate.called,
                     wrote_text=gate.text, attempt=attempt)
    raise apierr.ApiError(502, "upstream_tool_choice_ignored", "upstream did not honor the required client tool choice")


def channel_of(ident):
    """The channel behind a Minara reply id: gen- OpenRouter, msg_bdrk_ Bedrock, any other msg_ Anthropic's own API."""
    ident = str(ident or "")
    if ident.startswith("gen-"):
        return "openrouter"
    if ident.startswith("msg_bdrk"):
        return "bedrock"
    if ident.startswith("msg_"):
        return "anthropic"
    return "other" if ident else None


# Minara's own Anthropic and Bedrock channels are the provider themselves; OpenRouter names its own (Anthropic, Google,
# Azure, ...) on the chat path only, and nothing names the one behind its /v1/messages replies.
_CHANNEL_PROVIDER = {"anthropic": "Anthropic", "bedrock": "Amazon Bedrock"}


def provider_of(channel, reported=None):
    """Who served a Minara reply: the provider OpenRouter reported, else the one its channel is; None when unknown."""
    if isinstance(reported, str) and reported:
        return reported
    return _CHANNEL_PROVIDER.get(channel)


# ----- Claude through Minara's /v1/messages ------------------------------------------------------------------------------
# Minara's chat path sends some Claude models and levels to Bedrock or Anthropic's API without the cache marks (Sonnet 4.6,
# Opus 4.6 and Fable 5 at every level; Opus 4.7, 4.8, 5 and Sonnet 5 at none, minimal, xhigh and max): the whole prompt is
# billed every turn. Its /v1/messages reaches the same channels with the marks, and they cache there (measured
# 2026-11-10: the second request read the 8-13k-token head on all seven, a tool loop continued without its thinking
# blocks on all of them). Such levels go that way: minara_models.json lists them per model (messages_levels), and a chat
# reply that comes from a channel without cache moves its level there at once (switch_to_messages).
ANTHROPIC_VERSION = "2023-06-01"
MESSAGES_MAX_TOKENS = 32000
SWITCHED = {}  # upstream id -> levels moved to /v1/messages while the relay runs (switch_to_messages)
_SWITCH_LOCK = threading.Lock()
_switch_listeners = []


def _claude_name(upstream):
    return str(upstream or "").split("/", 1)[-1].replace(".", "-")


def messages_supported(upstream):
    """True for the Claude models whose /v1/messages request the relay knows how to write."""
    name = _claude_name(upstream)
    return str(upstream or "").startswith("anthropic/") and bool(
        name.startswith("claude-fable-") or re.fullmatch(r"claude-(opus|sonnet|haiku)-5-5", name)
        or name in ("claude-opus-5", "claude-sonnet-5") or re.fullmatch(r"claude-(opus|sonnet)-4-[678]", name))


def messages_reasoning(upstream, level):
    """(output_config.effort, thinking) of a /v1/messages request at `level` (None, "default": the model's own default).

    The Messages API thinks on Claude 4.x only when asked (Opus 4.8 and 4.7 run without thinking when it is left out); Fable,
    Opus 5 and Sonnet 5 think by default, and Fable and the 5.5 models refuse "disabled". Claude 4.6 knows no xhigh (it
    goes up, as the chat path's nearest level does). The reasoning is asked as summaries: the default on Claude 4.7 and
    later is an empty thinking block."""
    name = _claude_name(upstream)
    on_by_default = (name.startswith("claude-fable-") or bool(re.fullmatch(r"claude-(opus|sonnet|haiku)-5-5", name))
                     or name in ("claude-opus-5", "claude-sonnet-5"))
    can_disable = not (name.startswith("claude-fable-") or re.fullmatch(r"claude-(opus|sonnet|haiku)-5-5", name))
    summarized = {"type": "adaptive", "display": "summarized"}
    if level in (None, "", "default", "auto"):
        return None, (summarized if on_by_default else None)
    if level == "none" and can_disable:
        return None, {"type": "disabled"}
    effort = {"none": "low", "minimal": "low"}.get(level, level)
    if effort == "xhigh" and name in ("claude-opus-4-6", "claude-sonnet-4-6"):
        effort = "max"
    return effort, summarized


def messages_levels(model_provider, upstream):
    """The levels of one route that go through /v1/messages: the catalog's, and the ones moved there while running."""
    levels = set(model_provider.get("messages_levels") or ())
    with _SWITCH_LOCK:
        levels |= SWITCHED.get(upstream, set())
    return levels


def use_messages(model_provider, body):
    """True when this chat body should go through /v1/messages instead (its model and level lose the cache on chat)."""
    upstream = body.get("model")
    if not messages_supported(upstream):
        return False
    return (body.get("reasoning_effort") or "default") in messages_levels(model_provider, upstream)


def on_switch(listener):
    """Call listener(upstream, level, switched) whenever a level moves to /v1/messages or back (persistence)."""
    _switch_listeners.append(listener)


def switch_to_messages(upstream, level, switched=True, reason=None):
    """Move one model's level to /v1/messages (or, `switched` False, back to chat); False when nothing changed."""
    level = level or "default"
    if switched and not messages_supported(upstream):
        return False
    with _SWITCH_LOCK:
        current = SWITCHED.setdefault(upstream, set())
        if (level in current) == switched:
            return False
        (current.add if switched else current.discard)(level)
        if not current:
            SWITCHED.pop(upstream, None)
    if switched:
        journal.emit("cache.route_switched", "Minara отправила уровень %s модели %s на канал без кэша: дальше этот уровень "
                     "идёт через /v1/messages, где кэш работает" % (level, upstream), "warn", model=upstream,
                     effort=level, upstream="minara", path="messages", reason=reason)
    else:
        journal.emit("cache.route_switched", "Уровень %s модели %s снова идёт через чат: /v1/messages его не принял (%s)"
                     % (level, upstream, reason or "ошибка"), "warn", model=upstream, effort=level, upstream="minara",
                     path="chat", reason=reason)
    for listener in list(_switch_listeners):
        try:
            listener(upstream, level, switched)
        except Exception:  # noqa: BLE001 - persistence is best effort; the switch holds in memory
            pass
    return True


def _anthropic_parts(content):
    """Anthropic content blocks for a chat message's content (text and images; cache marks kept)."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        block = None
        if kind == "text" and part.get("text"):
            block = {"type": "text", "text": part["text"]}
        elif kind == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            match = re.match(r"data:([\w.+/-]+);base64,(.*)", url, re.DOTALL)
            if match:
                block = {"type": "image", "source": {"type": "base64", "media_type": match.group(1),
                                                     "data": match.group(2)}}
            elif url.startswith(("https://", "http://")):
                block = {"type": "image", "source": {"type": "url", "url": url}}
        if block is None:
            continue
        if part.get("cache_control"):
            block["cache_control"] = copy.deepcopy(part["cache_control"])
        blocks.append(block)
    return blocks


def _tool_input(arguments):
    try:
        value = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


# Minara's /v1/messages passes a request on in chat form, and a tool result loses its cache mark on the way (Fable 5.2 at
# max through OpenRouter, 2026-11-13): a mark on the tool_result block, or on the tool_use it answers, is dropped, and one on
# a text part inside its content reaches the model as text, because a content list arrives as its JSON (a result of 1,313
# characters was 1,546 prompt tokens as a list of one text part and 1,173 as a string). Only a text block of the user turn
# carries a mark there. A turn that ended in tool results therefore wrote nothing to the cache, and Minara bills a reply that
# writes nothing at the input price for its whole prompt, the part it read from the cache included: 15 of 36 turns of one
# Claude Desktop session (6,972 of 9,876 credits), and in a probe 20.5 credits for a turn that, written, costs 2.4. The mark
# that became text also made the same tool result 25 tokens longer while it was marked than after, so the next request
# could not read what this one wrote and wrote it again (36,864 tokens, then the same span less 25).
# So a tool result goes as its text, and each run of tool results in a user turn is closed by this block, which carries the
# turn's mark. It closes every such run, marked or not, so a turn reads the same in every request. In the probe each request
# then wrote its tail (input 4) and the next one read all of it.
TOOL_RESULTS_END = "<system-reminder>\nEnd of tool results.\n</system-reminder>"


def _close_tool_results(turn):
    """Put TOOL_RESULTS_END after the tool results of a user turn (messages_body puts them first) and move their mark to it."""
    blocks = turn["content"]
    count = sum(1 for block in blocks if block.get("type") == "tool_result")
    if turn["role"] != "user" or not count:
        return
    end = {"type": "text", "text": TOOL_RESULTS_END}
    for block in blocks[:count]:
        mark = block.pop("cache_control", None)
        if mark:
            end["cache_control"] = mark
    blocks.insert(count, end)


def messages_body(body):
    """The /v1/messages request for a chat body of build_body: the same history, tools, marks and level.

    Thinking blocks are not sent back, as on the chat path: Minara's /v1/messages continued a tool loop without them on all
    seven models and read the cache all the same (2026-11-10). A tool result goes as its text, and its mark goes onto the
    block that closes the tool results of its turn (TOOL_RESULTS_END)."""
    system, turns = [], []

    def add(role, blocks):
        if not blocks:
            return
        if turns and turns[-1]["role"] == role:
            merged = turns[-1]["content"] + blocks
            # A user turn starts with its tool results (Anthropic refuses text before them).
            turns[-1]["content"] = ([b for b in merged if b.get("type") == "tool_result"]
                                    + [b for b in merged if b.get("type") != "tool_result"])
        else:
            turns.append({"role": role, "content": list(blocks)})

    for message in body.get("messages") or []:
        role = message.get("role")
        if role == "system":
            if not turns:
                system.extend(_anthropic_parts(message.get("content")))
            else:  # a later instruction: Claude Code's own wrapper, in a user turn
                add("user", [{"type": "text", "text": "<system-reminder>\n%s\n</system-reminder>" % "\n".join(
                    part.get("text", "") for part in _anthropic_parts(message.get("content")) if part["type"] == "text")}])
        elif role == "tool":
            parts = _anthropic_parts(message.get("content"))
            marks = [part.pop("cache_control") for part in parts if "cache_control" in part]
            # Text alone goes as one string, what the model reads natively; a content list would reach it as its JSON.
            content = ("\n".join(part["text"] for part in parts) if all(part["type"] == "text" for part in parts)
                       else parts)
            result = {"type": "tool_result", "tool_use_id": message.get("tool_call_id"), "content": content}
            if marks:
                result["cache_control"] = marks[-1]  # _close_tool_results moves it to the text that closes the turn
            add("user", [result])
        elif role == "assistant":
            blocks = [part for part in _anthropic_parts(message.get("content")) if part["type"] == "text"]
            for part in blocks:
                part.pop("cache_control", None)
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                blocks.append({"type": "tool_use", "id": call.get("id"), "name": fn.get("name"),
                               "input": _tool_input(fn.get("arguments"))})
            add("assistant", blocks)
        else:
            add("user", _anthropic_parts(message.get("content")) or [{"type": "text", "text": "(empty)"}])
    for turn in turns:
        _close_tool_results(turn)
    upstream = body.get("model")
    result = {"model": upstream, "max_tokens": body.get("max_tokens") or MESSAGES_MAX_TOKENS, "messages": turns,
              "stream": True}
    if system:
        result["system"] = system
    tools = []
    for tool in body.get("tools") or []:
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        declared = {"name": fn.get("name"), "input_schema": copy.deepcopy(
            fn.get("parameters") or {"type": "object", "properties": {}})}
        if fn.get("description"):
            declared["description"] = fn["description"]
        if fn.get("strict") is True:
            declared["strict"] = True
        tools.append(declared)
    if tools:
        result["tools"] = tools
    choice = body.get("tool_choice")
    if tools and choice is not None:
        if choice in ("auto", "none"):
            result["tool_choice"] = {"type": choice}
        elif choice == "required":
            result["tool_choice"] = {"type": "any"}
        elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
            result["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
    effort, thinking = messages_reasoning(upstream, body.get("reasoning_effort"))
    if effort:
        result["output_config"] = {"effort": effort}
    if thinking:
        result["thinking"] = thinking
    return result


def normalize_messages_usage(value, pricing=None, channel=None):
    """The relay's usage counters for an Anthropic usage block (its input_tokens already leave the cache out)."""
    result = {}
    for source, target in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                           ("cache_read_input_tokens", "cache_read"), ("cache_creation_input_tokens", "cache_write")):
        number = _count(value.get(source))
        if number is not None:
            result[target] = number
    result["cache_write_reported"] = "cache_creation_input_tokens" in value
    result["channel_path"] = "messages"
    if channel:
        result["channel"] = channel
        provider = provider_of(channel)  # nothing in an Anthropic-shaped reply names OpenRouter's provider
        if provider:
            result["provider"] = provider
    estimate = estimated_cost(pricing, result) if pricing is not None and "input_tokens" in result else None
    if estimate is not None:
        result.update(provider_cost=estimate, provider_cost_currency="USD", provider_cost_estimated=True)
    return result


_STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop", "max_tokens": "length",
                 "model_context_window_exceeded": "length", "tool_use": "tool_calls", "refusal": "content_filter"}


class MessagesParser:
    """EventParser's events for Anthropic's stream (message_start ... message_stop) from Minara's /v1/messages."""

    def __init__(self, max_bytes=MAX_EVENT_BYTES, pricing=None):
        self.pricing = pricing
        self.max_bytes = max_bytes
        self.calls = {}
        self.blocks = {}
        self.finish = None
        self.done = False
        self.thinking = False
        self.signed = False
        self.text = ""
        self.reply_id = ""
        self.usage = {}

    def reasoning_details(self):
        return []

    def reply_key(self):
        calls = [self.calls[index] for index in sorted(self.calls)]
        if calls:
            return ("calls",) + tuple(call["id"] for call in calls)
        if self.text.strip():
            return ("text", hashlib.sha256(self.text.encode("utf-8")).hexdigest())
        return None

    def _usage_event(self):
        return {"type": "usage", "usage": normalize_messages_usage(self.usage, self.pricing, channel_of(self.reply_id))}

    def feed(self, data):
        if data == "[DONE]":
            return []  # not Anthropic's, but a proxy may add it after message_stop
        if self.done:
            raise apierr.upstream_error("Minara sent data after message_stop")
        try:
            item = _bounded_json(data)
        except (ValueError, RecursionError):
            raise apierr.upstream_error("Minara returned malformed SSE JSON") from None
        if not isinstance(item, dict):
            raise apierr.upstream_error("Minara returned a stream error")
        kind = item.get("type")
        events = []
        if kind == "error":
            error = item.get("error") if isinstance(item.get("error"), dict) else {}
            raise apierr.upstream_error("Minara returned a stream error" + (
                ": " + _upstream_message(json.dumps({"error": error})) if error.get("message") else ""))
        if kind == "message_start":
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            self.reply_id = str(message.get("id") or "")
            if isinstance(message.get("usage"), dict):
                self.usage.update(message["usage"])
        elif kind == "content_block_start":
            index, block = item.get("index"), item.get("content_block")
            if type(index) is not int or not 0 <= index < 256 or not isinstance(block, dict):
                raise apierr.upstream_error("Minara returned an invalid content block")
            self.blocks[index] = {"type": block.get("type"), "signature": ""}
            if block.get("type") == "tool_use":
                if not isinstance(block.get("id"), str) or not isinstance(block.get("name"), str):
                    raise apierr.upstream_error("Minara returned an incomplete tool call")
                start = block.get("input")
                self.calls[index] = {"id": block["id"], "name": block["name"],
                                     "arguments": json.dumps(start) if isinstance(start, dict) and start else ""}
                events.append({"type": "tool_call", **self.calls[index], "partial": True})
            elif block.get("type") == "thinking":
                if not self.thinking:
                    events.append({"type": "thinking_start"})
                    self.thinking = True
                if isinstance(block.get("thinking"), str) and block["thinking"]:
                    events.append({"type": "thinking", "delta": block["thinking"]})
            elif block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
                self.text += block["text"]
                events.append({"type": "text", "delta": block["text"]})
        elif kind == "content_block_delta":
            index, delta = item.get("index"), item.get("delta")
            if index not in self.blocks or not isinstance(delta, dict):
                raise apierr.upstream_error("Minara returned a delta of no block")
            dtype = delta.get("type")
            if dtype == "text_delta" and isinstance(delta.get("text"), str):
                self.text += delta["text"]
                events.append({"type": "text", "delta": delta["text"]})
            elif dtype == "thinking_delta" and isinstance(delta.get("thinking"), str) and delta["thinking"]:
                if not self.thinking:
                    events.append({"type": "thinking_start"})
                    self.thinking = True
                events.append({"type": "thinking", "delta": delta["thinking"]})
            elif dtype == "signature_delta" and isinstance(delta.get("signature"), str):
                self.blocks[index]["signature"] += delta["signature"]
                if len(self.blocks[index]["signature"]) > self.max_bytes:
                    raise apierr.upstream_error("Minara reasoning exceeds the size limit")
            elif dtype == "input_json_delta" and index in self.calls:
                piece = delta.get("partial_json")
                if not isinstance(piece, str):
                    raise apierr.upstream_error("Minara returned an invalid tool fragment")
                self.calls[index]["arguments"] += piece
                if len(self.calls[index]["arguments"].encode("utf-8")) > self.max_bytes:
                    raise apierr.upstream_error("Minara tool call exceeds the size limit")
                events.append({"type": "tool_call", **self.calls[index], "partial": True})
        elif kind == "content_block_stop":
            block = self.blocks.get(item.get("index")) or {}
            if block.get("type") == "thinking" and block.get("signature") and not self.signed:
                self.signed = True  # one signed block per reply, as the chat path gives
                events.append({"type": "thinking_signature", "signature": block["signature"]})
        elif kind == "message_delta":
            delta = item.get("delta") if isinstance(item.get("delta"), dict) else {}
            stop = delta.get("stop_reason")
            if stop is not None:
                if stop not in _STOP_REASONS:
                    raise apierr.upstream_error("Minara returned an unknown stop reason")
                self.finish = _STOP_REASONS[stop]
            if isinstance(item.get("usage"), dict):
                self.usage.update({key: value for key, value in item["usage"].items() if value is not None})
            events.append(self._usage_event())
        elif kind == "message_stop":
            if self.finish is None:
                raise apierr.upstream_error("Minara stream ended without a stop reason")
            for index in sorted(self.calls):
                call = self.calls[index]
                event = {"type": "tool_call", "id": call["id"], "name": call["name"], "arguments": call["arguments"] or "{}"}
                try:
                    def invalid_number(value):
                        raise ValueError("nonfinite argument")

                    def finite_float(value):
                        number = float(value)
                        return number if math.isfinite(number) else invalid_number(value)
                    parsed = _bounded_json(event["arguments"], parse_constant=invalid_number, parse_float=finite_float)
                    if not isinstance(parsed, dict):
                        raise ValueError("tool arguments must be an object")
                except (ValueError, RecursionError):
                    event["invalid"] = True
                events.append(event)
            self.done = True
            events.append({"type": "end", "reason": self.finish})
        return events


def _read_timeout(cfg, deadline):
    """(connect, read) for curl_cffi, whose low-speed window on a stream is their sum."""
    connect = float(cfg.get("upstream_timeout", 120))
    silence = max(1.0, min(SILENCE_LIMIT, deadline - time.monotonic()))
    return connect, max(1.0, silence - connect)


def _head_key(body):
    """What a Claude body shares with every request of its kind: the model, the tools and the system message with its
    cache mark; None when there is no such head, or one too small to be cached (direct.HEAD_MIN_CHARS)."""
    import direct
    messages = body.get("messages") or []
    first = messages[0] if messages and isinstance(messages[0], dict) else None
    if not str(body.get("model", "")).startswith("anthropic/") or first is None or first.get("role") != "system":
        return None
    content = first.get("content")
    parts = content if isinstance(content, list) else []
    if not any(isinstance(part, dict) and part.get("cache_control") for part in parts):
        return None
    tools = body.get("tools")
    size = sum(len(part.get("text") or "") for part in parts if isinstance(part, dict))
    size += len(json.dumps(tools, ensure_ascii=False, default=str)) if tools else 0
    if size < direct.HEAD_MIN_CHARS:
        return None
    # The effort is part of it: a request on another level may not read the entry (Opus 4.7 at high did not read what
    # low had written, 2026-11-10), so it must not wait for that leader.
    payload = json.dumps(["minara", body.get("model"), body.get("reasoning_effort"), tools, first], sort_keys=True,
                         ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


async def _summary_events(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens, chunks):
    """iter_events for a Codex compaction whose tools the relay declared again: the reply is text, or it is asked again
    (direct._summary_stream on Retool). An attempt that calls a tool is dropped (its cost still counts) and asked once
    more with an instruction after the last message; a second call sends the request as Codex made it, with no tools."""
    import summary_guard
    for attempt in range(1, summary_guard.TRIES_WITH_TOOLS + 2):
        fallback = attempt > summary_guard.TRIES_WITH_TOOLS
        gate = summary_guard.Gate()
        end = None
        instruction = summary_guard.INSTRUCTION if attempt == 2 else None
        async with contextlib.aclosing(_stream(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens,
                                               chunks, instruction, text_only=fallback)) as events:
            async for event in events:
                if event.get("type") == "end":
                    end = event
                    continue
                for ready in gate.feed(event):
                    yield ready
        if end is None:
            return
        if not gate.called or end.get("reason") == "cancelled" or fallback:
            for ready in gate.release():
                yield ready
            yield end
            return
        journal.emit("turn.compaction_tool_call",
                     "Модель вызвала инструмент вместо сводки сжатия: ответ отброшен, %s" % (
                         "запрос повторён с просьбой ответить текстом" if attempt < summary_guard.TRIES_WITH_TOOLS else
                         "запрос идёт без инструментов, как его прислал Codex"),
                     "warn", model=model_provider.get("model"), called=gate.called, wrote_text=gate.text,
                     attempt=attempt, tools_kept=attempt < summary_guard.TRIES_WITH_TOOLS, upstream="minara")


async def _stream(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens, chunks=None,
                  instruction=None, text_only=False):
    import direct
    side = client_context.current().get("client_task") in client_context.SIDE_TASKS
    # text_only: the request as the client's own text, no tools and no canonical history (the summary guard's last try).
    body = build_body(cfg, model_provider, head, prompt, None if text_only else tools, None if text_only else tool_choice,
                      max_tokens, chunks, None if text_only else session, instruction, mark_tail=not side)
    # The path: chat, or /v1/messages for a Claude level that the chat path sends to a channel without cache. A level the
    # relay moved there itself (not measured) goes back to chat when /v1/messages refuses it.
    level = body.get("reasoning_effort") or "default"
    route = {"messages": use_messages(model_provider, body)}
    if side and route["messages"]:
        # A side request marks no tail of its own, so on /v1/messages it read the dialog's cache and wrote nothing, and
        # Minara bills such a reply at the input price for its whole prompt (TOOL_RESULTS_END). There it writes its tail:
        # the instruction at the write price instead of the whole conversation at the input price.
        body = build_body(cfg, model_provider, head, prompt, None if text_only else tools,
                          None if text_only else tool_choice, max_tokens, chunks, None if text_only else session,
                          instruction)
    route["measured"] = route["messages"] and level in set(model_provider.get("messages_levels") or ())
    # Requests that go at once with one Claude head nobody has cached yet (Claude Code starts several subagents in one
    # message): the first writes it, the others wait for its answer to begin and then read it, as on Retool
    # (direct._claim_head). Minara's cache is shared by all its accounts; an entry is there once its response began.
    head_key = _head_key(body) if cfg.get("direct_cache", True) else None
    # Its cache lasts an hour: a head used within the last 55 minutes is read, no request waits for a leader.
    warm = HEAD_WARM if cfg.get("minara_cache_ttl", "1h") == "1h" else None  # 5-minute marks: Retool's window
    head_role, head_gate = (direct._claim_head(head_key, time.monotonic(), id(session), warm=warm)
                            if head_key else (None, None))
    head_marked = False
    token = token_of(cfg)
    if not isinstance(token, str) or not token.strip():
        raise apierr.service_unavailable("Minara Bearer token is missing")
    q = queue.Queue(maxsize=32)
    stopped = threading.Event()
    holder = {"response": None}
    close_lock = threading.Lock()
    close_claimed = False
    close_finished = threading.Event()
    slots = _worker_slots(cfg)
    admitted = started = False
    deadline = time.monotonic() + cfg.get("turn_timeout", 900)

    def put(item):
        while not stopped.is_set():
            try:
                q.put(item, timeout=0.1)
                return
            except queue.Full:
                if time.monotonic() >= deadline:
                    stopped.set()

    def close_response():
        # curl_cffi.Response.close waits for its stream task, including a pending
        # socket read. It must never run on the relay's shared asyncio loop.
        nonlocal close_claimed
        with close_lock:
            response = holder["response"]
            if response is None:
                return
            claimed = close_claimed
            if not claimed:
                close_claimed = True
        if claimed:
            # Only worker/cleanup daemons call this, never the asyncio loop.
            close_finished.wait()
            return
        try:
            response.close()
        except Exception:
            pass
        finally:
            close_finished.set()

    def abort():
        stopped.set()
        response = holder["response"]
        if response is not None:
            # Signal curl immediately, but leave the potentially blocking join
            # to a daemon. Before headers arrive the request timeout bounds it.
            quit_now = getattr(response, "quit_now", None)
            if quit_now is not None:
                quit_now.set()
            threading.Thread(target=close_response, name="minara-close", daemon=True).start()

    request_sent = getattr(session, "_provider_request_sent", None)

    def worker():
        try:
            from curl_cffi import requests
            from curl_cffi.requests.exceptions import Timeout as CurlTimeout
            if stopped.is_set():
                return
            if time.monotonic() >= deadline:
                raise apierr.upstream_timeout("Minara turn expired before upstream request")
            post = getattr(session.backend, "_minara_post", None) or requests.post
            if request_sent is not None:
                request_sent()
                put({"type": "request_sent"})
            delays = list(cfg.get("minara_channel_retry_delays", (3, 8)))  # numbers: config.load checked them
            base = cfg.get("base", BASE).rstrip("/")
            while True:
                if route["messages"]:
                    url, payload = base + "/messages", messages_body(body)
                    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                               "Accept": "text/event-stream", "anthropic-version": ANTHROPIC_VERSION}
                else:
                    url, payload = base + "/chat/completions", body
                    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                               "Accept": "text/event-stream"}
                response = post(url, headers=headers, json=payload, impersonate="chrome131", stream=True,
                                allow_redirects=False,
                                # (connect, read): curl_cffi makes a lone number on a stream its low-speed window too,
                                # and cut a reply silent for 120 s (a model that reasons before it writes anything: Opus
                                # at max took up to 292 s to its first event on Retool). curl adds the two: silence is
                                # allowed for SILENCE_LIMIT, never past the turn's deadline, so a cancelled silent request
                                # gives its worker slot back within that.
                                timeout=_read_timeout(cfg, deadline))
                holder["response"] = response
                if stopped.is_set():
                    return
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" not in content_type:
                        raise apierr.upstream_error("Minara did not return an SSE stream")
                    saw_frame = False
                    retry_stream = False
                    for data in sse_data(response.iter_content(chunk_size=16384)):
                        if stopped.is_set():
                            return
                        if not saw_frame and data != "[DONE]":
                            saw_frame = True
                            try:
                                first_item = _bounded_json(data)
                            except (ValueError, RecursionError):
                                first_item = None
                            if (isinstance(first_item, dict)
                                    and ("error" in first_item or first_item.get("type") == "error")
                                    and delays and time.monotonic() + delays[0] < deadline):
                                retry_stream = True
                                break
                        put(data)
                    if retry_stream:
                        journal.emit("upstream.channel_retry",
                                     "Minara: ошибка потока до начала генерации, повтор через %g с" % delays[0],
                                     "info", upstream="minara", status=200, delay=delays[0], model=body.get("model"))
                        holder["response"] = None
                        try:
                            response.close()
                        except Exception:
                            pass
                        if stopped.wait(delays.pop(0)):
                            return
                        continue
                    break
                text = _error_body(response)
                if (route["messages"] and not route["measured"] and response.status_code in (400, 404, 422)
                        and not _channel_trouble(response.status_code, text)):
                    # A level the relay moved to /v1/messages by itself, refused there before anything was generated:
                    # back to chat for good, and this request asks chat now.
                    switch_to_messages(body.get("model"), level, switched=False,
                                       reason="HTTP %s: %s" % (response.status_code, _upstream_message(text, 160)))
                    route["messages"] = False
                    holder["response"] = None
                    try:
                        response.close()
                    except Exception:
                        pass
                    continue
                error = _http_error(response.status_code, response.headers, text)
                if response.status_code == 402:
                    refused = getattr(getattr(getattr(session, "backend", None), "credits", None), "note_refusal", None)
                    if refused is not None:
                        refused()  # the balance check that follows marks the account exhausted
                if not (delays and _channel_trouble(response.status_code, text)
                        and time.monotonic() + delays[0] < deadline):
                    raise error
                # Said, so a slow answer can be told from Minara's own retries (one took 17 s on an image).
                journal.emit("upstream.channel_retry", "Minara: нет рабочего канала (HTTP %s), повтор через %g с" % (
                    response.status_code, delays[0]), "info", upstream="minara", status=response.status_code,
                    delay=delays[0], model=body.get("model"))
                # Minara's channel failures are answered before anything is generated or billed: ask again shortly.
                holder["response"] = None
                try:
                    response.close()
                except Exception:
                    pass
                if stopped.wait(delays.pop(0)):
                    return
        except apierr.ApiError as exc:
            put(exc)
        except Exception as exc:
            # curl exceptions may contain credential-bearing URLs/headers; never forward them.
            timed_out = isinstance(exc, TimeoutError) or (
                "CurlTimeout" in locals() and isinstance(exc, CurlTimeout)) or getattr(exc, "code", None) == 28
            put(apierr.upstream_timeout("Minara upstream request timed out") if timed_out else
                apierr.upstream_error("Minara connection or stream read failed"))
        finally:
            close_response()
            slots.release()
            put(None)

    session._direct_reply = None
    session._active_direct_abort = abort
    thread = threading.Thread(target=worker, name="minara-stream", daemon=True)
    parser = None  # made with the first data: the worker has settled the path by then
    try:
        yield {"type": "usage_start"}
        if head_role == "follow":
            waiting_since = time.monotonic()
            outcome = await direct._wait_for_head(head_gate)
            journal.emit("cache.head_wait", "Запрос ждал начала ответа соседнего запроса с той же головой: голова "
                         "читается из кэша, а не пишется второй раз", "info", outcome=outcome, model=body.get("model"),
                         upstream="minara", waited_ms=round((time.monotonic() - waiting_since) * 1000))
        while not slots.acquire(blocking=False):
            if stopped.is_set() or getattr(session, '_cancelled', False):
                yield {"type": "end", "reason": "cancelled"}
                return
            if time.monotonic() >= deadline:
                raise apierr.upstream_timeout("Minara worker admission exceeded its deadline")
            await asyncio.sleep(0.01)
        admitted = True
        if stopped.is_set() or getattr(session, '_cancelled', False):
            yield {"type": "end", "reason": "cancelled"}
            return
        if time.monotonic() >= deadline:
            raise apierr.upstream_timeout("Minara turn expired before upstream request")
        thread.start()
        started = True
        # No display status while Minara is silent: the client learns that the model reasons from Minara itself (a
        # thinking_start of the parser, real reasoning text), and a silent stream is kept alive by the keepalive of
        # its dialect (proxy._pump). A timer of the relay's own (54b1023: a status after 1.5 s, then a heartbeat every
        # 5 s, for every level but "none") printed "Модель думает…" with no signal from the backend and made a
        # refusal that came later than 1.5 s final instead of moving it to another account.
        while True:
            if getattr(session, "_cancelled", False):
                yield {"type": "end", "reason": "cancelled"}
                return
            now = time.monotonic()
            if now >= deadline:
                raise apierr.upstream_timeout("Minara turn exceeded its deadline")
            try:
                item = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            if item is None:
                if parser is None or not parser.done:
                    raise apierr.upstream_error("Minara stream ended before DONE")
                return
            if isinstance(item, dict) and item.get("type") == "request_sent":
                yield item
                continue
            if isinstance(item, Exception):
                raise item
            if parser is None:
                parser = (MessagesParser if route["messages"] else EventParser)(pricing=model_provider.get("pricing"))
            if head_gate is not None and not head_marked:
                head_marked = True
                direct._head_started(head_gate, head_key)  # the answer began: the head is in the cache now
            for event in parser.feed(item):
                yield event
            if parser.done:
                return
    finally:
        if head_role == "lead" and not head_marked:
            # The leader ended without an answer: nobody waits for it, the next request leads. A follower never releases
            # its leader's gate (the others would stop waiting and write the head themselves).
            direct._head_released(head_key, head_gate)
        abort()
        if admitted and not started:
            slots.release()
        if getattr(session, "_active_direct_abort", None) is abort:
            session._active_direct_abort = None
