"""Direct Retool inference transport (copilotStreamGPT, NDJSON) with prompt caching.

Drop-in alternative to the agent WebSocket path: same normalised event stream, but a
plain HTTP POST. The stable system head is sent as a cached block so a large project
context is billed once and reused, keeping time-to-first-token flat.
"""
import asyncio
import hashlib
import json
import socket
import time
import urllib.error
import urllib.request

import apierr
import credits
import journal
import models as model_catalog

ROUTE = "/api/gpt/copilotStreamGPT?origin=frontend"
MIN_OUTPUT_TOKENS = 16
_ASSIST_ONLY = {"claude-haiku-4-5-20251001", "claude-haiku"}  # models proven to live only on the Assist list


def _force_close_response(resp):
    """Instantaneously unblock threads waiting in WinSock recv() or HTTPResponse read()."""
    if resp is None:
        return
    try:
        fd = resp.fileno() if hasattr(resp, "fileno") else None
        if fd is not None and fd != -1:
            try:
                s = socket.socket(fileno=fd)
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass
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
    return format((length * 2654435761) & 0xFFFFFFFF, "08x")


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
        node["description"] = str(schema["description"])[:500]
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
                          "description": (function.get("description") or "")[:1000],
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
        kind = spec.get("type")
        if kind == "number" and isinstance(value, str):
            try:
                data[key] = float(value) if "." in value else int(value)
                changed = True
            except ValueError:
                pass
        elif spec.get("type") == "boolean" and isinstance(value, str):
            if value.lower() in ("true", "false"):
                data[key] = value.lower() == "true"
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
    """Cheap request that re-reads the cached prefix so its TTL does not lapse."""
    payload = dict(body)
    payload["maxCompletionTokens"] = MIN_OUTPUT_TOKENS
    model_name = payload.get("model")
    entry = model_catalog._BY_ID.get(model_name)
    levels = entry.get("reasoning_effort_levels") or () if entry else ()
    if levels:
        payload["reasoningEffort"] = "low" if "low" in levels else levels[0]
    else:
        payload["reasoningEffort"] = "low"
    request = urllib.request.Request(session.backend.cfg["base"] + ROUTE,
                                     data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
                                     headers=session.backend.headers(), method="POST")
    response = urllib.request.urlopen(request, timeout=120)
    try:
        for _ in response:
            break
    finally:
        response.close()


async def _keepwarm(session, cfg):
    """Hold the provider cache open while a conversation is in use."""
    interval = float(cfg.get("cache_keepwarm_seconds", 0) or 0)
    if interval <= 0:
        return
    idle = float(cfg.get("cache_keepwarm_idle", 1800) or 1800)
    while True:
        try:
            await asyncio.sleep(interval)
            last_req = getattr(session, "_last_request_at", getattr(session, "_warm_at", 0))
            if time.time() - last_req > idle:
                return
            body = getattr(session, "_warm_body", None)
            if not body or getattr(session, "_busy", False):
                continue
            await asyncio.to_thread(_ping, session, body)
            session._warm_at = time.time()
            prefix_tokens = int(getattr(session, "_last_prefix_tokens", 0) or 0)
            est_credits = prefix_tokens * credits.RATE_CACHE_READ
            session._keepwarm_cost = round(getattr(session, "_keepwarm_cost", 0.0) + est_credits, 4)
            journal.emit("cache.keepwarm", "Префикс кэша прогрет", "debug", model=body.get("model"),
                         thread=journal.tag(getattr(session, "thread_id", None)),
                         prefix_tokens=prefix_tokens, est_credits=round(est_credits, 4),
                         est_credits_total=session._keepwarm_cost)
            if cfg.get("direct_debug"):
                print("[warm] prefix refreshed", flush=True)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # A transient upstream/network error must not kill the warm cycle for
            # the rest of the idle window; log and retry on the next interval.
            journal.emit("cache.keepwarm", "Прогрев не удался, повтор на следующем интервале: " + str(exc), "warn")


async def iter_events(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens,
                      chunks=None):
    """Yield normalised relay events from the direct NDJSON stream."""
    body = build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks, session=session)
    param_types = {tool["name"]: tool.get("params") or {} for tool in (tools or [])}
    if model_provider.get("model") in _ASSIST_ONLY:
        body = {key: value for key, value in body.items() if key != "metadata"}
    session._busy = True
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
                _ASSIST_ONLY.clear()
            _ASSIST_ONLY.add(model_provider.get("model"))
            print("[assist] retry without agent marker: %s" % (error.message or "")[:80], flush=True)
            journal.emit("upstream.assist_retry", "Модель только в списке Assist — повтор без маркера", "warn",
                         model=model_provider.get("model"))
            body = {key: value for key, value in body.items() if key != "metadata"}
            async for event in _stream_body(cfg, session, body, param_types):
                yield event
    finally:
        session._busy = False
        if body.get("openAICacheKey"):
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
            session._warm_at = time.time()
            session._last_request_at = time.time()
            task = getattr(session, "_warm_task", None)
            if task is None or task.done():
                session._warm_task = asyncio.create_task(_keepwarm(session, cfg))


