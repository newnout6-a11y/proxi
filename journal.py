"""Structured, redacted event journal that feeds the admin console.

Every relay action (HTTP request, model turn, upstream call, tool call, local
command, session lifecycle, auth renewal, settings change) is recorded as one
event. Events live in a bounded ring buffer, are optionally appended to a JSONL
file and can be followed live by long-lived admin streams.

Metrics never read that buffer. Every event is also folded into fixed-size time
slots (``_Rollup``) that span the longest metrics window, so the turnover of raw
events cannot shrink or silently under-count the 1 h / 6 h / 24 h figures.
``metrics()["coverage"]`` states how much of a window the slots really cover.
"""
import array
import collections
import hashlib
import json
import math
import operator
import os
import threading
import time

import client_context
from redact import redact

LEVELS = ("debug", "info", "success", "warn", "error")
# A raw event is ~465 bytes of JSONL and ~0.8 KiB of memory. 20000 of them are about
# one rotated file generation (10 MiB), ~16 MiB of RAM and ~40 minutes at 500 events/min.
CAPACITY = 20000
MAX_FILE_BYTES = 10 * 1024 * 1024
_TOP_LEVEL = ("turn_id", "request_id", "conversation")
# Rollups: numeric counters per FINE seconds (the smallest chart bucket), distributions per
# COARSE seconds (window-wide figures only), both kept for the longest window plus a margin.
FINE = 10
COARSE = 60
RETENTION = 24 * 3600 + 600
MAX_SAMPLES = 1000  # latency/ttft samples kept per COARSE slot
SAMPLE_LIMIT = 20000  # samples a percentile is computed from; a longer window is thinned evenly
MAX_LIVE_TURNS = 4096
LIVE_TURN_TTL = 6 * 3600
_LABELS = 64  # distinct labels (models, statuses, ...) kept per slot; the rest fold into "<other>"
_LABEL_CHARS = 80
_FINE_MAX = RETENTION // FINE + 8
_COARSE_MAX = RETENTION // COARSE + 8
_ROUTES = ("chat", "responses", "anthropic")
# metrics() copies the slots it sums this many at a time under the journal lock, so emit() waits for one
# portion (well under a millisecond even for the heaviest slots), never for the whole window.
_PORTION = 64


def tag(value):
    """Short, stable, non-reversible label for identifiers that must stay private."""
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:8]


