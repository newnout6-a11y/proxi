"""Async SSE parser, bounded JSON reader, upstream HTTP error mapper, and payload helpers for universal drivers."""
from __future__ import annotations

import copy
import json
import math

import apierr
import redact

MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 128

_CONTEXT_KEYWORDS = (
    "context_length_exceeded",
    "maximum context length",
    "prompt is too long",
    "input is too long",
    "exceeds the model's maximum context",
    "context window",
    "too many tokens",
    "request_too_large",
)

_QUOTA_KEYWORDS = (
    "insufficient_quota",
    "insufficient_balance",
    "insufficient balance",
    "credit balance is too low",
    "out of credits",
    "exceeded your current quota",
    "billing_hard_limit_reached",
)


def bounded_json(text: str, **kwargs):
    """Parse JSON enforcing nesting depth and valid UTF-8 (no unpaired surrogates)."""
    depth = 0
    quoted = escaped = False
    for ch in text:
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON nesting exceeds maximum depth")
        elif ch in "]}":
            depth -= 1
    value = json.loads(text, **kwargs)
    stack = [value]
    while stack:
        part = stack.pop()
        if isinstance(part, str):
            try:
                part.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("JSON contains invalid Unicode") from None
        elif isinstance(part, dict):
            stack.extend(part.keys())
            stack.extend(part.values())
        elif isinstance(part, list):
            stack.extend(part)
    return value


def strict_tool_arguments(raw_args: str) -> tuple[dict | None, bool]:
    """Validate streamed JSON object arguments; returns `(parsed_dict_or_None, is_valid_object)`."""
    text = (raw_args or "").strip() or "{}"

    def bad_const(_val):
        raise ValueError("nonfinite")

    def check_float(val):
        num = float(val)
        if not math.isfinite(num):
            raise ValueError("nonfinite")
        return num

    try:
        parsed = bounded_json(text, parse_constant=bad_const, parse_float=check_float)
        if not isinstance(parsed, dict):
            return None, False
        return parsed, True
    except (ValueError, RecursionError):
        return None, False


async def aiter_sse_events(byte_chunks, *, label: str = "Upstream", max_bytes: int = MAX_EVENT_BYTES):
    """Async generator parsing raw HTTP byte chunks into `(event_name | None, data_str)` SSE frames."""
    buffer = b""
    event_name: str | None = None
    data_lines: list[str] = []
    data_size = 0

    async for chunk in byte_chunks:
        if not chunk:
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if len(line) > max_bytes:
                raise apierr.upstream_error(f"{label} SSE line exceeds size limit")
            if not line:
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                event_name = None
                data_lines = []
                data_size = 0
                continue
            if line.startswith(b":"):
                continue
            if line.startswith(b"event:"):
                raw_ev = line[6:].lstrip(b" ")
                event_name = raw_ev.decode("utf-8", errors="replace").strip() or None
            elif line.startswith(b"data:"):
                val = line[5:]
                if val.startswith(b" "):
                    val = val[1:]
                data_size += len(val)
                if data_size > max_bytes:
                    raise apierr.upstream_error(f"{label} SSE event exceeds size limit")
                try:
                    data_lines.append(val.decode("utf-8"))
                except UnicodeDecodeError:
                    raise apierr.upstream_error(f"{label} SSE event contains invalid UTF-8") from None
        if len(buffer) > max_bytes:
            raise apierr.upstream_error(f"{label} SSE buffer exceeds size limit")

    if buffer.strip():
        line = buffer.rstrip(b"\r")
        if line.startswith(b"data:"):
            val = line[5:]
            if val.startswith(b" "):
                val = val[1:]
            data_lines.append(val.decode("utf-8", errors="replace"))
    if data_lines:
        yield event_name, "\n".join(data_lines)


def extract_error_message(raw_text: str, cfg: dict | None = None, limit: int = 320) -> str:
    """Extract a clean, secret-scrubbed error message from an upstream JSON or text error body."""
    text = (raw_text or "").strip()
    if not text:
        return ""
    msg = ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("detail") or err.get("type") or "")
                meta_raw = (err.get("metadata") or {}).get("raw") if isinstance(err.get("metadata"), dict) else None
                if meta_raw and isinstance(meta_raw, str):
                    msg = f"{msg} ({meta_raw[:160]})" if msg else meta_raw[:160]
            elif isinstance(err, str):
                msg = err
            elif isinstance(parsed.get("message"), str):
                msg = parsed["message"]
            elif isinstance(parsed.get("detail"), str):
                msg = parsed["detail"]
    except ValueError:
        pass
    if not msg:
        msg = " ".join(text.split())
    secrets = (cfg or {}).get("cookie_values") or ()
    scrubbed = redact.redact(msg, cookie_values=secrets)
    return scrubbed[:limit]


_RATE_LIMIT_TYPES = frozenset({"rate_limit_error", "rate_limited", "rate_limit_exceeded"})
_QUOTA_TYPES = frozenset({"billing_error", "insufficient_quota", "insufficient_credits"})
_OVERLOADED_TYPES = frozenset({"overloaded_error", "service_unavailable", "model_overloaded"})
_DEFINITIVE_SSE_REFUSAL_STATUSES = frozenset({400, 401, 402, 403, 404, 413, 422, 429})


