"""Direct Retool inference transport (copilotStreamGPT, NDJSON) with prompt caching.

Drop-in alternative to the agent WebSocket path: same normalised event stream, but a
plain HTTP POST. The stable system head is sent as a cached block so a large project
context is billed once and reused, keeping time-to-first-token flat.
"""
import asyncio, hashlib, json, time, urllib.error, urllib.request

import apierr

ROUTE = "/api/gpt/copilotStreamGPT?origin=frontend"
MIN_OUTPUT_TOKENS = 16
_ASSIST_ONLY = set()  # models proven to live only on the Assist list
_CACHE_MIN_CHARS = 3000


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
    """Translate client function tools to the Retool tool declaration shape."""
    converted = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        params = {}
        schema = function.get("parameters") or {}
        required = schema.get("required") or []
        for name, sub in (schema.get("properties") or {}).items():
            params[name] = _param_schema(sub, name in required)
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


def cache_key_for(head):
    return hashlib.sha256(head.encode("utf-8")).hexdigest()[:24]


def build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks=None):
    """Assemble the request body.

    The stable head becomes a cached system block and the history is sent as one
    message per chunk, with the breakpoint moved to the end every turn so the
    previous prefix is read from cache instead of being reprocessed.
    """
    messages = []
    cache = cfg.get("direct_cache", True) and len(head or "") >= _CACHE_MIN_CHARS
    if head:
        block = {"type": "text", "text": head}
        if cache:
            block["cache"] = True
        messages.append({"role": "system", "content": [block]})
    history = [str(part) for part in (chunks or []) if part]
    if not history:
        if prompt:
            messages.append({"role": "user", "content": prompt})
    else:
        for index, part in enumerate(history):
            block = {"type": "text", "text": part}
            if cfg.get("direct_cache", True) and index == len(history) - 1:
                block["cache"] = True
            messages.append({"role": "user", "content": [block]})
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
    if cache:
        body["openAICacheKey"] = cache_key_for(head)
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
        await asyncio.sleep(interval)
        body = getattr(session, "_warm_body", None)
        if not body or getattr(session, "_busy", False):
            continue
        if time.time() - getattr(session, "_warm_at", 0) > idle:
            return
        try:
            await asyncio.to_thread(_ping, session, body)
            session._warm_at = time.time()
            if cfg.get("direct_debug"):
                print("[warm] prefix refreshed", flush=True)
        except Exception:
            return


async def iter_events(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens,
                      chunks=None):
    """Yield normalised relay events from the direct NDJSON stream."""
    body = build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks)
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
            body = {key: value for key, value in body.items() if key != "metadata"}
            async for event in _stream_body(cfg, session, body, param_types):
                yield event
    finally:
        session._busy = False
        if body.get("openAICacheKey"):
            session._warm_body = body
            session._warm_at = time.time()
            task = getattr(session, "_warm_task", None)
            if task is None or task.done():
                session._warm_task = asyncio.create_task(_keepwarm(session, cfg))


async def _stream_body(cfg, session, body, param_types=None):
    payload = json.dumps(body, ensure_ascii=True).encode("utf-8")
    if cfg.get("direct_debug"):
        print("[body]", json.dumps({k: v for k, v in body.items() if k != "messages"}, ensure_ascii=False), flush=True)
        print("[msgs]", json.dumps(body["messages"], ensure_ascii=False)[:1500], flush=True)
    headers = session.backend.headers()
    headers["content-type"] = "application/json"
    headers["accept"] = "application/json"
    timeout = cfg.get("turn_timeout", 900)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    holder = {"response": None}

    def worker():
        try:
            attempt = 0
            while True:
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
            for raw in response:
                line = raw.strip()
                if not line:
                    continue
                loop.call_soon_threadsafe(queue.put_nowait, line)
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            try:
                if holder["response"] is not None:
                    holder["response"].close()
            except Exception:
                pass
            loop.call_soon_threadsafe(queue.put_nowait, None)

    text, thinking = [], []
    call = None
    produced = False
    attempts_left = 2
    while True:
        task = loop.run_in_executor(None, worker)
        try:
            async for event in _drain(cfg, session, queue, text, thinking, param_types):
                produced = True
                yield event
        finally:
            if not task.done():
                try:
                    holder["response"] and holder["response"].close()
                except Exception:
                    pass
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


async def _drain(cfg, session, queue, text, thinking, param_types=None):
    started = loop_now()
    """Consume one attempt's NDJSON lines, yielding normalised events.

    Calls are keyed by id: a provider may interleave several tool calls in one
    turn, and a single slot would merge their arguments.
    """
    stack = []  # open calls, newest last; deltas belong to the newest open block
    calls = {}
    current = None
    while True:
        if session._cancelled:
            yield {"type": "end", "reason": "cancelled"}
            return
        item = await queue.get()
        if item is None:
            return
        if isinstance(item, BaseException):
            raise item
        if cfg.get("direct_debug"):
            print("[raw]", str(item)[:200], flush=True)
        try:
            event = json.loads(item)
        except Exception:
            continue
        kind = event.get("event")
        if kind == "content_block_start":
            if event.get("type") == "tool_call":
                current = event.get("toolCallId") or "call_%d" % len(calls)
                calls[current] = {"id": event.get("toolCallId"), "name": event.get("toolName"), "args": []}
                stack.append(current)
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
                piece = event.get("partialThinking") or ""
                if piece:
                    thinking.append(piece)
                    yield {"type": "thinking", "delta": piece}
            elif block == "signature":
                if event.get("partialSignature"):
                    yield {"type": "thinking_signature", "signature": event["partialSignature"]}
            elif block == "tool_call" and current in calls:
                calls[current]["args"].append(event.get("partialInputs") or "")
        elif kind == "content_block_end":
            key = stack.pop() if stack else current
            call = calls.pop(key, None) if key is not None else None
            if call is not None:
                yield {"type": "tool_call", "id": call["id"], "name": call["name"],
                       "arguments": coerce_arguments(
                           "".join(call["args"]), (param_types or {}).get(call["name"]))}
            current = stack[-1] if stack else None
        elif kind == "metadata":
            usage = event.get("usage") or {}
            if cfg.get("direct_debug"):
                print("[cache] in=%s read=%s write=%s credits=%s" % (
                    usage.get("inputTokens"), usage.get("promptTokensReadFromCache"),
                    usage.get("promptTokensWrittenToCache"), usage.get("creditsUsed")), flush=True)
            if usage.get("creditsUsed") is not None:
                yield {"type": "credits", "value": float(usage["creditsUsed"])}
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