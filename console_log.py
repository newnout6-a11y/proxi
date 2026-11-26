"""Human-readable console output, structured text formatting, and redacted JSONL diagnostics."""
import json
import math
import os
import re
import sys
import threading

import client_context
import console_records
import credits
import journal
from console_clock import timestamp

# Terminal theme colors, by meaning; no fixed background or truecolor dependency. "route" (who answered a Minara reply:
# its channel and provider) is the 256-color orange, apart from the yellow of a cache write.
PALETTE = {"read": "36", "write": "33", "alert": "1;91", "success": "32",
           "request": "34", "model": "92", "balance": "32", "muted": "90", "route": "38;5;208"}
LARGE_WRITE = 50_000
LARGE_INPUT = 50_000
WRITE_JUMP = 8_192
# A single cache write worth this much is called out in the same `[cache]` line as its own marker; the journal event
# (cache.write_expensive) uses the same figure, so the one constant lives in credits.py.
WRITE_WARN = credits.WRITE_WARN
_LOCK = threading.Lock()
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_FIELDS = re.compile(r"\b(in|out|read|write|credits|balance|limit|spent|channel|path|provider)=([^\s]+)")
_TIMESTAMP = re.compile(r"^(\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}\] )")
_STRUCTURED_PREFIX = re.compile(
    r"^((?=[^|]*\bphase=\S+)(?:(?:req|turn|attempt|conv|client|route|model|account|phase|ttft|tokens|cost|status)=\S+\s+)+\|\s*)(.*)$"
)
_ENRICHED_KINDS = frozenset({
    "cache.summary", "cache.tail_unwritten", "account.balance",
    "upstream.body", "upstream.ttft", "route.bound",
})


def structured_enabled():
    return console_records.structured_enabled()


def number(value, *, signed=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and (signed or value >= 0) else None
    except (ValueError, TypeError, OverflowError):
        return None


def amount(value, *, tokens=False, signed=False):
    value = number(value, signed=signed)
    if value is None:
        return "?"
    return format(int(value), ",") if tokens else format(value, ",.4f")


def color_enabled(stream=None):
    stream = sys.stdout if stream is None else stream
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("FORCE_COLOR") not in (None, "", "0"):
        return True
    enabled = os.environ.get("TERM") != "dumb" and bool(getattr(stream, "isatty", lambda: False)())
    if enabled and os.name == "nt":
        # Enable ANSI processing only on this process's attached console handle.
        try:
            import ctypes
            import msvcrt
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
            mode = ctypes.c_uint()
            kernel = ctypes.windll.kernel32
            enabled = bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and
                           kernel.SetConsoleMode(handle, mode.value | 0x0004))
        except (OSError, ValueError, AttributeError):
            enabled = False
    return enabled


def _paint(text, role, enabled):
    return "\x1b[%sm%s\x1b[0m" % (PALETTE[role], text) if enabled else text


def _style_body(line):
    if line.startswith("[err]") or " EXC " in line or "[cache.tail_unwritten]" in line:
        return _paint(line, "alert", True)
    write_alert = "[WRITE SPIKE" in line or "[WRITE HIGH" in line
    input_alert = "[INPUT HIGH]" in line

    def field(match):
        key = match[1]
        role = {"read": "read", "write": "alert" if write_alert else "write",
                "in": "alert" if input_alert else "muted",
                "credits": "alert" if input_alert else "model",
                "balance": "balance", "spent": "model", "channel": "route", "provider": "route"}.get(key, "muted")
        if match[2] == "?":
            role = "muted"
        elif key == "balance" and match[2].startswith("-"):
            role = "alert"
        return _paint(match[0], role, True)

    line = _FIELDS.sub(field, line)
    line = re.sub(r"\[(?:WRITE (?:SPIKE|HIGH|COSTLY)|INPUT HIGH)[^\]]*\]",
                  lambda m: _paint(m[0], "alert", True), line)
    line = re.sub(r"\b(?:MODEL|EFFORT):\s*\S+", lambda m: _paint(m[0], "model", True), line)
    line = re.sub(r"^\[([^\]]+)\]", lambda m: _paint(m[0], "request", True), line)
    match = re.match(r"(relay \S+ \S+ \S+ )(\d+)(.*)", line)
    if match:
        status = int(match[2])
        # An expected refusal (proxy marks it [expected]) is a step the client takes on its own, not a failure.
        role = ("muted" if "[expected]" in match[3] and 400 <= status < 500
                else "alert" if status >= 400 or status == 0 else "success")
        line = match[1] + _paint(match[2], role, True) + match[3]
    return line


