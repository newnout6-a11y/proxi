"""Response-shape summaries and explicitly enabled private wire traces."""
from collections import Counter
import hashlib
import json
import math
import pathlib
import threading
import time
import uuid

import journal
from redact import redact
from upstream_attempts import MAX_ATTEMPTS

_KNOWN = frozenset({"content_block_start", "content_block_delta", "content_block_end",
                    "metadata", "end_of_stream", "error", "ping", "message_start", "message_delta", "message_stop"})
_HEADERS = ("request-id", "x-request-id", "cf-ray", "content-type", "x-retool-api-version")
_SIGNALS = ("stop_reason", "stopReason", "finish_reason", "finishReason", "statusCode", "code")


class WireTrace:
    """Opt-in private bodies plus a timed timeline; never capture auth headers."""
    def __init__(self, cfg, trace_id, payload, *, root_key="upstream_trace_dir",
                 response_file="response-body.ndjson"):
        self.folder = None
        self.failed = False
        self.started = time.monotonic()
        # The reader runs in an executor while parser events and cancellation
        # run on the event loop. Serialize close with writes on both threads.
        self._lock = threading.RLock()
        self._seq = 0
        self._offset = 0
        self._hash = hashlib.sha256()
        self._raw = self._timeline = None
        root = cfg.get(root_key)
        self.enabled = bool(root)
        if not self.enabled:
            return
        try:
            self.folder = pathlib.Path(root) / trace_id
            self.folder.mkdir(parents=True, exist_ok=False)
            (self.folder / "request-body.json").write_bytes(payload)
            self._raw = (self.folder / response_file).open("wb")
            self._timeline = (self.folder / "timeline.jsonl").open("w", encoding="utf-8")
            self.event("request", body_bytes=len(payload), body_sha256=hashlib.sha256(payload).hexdigest())
        except (OSError, UnicodeError, TypeError, ValueError):
            self.failed = True
            self.close()

    @staticmethod
    def safe_headers(headers):
        # Whitelist excludes cookies, auth, CSRF, request URLs and redirect URLs.
        permitted = (*_HEADERS, "content-encoding", "content-length", "transfer-encoding",
                     "date", "server", "cf-cache-status", "x-retool-client-version",
                     "accept", "user-agent")
        return {name: str(headers.get(name)) for name in permitted if headers.get(name)}

    def event(self, stage, **fields):
        with self._lock:
            if self._timeline is None or self._timeline.closed or self.failed:
                return
            try:
                self._seq += 1
                row = {"seq": self._seq, "elapsed_ms": round((time.monotonic()-self.started)*1000, 3),
                       "stage": stage, **fields}
                self._timeline.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                self._timeline.flush()
            except (OSError, UnicodeError, TypeError, ValueError, RecursionError):
                self.failed = True

    def request(self, headers):
        self.event("request_headers", headers=self.safe_headers(headers))

    def response(self, response, status):
        self.event("response_headers", http_status=status,
                   headers=self.safe_headers(getattr(response, "headers", None) or {}),
                   response_version=getattr(response, "version", None),
                   chunked=getattr(response, "chunked", None))

    def raw(self, data):
        with self._lock:
            if self._raw is None or self._raw.closed or self.failed:
                return
            try:
                offset = self._offset
                self._raw.write(data)
                self._raw.flush()
                self._hash.update(data)
                self._offset += len(data)
                self.event("response_bytes", offset=offset, length=len(data))
            except (OSError, ValueError):
                self.failed = True

    def close(self):
        with self._lock:
            if self._timeline is not None and not self._timeline.closed:
                self.event("wire_end", response_bytes=self._offset, response_sha256=self._hash.hexdigest(),
                           capture_failed=self.failed)
            for handle in (self._raw, self._timeline):
                try:
                    if handle is not None:
                        handle.close()
                except OSError:
                    self.failed = True


