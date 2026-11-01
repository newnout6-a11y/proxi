"""Structured, redacted event journal that feeds the admin console.

Every relay action (HTTP request, model turn, upstream call, tool call, local
command, session lifecycle, auth renewal, settings change) is recorded as one
event. Events live in a bounded ring buffer, are optionally appended to a JSONL
file and can be followed live by long-lived admin streams.
"""
import collections
import hashlib
import json
import os
import threading
import time

from redact import redact

LEVELS = ("debug", "info", "success", "warn", "error")
CAPACITY = 5000
MAX_FILE_BYTES = 10 * 1024 * 1024
_TOP_LEVEL = ("turn_id", "request_id", "conversation")


def tag(value):
    """Short, stable, non-reversible label for identifiers that must stay private."""
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:8]


class Journal:
    def __init__(self, capacity=CAPACITY):
        self._cond = threading.Condition()
        self._events = collections.deque(maxlen=capacity)
        self._seq = 0
        self._cfg = None
        self._path = None
        self._file = None
        self._file_lock = threading.Lock()
        self.started = time.time()

    # -- configuration -----------------------------------------------------
    def configure(self, cfg=None, path=None, load=True):
        """Attach secrets for redaction and an optional JSONL file."""
        self._cfg = cfg
        with self._file_lock:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._path = path
        if path and load:
            self._load_tail(path)

    def _load_tail(self, path):
        try:
            with open(path, encoding="utf-8") as source:
                lines = collections.deque(source, maxlen=self._events.maxlen)
        except OSError:
            return
        restored = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("seq"), int):
                event["restored"] = True
                restored.append(event)
        with self._cond:
            for event in restored:
                self._events.append(event)
            if restored:
                self._seq = max(self._seq, max(event["seq"] for event in restored))

    def reset(self):
        with self._cond:
            self._events.clear()
            self._seq = 0
            self._cond.notify_all()
        with self._file_lock:
            if self._file is not None:
                self._file.close()
                self._file = None
            if self._path:
                try:
                    open(self._path, "w", encoding="utf-8").close()
                except OSError:
                    pass

    # -- redaction ---------------------------------------------------------
    def _secrets(self):
        cfg = self._cfg or {}
        values = [cfg.get(key) for key in ("relay_api_key", "cookie", "xsrf", "thread_id", "page_uuid")]
        values += list(cfg.get("cookie_values") or [])
        return [value for value in values if isinstance(value, str) and len(value) >= 4]

    def _clean(self, value, secrets, depth=0):
        if depth > 6:
            return "…"
        if isinstance(value, str):
            text = redact(value, cookie_values=(self._cfg or {}).get("cookie_values", ()))
            for secret in secrets:
                text = text.replace(secret, "[redacted]")
            return text if len(text) <= 4000 else text[:4000] + "…[+%d]" % (len(text) - 4000)
        if isinstance(value, dict):
            return {str(key): self._clean(item, secrets, depth + 1) for key, item in list(value.items())[:64]}
        if isinstance(value, (list, tuple, set)):
            return [self._clean(item, secrets, depth + 1) for item in list(value)[:64]]
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return round(value, 5) if value == value and value not in (float("inf"), float("-inf")) else None
        return self._clean(str(value), secrets, depth + 1)

    # -- recording ---------------------------------------------------------
    def emit(self, kind, message="", level="info", **fields):
        """Record one event. Never raises: logging must not break a request."""
        try:
            if level not in LEVELS:
                level = "info"
            secrets = self._secrets()
            event = {"ts": round(time.time(), 3), "kind": kind, "category": kind.split(".", 1)[0],
                     "level": level, "message": self._clean(message or kind, secrets)}
            for key in _TOP_LEVEL:
                if fields.get(key):
                    event[key] = self._clean(fields.pop(key), secrets)
                else:
                    fields.pop(key, None)
            event["data"] = self._clean(fields, secrets)
            with self._cond:
                self._seq += 1
                event["seq"] = self._seq
                self._events.append(event)
                self._cond.notify_all()
            self._write(event)
            return event
        except Exception:  # noqa: BLE001 - the journal is strictly best effort
            return None

    def _write(self, event):
        if not self._path:
            return
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self._file_lock:
            try:
                if self._file is None:
                    self._file = open(self._path, "a", encoding="utf-8")
                self._file.write(line)
                self._file.flush()
                if self._file.tell() > MAX_FILE_BYTES:
                    self._file.close()
                    self._file = None
                    os.replace(self._path, self._path + ".1")
            except OSError:
                self._file = None

    # -- reading -----------------------------------------------------------
    @property
    def last_seq(self):
        with self._cond:
            return self._seq

    def _snapshot(self):
        with self._cond:
            return list(self._events)

    def query(self, since=0, limit=300, levels=None, categories=None, text=None, turn=None, before=None, order="desc"):
        items = []
        needle = (text or "").lower()
        events = self._snapshot()
        if order == "asc":
            for event in events:
                if event["seq"] <= since:
                    continue
                if before is not None and event["seq"] >= before:
                    break
                if levels and event["level"] not in levels:
                    continue
                if categories and event["category"] not in categories:
                    continue
                if turn and event.get("turn_id") != turn:
                    continue
                if needle and needle not in json.dumps(event, ensure_ascii=False).lower():
                    continue
                items.append(event)
                if len(items) >= limit:
                    break
            return items
        for event in reversed(events):
            if event["seq"] <= since:
                break
            if before is not None and event["seq"] >= before:
                continue
            if levels and event["level"] not in levels:
                continue
            if categories and event["category"] not in categories:
                continue
            if turn and event.get("turn_id") != turn:
                continue
            if needle and needle not in json.dumps(event, ensure_ascii=False).lower():
                continue
            items.append(event)
            if len(items) >= limit:
                break
        items.reverse()
        return items

    def wait(self, since, timeout):
        """Block until an event newer than ``since`` exists or the timeout elapses."""
        with self._cond:
            if self._seq > since:
                return True
            self._cond.wait(timeout)
            return self._seq > since

    def turns(self, limit=60):
        """Group turn-scoped events into compact traces, newest first."""
        traces = collections.OrderedDict()
        snapshot = self._snapshot()
        # http.request events carry the client user-agent; attach it to the turn
        # trace so per-client cache economics (opencode vs claude-cli) are queryable.
        clients = {}
        for event in snapshot:
            if event["kind"] == "http.request":
                rid = str(event.get("request_id") or "")
                client = (event.get("data") or {}).get("client")
                if rid and client:
                    clients[rid] = str(client)[:60]
        for event in snapshot:
            turn_id = event.get("turn_id")
            if not turn_id:
                continue
            trace = traces.get(turn_id)
            if trace is None:
                trace = traces[turn_id] = {"turn_id": turn_id, "started": event["ts"], "ended": None,
                                           "status": "running", "model": None, "effort": None,
                                           "transport": None, "dialect": None, "client": clients.get(str(event.get("request_id"))),
                                           "credits": 0.0, "ttft_ms": None, "duration_ms": None,
                                           "tools": 0, "commands": 0,
                                           "errors": 0, "cache_read": 0, "cache_write": 0,
                                           "events": 0, "request_id": event.get("request_id")}
            data = event.get("data") or {}
            trace["events"] += 1
            kind = event["kind"]
            if kind == "turn.begin":
                for key in ("model", "effort", "transport", "dialect"):
                    trace[key] = data.get(key)
            elif kind == "turn.end":
                trace["ended"] = event["ts"]
                trace["duration_ms"] = data.get("duration_ms")
                trace["status"] = "error" if data.get("reason") == "error" else (data.get("reason") or "stop")
            elif kind == "upstream.first_token":
                trace["ttft_ms"] = data.get("ttft_ms")
            elif kind == "upstream.usage":
                trace["credits"] = round(trace["credits"] + float(data.get("credits") or 0), 5)
                trace["cache_read"] += int(data.get("cache_read") or 0)
                trace["cache_write"] += int(data.get("cache_write") or 0)
            elif kind == "tool.call":
                trace["tools"] += 1
            elif kind == "exec.result":
                trace["commands"] += 1
            if event["level"] == "error":
                trace["errors"] += 1
        return list(reversed(traces.values()))[:limit]

    def metrics(self, window=3600, bucket=60):
        now = time.time()
        start = now - window
        count = int(window // bucket)
        buckets = [{"t": int(start + index * bucket), "requests": 0, "errors": 0, "latency": [],
                    "ttft": [], "credits": 0.0, "cache_read": 0, "cache_write": 0, "input": 0,
                    "turns": 0, "tools": 0, "commands": 0} for index in range(count)]
        totals = collections.Counter()
        statuses = collections.Counter()
        models = collections.Counter()
        categories = collections.Counter()
        levels = collections.Counter()
        latency_all, ttft_all = [], []
        for event in self._snapshot():
            if event["ts"] < start:
                continue
            categories[event["category"]] += 1
            levels[event["level"]] += 1
            slot = buckets[min(count - 1, int((event["ts"] - start) // bucket))]
            data = event.get("data") or {}
            kind = event["kind"]
            if event["level"] == "error":
                slot["errors"] += 1
                totals["errors"] += 1
            if kind == "http.request":
                slot["requests"] += 1
                totals["requests"] += 1
                statuses[str(data.get("status"))] += 1
                if isinstance(data.get("duration_ms"), (int, float)) and data.get("route") in (
                        "chat", "responses", "anthropic"):
                    slot["latency"].append(data["duration_ms"])
                    latency_all.append(data["duration_ms"])
            elif kind == "turn.begin":
                slot["turns"] += 1
                totals["turns"] += 1
                models[str(data.get("model"))] += 1
            elif kind == "upstream.first_token" and isinstance(data.get("ttft_ms"), (int, float)):
                slot["ttft"].append(data["ttft_ms"])
                ttft_all.append(data["ttft_ms"])
            elif kind == "upstream.usage":
                slot["credits"] += float(data.get("credits") or 0)
                slot["cache_read"] += int(data.get("cache_read") or 0)
                slot["cache_write"] += int(data.get("cache_write") or 0)
                slot["input"] += int(data.get("input_tokens") or 0)
                totals["credits"] += float(data.get("credits") or 0)
                totals["cache_read"] += int(data.get("cache_read") or 0)
                totals["cache_write"] += int(data.get("cache_write") or 0)
                totals["input_tokens"] += int(data.get("input_tokens") or 0)
            elif kind == "tool.call":
                slot["tools"] += 1
                totals["tools"] += 1
            elif kind == "exec.result":
                slot["commands"] += 1
                totals["commands"] += 1
        for slot in buckets:
            slot["latency"] = round(sum(slot["latency"]) / len(slot["latency"])) if slot["latency"] else None
            slot["ttft"] = round(sum(slot["ttft"]) / len(slot["ttft"])) if slot["ttft"] else None
            slot["credits"] = round(slot["credits"], 4)

        def percentile(values, share):
            if not values:
                return None
            ordered = sorted(values)
            return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))])

        cached = totals["cache_read"]
        prompt = cached + totals["input_tokens"] + totals["cache_write"]
        return {
            "window_seconds": window, "bucket_seconds": bucket, "buckets": buckets,
            "totals": {"requests": totals["requests"], "errors": totals["errors"], "turns": totals["turns"],
                       "tools": totals["tools"], "commands": totals["commands"],
                       "credits": round(totals["credits"], 4), "cache_read": cached,
                       "cache_write": totals["cache_write"], "input_tokens": totals["input_tokens"],
                       "cache_hit_ratio": round(cached / prompt, 4) if prompt else None,
                       "latency_p50": percentile(latency_all, 0.5), "latency_p95": percentile(latency_all, 0.95),
                       "ttft_p50": percentile(ttft_all, 0.5), "ttft_p95": percentile(ttft_all, 0.95)},
            "statuses": dict(statuses), "models": dict(models.most_common(12)),
            "categories": dict(categories), "levels": dict(levels),
            "last_seq": self.last_seq, "buffered": len(self._events), "since": self.started,
        }


JOURNAL = Journal()


def emit(kind, message="", level="info", **fields):
    return JOURNAL.emit(kind, message, level, **fields)