def render(line, *, color=None):
    # Scrub before inserting escape sequences, including raw model/effort fields.
    line = _ANSI.sub("", str(journal.scrub(str(line)))).replace("\x1b", "")
    enabled = color_enabled() if color is None else color
    if not enabled:
        return line
    # The log viewer renders already-stamped lines; the prefix must not hide request/error classification.
    stamp = _TIMESTAMP.match(line)
    prefix = stamp[1] if stamp else ""
    line = line[len(prefix):]
    structured = _STRUCTURED_PREFIX.match(line)
    if structured:
        return prefix + _paint(structured.group(1), "muted", True) + _style_body(structured.group(2))
    return prefix + _style_body(line)


def _clean_token(value, limit=80):
    text = _ANSI.sub("", str(value)).replace("\x1b", "")
    text = "".join(ch if ch.isprintable() and not ch.isspace() else "_" for ch in text)
    return text[:limit] or "-"


def _structured_prefix(data, kind):
    parts = []
    for src, key, slice_len in (
        ("request_id", "req", 40),
        ("turn_id", "turn", 8),
        ("attempt", "attempt", 16),
        ("conversation", "conv", 40),
        ("client", "client", 40),
        ("route", "route", 40),
        ("model", "model", 80),
        ("account", "account", 40),
    ):
        val = data.get(src)
        if val is not None and val != "":
            rendered = _clean_token(val, slice_len)
            parts.append(f"{key}={rendered}")
    parts.append(f"phase={_clean_token(data.get('phase') or kind, 64)}")
    if data.get("ttft_ms") is not None:
        parts.append(f"ttft={data['ttft_ms']}ms")
    token_parts = []
    for src, label in (("input_tokens", "in"), ("cache_read", "read"), ("cache_write", "write"), ("output_tokens", "out")):
        if data.get(src) is not None:
            token_parts.append(f"{label}:{data[src]}")
    if token_parts:
        parts.append("tokens=" + ",".join(token_parts))
    cost_val = data.get("credits")
    if cost_val is None:
        cost_val = data.get("cost_usd")
    if cost_val is None:
        cost_val = data.get("minara_credits")
    if cost_val is None:
        cost_val = data.get("spent")
    if cost_val is not None:
        parts.append(f"cost={amount(cost_val)}")
    if data.get("status") is not None:
        parts.append(f"status={data['status']}")
    return " ".join(parts)