class AttemptTrace:
    def __init__(self, cfg, budget, request_id, model, payload):
        self.cfg, self.budget = cfg, budget
        self.request_id, self.model = request_id, model
        tag = "".join(c for c in str(request_id or "unknown") if c.isalnum())[:64]
        self.id = "%s-%s-attempt%d-%s" % (time.strftime("%Y%m%d-%H%M%S"), tag, budget.used, uuid.uuid4().hex[:12])
        self.attempt = budget.used
        self.started = time.monotonic()
        self.http_status = None
        self.headers = {}
        self.lines = self.bytes = self.malformed = self.scalars = 0
        self.shapes, self.unknown = Counter(), Counter()
        self.unsupported = Counter()
        self.keys = []
        self.text_chars = self.tool_chars = self.thinking_chars = 0
        self.signals = []
        self.usage = {}
        self.ended = False
        self.saved = False
        self.payload_hash = hashlib.sha256(payload).hexdigest()
        self.wire = WireTrace(cfg, self.id, payload)

    def response(self, response, status=None):
        self.http_status = status or getattr(response, "status", None)
        headers = getattr(response, "headers", None) or {}
        self.headers = {key: str(headers.get(key))[:160] for key in _HEADERS if headers.get(key)}
        self.wire.response(response, self.http_status)

    @staticmethod
    def count(counter, key):
        key = str(key or "<missing>")[:80]
        counter[key if key in counter or len(counter) < 24 else "<other>"] += 1

    def observe(self, raw):
        self.lines += 1
        self.bytes += len(raw)
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            self.malformed += 1
            return
        if not isinstance(event, dict):
            self.scalars += 1
            return
        kind = event.get("event") or event.get("type")
        kind = kind if isinstance(kind, str) else "<invalid>"
        block_type = event.get("type")
        block_type = block_type if isinstance(block_type, str) else "<missing>"
        self.count(self.shapes, "%s/%s" % (kind[:80], block_type[:80]))
        keys = sorted(str(k)[:32] for k in event)[:12]
        if keys not in self.keys and len(self.keys) < 12:
            self.keys.append(keys)
        if kind not in _KNOWN:
            self.count(self.unknown, kind)
        if not event.get("event") and kind != "end_of_stream":
            self.count(self.unsupported, kind)
        self.ended |= kind == "end_of_stream"
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        block = event.get("type") if event.get("event") else delta.get("type")
        if kind == "content_block_delta":
            self.text_chars += len(str(event.get("partialText") or delta.get("text") or ""))
            self.tool_chars += len(str(event.get("partialInputs") or delta.get("partialInputs") or delta.get("partial_json") or ""))
            if block in ("thinking", "thinking_delta"):
                self.thinking_chars += len(str(event.get("partialThinking") or delta.get("thinking") or ""))
        usage = event.get("usage")
        if isinstance(usage, dict):
            # Only numeric counters; arbitrary metadata never becomes diagnostic text.
            self.usage.update({k: v for k, v in usage.items() if k in (
                "inputTokens", "outputTokens", "totalTokens", "creditsUsed",
                "promptTokensReadFromCache", "promptTokensWrittenToCache")
                and isinstance(v, (int, float)) and not isinstance(v, bool)
                and abs(v) <= 10**15 and math.isfinite(v)})
        error = event.get("error") if isinstance(event.get("error"), dict) else {}
        for container in (event, delta, error):
            for key in _SIGNALS:
                if key in container and len(self.signals) < 12:
                    value = container[key]
                    signal = {"field": key, "value": str(value)[:120] if isinstance(value, (str, int, float, bool)) else type(value).__name__}
                    if signal not in self.signals:
                        self.signals.append(signal)
        if "refusal" in str(kind).lower() or "refusal" in str(block).lower() or "refusal" in event:
            if len(self.signals) < 12:
                self.signals.append({"field": "refusal", "present": True})
        if kind == "error" and len(self.signals) < 12:
            message = event.get("error") if isinstance(event.get("error"), str) else error.get("message")
            signal = {"field": "error", "type": str(error.get("type") or "")[:80]}
            if isinstance(message, str):
                signal.update(message_chars=len(message), message_sha256=hashlib.sha256(message.encode("utf-8", "replace")).hexdigest())
            self.signals.append(signal)

    def finish(self, outcome):
        if self.saved:
            return self.budget.last
        self.saved = True
        summary = {"diagnostic_id": self.id, "request_id": self.request_id,
                   "attempt": self.attempt, "max_attempts": MAX_ATTEMPTS, "model": self.model,
                   "outcome": outcome, "http_status": self.http_status,
                   "request_diagnostic_id": self.budget.empty_dump_id,
                   "upstream_request_id": self.headers.get("request-id") or self.headers.get("x-request-id"),
                   "response_headers": self.headers, "request_body_sha256": self.payload_hash,
                   "duration_ms": round((time.monotonic() - self.started) * 1000),
                   "lines": self.lines, "bytes": self.bytes, "malformed_lines": self.malformed,
                   "scalar_lines": self.scalars, "event_shapes": dict(self.shapes),
                   "observed_field_sets": self.keys,
                   "unknown_events": dict(self.unknown), "text_delta_chars": self.text_chars,
                   "unsupported_event_shapes": dict(self.unsupported),
                   "tool_delta_chars": self.tool_chars, "thinking_delta_chars": self.thinking_chars,
                   "end_of_stream": self.ended, "signals": self.signals, "usage": self.usage}
        if self.wire.enabled:
            self.wire.event("attempt_end", outcome=outcome, summary=summary)
            self.wire.close()
            summary.update(wire_trace_id=self.id,
                           wire_trace_saved=not self.wire.failed and self.wire.folder is not None)
        secrets = list(self.cfg.get("cookie_values") or [])
        secrets += [str(self.cfg[k]) for k in ("cookie", "xsrf", "relay_api_key") if self.cfg.get(k)]
        secrets += [segment.strip().split("=", 1)[1] for segment in self.cfg.get("cookie", "").split(";") if "=" in segment]
        def scrub(value):
            if isinstance(value, str):
                for secret in secrets:
                    if len(secret) >= 4:
                        value = value.replace(secret, "[redacted]")
                return redact(value, cookie_values=secrets)
            if isinstance(value, dict):
                return {scrub(k): scrub(v) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            return value
        summary = scrub(summary)
        wire = json.dumps(summary, ensure_ascii=False, allow_nan=False)
        try:
            folder = pathlib.Path(self.cfg.get("upstream_error_dir") or "logs/upstream-400")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (self.id + "-response.json")).write_text(wire + "\n", encoding="utf-8")
            summary["diagnostic_saved"] = True
        except (OSError, UnicodeError):
            summary["diagnostic_saved"] = False
        if self.attempt >= self.budget.last.get("attempt", 0):
            self.budget.last = summary
        journal.emit("upstream.attempt", "Попытка %d/5: %s" % (self.attempt, outcome),
                     "debug" if outcome == "content" else "warn", **summary)
        return summary
