"""Minara's OpenAI-compatible SSE transport, isolated from Retool state and billing.

No automatic generation retries: a disconnected request may already have been billed.
"""
import asyncio
import copy
import json
import math
import os
import queue
import threading
import time

import apierr

BASE = "https://api.minara.ai/v1"
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
        if not isinstance(result, str):
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


def build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks=None, session=None):
    """Translate canonical client history, not signed Retool replies, to OpenAI roles."""
    model = model_provider.get("upstream_model") or model_provider.get("model", "")
    if model.startswith("minara/"):
        model = model[len("minara/"):]
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
    body = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if tools:
        declared = []
        for tool in tools:
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                declared.append(copy.deepcopy(tool))
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


def normalize_usage(value):
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
    if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
        result["provider_cost"] = cost  # NOT Retool credits; currency is not inferred.
        currency = value.get("currency")
        if isinstance(currency, str) and 1 <= len(currency) <= 16 and currency.isascii() and currency.isalpha():
            result["provider_cost_currency"] = currency.upper()
    return result


class EventParser:
    def __init__(self, max_bytes=MAX_EVENT_BYTES):
        self.calls = {}
        self.finish = None
        self.done = False
        self.thinking = False
        self.max_bytes = max_bytes

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
        events = []
        if isinstance(item.get("usage"), dict):
            events.append({"type": "usage", "usage": normalize_usage(item["usage"])})
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
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                if not self.thinking:
                    events.append({"type": "thinking_start"})
                    self.thinking = True
                events.append({"type": "thinking", "delta": reasoning})
            text = delta.get("content")
            if isinstance(text, str) and text:
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


def _http_error(status, headers):
    if status in (401, 403):
        return apierr.upstream_auth_failed("Minara rejected upstream authentication")
    if status == 429:
        return apierr.too_many_requests("Minara rate limit", apierr.retry_after_seconds(headers.get("retry-after")) or 2)
    if status == 402:
        return apierr.ApiError(402, "insufficient_credits", "Minara account has insufficient balance")
    if status in (400, 404, 422):
        return apierr.bad_request("Minara rejected the upstream request (HTTP %s)" % status)
    return apierr.upstream_error("Minara upstream HTTP %s" % status)


async def iter_events(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens, chunks=None):
    body = build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks, session)
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
            response = post(cfg.get("base", BASE).rstrip("/") + "/chat/completions",
                            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "text/event-stream"},
                            json=body, impersonate="chrome131", stream=True, allow_redirects=False,
                            timeout=cfg.get("upstream_timeout", 120))
            holder["response"] = response
            if stopped.is_set():
                return
            if response.status_code != 200:
                raise _http_error(response.status_code, response.headers)
            content_type = response.headers.get("content-type", "").lower()
            if "text/event-stream" not in content_type:
                raise apierr.upstream_error("Minara did not return an SSE stream")
            for data in sse_data(response.iter_content(chunk_size=16384)):
                if stopped.is_set():
                    return
                put(data)
        except apierr.ApiError as exc:
            put(exc)
        except Exception as exc:
            # curl exceptions may contain credential-bearing URLs/headers; never forward them.
            timed_out = isinstance(exc, TimeoutError) or (
                "CurlTimeout" in locals() and isinstance(exc, CurlTimeout))
            put(apierr.upstream_timeout("Minara upstream request timed out") if timed_out else
                apierr.upstream_error("Minara connection or stream read failed"))
        finally:
            close_response()
            slots.release()
            put(None)

    session._direct_reply = None
    session._active_direct_abort = abort
    thread = threading.Thread(target=worker, name="minara-stream", daemon=True)
    parser = EventParser()
    try:
        yield {"type": "usage_start"}
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
        while True:
            if getattr(session, "_cancelled", False):
                yield {"type": "end", "reason": "cancelled"}
                return
            if time.monotonic() >= deadline:
                raise apierr.upstream_timeout("Minara turn exceeded its deadline")
            try:
                item = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            if item is None:
                if not parser.done:
                    raise apierr.upstream_error("Minara stream ended before DONE")
                return
            if isinstance(item, dict) and item.get("type") == "request_sent":
                yield item
                continue
            if isinstance(item, Exception):
                raise item
            for event in parser.feed(item):
                yield event
            if parser.done:
                return
    finally:
        abort()
        if admitted and not started:
            slots.release()
        if getattr(session, "_active_direct_abort", None) is abort:
            session._active_direct_abort = None