def write(line, *, file=None, with_time=True, kind="relay.diagnostic", level="info", text_only=False, **fields):
    """Best effort, as the journal is: a console that cannot be written (the supervisor that read the relay's stdout was
    killed without /T, and the pipe is gone) used to raise OSError out of request handling, so every completion and every
    error ended with no response at all, and the journal lost the event too."""
    eff_kind, eff_level = kind, level
    parsed_fields = {}
    if with_time and (not text_only or console_records.structured_enabled()):
        if eff_kind == "relay.diagnostic" or eff_kind in _ENRICHED_KINDS:
            parsed_kind, parsed_level, parsed_fields = console_records.parse_line(line)
            if eff_kind == "relay.diagnostic" and parsed_kind != "relay.diagnostic":
                eff_kind = parsed_kind
                if eff_level == "info":
                    eff_level = parsed_level
            elif eff_kind == "cache.summary" and eff_level == "info" and parsed_fields.get("alerts"):
                eff_level = "warn"

    if with_time and (console_records.enabled() or console_records.structured_enabled()):
        if console_records.enabled() and text_only:
            return
        identity = client_context.current()
        context = {key: identity[key] for key in console_records.IDENTITY if identity.get(key) is not None}
        client_name = client_context.log_client(identity)
        if client_name:
            context["client"] = client_name
        tag_fn = getattr(journal, "tag", None)
        client_session = tag_fn(identity.get("client_session_id")) if callable(tag_fn) else None
        if client_session:
            context["client_session"] = client_session
        merged = {**context, **parsed_fields}
        for key, value in fields.items():
            if key == "message" or value is not None or key not in merged:
                merged[key] = value
        merged.setdefault("phase", eff_kind)

        if console_records.enabled():
            # Redact before removing escapes or serializing; a literal secret can span physical lines.
            raw = {"message": str(line), **merged}
            try:
                # Keep the schema's fixed field names even when one matches a configured secret.
                data = dict(zip(raw, journal.scrub(tuple(raw.values()))))
                if isinstance(data["message"], str):
                    data["message"] = _ANSI.sub("", data["message"]).replace("\x1b", "")
                console_records.write(eff_kind, eff_level, data, file=file)
            except (OSError, TypeError, ValueError):
                pass
            return

        # Structured text mode: prepend consistent timestamp + scrubbed key=value context before rendered message.
        with _LOCK:
            try:
                stamp = timestamp()
                scrubbed_ctx = dict(zip(merged, journal.scrub(tuple(merged.values()))))
                prefix = _structured_prefix(scrubbed_ctx, eff_kind)
                cleaned = str(journal.scrub(str(line)))
                enabled = color_enabled(file)
                for part in cleaned.splitlines() or [""]:
                    rendered_body = _style_body(_ANSI.sub("", part).replace("\x1b", "")) if enabled else _ANSI.sub("", part).replace("\x1b", "")
                    rendered_prefix = _paint(prefix + " | ", "muted", enabled)
                    print(stamp + rendered_prefix + rendered_body, file=file, flush=True)
            except (OSError, ValueError):
                pass
        return

    with _LOCK:
        try:
            # New messages always get actual event time, even if upstream payload text looks like an old timestamp.
            # Only the explicit replay path (with_time=False) preserves original lines without adding a new date.
            stamp = timestamp() if with_time else ""
            # Mask whole literal secrets before splitting: a configured secret can itself contain a line break.
            cleaned = str(journal.scrub(str(line)))
            for part in cleaned.splitlines() or [""]:
                print(stamp + render(part, color=color_enabled(file)), file=file, flush=True)
        except (OSError, ValueError):
            pass


def emit_ttft(request_id, elapsed_seconds, *, model=None, account=None, attempt=None, file=None):
    """Emit a structured TTFT diagnostic event across text, structured text, and JSONL modes."""
    req_tag = request_id or "req"
    seconds = max(0.0, float(elapsed_seconds))
    write(
        "[%s] [ttft] %.2fs" % (req_tag, seconds),
        file=file,
        kind="upstream.ttft",
        level="debug",
        request_id=request_id,
        ttft_ms=round(seconds * 1000),
        model=model,
        account=account,
        attempt=attempt,
    )


def emit_body(request_id, body_summary, *, model=None, account=None, attempt=None, file=None):
    """Emit a structured upstream body summary across text, structured text, and JSONL modes."""
    req_tag = request_id or "req"
    safe_summary = journal.scrub(body_summary)
    summary_model = safe_summary.get("model") if isinstance(safe_summary, dict) else None
    write(
        "[%s] [body] %s" % (req_tag, json.dumps(safe_summary, ensure_ascii=False)),
        file=file,
        kind="upstream.body",
        level="debug",
        request_id=request_id,
        model=model or summary_model,
        account=account,
        attempt=attempt,
        body=safe_summary,
    )


