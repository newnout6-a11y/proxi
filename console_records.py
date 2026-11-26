"""JSONL framing, structured line parsing, and mode selection shared by console output and the journal."""
import datetime
import json
import os
import re
import sys
import threading

import client_context


IDENTITY = (
    "request_id", "turn_id", "attempt", "conversation", "client", "chat_title", "agent",
    "client_task", "request_purpose", "route", "upstream", "account", "model",
    "upstream_model", "effective_effort", "phase",
)
_SAFE_CONTEXT_KEYS = (
    "request_id", "turn_id", "attempt", "conversation", "client", "route",
    "upstream", "account", "model", "upstream_model", "effective_effort",
)
_LOCK = threading.Lock()
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_STAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}\] ")
_CACHE_ALERTS_RE = re.compile(r"\[(WRITE SPIKE[^\]]*|WRITE HIGH|INPUT HIGH|WRITE COSTLY[^\]]*)\]")
_KV_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)=([^\s]+)")
_HTTP_LINE_RE = re.compile(r"^relay\s+(\S+)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\s+(\S+)\s+(\d+)\b(.*)$")
_VALIDATED_RE = re.compile(r"^\[([^\]]+)\]\s+.*\|\s*MODEL:\s*([^\s|]+)\s*\|\s*EFFORT:\s*([^\s|]+)\s*\|\s*PURPOSE:\s*([^\s|]+)")


def mode():
    """Return active console log mode: 'jsonl', 'structured', or 'text'."""
    value = os.environ.get("RELAY_LOG_FORMAT", "").strip().lower()
    if value == "jsonl":
        return "jsonl"
    if value in ("structured", "structured-text", "structured_text"):
        return "structured"
    return "text"


def enabled():
    return mode() == "jsonl"


def structured_enabled():
    return mode() == "structured"


def _parse_number(raw, *, integer=False):
    if not raw or raw == "?":
        return None
    cleaned = raw.replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if integer and value.is_integer():
        return int(value)
    return value


def _parse_cache_rest(req_id, rest):
    fields = {"request_id": req_id}
    alerts = _CACHE_ALERTS_RE.findall(rest)
    if alerts:
        fields["alerts"] = alerts
    if "[keepwarm]" in rest:
        fields["keepwarm"] = True
    key_map = {
        "in": ("input_tokens", True),
        "read": ("cache_read", True),
        "write": ("cache_write", True),
        "out": ("output_tokens", True),
        "credits": ("credits", False),
    }
    for key, val in _KV_RE.findall(rest):
        if key in key_map:
            target, is_int = key_map[key]
            parsed = _parse_number(val, integer=is_int)
            if parsed is not None:
                fields[target] = parsed
        elif key in ("channel", "path", "provider") and val != "?":
            fields[key] = val
    level = "warn" if alerts else "info"
    return "cache.summary", level, fields


def _parse_balance_rest(req_id, rest):
    fields = {"request_id": req_id}
    if "[keepwarm]" in rest:
        fields["keepwarm"] = True
    state_match = re.search(r"\[(UNAVAILABLE|UNREPORTED USAGE|STALE|LOCAL ESTIMATE|CACHED)\]", rest)
    if state_match:
        fields["state"] = state_match.group(1)
    for key, val in _KV_RE.findall(rest):
        if key == "account" and val != "?":
            fields["account"] = val
        elif key == "balance":
            parsed = _parse_number(val)
            if parsed is not None:
                fields["available"] = parsed
        elif key in ("limit", "spent"):
            parsed = _parse_number(val)
            if parsed is not None:
                fields[key] = parsed
    state = fields.get("state")
    avail = fields.get("available")
    level = "warn" if state in ("UNAVAILABLE", "UNREPORTED USAGE", "STALE") or (
        isinstance(avail, (int, float)) and avail < 0
    ) else "info"
    return "account.balance", level, fields


def _parse_route_rest(req_id, rest):
    fields = {"request_id": req_id}
    for key, val in _KV_RE.findall(rest):
        if val == "?":
            continue
        if key == "kind":
            fields["child"] = val == "subagent"
        elif key in ("provider", "account", "model", "upstream_model", "effort"):
            fields[key] = val
    return "route.bound", "info", fields


def _parse_body_rest(req_id, rest):
    fields = {}
    if req_id:
        fields["request_id"] = req_id
    try:
        parsed = json.loads(rest.strip())
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        fields["body"] = parsed
        if isinstance(parsed.get("model"), str) and parsed["model"]:
            fields["model"] = parsed["model"]
    return "upstream.body", "debug", fields


def _parse_ttft_rest(req_id, rest):
    fields = {}
    if req_id:
        fields["request_id"] = req_id
    match = re.search(r"(\d+(?:\.\d+)?)\s*s\b", rest)
    if match:
        fields["ttft_ms"] = round(float(match.group(1)) * 1000)
    return "upstream.ttft", "debug", fields