def _infer_sse_error_status(raw_body: str) -> int:
    """Infer an HTTP-equivalent status from an upstream SSE JSON error payload."""
    try:
        parsed = json.loads((raw_body or "").strip())
    except ValueError:
        return 502
    if not isinstance(parsed, dict):
        return 502
    err_obj = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
    raw_status = err_obj.get("status") or err_obj.get("status_code") or parsed.get("status")
    if type(raw_status) is int and 400 <= raw_status <= 599:
        return raw_status
    raw_code = err_obj.get("code") if err_obj.get("code") is not None else parsed.get("code")
    if type(raw_code) is int and 400 <= raw_code <= 599:
        return raw_code
    code_str = str(raw_code or "").strip().lower()
    type_str = str(err_obj.get("type") or parsed.get("type") or "").strip().lower()
    msg_low = str(err_obj.get("message") or parsed.get("message") or "").lower()

    if type_str in _RATE_LIMIT_TYPES or code_str in _RATE_LIMIT_TYPES or any(
        kw in msg_low for kw in ("rate limit", "rate-limit", "too many requests")
    ):
        return 429
    if type_str in _QUOTA_TYPES or code_str in _QUOTA_TYPES or any(kw in msg_low for kw in _QUOTA_KEYWORDS):
        return 402
    if type_str in _OVERLOADED_TYPES or code_str in _OVERLOADED_TYPES or "overloaded" in msg_low:
        return 503
    if type_str in ("timeout_error", "upstream_timeout") or code_str in ("timeout", "upstream_timeout"):
        return 504
    if type_str == "authentication_error" or code_str in ("invalid_api_key", "authentication_error"):
        return 401
    if type_str == "permission_error" or code_str == "permission_error":
        return 403
    if type_str == "request_too_large":
        return 413
    if type_str == "not_found_error":
        return 404
    if type_str == "invalid_request_error" or code_str == "context_length_exceeded" or any(
        kw in msg_low for kw in _CONTEXT_KEYWORDS
    ):
        return 400
    return 502


def map_sse_error(raw_body: str, headers, cfg: dict | None = None, *, pre_output: bool = False) -> apierr.ApiError:
    """Translate an in-stream SSE JSON error into a typed `ApiError` without dropping 429/402/503 signals."""
    status = _infer_sse_error_status(raw_body)
    unbilled = bool(pre_output and status in _DEFINITIVE_SSE_REFUSAL_STATUSES)
    err = map_http_error(status, headers, raw_body, cfg, unbilled=unbilled)
    if status == 429 and not pre_output:
        err.account_quota = False
    return err


def map_http_error(status: int, headers, raw_body: str, cfg: dict | None = None, *, unbilled: bool = True) -> apierr.ApiError:
    """Translate an upstream HTTP error response into a typed `apierr.ApiError`."""
    provider_label = (cfg or {}).get("provider_name") or (cfg or {}).get("provider_id") or "Upstream"
    detail = extract_error_message(raw_body, cfg)
    low = f"{detail} {(raw_body or '')[:500]}".lower()
    suffix = f": {detail}" if detail else ""

    if status in (400, 413, 422) and any(kw in low for kw in _CONTEXT_KEYWORDS):
        err = apierr.context_length_exceeded(f"{provider_label} rejected prompt length (HTTP {status}){suffix}")
    elif status == 402 or (status in (400, 403, 429) and any(kw in low for kw in _QUOTA_KEYWORDS)):
        err = apierr.ApiError(402, "insufficient_credits", f"{provider_label} quota/credits exhausted (HTTP {status}){suffix}")
    elif status in (401, 403):
        err = apierr.ApiError(502, "upstream_auth_error", f"{provider_label} rejected API credentials (HTTP {status}){suffix}")
    elif status == 429:
        retry_after = None
        if headers is not None:
            retry_after = apierr.retry_after_seconds(headers.get("retry-after") or headers.get("Retry-After"))
        err = apierr.account_rate_limited(f"{provider_label} rate limit (HTTP 429){suffix}", retry_after=retry_after)
    elif status == 404:
        err = apierr.not_found(f"{provider_label} returned HTTP 404{suffix}")
    elif status == 413:
        err = apierr.ApiError(413, "prompt_too_large", f"{provider_label} payload too large (HTTP {status}){suffix}")
    elif status in (400, 422):
        err = apierr.bad_request(f"{provider_label} rejected request (HTTP {status}){suffix}")
    elif status in (504, 524):
        err = apierr.upstream_timeout(f"{provider_label} gateway timeout (HTTP {status}){suffix}")
    elif status in (503, 529):
        err = apierr.service_unavailable(f"{provider_label} overloaded/unavailable (HTTP {status}){suffix}")
    else:
        err = apierr.upstream_error(f"{provider_label} error (HTTP {status}){suffix}")

    err.upstream_status = status
    err.unbilled = bool(unbilled)
    return err


def clean_openai_tools(tools: list | None) -> list[dict]:
    """Normalize client tools to standard OpenAI `{type: 'function', function: {...}}` without internal x-* markers."""
    if not tools:
        return []
    declared: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = {
                k: copy.deepcopy(v)
                for k, v in tool["function"].items()
                if k in ("name", "description", "parameters", "strict")
            }
            declared.append({"type": "function", "function": fn})
        else:
            fn = {k: copy.deepcopy(tool[k]) for k in ("name", "description", "parameters", "strict") if k in tool}
            if "parameters" not in fn:
                fn["parameters"] = copy.deepcopy(tool.get("input_schema", {"type": "object", "properties": {}}))
            declared.append({"type": "function", "function": fn})
    return declared


def apply_body_overrides(body: dict, overrides: dict | None) -> dict:
    """Apply provider-configured `body_overrides` (supporting `__remove__: [...]` and shallow/nested dict merges)."""
    if not isinstance(overrides, dict) or not overrides:
        return body
    result = copy.deepcopy(body)
    to_remove = overrides.get("__remove__")
    if isinstance(to_remove, list):
        for key in to_remove:
            if isinstance(key, str):
                result.pop(key, None)
    for key, val in overrides.items():
        if key == "__remove__":
            continue
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **copy.deepcopy(val)}
        else:
            result[key] = copy.deepcopy(val)
    return result