class CacheMonitor:
    """Compare writes within one session/model scope, never across other chats."""

    def __init__(self):
        self.scope = None
        self.request = None
        self.previous_write = None
        self.last_write = None

    def _inspect(self, request_id, usage, *, scope=None, sample_id=None, credits=None, write_cost=None, channel=None,
                 path=None, provider=None):
        def count(camel, snake):
            return usage.get(camel) if usage.get(camel) is not None else usage.get(snake)

        if scope != self.scope:
            self.scope = scope
            self.request = self.previous_write = self.last_write = None
        sample_id = request_id if sample_id is None else sample_id
        if sample_id != self.request:
            self.previous_write = self.last_write
            self.request = sample_id
        read = number(count("promptTokensReadFromCache", "cache_read"))
        written = number(count("promptTokensWrittenToCache", "cache_write"))
        marker = ""
        alerts = []
        if written is not None:
            previous = self.previous_write
            if previous is not None and written - previous >= WRITE_JUMP and written >= 4 * max(previous, 1):
                tag = "WRITE SPIKE +%s" % amount(written - previous, tokens=True)
                marker = " [%s]" % tag
                alerts.append(tag)
            elif written >= LARGE_WRITE:
                marker = " [WRITE HIGH]"
                alerts.append("WRITE HIGH")
            self.last_write = written
        uncached = number(count("inputTokens", "input_tokens"))
        if uncached is not None and uncached >= LARGE_INPUT:
            marker += " [INPUT HIGH]"
            alerts.append("INPUT HIGH")
        cost_val = number(write_cost)
        if cost_val is not None and cost_val >= WRITE_WARN:
            # One write worth this much can cost more than a whole account's plan (Minara credits, or Retool's own).
            tag = "WRITE COSTLY %s" % amount(cost_val)
            marker += " [%s]" % tag
            alerts.append(tag)
        output = number(count("outputTokens", "output_tokens"))
        output_note = " out=%s" % amount(output, tokens=True) if output is not None else ""
        spent = number(usage.get("creditsUsed")) if credits is None else number(credits)
        clean_via = {}
        for key, value in (("channel", channel), ("path", path), ("provider", provider)):
            if isinstance(value, str) and value:
                clean_via[key] = "".join(ch for ch in value if ch.isprintable() and not ch.isspace())[:40]
        via = "".join(" %s=%s" % (key, clean_via[key]) for key in ("channel", "path", "provider") if key in clean_via)
        text = "[%s] [cache] in=%s read=%s write=%s credits=%s%s%s%s" % (
            request_id, amount(uncached, tokens=True), amount(read, tokens=True),
            amount(written, tokens=True), amount(spent), output_note, via, marker)
        struct_fields = {
            "request_id": request_id,
            "input_tokens": int(uncached) if uncached is not None else None,
            "cache_read": int(read) if read is not None else None,
            "cache_write": int(written) if written is not None else None,
            "output_tokens": int(output) if output is not None else None,
            "credits": spent,
            "write_cost": cost_val,
            "alerts": alerts or None,
            **{k: v for k, v in clean_via.items() if v != "?"},
        }
        return text, {k: v for k, v in struct_fields.items() if v is not None}, ("warn" if alerts else "info")

    def fields(self, request_id, usage, *, scope=None, sample_id=None, credits=None, write_cost=None, channel=None,
               path=None, provider=None):
        """Return (text_line, structured_fields, level) for one upstream cache attempt."""
        return self._inspect(request_id, usage, scope=scope, sample_id=sample_id, credits=credits,
                             write_cost=write_cost, channel=channel, path=path, provider=provider)

    def line(self, request_id, usage, *, scope=None, sample_id=None, credits=None, write_cost=None, channel=None,
             path=None, provider=None):
        """The `[cache]` line of one upstream attempt. `usage` is Retool's metadata (inputTokens, ...) or the relay's own
        counters (input_tokens, cache_read, ...), as a Minara reply has them: the line is the same for both. `channel`,
        `path` and `provider` name who answered a Minara reply (its channel, chat or /v1/messages, and OpenRouter's
        provider behind it): a reply's cache and its thinking depend on them."""
        return self._inspect(request_id, usage, scope=scope, sample_id=sample_id, credits=credits,
                             write_cost=write_cost, channel=channel, path=path, provider=provider)[0]