async def _stream_body(cfg, session, body, param_types=None):
    payload = json.dumps(body, ensure_ascii=True).encode("utf-8")
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
    queue: asyncio.Queue = asyncio.Queue()
    holder = {"response": None}

    def abort_direct():
        _force_close_response(holder.get("response"))
        loop.call_soon_threadsafe(queue.put_nowait, asyncio.CancelledError())

    session._active_direct_abort = abort_direct

    def worker():
        try:
            attempt = 0
            while True:
                if session._cancelled:
                    return
                request = urllib.request.Request(session.backend.cfg["base"] + ROUTE,
                                                 data=payload, headers=headers, method="POST")
                try:
                    response = urllib.request.urlopen(request, timeout=timeout)
                    break
                except urllib.error.HTTPError as exc:
                    detail = exc.read()[:400].decode("utf-8", "replace")
                    if exc.code in (401, 403) and attempt == 0:
                        attempt = 1
                        if session.backend.refresher.refresh():
                            headers.update(session.backend.headers())
                            continue
                        raise apierr.upstream_auth_failed(detail)
                    holder["response"] = exc
                    raise apierr.ApiError(*_http_error(exc.code, detail))
            holder["response"] = response
            if session._cancelled:
                _force_close_response(response)
                return
            for raw in response:
                if session._cancelled:
                    break
                line = raw.strip()
                if not line:
                    continue
                loop.call_soon_threadsafe(queue.put_nowait, line)
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            if not session._cancelled:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            _force_close_response(holder.get("response"))
            loop.call_soon_threadsafe(queue.put_nowait, None)

    text, thinking = [], []
    produced = False
    attempts_left = 2
    try:
        while True:
            task = loop.run_in_executor(None, worker)
            try:
                async for event in _drain(cfg, session, queue, text, thinking, param_types):
                    produced = True
                    yield event
            finally:
                if not task.done():
                    _force_close_response(holder.get("response"))
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                except Exception:
                    pass
            if session._cancelled:
                yield {"type": "end", "reason": "cancelled"}
                return
            if produced or attempts_left <= 0:
                break
            # Copilot occasionally returns metadata+end_of_stream with no blocks at all.
            attempts_left -= 1
            holder["response"] = None
        if text:
            yield {"type": "final_text", "text": "".join(text)}
        yield {"type": "end", "reason": "stop"}
    finally:
        session._active_direct_abort = None


async def _drain(cfg, session, queue, text, thinking, param_types=None):
    started = loop_now()
    think_started = None
    """Consume one attempt's NDJSON lines, yielding normalised events.

    Calls are keyed by id: a provider may interleave several tool calls in one
    turn, and a single slot would merge their arguments.
    """
    stack = []  # open calls, newest last; deltas belong to the newest open block
    calls = {}
    current = None
    req_id = getattr(session, "thread_id", None) or "req"
    while True:
        if session._cancelled:
            yield {"type": "end", "reason": "cancelled"}
            return
        try:
            item = await asyncio.wait_for(queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            if think_started is not None and not text and not stack and not calls:
                elapsed = loop_now() - think_started
                print(f"[{req_id}] Model is actively reasoning... ({round(elapsed)}s elapsed)", flush=True)
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
        kind = event.get("event")
        if kind == "content_block_start":
            if event.get("type") == "thinking":
                # Opus 5.5 streams no thinking text, only this early start and a final
                # signature; surfacing the start replaces silence with "thinking".
                think_started = loop_now()
                yield {"type": "thinking_start"}
            elif event.get("type") == "tool_call":
                think_started = None
                current = event.get("toolCallId") or "call_%d" % len(calls)
                calls[current] = {"id": event.get("toolCallId"), "name": event.get("toolName"), "args": [],
                                  "out": "", "stream": ArgStream((param_types or {}).get(event.get("toolName")))}
                stack.append(current)
                # Announce the call immediately so clients render it while arguments generate.
                yield {"type": "tool_call", "id": event.get("toolCallId"), "name": event.get("toolName"),
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
            key = stack.pop() if stack else current
            call = calls.pop(key, None) if key is not None else None
            if call is not None:
                streamed = call["out"] + call["stream"].finish()
                raw = "".join(call["args"])
                # The streamed text is already coerced; fall back for non-JSON or empty input.
                try:
                    json.loads(streamed)
                    final = streamed
                except ValueError:
                    final = coerce_arguments(raw, (param_types or {}).get(call["name"]))
                yield {"type": "tool_call", "id": call["id"], "name": call["name"], "arguments": final}
            current = stack[-1] if stack else None
        elif kind == "metadata":
            usage = event.get("usage") or {}
            if any(k in usage for k in ("promptTokensReadFromCache", "promptTokensWrittenToCache", "creditsUsed")):
                print("[cache] in=%s read=%s write=%s credits=%s" % (
                    usage.get("inputTokens"), usage.get("promptTokensReadFromCache"),
                    usage.get("promptTokensWrittenToCache"), usage.get("creditsUsed")), flush=True)
            if usage.get("creditsUsed") is not None:
                yield {"type": "credits", "value": float(usage["creditsUsed"]),
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
        text = parsed.get("message") or text
    except Exception:
        pass
    if status == 429:
        return (429, "rate_limit_error", text)
    if status in (401, 403):
        return (502, "upstream_auth_error", text)
    if status in (400, 422):
        return (400, "invalid_request_error", text)
    return (502, "upstream_error", text)