def parse_line(line):
    """Classify and extract structured fields from diagnostic/console lines."""
    text = _ANSI_RE.sub("", str(line)).replace("\x1b", "").strip()
    stamp = _STAMP_RE.match(text)
    if stamp:
        text = text[len(stamp.group(0)):].strip()
    if not text:
        return "relay.diagnostic", "info", {}

    match = re.match(r"^\[([^\]]+)\]\s+\[([a-zA-Z0-9_.-]+)\](.*)$", text)
    if match:
        req_id, tag, rest = match.group(1), match.group(2), match.group(3).strip()
        if tag == "cache":
            return _parse_cache_rest(req_id, rest)
        if tag == "cache.tail_unwritten":
            fields = {"request_id": req_id}
            tok_m = re.search(r":\s*([\d,]+)\s+токенов", rest)
            if tok_m:
                fields["input_tokens"] = _parse_number(tok_m.group(1), integer=True)
            cred_m = re.search(r"\(([\d.,]+)\s+кредитов Minara\)", rest)
            if cred_m:
                fields["minara_credits"] = _parse_number(cred_m.group(1))
            return "cache.tail_unwritten", "warn", fields
        if tag == "balance":
            return _parse_balance_rest(req_id, rest)
        if tag == "route":
            return _parse_route_rest(req_id, rest)
        if tag == "body":
            return _parse_body_rest(req_id, rest)
        if tag == "ttft":
            return _parse_ttft_rest(req_id, rest)
        if tag == "history":
            fields = {"request_id": req_id, "mode": "forked" if "forked" in rest else "restored"}
            for key, val in _KV_RE.findall(rest):
                if key in ("thinking", "tool_calls"):
                    parsed = _parse_number(val, integer=True)
                    if parsed is not None:
                        fields[key] = parsed
            return "native_history.continuation", "info", fields
        if tag == "account":
            return "account.event", "warn", {"request_id": req_id}
        if tag == "cost":
            return "cost.guard", "warn" if rest.startswith("blocked") else "info", {"request_id": req_id}
        if tag == "thinking":
            fields = {"request_id": req_id}
            for key, val in _KV_RE.findall(rest):
                if key in ("source", "event", "block"):
                    fields[key] = val
            return "upstream.thinking_signal", "info", fields

    if text.startswith("[body] "):
        return _parse_body_rest(None, text[7:])
    if text.startswith("[ttft] "):
        return _parse_ttft_rest(None, text[7:])
    if text.startswith("[raw] "):
        return "upstream.raw_diagnostic", "debug", {}
    if text.startswith("[assist] "):
        return "upstream.assist_retry", "warn", {}
    if text.startswith("[debug] "):
        return "relay.debug", "debug", {}
    if text.startswith("[catalog] "):
        return "catalog.sync", "info", {}
    if text.startswith("[state] "):
        return "session.state", "info", {}
    if text.startswith("[effort] "):
        return "request.effort", "debug", {}
    if text.startswith("[prompt] "):
        return "prompt.diagnostic", "warn", {}
    if text.startswith("[startup]"):
        return "relay.startup", "warn" if "failed" in text else "info", {}
    if text.startswith("[explain] "):
        return "upstream.explain_failed", "warn", {}
    if text.startswith("[err] "):
        return "relay.error", "error", {}
    if text.startswith("warning: "):
        return "relay.warning", "warn", {}

    hb_match = re.match(r"^\[([^\]]+)\]\s+Waiting for upstream reasoning output\.\.\.\s*\((\d+)s elapsed\)", text)
    if hb_match:
        return "upstream.thinking_heartbeat", "debug", {
            "request_id": hb_match.group(1),
            "elapsed_ms": int(hb_match.group(2)) * 1000,
        }

    exc_match = re.match(r"^relay\s+(\S+)\s+EXC\s+([^\s:]+):", text)
    if exc_match:
        return "http.exception", "error", {"request_id": exc_match.group(1), "exception_type": exc_match.group(2)}

    http_match = _HTTP_LINE_RE.match(text)
    if http_match:
        status = int(http_match.group(4))
        tail = http_match.group(5)
        expected = "[expected]" in tail and 400 <= status < 500
        level = "info" if expected else "error" if status >= 500 or status == 0 else "warn" if status >= 400 else "success"
        return "http.request", level, {
            "request_id": http_match.group(1),
            "method": http_match.group(2),
            "path": http_match.group(3),
            "status": status,
        }

    val_match = _VALIDATED_RE.match(text)
    if val_match:
        return "request.validated", "info", {
            "request_id": val_match.group(1),
            "model": val_match.group(2),
            "requested_effort": val_match.group(3).lower(),
            "request_purpose": val_match.group(4),
        }

    return "relay.diagnostic", "info", {}


def write(kind, level, fields, *, when=None, file=None):
    """Write one already redacted event. Controls and newlines stay escaped inside the record."""
    try:
        now = (datetime.datetime.now().astimezone() if when is None
               else datetime.datetime.fromtimestamp(when, datetime.timezone.utc).astimezone())
        record = {
            "ts": now.isoformat(timespec="milliseconds"),
            "kind": kind,
            "level": level,
            "phase": fields.get("phase") or kind,
        }
        record.update((key, value) for key, value in fields.items() if value is not None and key not in record)
        if kind != "journal.rotation_failed":
            identity = client_context.current()
            if identity:
                for key in _SAFE_CONTEXT_KEYS:
                    if key not in record:
                        value = client_context.log_client(identity) if key == "client" else identity.get(key)
                        if value is not None:
                            record[key] = value
        line = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        stream = sys.stdout if file is None else file
        with _LOCK:
            stream.write(line + "\n")
            stream.flush()
    except (OSError, TypeError, ValueError, OverflowError):
        pass  # a disconnected supervisor or unusable diagnostic must not stop requests