def tail_unwritten_text(input_tokens, credits=None):
    """What cache.tail_unwritten says: a Minara Claude reply carried marks, read the cache and wrote nothing, and Minara
    billed its prompt at the input price."""
    spent = " (%s кредитов Minara)" % amount(credits) if number(credits) is not None else ""
    return "Minara ничего не записала в кэш: %s токенов промпта оплачены по цене входа%s" % (
        amount(input_tokens, tokens=True), spent)


def tail_unwritten_line(request_id, input_tokens, credits=None):
    """The line under such a reply's `[cache]` line: red and whole, like an error line."""
    return "[%s] [cache.tail_unwritten] %s" % (request_id, tail_unwritten_text(input_tokens, credits))


def route_fields(request_id, *, upstream, account, model, upstream_model, effort=None, child=False):
    """Return (text_line, structured_fields, level) for the bound serving route."""
    def field(value):
        text = str(value) if value is not None else '?'
        return ''.join(char if char.isprintable() else '?' for char in text)[:200]
    provider = 'Minara' if upstream == 'minara' else 'Retool' if upstream == 'retool' else field(upstream)
    eff = field(effort) if effort is not None else 'auto'
    kind_label = 'subagent' if child else 'conversation'
    text = '[%s] [route] provider=%s account=%s model=%s upstream_model=%s effort=%s kind=%s' % (
        field(request_id), provider, field(account), field(model), field(upstream_model), eff, kind_label)
    struct = {
        "request_id": request_id,
        "upstream": upstream,
        "provider": provider,
        "account": account,
        "model": model,
        "upstream_model": upstream_model,
        "effective_effort": effort,
        "child": bool(child),
    }
    return text, {k: v for k, v in struct.items() if v is not None}, "info"


def route_line(request_id, *, upstream, account, model, upstream_model, effort=None, child=False):
    """The serving route after acquisition, never a speculative candidate."""
    return route_fields(request_id, upstream=upstream, account=account, model=model,
                        upstream_model=upstream_model, effort=effort, child=child)[0]


def balance_fields(request_id, snapshot, spent=None, account=None, *, upstream=None):
    """Return (text_line, structured_fields, level) for an account balance snapshot."""
    avail = number(snapshot.get("available"), signed=True)
    limit = number(snapshot.get("monthly_allocation"))
    spent_num = number(spent)
    state = ("UNAVAILABLE" if avail is None else
             "UNREPORTED USAGE" if snapshot.get("unreported_usage") else
             "STALE" if snapshot.get("stale") else "LOCAL ESTIMATE" if snapshot.get("estimated") else "CACHED")
    text = "[%s] [balance]%s balance=%s limit=%s spent=%s [%s]" % (
        request_id, " account=%s" % account if account else "", amount(avail, signed=True),
        amount(limit), amount(spent_num), state)
    unit = snapshot.get("unit") or ("retool_credits" if upstream == "retool" else None)
    struct = {
        "request_id": request_id,
        "account": account,
        "upstream": upstream,
        "available": avail,
        "limit": limit,
        "spent": spent_num,
        "state": state,
        "unit": unit,
        "stale": snapshot.get("stale"),
        "estimated": snapshot.get("estimated"),
        "unreported_usage": snapshot.get("unreported_usage"),
    }
    level = "warn" if state in ("UNAVAILABLE", "UNREPORTED USAGE", "STALE") or (avail is not None and avail < 0) else "info"
    return text, {k: v for k, v in struct.items() if v is not None}, level


def balance_line(request_id, snapshot, spent=None, account=None):
    return balance_fields(request_id, snapshot, spent, account=account)[0]