def _number(value):
    """A finite int or float (never a bool), else None."""
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _price(value):
    """A reported price is a finite number >= 0; anything else is 'unknown'."""
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _whole(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _attempt_number(data):
    value = data.get("attempt")
    return value if type(value) is int else None


class _Attempt:
    """One paid upstream attempt: whether it produced usage telemetry and whether a price came with it."""
    __slots__ = ("where", "observed", "known", "price")

    def __init__(self, where):
        self.where = where
        self.observed = False  # any upstream.usage, including the empty marker direct.py emits
        self.known = False  # a valid price was reported
        self.price = 0.0

    @property
    def unknown(self):
        return self.observed and not self.known


class _Turn:
    """Price bookkeeping of one relay turn, shared by the metrics rollup and the turn traces.

    An attempt costs nothing until it is observed: one that only started (the upstream refused it
    or the request never left) is free. An observed attempt without a valid price is unknown and
    stays so; the same rule adapters.py applies when it marks a prior attempt as unpriced. An
    attempt a client cancelled before any telemetry is treated as observed (see ``end``).
    """
    __slots__ = ("attempts", "current", "ended", "restored", "touched")

    def __init__(self, restored=False, touched=0.0):
        self.attempts = {}
        self.current = None
        self.ended = False
        self.restored = restored
        self.touched = touched

    def start(self, number, where):
        if number is None:
            number = max(self.attempts, default=-1) + 1
        self.current = number
        return self.attempts.setdefault(number, _Attempt(where))

    def usage(self, number, price, where):
        """Apply one usage event; returns the attempt and the change in its 'unknown' state (-1, 0, +1)."""
        if number is None:
            number = self.current if self.current is not None else 0
        attempt = self.attempts.get(number)
        if attempt is None:
            attempt = self.attempts[number] = _Attempt(where)
        before = attempt.unknown
        attempt.observed = True
        if price is not None:
            attempt.known = True
            attempt.price += price
        return attempt, int(attempt.unknown) - int(before)

    @property
    def pending(self):
        """The latest attempt has started but reported nothing yet: its price may still arrive."""
        attempt = self.attempts.get(self.current)
        return attempt is not None and not attempt.observed

    def end(self, reason):
        """The turn is over; returns an attempt that has just become unknown, if any.

        A client that cancels mid-generation does not wait for the telemetry, and direct.py cannot
        emit its empty usage marker through a cancelled generator, yet it flags the account balance
        as unreported (mark_usage_unknown): the silent attempt may well have been billed.
        """
        self.ended = True
        attempt = self.attempts.get(self.current)
        if reason == "cancelled" and attempt is not None and not attempt.observed:
            attempt.observed = True
            return attempt
        return None

    def summary(self):
        """(sum of valid prices, priced attempts, unknown attempts)."""
        found = self.attempts.values()
        return (sum(item.price for item in found), sum(item.known for item in found),
                sum(item.unknown for item in found))


class _Fine:
    """Numeric counters of one FINE-second interval: everything a chart bucket shows."""
    __slots__ = ("requests", "errors", "latency_sum", "latency_n", "ttft_sum", "ttft_n", "credits", "keepwarm",
                 "keepwarm_pings", "keepwarm_unknown", "cache_read", "cache_write", "input", "output",
                 "turns", "tools", "commands", "unknown")

    def __init__(self):
        self.requests = self.errors = self.latency_n = self.ttft_n = 0
        self.keepwarm_pings = self.keepwarm_unknown = self.cache_read = self.cache_write = 0
        self.input = self.output = self.turns = self.tools = self.commands = self.unknown = 0
        self.latency_sum = self.ttft_sum = self.credits = self.keepwarm = 0.0


# A frozen copy of a _Fine slot (same attribute names) that metrics() can read outside the journal lock.
_FineView = collections.namedtuple("_FineView", _Fine.__slots__)
_fine_values = operator.attrgetter(*_Fine.__slots__)


class _Coarse:
    """Distributions of one COARSE-second interval; only whole-window figures use them."""
    __slots__ = ("statuses", "models", "categories", "levels", "latency", "ttft")

    def __init__(self):
        self.statuses, self.models, self.categories, self.levels = {}, {}, {}, {}
        self.latency, self.ttft = array.array("d"), array.array("d")

    def copy(self):
        clone = _Coarse.__new__(_Coarse)
        clone.statuses, clone.models = dict(self.statuses), dict(self.models)
        clone.categories, clone.levels = dict(self.categories), dict(self.levels)
        clone.latency, clone.ttft = self.latency[:], self.ttft[:]
        return clone


def _count(counter, key):
    """Count a label; user-controlled ones (model names) must not grow a slot without bound."""
    key = key[:_LABEL_CHARS]
    if key not in counter and len(counter) >= _LABELS:
        key = "<other>"
    counter[key] = counter.get(key, 0) + 1


def _thinned(slots, name, limit=None):
    """The samples of all slots, evenly thinned to about ``limit`` so a 24 h percentile stays cheap."""
    step = max(1, -(-sum(len(getattr(slot, name)) for slot in slots) // (limit or SAMPLE_LIMIT)))
    merged = []
    for slot in slots:
        merged.extend(getattr(slot, name)[::step])
    return merged


def _trim(slots, limit, cutoff):
    """Drop slots older than ``cutoff``; if the bound still holds too many, drop the oldest."""
    for index in [index for index in slots if index < cutoff]:
        del slots[index]
    for index in sorted(slots)[:max(0, len(slots) - limit + 1)]:
        del slots[index]


def _aggregate(fine, coarse, start, bucket, count, in_progress):
    """The rows, totals and label counts of copied slots.

    ``fine`` is (index, _FineView) pairs and ``coarse`` is _Coarse copies, both already limited to the window.
    It takes no lock and reads no live state, so it may take as long as it likes without holding emit() back.
    """
    rows = [{"t": int(start + index * bucket), "requests": 0, "errors": 0, "latency": None, "ttft": None,
             "credits": 0.0, "keepwarm_credits": 0.0, "cache_read": 0, "cache_write": 0, "input": 0,
             "output": 0, "turns": 0, "tools": 0, "commands": 0, "credits_unknown": 0}
            for index in range(count)]
    latency = [[0.0, 0] for _ in range(count)]
    ttft = [[0.0, 0] for _ in range(count)]
    pings = pings_unknown = 0
    statuses, models, categories, levels = {}, {}, {}, {}
    for index, slot in fine:
        at = min(count - 1, int((index * FINE - start) // bucket))
        row = rows[at]
        row["requests"] += slot.requests
        row["errors"] += slot.errors
        row["cache_read"] += slot.cache_read
        row["cache_write"] += slot.cache_write
        row["input"] += slot.input
        row["output"] += slot.output
        row["turns"] += slot.turns
        row["tools"] += slot.tools
        row["commands"] += slot.commands
        row["credits"] += slot.credits
        row["keepwarm_credits"] += slot.keepwarm
        row["credits_unknown"] += slot.unknown
        pings += slot.keepwarm_pings
        pings_unknown += slot.keepwarm_unknown
        pair = latency[at]
        pair[0] += slot.latency_sum
        pair[1] += slot.latency_n
        pair = ttft[at]
        pair[0] += slot.ttft_sum
        pair[1] += slot.ttft_n
    for slot in coarse:
        for source, target in ((slot.statuses, statuses), (slot.models, models),
                               (slot.categories, categories), (slot.levels, levels)):
            for key, number in source.items():
                target[key] = target.get(key, 0) + number
    latency_all, ttft_all = _thinned(coarse, "latency"), _thinned(coarse, "ttft")
    # Totals come from the unrounded rows: summing per-bucket roundings drifts by up to 5e-5 per bucket.
    credits_sum = sum(row["credits"] for row in rows)
    keepwarm_sum = sum(row["keepwarm_credits"] for row in rows)
    for row, (latency_sum, latency_n), (ttft_sum, ttft_n) in zip(rows, latency, ttft):
        row["latency"] = round(latency_sum / latency_n) if latency_n else None
        row["ttft"] = round(ttft_sum / ttft_n) if ttft_n else None
        row["credits"] = round(row["credits"], 4)
        row["keepwarm_credits"] = round(row["keepwarm_credits"], 4)
        row["credits_unknown"] = max(0, row["credits_unknown"])
        row["credits_complete"] = row["credits_unknown"] == 0

    def total(field):
        return sum(row[field] for row in rows)

    def percentile(ordered, share):
        return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))]) if ordered else None

    latency_all.sort()
    ttft_all.sort()
    cached = total("cache_read")
    prompt = cached + total("input") + total("cache_write")
    unknown = total("credits_unknown")
    totals = {"requests": total("requests"), "errors": total("errors"), "turns": total("turns"),
              "tools": total("tools"), "commands": total("commands"),
              "credits": round(credits_sum, 4), "cache_read": cached,
              "keepwarm_credits": round(keepwarm_sum, 4),
              "keepwarm_pings": pings, "keepwarm_unknown": pings_unknown,
              "credits_complete": unknown == 0, "credits_unknown": unknown,
              "credits_in_progress": in_progress,
              "cache_write": total("cache_write"), "input_tokens": total("input"),
              "output_tokens": total("output"),
              "cache_hit_ratio": round(cached / prompt, 4) if prompt else None,
              "latency_p50": percentile(latency_all, 0.5), "latency_p95": percentile(latency_all, 0.95),
              "ttft_p50": percentile(ttft_all, 0.5), "ttft_p95": percentile(ttft_all, 0.95)}
    return rows, totals, statuses, models, categories, levels


class _Rollup:
    """Bounded aggregates of the event stream that do not depend on the raw ring buffer.

    ``covered_from`` is the instant from which every event reached the slots: the process start,
    pulled back by events restored from the JSONL tail and pushed forward by a clear or a
    failed ingest. Windows that begin earlier are reported as incompletely covered.
    """

    def __init__(self, covered_from):
        self.fine = {}
        self.coarse = {}
        self.turns = collections.OrderedDict()  # turns that have not ended yet
        self.covered_from = covered_from
        self.oldest = None

    def gap(self, ts):
        """An event was lost to the aggregates: coverage restarts after it."""
        ts = _number(ts)
        if ts is not None:
            self.covered_from = max(self.covered_from, ts)

    def fine_slot(self, index):
        slot = self.fine.get(index)
        if slot is None:
            slot = self.fine[index] = _Fine()
        return slot

    def coarse_slot(self, now, index):
        slot = self.coarse.get(index)
        if slot is None:
            if len(self.coarse) >= _COARSE_MAX:
                self.prune(now)
            slot = self.coarse[index] = _Coarse()
        return slot

    def prune(self, now):
        cutoff = now - RETENTION
        _trim(self.fine, _FINE_MAX, cutoff // FINE)
        _trim(self.coarse, _COARSE_MAX, cutoff // COARSE)
        for key in [key for key, turn in self.turns.items() if turn.touched < now - LIVE_TURN_TTL]:
            del self.turns[key]
        if self.oldest is not None and self.oldest < cutoff:
            self.oldest = cutoff

    def turn(self, key, ts, restored):
        turn = self.turns.get(key)
        if turn is None:
            if len(self.turns) >= MAX_LIVE_TURNS:
                self.turns.popitem(last=False)
            turn = self.turns[key] = _Turn(restored)
        else:
            self.turns.move_to_end(key)
        turn.touched = ts
        return turn

    def settle_restored(self):
        """Turns replayed from a previous run without an end belonged to a process that is gone."""
        for key in [key for key, turn in self.turns.items() if turn.restored]:
            del self.turns[key]

    def _shift_unknown(self, now, index, change):
        # Creating the slot is right (the attempt started in an interval without numeric events),
        # but an interval already past retention must not be resurrected.
        if index * FINE >= now - RETENTION:
            self.fine_slot(index).unknown += change

    def ingest(self, event, now, restored=False):
        ts = _number(event.get("ts"))
        if ts is None or ts < now - RETENTION:
            return
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        kind = event.get("kind") if isinstance(event.get("kind"), str) else ""
        if self.oldest is None or ts < self.oldest:
            self.oldest = ts
        if restored and ts < self.covered_from:
            self.covered_from = ts
        index = int(ts // FINE)
        coarse = self.coarse_slot(now, int(ts // COARSE))
        category, level = event.get("category"), event.get("level")
        if isinstance(category, str):
            _count(coarse.categories, category)
        if isinstance(level, str):
            _count(coarse.levels, level)
        key = event.get("turn_id") or event.get("request_id") or ("event", event.get("seq"))
        if level == "error":
            self.fine_slot(index).errors += 1
        if kind == "http.request":
            fine = self.fine_slot(index)
            fine.requests += 1
            _count(coarse.statuses, str(data.get("status")))
            duration = _number(data.get("duration_ms"))
            if duration is not None and data.get("route") in _ROUTES:
                fine.latency_sum += duration
                fine.latency_n += 1
                if len(coarse.latency) < MAX_SAMPLES:
                    coarse.latency.append(duration)
        elif kind == "turn.begin":
            self.fine_slot(index).turns += 1
            _count(coarse.models, str(data.get("model")))
            self.turn(key, ts, restored)
        elif kind == "turn.end":
            turn = self.turns.pop(key, None)
            attempt = turn.end(data.get("reason")) if turn is not None else None
            if attempt is not None:
                self._shift_unknown(now, attempt.where, 1)
        elif kind == "upstream.first_token":
            ttft = _number(data.get("ttft_ms"))
            if ttft is not None:
                fine = self.fine_slot(index)
                fine.ttft_sum += ttft
                fine.ttft_n += 1
                if len(coarse.ttft) < MAX_SAMPLES:
                    coarse.ttft.append(ttft)
        elif kind == "upstream.usage_start":
            self.turn(key, ts, restored).start(_attempt_number(data), index)
        elif kind == "upstream.usage":
            fine = self.fine_slot(index)
            price = _price(data.get("credits"))
            attempt, change = self.turn(key, ts, restored).usage(_attempt_number(data), price, index)
            if price is not None:
                fine.credits += price
            if change:
                self._shift_unknown(now, attempt.where, change)
            fine.cache_read += _whole(data.get("cache_read"))
            fine.cache_write += _whole(data.get("cache_write"))
            fine.input += _whole(data.get("input_tokens"))
            fine.output += _whole(data.get("output_tokens"))
        elif kind == "cache.keepwarm_usage":
            # One event per paid refresh ping; the sibling cache.keepwarm event repeats its price.
            fine = self.fine_slot(index)
            price = _price(data.get("actual_credits"))
            fine.keepwarm_pings += 1
            if price is None:
                fine.keepwarm_unknown += 1
                fine.unknown += 1
            else:
                fine.keepwarm += price
                fine.credits += price
        elif kind == "tool.call":
            self.fine_slot(index).tools += 1
        elif kind == "exec.result":
            self.fine_slot(index).commands += 1


class Journal:
    def __init__(self, capacity=CAPACITY, clock=None):
        self._cond = threading.Condition()
        self._events = collections.deque(maxlen=capacity)
        self._seq = 0
        self._cfg = None
        self._path = None
        self._file = None
        self._file_lock = threading.Lock()
        self._rotate_retry_at = 0.0
        self._clock = clock
        self.started = self._now()
        self._rollup = _Rollup(self.started)

    def _now(self):
        return self._clock() if self._clock else time.time()

    def _ingest(self, event, now, restored=False):
        """Fold an event into the metrics rollup; the caller holds ``self._cond``."""
        try:
            self._rollup.ingest(event, now, restored)
        except Exception:  # noqa: BLE001 - metrics must never lose or block the event itself
            # Counting stopped for this event: say so through coverage instead of under-counting silently.
            self._rollup.gap(event.get("ts"))

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
        # The rotated generation (.1) is read too: without it every rotation
        # would silently drop its events from the restored history.
        lines = collections.deque()
        for source in (path + ".1", path):
            try:
                with open(source, encoding="utf-8") as handle:
                    lines.extend(handle)
            except OSError:
                continue
        restored = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or not isinstance(event.get("seq"), int):
                continue
            # Re-clean with the CURRENT patterns and secrets: events written by
            # older code (or before a redact fixup) must not serve raw.
            event = self._clean(event, self._secrets())
            event["restored"] = True
            restored.append(event)
        with self._cond:
            # Concurrent writers may append to the file out of seq order; sort so
            # the since/before scan in query() never truncates after a restart.
            restored.sort(key=lambda item: item["seq"])
            now = self._now()
            for event in restored:
                if event["seq"] <= self._seq:
                    continue  # already known (re-configure), never duplicate
                self._events.append(event)
                # The rollup takes every restored event, not just the few the ring buffer keeps.
                self._ingest(event, now, restored=True)
            if restored:
                self._seq = max(self._seq, max(event["seq"] for event in restored))
            self._rollup.settle_restored()

    def reset(self):
        with self._cond:
            self._events.clear()
            self._seq = 0
            # A cleared journal has no history: coverage starts over from this moment.
            self._rollup = _Rollup(self._now())
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
                try:
                    # _load_tail restores .1 too: a cleared journal must not
                    # come back after a restart.
                    os.remove(self._path + ".1")
                except OSError:
                    pass

    # -- redaction ---------------------------------------------------------
    def _secrets(self):
        cfg = self._cfg or {}
        values = [cfg.get(key) for key in ("relay_api_key", "cookie", "xsrf", "thread_id", "page_uuid")]
        values += list(cfg.get("cookie_values") or [])
        for account in cfg.get("accounts") or []:
            if isinstance(account, dict):
                values += [account.get(key) for key in ("cookie", "xsrf", "thread_id", "page_uuid")]
                values += list(account.get("cookie_values") or [])
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
            cleaned = {}
            for key, item in list(value.items())[:64]:
                # Keys are user-adjacent data too: a secret can sit in a key.
                cleaned[self._clean(str(key), secrets, depth + 1)] = self._clean(item, secrets, depth + 1)
            return cleaned
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
            identity = client_context.current()
            fields = {**identity, **fields}
            caption = client_context.label(identity)
            if caption:
                message = "[%s] %s" % (caption, message or kind)
            secrets = self._secrets()
            event = {"ts": round(self._now(), 3), "kind": kind, "category": kind.split(".", 1)[0],
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
                self._ingest(event, event["ts"])
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
                if self._file.tell() > MAX_FILE_BYTES and time.time() >= self._rotate_retry_at:
                    self._file.close()
                    self._file = None
                    os.replace(self._path, self._path + ".1")
            except OSError as exc:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
                # Back off: an oversized file would otherwise retry (and print)
                # on every single event.
                self._rotate_retry_at = time.time() + 60.0
                # A failed rotation (e.g. a locked .1 on Windows) would otherwise
                # grow the file unbounded and silently; the journal cannot report
                # on itself, so use stderr.
                print("[journal] rotation failed: %s" % exc, flush=True)

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
        """Group turn-scoped events into compact traces, newest first.

        ``credits`` is the sum of the valid prices the turn's attempts reported, or ``None`` while
        no price is known for a turn that may have cost something. ``credits_complete`` is true
        only for an ended turn whose every paid attempt reported a valid price;
        ``credits_unknown`` counts the attempts that produced usage without one.
        """
        traces = collections.OrderedDict()
        costs = {}
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
                                           "credits": None, "credits_complete": False, "credits_unknown": 0,
                                           "ttft_ms": None, "duration_ms": None,
                                           "tools": 0, "commands": 0,
                                           "errors": 0, "cache_read": 0, "cache_write": 0,
                                           "events": 0, "request_id": event.get("request_id")}
                costs[turn_id] = _Turn()
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
                costs[turn_id].end(data.get("reason"))
            elif kind == "upstream.first_token":
                trace["ttft_ms"] = data.get("ttft_ms")
            elif kind == "upstream.usage_start":
                costs[turn_id].start(_attempt_number(data), None)
            elif kind == "upstream.usage":
                costs[turn_id].usage(_attempt_number(data), _price(data.get("credits")), None)
                trace["cache_read"] += _whole(data.get("cache_read"))
                trace["cache_write"] += _whole(data.get("cache_write"))
            elif kind == "tool.call":
                trace["tools"] += 1
            elif kind == "exec.result":
                trace["commands"] += 1
            if event["level"] == "error":
                trace["errors"] += 1
        for turn_id, trace in traces.items():
            total, priced, unknown = costs[turn_id].summary()
            ended = costs[turn_id].ended
            trace["credits_unknown"] = unknown
            trace["credits_complete"] = ended and not unknown
            if priced:
                trace["credits"] = round(total, 5)
            elif ended and not unknown:
                trace["credits"] = 0.0
        return list(reversed(traces.values()))[:limit]

    def metrics(self, window=3600, bucket=60):
        """Counters per ``bucket`` seconds over the last ``window`` seconds, read from the rollup.

        The window starts on a whole minute at or before ``now - window``. Credits are the
        valid prices of upstream attempts plus the cache refresh pings; an attempt that produced
        usage without a valid price counts as ``credits_unknown`` (``credits_complete`` is then
        false) and one that is still running is only listed in ``credits_in_progress``.
        ``coverage`` reports how much of the window the aggregates can vouch for. The journal lock
        is held only to copy slots, a portion at a time; the sums run without it, so a metrics call
        never keeps emit() waiting for the calculation.
        """
        now = self._now()
        bucket = max(1, bucket)
        count = max(1, int(window // bucket))
        start = math.floor((now - window) / COARSE) * COARSE
        begin = max(start, math.floor((now - RETENTION) / COARSE) * COARSE)
        with self._cond:
            rollup = self._rollup
            fine_slots, coarse_slots = list(rollup.fine.items()), list(rollup.coarse.items())
            in_progress = sum(1 for turn in rollup.turns.values()
                              if turn.pending and turn.touched >= now - LIVE_TURN_TTL)
            covered_from = max(rollup.covered_from, now - RETENTION)
            oldest = rollup.oldest
            buffered = len(self._events)
            buffer_oldest = self._events[0].get("ts") if buffered else None
            last_seq = self._seq
        # The slots are copied under the lock a portion at a time and summed without it.
        fine = self._copy_slots([item for item in fine_slots if item[0] * FINE >= begin],
                                lambda items: [(index, _FineView._make(_fine_values(slot))) for index, slot in items])
        coarse = self._copy_slots([item for item in coarse_slots if item[0] * COARSE >= begin],
                                  lambda items: [slot.copy() for _, slot in items])
        rows, totals, statuses, models, categories, levels = _aggregate(fine, coarse, start, bucket, count,
                                                                        in_progress)
        return {
            "window_seconds": window, "bucket_seconds": bucket, "buckets": rows, "totals": totals,
            "statuses": statuses, "models": dict(sorted(models.items(), key=lambda item: -item[1])[:12]),
            "categories": categories, "levels": levels,
            "coverage": {"complete": covered_from <= now - window, "window_start": int(start),
                         "covered_from": round(covered_from, 3),
                         "covered_seconds": round(max(0.0, min(window, now - covered_from)), 3),
                         "oldest_event_ts": oldest, "buffer_oldest_ts": buffer_oldest,
                         "buffer_events": buffered, "buffer_capacity": self._events.maxlen},
            "last_seq": last_seq, "buffered": buffered, "since": self.started,
        }

    def _copy_slots(self, items, copy):
        """Copy ``items`` under the lock one portion at a time: emit() waits for one portion at most."""
        copies = []
        for first in range(0, len(items), _PORTION):
            with self._cond:
                copies += copy(items[first:first + _PORTION])
        return copies


JOURNAL = Journal()


def scrub(value):
    """Apply the journal's configured secret redaction to console diagnostics."""
    return JOURNAL._clean(value, JOURNAL._secrets())


def emit(kind, message="", level="info", **fields):
    return JOURNAL.emit(kind, message, level, **fields)
