"""Direct Retool inference transport (copilotStreamGPT, NDJSON) with prompt caching.

Drop-in alternative to the agent WebSocket path: same normalised event stream, but a
plain HTTP POST. The stable system head is sent as a cached block so a large project
context is billed once and reused, keeping time-to-first-token flat.
"""
import asyncio
import contextvars
import hashlib
import http.client
import json
import pathlib
import socket
import threading
import time
import uuid
import urllib.error
import urllib.request

import apierr
import credits
import journal
import client_context
import console_log
import models as model_catalog
from upstream_attempts import AttemptBudget
from upstream_diagnostics import AttemptTrace, prune_diagnostics

ROUTE = "/api/gpt/copilotStreamGPT?origin=frontend"
MIN_OUTPUT_TOKENS = 16
# Billing and keep-alive events describe the attempt, not its content: never held back.
_LIVE_EVENTS = frozenset({"credits", "thinking_heartbeat", "end"})
_EMPTY_DUMPED = set()  # (request_purpose, model) pairs whose empty answer is already saved this run


def _int_setting(cfg, key, default):
    try:
        return max(0, int(cfg.get(key, default)))
    except (TypeError, ValueError, OverflowError):
        return default
_ASSIST_ONLY = {"claude-haiku-4-5-20251001", "claude-haiku"}  # models proven to live only on the Assist list
_ASSIST_SEEDS = frozenset(_ASSIST_ONLY)


def _owning_socket(resp):
    """Best-effort walk to the socket OBJECT that owns the descriptor (a real
    http.client response is resp.fp.raw._sock). Closing through the owner makes
    the owner forget the number, so no second close can hit a reused fd."""
    seen = set()
    cur = resp
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        raw = getattr(cur, "raw", None)
        if raw is not None:
            sock = getattr(raw, "_sock", None)
            if sock is not None and hasattr(sock, "shutdown"):
                return sock
        cur = getattr(cur, "fp", None)
    return None


def _force_close_response(resp):
    """Instantaneously unblock threads waiting in WinSock recv() or HTTPResponse read().

    Windows needs a real closesocket() to abort a blocked recv(); shutdown() alone
    does not wake it. The fd is closed through its OWNING socket object, which then
    remembers -1: a worker thread that still holds the buffered reader (its own
    reference survives resp.fp = None) can never close the number a second time
    after it has been reused by an unrelated connection."""
    if resp is None:
        return
    owner = None
    try:
        fd = resp.fileno() if hasattr(resp, "fileno") else None
        owner = _owning_socket(resp) if fd is not None and fd != -1 else None
    except Exception:
        owner = None
    if owner is not None:
        try:
            owner.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            # Bypass the makefile reference count: the blocked worker holds a
            # reader, so a plain close() would only defer the real closesocket.
            owner._real_close()
        except Exception:
            try:
                owner.close()
            except Exception:
                pass
        try:
            if getattr(resp, "fp", None) is not None:
                resp.fp = None
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass
        return
    # Fallback for response shapes without a walkable socket chain.
    try:
        fd = resp.fileno() if hasattr(resp, "fileno") else None
        if fd is not None and fd != -1:
            try:
                if getattr(resp, "fp", None) is not None:
                    resp.fp = None
            except Exception:
                pass
            try:
                s = socket.socket(fileno=fd)
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                s.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        resp.close()
    except Exception:
        pass

_CACHE_MIN_CHARS = 0


def loop_now():
    return time.time()


def _js_length(value):
    """JSON.stringify(value).length in UTF-16 code units."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-16-le")) // 2


def _hash32(length):
    # Match JavaScript Number multiplication before >>> 0. The product exceeds
    # 2**53 around 3.4M UTF-16 units; exact Python integers then produce a different
    # hint for some lengths, so otherwise valid large requests are rejected.
    return format(int(length * 2654435761.0) & 0xFFFFFFFF, "08x")


def make_rit(messages, tools=None):
    """Non-cryptographic integrity hint the endpoint validates."""
    return _hash32(_js_length(messages)) + _hash32(_js_length(tools or []))


_KNOWN_TYPES = ("string", "number", "boolean", "null", "array", "object")


def _param_schema(schema, required):
    """Convert one JSON-Schema property to Retool's typed param shape.

    Retool accepts only literal/string/number/boolean/null/array/object/enum/anyOf,
    so an unsupported or union type must degrade instead of emitting "any".
    """
    if not isinstance(schema, dict):
        return {"type": "string", "required": bool(required)}
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item in _KNOWN_TYPES), None)
    node = {"type": "string"}
    if kind in ("string", "number", "integer", "boolean", "null"):
        node = {"type": "string" if kind == "string" else
                ("number" if kind in ("number", "integer") else kind)}
    elif kind == "array":
        node = {"type": "array", "items": _param_schema(schema.get("items") or {}, False)}
    elif kind == "object":
        node = {"type": "object", "properties": {
            name: _param_schema(sub, name in (schema.get("required") or []))
            for name, sub in (schema.get("properties") or {}).items()}}
    elif schema.get("enum"):
        node = {"type": "enum", "enum": [str(item) for item in schema["enum"]]}
    elif (schema.get("properties") or schema.get("additionalProperties")):
        node = {"type": "object", "properties": {
            name: _param_schema(sub, name in (schema.get("required") or []))
            for name, sub in (schema.get("properties") or {}).items()}}
    if schema.get("description"):
        node["description"] = str(schema["description"])
    node["required"] = bool(required)
    return node


def tools_to_retool(tools):
    """Translate client function tools to the Retool tool declaration shape with canonical ordering."""
    converted = []
    sorted_tools = sorted(
        tools or [],
        key=lambda item: (item.get("function", {}).get("name") if isinstance(item, dict) and isinstance(item.get("function"), dict)
                          else (item.get("name") if isinstance(item, dict) else "")) or ""
    )
    for tool in sorted_tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        params = {}
        schema = function.get("parameters") or {}
        required = set(schema.get("required") or [])
        properties = schema.get("properties") or {}
        for name in sorted(properties.keys()):
            params[name] = _param_schema(properties[name], name in required)
        converted.append({"name": function["name"],
                          "description": function.get("description") or "",
                          "params": params, "strict": True})
    return converted


def coerce_arguments(raw, params):
    """Repair scalar types the provider emits as strings (e.g. "1" for a number)."""
    text = raw or "{}"
    try:
        data = json.loads(text)
    except Exception:
        return text
    if not isinstance(data, dict):
        return text
    changed = False
    for key, spec in (params or {}).items():
        if key not in data or not isinstance(spec, dict):
            continue
        value = data[key]
        # _coerce_value rejects inf/nan, so the batch path cannot emit a bare
        # Infinity that would make the arguments invalid JSON.
        coerced = _coerce_value(value, spec) if isinstance(value, str) else None
        if coerced is not None:
            data[key] = coerced
            changed = True
    return json.dumps(data, ensure_ascii=False) if changed else text


def _coerce_value(value, spec):
    """Coerce one string scalar like coerce_arguments; None when it stays a string."""
    kind = spec.get("type") if isinstance(spec, dict) else None
    if kind == "number":
        try:
            number = float(value) if "." in value else int(value)
        except ValueError:
            return None
        return number if number == number and number not in (float("inf"), float("-inf")) else None
    if kind == "boolean" and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


class ArgStream:
    """Stream tool-call JSON as it arrives while keeping coerce_arguments' repairs.

    Only a top-level string value of a number/boolean parameter is held until it
    closes, then emitted coerced. Everything else (long string bodies, nested
    values) passes through at once, so the emitted text is always a prefix of
    the final arguments and clients can render the call while it is generated.
    Anything unexpected switches to verbatim pass-through.
    """

    def __init__(self, params):
        self.params = params or {}
        self.state = "pre"
        self.depth = 0
        self.in_string = False
        self.escape = False
        self.key = []
        self.held = None

    def _coercible(self, raw_key):
        try:
            name = json.loads('"' + raw_key + '"')
        except ValueError:
            return False
        spec = self.params.get(name)
        return isinstance(spec, dict) and spec.get("type") in ("number", "boolean")

    def feed(self, chunk):
        out = []
        for char in chunk or "":
            state = self.state
            if state == "pass":
                out.append(char)
            elif state == "pre":
                out.append(char)
                if char == "{":
                    self.depth, self.state = 1, "key"
                elif not char.isspace():
                    self.state = "pass"
            elif state == "hold":
                self.held.append(char)
                if self.escape:
                    self.escape = False
                elif char == "\\":
                    self.escape = True
                elif char == '"':
                    raw = "".join(self.held)
                    self.held = None
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = None
                    coerced = _coerce_value(value, self.params.get(self.key_name)) if isinstance(value, str) else None
                    out.append(raw if coerced is None else json.dumps(coerced))
                    self.state = "after"
            elif state == "keystr":
                out.append(char)
                if self.escape:
                    self.escape = False
                    self.key.append(char)
                elif char == "\\":
                    self.escape = True
                    self.key.append(char)
                elif char == '"':
                    self.state = "colon"
                else:
                    self.key.append(char)
            elif state == "key":
                out.append(char)
                if char == '"':
                    self.key, self.state = [], "keystr"
                elif char == "}":
                    self.state = "done"
                elif not char.isspace() and char != ",":
                    self.state = "pass"
            elif state == "colon":
                out.append(char)
                if char == ":":
                    self.state = "value"
                elif not char.isspace():
                    self.state = "pass"
            elif state == "value":
                if char.isspace():
                    out.append(char)
                elif char == '"' and self._coercible("".join(self.key)):
                    self.key_name = json.loads('"' + "".join(self.key) + '"')
                    self.held, self.escape, self.state = ['"'], False, "hold"
                else:
                    out.append(char)
                    self.state = "inval"
                    self._track(char)
            elif state == "inval":
                out.append(char)
                if not self.in_string and self.depth == 1 and char in ",}":
                    self.state = "key" if char == "," else "done"
                else:
                    self._track(char)
            elif state == "after":
                out.append(char)
                if char == ",":
                    self.state = "key"
                elif char == "}":
                    self.state = "done"
                elif not char.isspace():
                    self.state = "pass"
            else:  # done: trailing characters are relayed verbatim
                out.append(char)
        return "".join(out)

    def _track(self, char):
        if self.in_string:
            if self.escape:
                self.escape = False
            elif char == "\\":
                self.escape = True
            elif char == '"':
                self.in_string = False
        elif char == '"':
            self.in_string = True
        elif char in "{[":
            self.depth += 1
        elif char in "}]":
            self.depth -= 1

    def finish(self):
        """Flush a value left open by a truncated stream verbatim."""
        if self.held is not None:
            raw, self.held = "".join(self.held), None
            return raw
        return ""


def _extract_chunk_text(chunk):
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, dict):
        return str(chunk.get("text") or chunk.get("content") or "")
    if isinstance(chunk, list):
        parts = []
        for b in chunk:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or b.get("content") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(parts)
    return str(chunk or "")


def _is_user_chunk(chunk):
    if isinstance(chunk, dict):
        role = chunk.get("role")
        if role in ("user", "human", "client"):
            return True
    elif isinstance(chunk, list):
        if any(isinstance(b, dict) and b.get("role") in ("user", "human", "client") for b in chunk):
            return True
    text = _extract_chunk_text(chunk).lstrip()
    lowered = text.lower()
    return (lowered.startswith(("user:", "human:", "client:", "user task:"))
            or text.startswith(("User:", "Human:", "Client:", "USER TASK:")))


def _is_tool_result_chunk(chunk):
    if isinstance(chunk, dict):
        role = chunk.get("role")
        ctype = chunk.get("type")
        if role in ("tool", "function") or ctype in ("tool_result", "exec_result"):
            return True
    elif isinstance(chunk, list):
        if any(isinstance(b, dict) and (b.get("role") in ("tool", "function") or b.get("type") in ("tool_result", "exec_result")) for b in chunk):
            return True
    text = _extract_chunk_text(chunk).lstrip()
    lowered = text.lower()
    return (lowered.startswith(("[tool result", "[tool_result", "[command result", "[exec_result", "tool:", "tool result"))
            or "[tool result" in lowered or "[tool_result" in lowered or "[command result" in lowered)


def cache_key_for(head, model="", tools=None):
    """Prefix cache key. The model and the toolset are part of the upstream prompt
    prefix, so a change to either must not reuse the previous cache entry."""
    digest = hashlib.sha256()
    digest.update((model or "").encode("utf-8"))
    digest.update(b"\x00")
    if tools:
        digest.update(json.dumps(tools, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    digest.update(b"\x00")
    digest.update((head or "").encode("utf-8"))
    return digest.hexdigest()[:24]


def build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks=None, session=None):
    """Assemble the request body.

    The stable head becomes a cached system block and the history is sent as one
    message per chunk, preserving prior turn endpoints as cache breakpoints so
    intermediate multi-tool turns do not discard prefix caches.
    """
    messages = []
    cache = cfg.get("direct_cache", True) and len(head or "") >= _CACHE_MIN_CHARS
    if head:
        block = {"type": "text", "text": head}
        if cache:
            block["cache"] = True
        messages.append({"role": "system", "content": [block]})
    history = [part for part in (chunks or []) if part]
    if not history:
        if prompt:
            messages.append({"role": "user", "content": prompt})
    else:
        cache_indices = set()
        turn_boundaries = set()
        if cfg.get("direct_cache", True):
            session_ends = [idx for idx in getattr(session, "_cached_turn_ends", []) if 0 <= idx < len(history) - 1] if session else []

            # Chunk 0 is the initial conversation prompt and must ALWAYS be preserved
            turn_boundaries.add(0)

            for i, chunk in enumerate(history):
                if _is_user_chunk(chunk):
                    turn_boundaries.add(i)
                if i + 1 < len(history) and _is_user_chunk(history[i + 1]):
                    turn_boundaries.add(i)
                if _is_tool_result_chunk(chunk):
                    turn_boundaries.add(i)

            known_boundaries = sorted(set(session_ends) | turn_boundaries)
            # Anthropic enforces a strict maximum of 4 blocks with cache_control across the request.
            # If head is cached, that leaves at most 3 blocks for history. Otherwise at most 4.
            # The upstream backend adds its own cache_control breakpoint on the tool
            # definitions, so with tools present one block stays reserved for it.
            budget = 4 - (1 if tools else 0)
            max_history_cache = budget - 1 if (head and cache) else budget

            chosen = set()
            chosen.add(0)
            chosen.add(len(history) - 1)

            slots_left = max_history_cache - len(chosen)
            intermediates = [i for i in known_boundaries if 0 < i < len(history) - 1]
            if slots_left > 0 and intermediates:
                for idx in intermediates[-slots_left:]:
                    chosen.add(idx)

            if len(chosen) < max_history_cache and len(history) >= 3:
                for idx in reversed(range(1, len(history) - 1)):
                    if len(chosen) >= max_history_cache:
                        break
                    chosen.add(idx)

            cache_indices = chosen

        for index, part in enumerate(history):
            should_cache = index in cache_indices
            if isinstance(part, list):
                blocks = [dict(b) if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in part]
                if should_cache:
                    # Only text blocks carry the cache flag; an image-only chunk has no
                    # cacheable text and upstream may reject the unknown placement.
                    target = next((b for b in reversed(blocks) if b.get("type") == "text"), None)
                    if target is not None:
                        target["cache"] = True
                messages.append({"role": "user", "content": blocks})
            elif isinstance(part, dict):
                block = dict(part)
                if should_cache and block.get("type") == "text":
                    block["cache"] = True
                messages.append({"role": "user", "content": [block]})
            else:
                block = {"type": "text", "text": str(part)}
                if should_cache:
                    block["cache"] = True
                messages.append({"role": "user", "content": [block]})

    # Hard safety check: ensure total blocks with cache: True NEVER exceeds the Anthropic
    # limit. With tools present one block is reserved for the upstream tool breakpoint.
    max_cache_blocks = 3 if tools else 4
    cached_blocks = [
        b
        for m in messages
        for b in m.get("content", [])
        if isinstance(b, dict) and b.get("cache")
    ]
    if len(cached_blocks) > max_cache_blocks:
        for b in cached_blocks[1:-1]:
            if len(cached_blocks) <= max_cache_blocks:
                break
            b.pop("cache", None)
            cached_blocks = [
                blk for m in messages for blk in m.get("content", [])
                if isinstance(blk, dict) and blk.get("cache")
            ]

    if session is not None and history and cfg.get("direct_cache", True):
        prior = [e for e in getattr(session, "_cached_turn_ends", []) if e < len(history) - 1]
        all_ends = set(prior) | {e for e in turn_boundaries if e < len(history) - 1}
        all_ends.add(0)
        all_ends.add(len(history) - 1)
        session._cached_turn_ends = sorted(all_ends)

    body = {"model": model_provider.get("model"),
            "messages": messages,
            "rit": make_rit(messages, tools),
            "metadata": {"sandpackAgent": True}}
    resource = model_provider.get("providerResourceName")
    if resource:
        body["providerResourceName"] = resource
    if model_provider.get("reasoningEffort"):
        body["reasoningEffort"] = model_provider["reasoningEffort"]
    if max_tokens:
        body["maxCompletionTokens"] = max(int(max_tokens), MIN_OUTPUT_TOKENS)
    if tools:
        body["tools"] = tools
        choice = tool_choice if isinstance(tool_choice, str) else (
            (tool_choice or {}).get("function", {}).get("name") if isinstance(tool_choice, dict) else None)
        if choice and choice != "auto":
            body["toolChoice"] = choice
    if cache and head:
        body["openAICacheKey"] = cache_key_for(head, body.get("model"), tools)
    return body


def _bounded_put(loop, queue, item, is_cancelled, max_wait=120.0):
    """Thread-safe bounded put that never duplicates: one put future is awaited
    until it lands (a blocked worker is the backpressure) or the session dies.
    Re-creating the future on a timeout would enqueue the item twice. A consumer
    that vanished without cancelling cannot hold the worker forever: after
    max_wait the line is dropped — the turn is dead by then anyway."""
    future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
    deadline = time.monotonic() + max_wait if max_wait else None
    while True:
        timeout = 5.0 if deadline is None else max(0.05, min(5.0, deadline - time.monotonic()))
        try:
            future.result(timeout=timeout)
            return True
        except TimeoutError:
            if is_cancelled():
                future.cancel()
                return False
            if deadline is not None and time.monotonic() >= deadline:
                future.cancel()
                journal.emit("direct.queue_drop",
                             "Кадр отброшен: потребитель не отвечал дольше %d с — ход прерван" % int(max_wait),
                             "warn")
                return False


def _error_from_event(event):
    message = event.get("error")
    if isinstance(message, dict):
        message = message.get("message")
    status = event.get("statusCode")
    text = str(message or "upstream stream error")
    if status == 429:
        return apierr.too_many_requests(text)
    if status in (401, 403):
        return apierr.upstream_auth_failed(text)
    if status in (400, 404, 422):
        return apierr.bad_request(text)
    return apierr.upstream_error(text)


def _ping(session, body):
    """Cheap request that re-reads the cached prefix so its TTL does not lapse.

    Returns the credits Retool actually billed for the ping (from the usage
    metadata event), or None when the stream ended without reporting them."""
    payload = dict(body)
    payload["maxCompletionTokens"] = MIN_OUTPUT_TOKENS
    # Effort can be part of the provider cache scope. Preserve the exact request
    # settings rather than replacing max with low during a cache refresh.
    headers = session.backend.headers()
    headers["content-type"] = "application/json"
    request = urllib.request.Request(session.backend.cfg["base"] + ROUTE,
                                     data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
                                     headers=headers, method="POST")
    response = urllib.request.urlopen(request, timeout=120)
    actual_credits = None
    complete = False
    billing_id = "keepwarm:" + uuid.uuid4().hex
    warm_usage = {}
    try:
        for index, raw in enumerate(response):
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            warm_usage.update(usage)
            if usage.get("creditsUsed") is not None:
                reported = console_log.number(usage["creditsUsed"])
                if reported is not None:
                    actual_credits = reported
            if event.get("event") == "error":
                # Preserve any billed usage attached to the error, without claiming a refreshed TTL.
                raise _error_from_event(event)
            if event.get("event") == "end_of_stream" or event.get("type") == "end_of_stream":
                complete = True
                break
            if index >= 200:
                break
    finally:
        try:
            response.close()
        finally:
            session._last_warm_usage = warm_usage
            # Run in the worker, which can outlive a cancelled scheduler/lease.
            # Multiple metadata snapshots belong to this ONE billed ping.
            manager = getattr(session, "_credits_manager", None)
            if actual_credits is not None:
                session._keepwarm_cost = getattr(session, "_keepwarm_cost", 0.0) + actual_credits
                if manager is not None:
                    manager.record_turn(billing_id, actual_credits)
            else:
                mark_unknown = getattr(manager, "mark_usage_unknown", None)
                if mark_unknown:
                    mark_unknown()
            try:
                request_id = client_context.current().get("request_id") or getattr(session, "_request_tag", None) or "keepwarm"
                monitor = getattr(session, "_console_warm_cache_monitor", None)
                if monitor is None:
                    monitor = session._console_warm_cache_monitor = console_log.CacheMonitor()
                console_log.write(monitor.line(request_id, warm_usage, sample_id=billing_id,
                                              scope=(body.get("model"), body.get("openAICacheKey")))
                                  + " [keepwarm] " + client_context.label())
                snapshot = getattr(manager, "balance_snapshot", None)
                if snapshot:
                    console_log.write(console_log.balance_line(request_id, snapshot(), actual_credits) + " [keepwarm]")
                journal.emit("cache.keepwarm_usage", "Стоимость запроса прогрева", "info",
                             billing_id=billing_id, model=body.get("model"), source="relay",
                             request_purpose="cache-keepwarm", actual_credits=actual_credits,
                             complete=complete, usage=warm_usage)
            except Exception:  # noqa: BLE001 - reporting must not mask the ping's own outcome
                pass
    if not complete:
        raise apierr.upstream_error("cache refresh ended without end_of_stream")
    return actual_credits


async def _keepwarm(session, cfg):
    """Refresh active and idle prefixes, scheduling from request starts.

    Only one ping runs per session. Active turns get their first refresh at
    the configured offset, then at most every 240s (inside the 5-minute TTL).
    Failures back off; an unknown cache result is never reported as a hit.
    """
    interval = float(cfg.get("cache_keepwarm_seconds", 0) or 0)
    if interval <= 0:
        return
    idle = float(cfg.get("cache_keepwarm_idle", 1800) or 1800)
    during_turns = bool(cfg.get("cache_keepwarm_during_turns", True))
    turn_offset = min(max(float(cfg.get("cache_keepwarm_turn_offset", 200) or 200), 30.0), 285.0)
    active_interval = min(interval, 240.0)
    scheduled_seq = None
    next_turn_ping = 0.0
    retry_at = 0.0
    while True:
        context_token = client_context.bind({})
        try:
            await asyncio.sleep(0.05)
            now = time.time()
            busy = bool(getattr(session, "_busy", False))
            started = getattr(session, "_turn_started_at", 0) or 0
            seq = getattr(session, "_turn_seq", 0)
            if seq != scheduled_seq:
                scheduled_seq = seq
                next_turn_ping = started + turn_offset
            last_req = getattr(session, "_last_request_at", getattr(session, "_warm_at", 0))
            if not busy and not getattr(session, "_cache_dependents", 0) and now - last_req > idle:
                return
            body = getattr(session, "_warm_body", None)
            identity = getattr(session, "_warm_identity", None) or {}
            if busy:
                if not during_turns or not started:
                    await asyncio.sleep(0.95)
                    continue
                turn_body = getattr(session, "_turn_body", None)
                if turn_body:
                    body = turn_body
                    identity = getattr(session, "_turn_identity", None) or {}
                due = max(next_turn_ping, retry_at)
            else:
                due = max((getattr(session, "_warm_at", 0) or 0) + interval, retry_at)
            if not body:
                await asyncio.sleep(0.95)
                continue
            if now < due:
                await asyncio.sleep(min(0.95, max(0.05, due - now)))
                continue
            # The scheduler spans turns: bind the owner of this exact body,
            # snapshotting before to_thread so a following turn cannot relabel it.
            client_context.bind(identity)
            ping_started = now
            actual = await asyncio.to_thread(_ping, session, body)
            if busy and getattr(session, "_turn_seq", 0) == seq:
                session._turn_pinged = True
                next_turn_ping = ping_started + active_interval
            session._warm_at = ping_started
            session._warm_failures = 0
            retry_at = 0.0
            prefix_tokens = int(getattr(session, "_last_prefix_tokens", 0) or 0)
            estimate = prefix_tokens * credits.RATE_CACHE_READ
            if actual is None:
                session._keepwarm_estimated_cost = round(
                    getattr(session, "_keepwarm_estimated_cost", 0.0) + estimate, 4)
            usage = getattr(session, "_last_warm_usage", {})
            touched = any(isinstance(usage.get(key), (int, float)) and usage[key] > 0 for key in (
                "promptTokensReadFromCache", "promptTokensWrittenToCache"))
            journal.emit("cache.keepwarm",
                         "Кэш прочитан или записан при прогреве" if touched else
                         "Запрос прогрева завершён; использование кэша не подтверждено",
                         "info" if busy else "debug",
                         model=body.get("model"), thread=journal.tag(getattr(session, "thread_id", None)),
                         during_turn=busy, turn_age_seconds=round(ping_started - started) if busy else None,
                         prefix_tokens=prefix_tokens, est_credits=round(estimate, 4),
                         actual_credits=actual, credits_total=getattr(session, "_keepwarm_cost", 0.0),
                         cache_verified=touched, usage=usage)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if busy and getattr(session, "_turn_seq", 0) == seq:
                session._turn_pinged = True
            failures = getattr(session, "_warm_failures", 0) + 1
            session._warm_failures = failures
            if failures >= 5:
                journal.emit("cache.keepwarm", "Прогрев остановлен после пяти неудач", "warn")
                return
            retry_at = time.time() + min(active_interval * (2 ** (failures - 1)), 900.0)
            next_turn_ping = retry_at
            journal.emit("cache.keepwarm", "Прогрев не удался; повтор с задержкой: %s" % str(exc)[:200], "warn")
        finally:
            client_context.reset(context_token)


async def iter_events(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens,
                      chunks=None):
    """Yield normalised relay events from the direct NDJSON stream."""
    body = build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks, session=session)
    param_types = {tool["name"]: tool.get("params") or {} for tool in (tools or [])}
    if model_provider.get("model") in _ASSIST_ONLY:
        body = {key: value for key, value in body.items() if key != "metadata"}
    identity = client_context.current()
    session._busy = True
    session._turn_identity = identity
    session._turn_started_at = time.time()
    session._turn_pinged = False
    session._turn_seq = getattr(session, "_turn_seq", 0) + 1
    session._last_request_at = time.time()
    rejected = False
    if body.get("openAICacheKey"):
        session._turn_body = body
        # Start the scheduler with the turn, not after it: a session's first long
        # turn needs its anchored during-turn ping too.
        _ensure_keepwarm(session, cfg)
    try:
        try:
            async for event in _stream_body(cfg, session, body, param_types):
                yield event
        except apierr.ApiError as error:
            # Models absent from the App Builder list (legacy GPT-5, Haiku) live on
            # the Assist list, which is the same route without the agent marker.
            if (error.status != 400 or "metadata" not in body
                    or "not available right now" not in (error.message or "").lower()):
                raise
            if len(_ASSIST_ONLY) > 200:
                # Flush only self-learned names: wiping the proven seeds would make
                # known assist models pay a 400 + retry cycle all over again.
                _ASSIST_ONLY.intersection_update(_ASSIST_SEEDS)
            _ASSIST_ONLY.add(model_provider.get("model"))
            print("[assist] retry without agent marker: %s" % (error.message or "")[:80], flush=True)
            journal.emit("upstream.assist_retry", "Модель только в списке Assist — повтор без маркера", "warn",
                         model=model_provider.get("model"))
            body = {key: value for key, value in body.items() if key != "metadata"}
            if body.get("openAICacheKey"):
                session._turn_body = body
            async for event in _stream_body(cfg, session, body, param_types):
                yield event
    except apierr.ApiError as error:
        # A body upstream rejected (400/413/422) must not become the keepwarm body.
        rejected = 400 <= error.status < 500 and error.status not in (401, 403, 408, 429)
        raise
    finally:
        session._busy = False
        session._turn_body = None
        session._turn_identity = None
        if body.get("openAICacheKey") and not rejected:
            prior_key = getattr(session, "_last_cache_key", None)
            if prior_key and prior_key != body["openAICacheKey"]:
                # A new key means the upstream prefix cache starts from zero: every
                # history breakpoint misses and the whole prompt is written again.
                journal.emit("cache.key_changed", "Кэш-ключ сменился — префикс будет перезаписан", "info",
                             old=prior_key, new=body["openAICacheKey"], model=body.get("model"),
                             tools=len(body.get("tools") or []),
                             thread=journal.tag(getattr(session, "thread_id", None)))
            session._last_cache_key = body["openAICacheKey"]
            session._warm_body = body
            session._warm_identity = identity
            # The TTL counts from the last cache touch: this request's start or a
            # later during-turn ping, NOT the turn's end; otherwise a pause after
            # a long turn outlives the 5-minute entry before the next idle ping.
            session._warm_at = max(session._turn_started_at, getattr(session, "_warm_at", 0) or 0)
            session._last_request_at = time.time()
            _ensure_keepwarm(session, cfg)


def _ensure_keepwarm(session, cfg):
    task = getattr(session, "_warm_task", None)
    if task is None or task.done():
        # A fresh cycle gets a fresh failure budget.
        session._warm_failures = 0
        session._warm_task = asyncio.create_task(_keepwarm(session, cfg), context=contextvars.Context())


def _dump_rejected_body(payload, detail, *, cfg=None, status=400, request_id=None):
    """Land the exact upstream-rejected body on disk for offline bisection.

    A 400 from the route validator ("Invalid request format") carries no field
    detail, and client history only grows — every later retry re-sends the same
    poison. The dump is local-only and holds the user's own conversation text,
    so a repeated identical body reuses its saved set and only the newest
    `upstream_error_max_dumps` sets are kept.
    """
    try:
        cfg = cfg or {}
        folder = pathlib.Path(cfg.get("upstream_error_dir") or "logs/upstream-400")
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(payload).hexdigest()
        key = (str(folder.resolve()), digest)
        with _DUMP_LOCK:
            known = _DUMP_RECENT.get(key)
            if known and (folder / ("%s-body.json" % known)).exists():
                print("[debug] upstream %s diagnostic: %s (same body already saved)" % (status, known), flush=True)
                return known
            request_tag = "".join(c for c in str(request_id or "unknown") if c.isalnum())[:64]
            stamp = "%s-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), request_tag, uuid.uuid4().hex[:12])
            (folder / ("%s-body.json" % stamp)).write_bytes(payload)
            (folder / ("%s-error.txt" % stamp)).write_text(detail, encoding="utf-8")
            metadata = {**client_context.current(), "request_id": request_id,
                        "upstream_status": status, "diagnostic_id": stamp,
                        "body_sha256": digest}
            (folder / ("%s-metadata.json" % stamp)).write_text(
                json.dumps(journal.scrub(metadata), ensure_ascii=False, indent=2), encoding="utf-8")
            _DUMP_RECENT.pop(key, None)
            _DUMP_RECENT[key] = stamp
            while len(_DUMP_RECENT) > 256:
                del _DUMP_RECENT[next(iter(_DUMP_RECENT))]
            prune_diagnostics(cfg, folder, keep=stamp)
        print("[debug] upstream %s diagnostic: %s" % (status, stamp), flush=True)
        return stamp
    except Exception:  # noqa: BLE001 - diagnostics must never break the turn
        pass


_DUMP_RECENT = {}
_DUMP_LOCK = threading.Lock()


def _dump_empty_once(payload, flags, cfg, session, model=None):
    """Keep one sample per request kind and model per relay run: each dump holds
    conversation text, so repeated empty answers must not pile up on disk."""
    budget = flags["budget"]
    if budget.empty_dump_attempted:
        return budget.empty_dump_id
    kind = (client_context.current().get("request_purpose") or "generation", model)
    if kind in _EMPTY_DUMPED:
        return None
    budget.empty_dump_attempted = True
    detail = json.dumps({"reason": "empty turn: end_of_stream without content blocks",
                         "usage": budget.last.get("usage")}, ensure_ascii=False)
    stamp = _dump_rejected_body(payload, detail, cfg=cfg, status="empty",
                                request_id=client_context.current().get("request_id")
                                or getattr(session, "_request_tag", None))
    if stamp:
        # Marked only after a successful write, so a failed write is retried later.
        _EMPTY_DUMPED.add(kind)
    journal.emit("upstream.empty_turn", "Пустой ответ upstream сохранён для диагностики", "warn",
                 diagnostic_id=stamp)
    budget.empty_dump_id = stamp
    return stamp


async def _stream_body(cfg, session, body, param_types=None, *, payload=None):
    if payload is None:
        payload = json.dumps(body, ensure_ascii=True).encode("utf-8")
    elif json.loads(payload) != body:
        raise ValueError("replay payload does not match the supplied body")
    if cfg.get("direct_debug"):
        body_summary = {k: v for k, v in body.items() if k not in ("messages", "tools")}
        if "tools" in body:
            body_summary["tools_count"] = len(body["tools"])
        print("[body]", json.dumps(body_summary, ensure_ascii=False), flush=True)
    headers = session.backend.headers()
    headers["content-type"] = "application/json"
    headers["accept"] = "application/json"
    timeout = cfg.get("turn_timeout", 900)
    loop = asyncio.get_running_loop()
    budget = getattr(session, "_attempt_budget", None) or AttemptBudget()
    request_id = client_context.current().get("request_id") or getattr(session, "_request_tag", None)

    def is_cancelled():
        return session._cancelled or bool(budget.cancel_event and budget.cancel_event.is_set())
    # Bounded so a stalled consumer (slow or gone client) cannot balloon memory:
    # the upstream worker blocks on hand-off, which backpressures the TCP read.
    # One queue per attempt: a retried attempt must never read the previous
    # worker's terminal sentinel (it made every retry end empty at once).
    holder = {"response": None, "queue": None, "trace": None}

    def hand_off_final(item, queue=None):
        """Land a terminal item (sentinel/error/cancel) without blocking. Safe from
        any thread; sacrifices one buffered data line if the queue is full."""
        queue = queue if queue is not None else holder["queue"]
        if queue is None:
            return

        def _put():
            try:
                queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                pass
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass
        loop.call_soon_threadsafe(_put)

    def abort_direct():
        _force_close_response(holder.get("response"))
        hand_off_final(asyncio.CancelledError())

    session._active_direct_abort = abort_direct

    def worker(queue):
        try:
            attempt = 0
            while True:
                if is_cancelled():
                    return
                budget.take(is_cancelled)
                trace = AttemptTrace(cfg, budget, request_id, body.get("model"), payload)
                holder["trace"] = trace
                request = urllib.request.Request(session.backend.cfg["base"] + ROUTE,
                                                 data=payload, headers=headers, method="POST")
                trace.wire.request(headers)
                try:
                    response = urllib.request.urlopen(request, timeout=timeout)
                    break
                except urllib.error.HTTPError as exc:
                    trace.response(exc, exc.code)
                    holder["response"] = exc
                    detail_bytes = exc.read(16384)
                    trace.wire.raw(detail_bytes)
                    trace.wire.event("http_error_body_read", limit_bytes=16384,
                                     bytes_read=len(detail_bytes),
                                     may_be_incomplete=len(detail_bytes) == 16384)
                    detail = detail_bytes.decode("utf-8", "replace")
                    diagnostic = None
                    if exc.code in (400, 422):
                        diagnostic = _dump_rejected_body(
                            payload, detail, cfg=cfg, status=exc.code,
                            request_id=client_context.current().get("request_id") or getattr(session, "_request_tag", None))
                        journal.emit("upstream.rejected", detail[:1000], "warn", upstream_status=exc.code,
                                     diagnostic_id=diagnostic, model=body.get("model"),
                                     effort=body.get("reasoningEffort"),
                                     upstream_request_id=exc.headers.get("request-id") or exc.headers.get("x-request-id"))
                    if exc.code in (401, 403) and attempt == 0:
                        attempt = 1
                        # The 401 may come from a header snapshot taken just before
                        # a keepalive rotation: pass the rejected cookie so the
                        # refresher adopts already-rotated credentials despite the
                        # MIN_INTERVAL gate.
                        if session.backend.refresher.refresh(rejected_cookie=headers.get("cookie")):
                            trace.finish("auth_retry")
                            exc.close()
                            headers.update(session.backend.headers())
                            continue
                        error = apierr.upstream_auth_failed(detail[:400])
                        error.upstream_status = exc.code
                        raise error
                    holder["response"] = exc
                    error = apierr.ApiError(*_http_error(exc.code, detail))
                    error.upstream_status = exc.code
                    error.diagnostic_id = diagnostic
                    raise error
            holder["response"] = response
            trace.response(response)
            if is_cancelled():
                _force_close_response(response)
                return
            for raw in response:
                trace.wire.raw(raw)
                if is_cancelled():
                    break
                line = raw.strip()
                if not line:
                    continue
                trace.observe(line)
                # Bounded hand-off: block this worker when the consumer is slower
                # than upstream instead of buffering the whole stream in memory.
                if not _bounded_put(loop, queue, line, is_cancelled):
                    # Dropped or cancelled: stop reading. The missing end_of_stream
                    # surfaces as a truncated-stream error, never a silent hole.
                    break
            trace.wire.event("reader_end", cancelled=is_cancelled())
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            trace = holder.get("trace")
            if trace:
                trace.wire.event("reader_error", exception_type=type(exc).__name__)
            if not is_cancelled():
                if isinstance(exc, (TimeoutError, urllib.error.URLError, ConnectionError, http.client.HTTPException,
                                    OSError)):  # OSError covers ssl.SSLError (TLS drops mid-stream)
                    timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
                    exc = (apierr.upstream_timeout("Retool response timed out") if timed_out else
                           apierr.upstream_error("Retool connection failed: %s" % type(exc).__name__))
                hand_off_final(exc, queue)
        finally:
            _force_close_response(holder.get("response"))
            hand_off_final(None, queue)

    text, thinking = [], []
    saw_end = False
    # Client service checks (Auto mode, titles) fail deterministically when empty:
    # re-sending the same body only pays for the same empty answer again.
    service_request = client_context.current().get("request_purpose") in client_context.SERVICE_KINDS
    try:
        max_empty_retries = _int_setting(cfg, "upstream_empty_retries", 1)
        empty_retries = 0
        while True:
            usable = False
            thinking_seen = False
            # Events of an attempt are held back until it proves usable, so a retried
            # attempt can never leak thinking blocks, signatures or blank text.
            held = []
            may_retry = (not service_request and empty_retries < max_empty_retries and budget.remaining > 1)
            yield {"type": "usage_start"}
            queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
            holder["queue"] = queue
            task = loop.run_in_executor(None, contextvars.copy_context().run, worker, queue)
            flags = {"budget": budget}
            failure = None
            try:
                async for event in _drain(cfg, session, queue, text, thinking, param_types, flags,
                                          cache_scope=(body.get("model"), body.get("openAICacheKey"))):
                    trace = holder.get("trace")
                    if trace:
                        trace.wire.event("parser_event", event=event)
                    kind = event.get("type")
                    if kind == "tool_call" or (kind == "text" and event.get("delta", "").strip()):
                        usable = True
                    if kind in ("thinking", "thinking_start", "thinking_signature", "thinking_heartbeat"):
                        thinking_seen = True
                    if usable:
                        for pending in held:
                            yield pending
                        held.clear()
                        yield event
                    elif kind in _LIVE_EVENTS or not may_retry:
                        yield event
                    else:
                        held.append(event)
            except BaseException as exc:
                failure = exc
                raise
            finally:
                if flags.get("usage_seen") and not flags.get("credits_reported"):
                    mark_unknown = getattr(getattr(session, "_credits_manager", None), "mark_usage_unknown", None)
                    if mark_unknown:
                        mark_unknown()
                if not task.done():
                    _force_close_response(holder.get("response"))
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                except Exception:
                    pass
                trace = holder.get("trace")
                if trace is not None:
                    outcome = ("cancelled" if is_cancelled() or isinstance(failure, asyncio.CancelledError) else
                               "error" if failure else "truncated" if not flags.get("end") else
                               "content" if usable else "thinking_only" if thinking_seen else "empty")
                    trace.finish(outcome)
                if isinstance(failure, apierr.ApiError):
                    budget.attach(failure)
            saw_end = bool(flags.get("end"))
            if is_cancelled():
                yield {"type": "end", "reason": "cancelled"}
                return
            if not usable and saw_end:
                _dump_empty_once(payload, flags, cfg, session, body.get("model"))
            if (usable or not saw_end or service_request or not budget.remaining
                    or empty_retries >= max_empty_retries):
                # This attempt is the final one: whatever it produced is the answer.
                for pending in held:
                    yield pending
                held.clear()
                break
            empty_retries += 1
            # Retry the same request, including instructions, tool choice and
            # full history. Never turn the user's task into a status-only job.
            text.clear()
            thinking.clear()
            holder["response"] = None
        if text:
            yield {"type": "final_text", "text": "".join(text)}
        if not saw_end and not is_cancelled():
            journal.emit("upstream.stream_truncated",
                         "Поток upstream оборвался без end_of_stream — ход завершён ошибкой", "warn",
                         model=body.get("model"), thread=journal.tag(getattr(session, "thread_id", None)))
            # An aborted stream must not masquerade as a clean stop: surface the
            # failure instead of a successful end. No auto-retry — tool calls may
            # already have been streamed to the client.
            yield {"type": "error", "error": budget.attach(apierr.upstream_error(
                "upstream stream ended without end_of_stream; the response may be incomplete"))}
            return
        yield {"type": "end", "reason": "stop"}
    finally:
        session._active_direct_abort = None


async def _drain(cfg, session, queue, text, thinking, param_types=None, flags=None, *, cache_scope=None):
    started = loop_now()
    think_started = None
    flags = {} if flags is None else flags
    """Consume one attempt's NDJSON lines, yielding normalised events.

    Calls are keyed by id: a provider may interleave several tool calls in one
    turn, and a single slot would merge their arguments.
    """
    stack = []  # open calls, newest last; deltas belong to the newest open block
    calls = {}
    current = None
    call_seq = 0  # monotonic: a finished call leaves `calls`, len() would repeat ids
    # The relay request id matches the `relay <id> POST ...` console lines:
    # concurrent requests of one conversation interleave in the same console.
    req_id = getattr(session, "_request_tag", None) or getattr(session, "thread_id", None) or "req"
    monitor = getattr(session, "_console_cache_monitor", None)
    if monitor is None:
        monitor = session._console_cache_monitor = console_log.CacheMonitor()

    def _final_call_event(key):
        call = calls.pop(key, None)
        if call is None:
            return None
        streamed = call["out"] + call["stream"].finish()
        raw = "".join(call["args"])
        # The streamed text is already coerced; fall back for non-JSON or empty input.
        try:
            json.loads(streamed)
            final = streamed
        except ValueError:
            final = coerce_arguments(raw, (param_types or {}).get(call["name"]))
        return {"type": "tool_call", "id": call["id"], "name": call["name"], "arguments": final}
    while True:
        if session._cancelled:
            yield {"type": "end", "reason": "cancelled"}
            return
        try:
            item = await asyncio.wait_for(queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            if think_started is not None and not text and not stack and not calls:
                elapsed = loop_now() - think_started
                console_log.write(f"[{req_id}] Model is actively reasoning... ({round(elapsed)}s elapsed)")
                journal.emit("upstream.thinking_heartbeat",
                             f"Модель активно думает ({round(elapsed)} с)",
                             "debug", elapsed=round(elapsed, 1), thread=journal.tag(req_id))
                yield {"type": "thinking_heartbeat", "elapsed": round(elapsed, 1)}
            continue
        if item is None:
            return
        if isinstance(item, asyncio.CancelledError) or session._cancelled:
            yield {"type": "end", "reason": "cancelled"}
            return
        if isinstance(item, BaseException):
            raise item

        if cfg.get("direct_raw"):
            print("[raw]", str(item)[:200], flush=True)
        try:
            event = json.loads(item)
        except Exception:
            continue
        if not isinstance(event, dict):
            # A valid JSON line that is not an object (a number, a list) must be
            # skipped, not kill the turn with an AttributeError.
            continue
        if event.get("event") == "end_of_stream" or event.get("type") == "end_of_stream":
            flags["end"] = True
            # A stream can end with calls still open: finalize them exactly like
            # content_block_end would, or the client keeps a dangling partial.
            while stack:
                final_event = _final_call_event(stack.pop())
                if final_event is not None:
                    yield final_event
            current = None
            return
        kind = event.get("event")
        if kind == "content_block_start":
            if event.get("type") == "thinking":
                # Opus 5.5 streams no thinking text, only this early start and a final
                # signature; surfacing the start replaces silence with "thinking".
                think_started = loop_now()
                yield {"type": "thinking_start"}
            elif event.get("type") == "tool_call":
                think_started = None
                if event.get("toolCallId"):
                    current = event["toolCallId"]
                else:
                    current = "call_%d" % call_seq
                    call_seq += 1
                # Store the resolved key as the id: upstream may omit toolCallId,
                # and adapters merge calls by id — an absent id would glue every
                # id-less call into one "call_0".
                calls[current] = {"id": current, "name": event.get("toolName"), "args": [],
                                  "out": "", "stream": ArgStream((param_types or {}).get(event.get("toolName")))}
                stack.append(current)
                # Announce the call immediately so clients render it while arguments generate.
                yield {"type": "tool_call", "id": current, "name": event.get("toolName"),
                       "arguments": "", "partial": True}
        elif kind == "content_block_delta":
            block = event.get("type")
            if block == "text":
                piece = event.get("partialText") or ""
                if piece and cfg.get("direct_debug") and not text:
                    print("[ttft] %.2fs" % (time.time() - started), flush=True)
                if piece:
                    text.append(piece)
                    yield {"type": "text", "delta": piece}
            elif block == "thinking":
                if think_started is None:
                    think_started = loop_now()
                piece = event.get("partialThinking") or ""
                if piece:
                    thinking.append(piece)
                    yield {"type": "thinking", "delta": piece}
            elif block == "signature":
                think_started = None
                if event.get("partialSignature"):
                    yield {"type": "thinking_signature", "signature": event["partialSignature"]}
            elif block == "tool_call" and current in calls:
                call = calls[current]
                piece = event.get("partialInputs") or ""
                call["args"].append(piece)
                emitted = call["stream"].feed(piece)
                if emitted:
                    call["out"] += emitted
                    yield {"type": "tool_call", "id": call["id"], "name": call["name"],
                           "arguments": call["out"], "partial": True}
        elif kind == "content_block_end":
            btype = event.get("type")
            if btype is not None and btype != "tool_call":
                # Ends of text/thinking blocks must not finalize an open tool call:
                # the streamed arguments would be cut short and later deltas lost.
                continue
            ended = event.get("toolCallId")
            if ended is not None and ended not in calls:
                # An end for an unknown (already finished) call must not close
                # whichever other call happens to be on top of the stack.
                continue
            if ended is not None and ended in calls:
                key = ended
                if ended in stack:
                    stack.remove(ended)
            else:
                key = stack.pop() if stack else current
            final_event = _final_call_event(key)
            if final_event is not None:
                yield final_event
            current = stack[-1] if stack else None
        elif kind == "metadata":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            if usage:
                flags["usage_seen"] = True
                flags["last_usage"] = usage
            if any(k in usage for k in ("promptTokensReadFromCache", "promptTokensWrittenToCache", "creditsUsed")):
                console_log.write(monitor.line(req_id, usage, scope=cache_scope) +
                                  (" " + client_context.label() if client_context.label() else ""))
            credits_value = console_log.number(usage.get("creditsUsed"))
            if credits_value is not None:
                flags["credits_reported"] = True
                yield {"type": "credits", "value": credits_value,
                       "usage": {"input_tokens": usage.get("inputTokens"),
                                 "output_tokens": usage.get("outputTokens"),
                                 "cache_read": usage.get("promptTokensReadFromCache"),
                                 "cache_write": usage.get("promptTokensWrittenToCache")}}
        elif kind == "error":
            raise _error_from_event(event)


def _http_error(status, detail):
    text = detail[:300]
    try:
        parsed = json.loads(detail)
        text = str(parsed.get("message") or text)[:300]
    except Exception:
        pass
    if status == 429:
        return (429, "rate_limit_error", text)
    if status in (401, 403):
        return (502, "upstream_auth_error", text)
    if status in (400, 422):
        return (400, "invalid_request_error", text)
    return (502, "upstream_error", text)
