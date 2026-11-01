"""Thread-safe credits tracking and usage history."""
import json
import math
import threading
import time
import urllib.request

CACHE_TTL = 30.0
HISTORY_MAX = 200

# Retool rates measured by a least-squares fit over 350 journal turns (2026-10-31).
# Retool passes Anthropic pricing through: cache read 0.05x, cache write ~1.25x and
# output ~5x of the base input rate. The tariff is flat — no long-context premium
# up to at least 460k tokens (fit predicted 46.52 vs 46.53 actual on the record turn).
RATE_INPUT = 0.000075
RATE_CACHE_READ = 0.000004
RATE_CACHE_WRITE = 0.000100
RATE_OUTPUT = 0.000401


def cost_breakdown(usage):
    """Per-category credit cost and cache hit ratio for one turn's usage."""
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    read = int(usage.get("cache_read") or 0)
    write = int(usage.get("cache_write") or 0)
    prompt = inp + read + write
    return {
        "cost_input": round(inp * RATE_INPUT, 5),
        "cost_cache_read": round(read * RATE_CACHE_READ, 5),
        "cost_cache_write": round(write * RATE_CACHE_WRITE, 5),
        "cost_output": round(out * RATE_OUTPUT, 5),
        "prompt_tokens": prompt,
        "cache_hit_ratio": round(read / prompt, 4) if prompt else None,
    }


class CreditsManager:
    def __init__(self, base_url, headers_fn, refresher=None):
        self.base_url = base_url
        self.headers_fn = headers_fn
        self.refresher = refresher
        self._lock = threading.RLock()
        self._fetch_lock = threading.Lock()
        self._balance = None
        self._cached_at = float("-inf")
        self.session_used = 0.0
        self._history = []
        self._seen_turns = set()
        self.last_error = None

    def get_balance(self, force_refresh=False):
        # Coalesce simultaneous cache misses, including a failed auth refresh.
        with self._fetch_lock:
            return self._get_balance(force_refresh)

    def _get_balance(self, force_refresh):
        now = time.monotonic()
        with self._lock:
            if not force_refresh and now - self._cached_at < CACHE_TTL:
                if self._balance:
                    return dict(self._balance, stale=bool(self.last_error),
                                **({"stale_error": self.last_error} if self.last_error else {}))
                return {"available": None, "error": self.last_error, "session_used": self.session_used}
        def fetch():
            req = urllib.request.Request(self.base_url + "/api/credits/balance",
                                         headers=self.headers_fn(), method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))

        try:
            data = self.refresher.call(fetch) if self.refresher is not None else fetch()
            if not isinstance(data, dict) or not isinstance(data.get("org"), dict):
                raise ValueError("invalid upstream balance object")
            org = data["org"]
            available = org.get("balance")
            allocation = org.get("monthlyAllocation", 0.0)
            if isinstance(available, bool) or isinstance(allocation, bool):
                raise ValueError("invalid upstream balance amount")
            available, allocation = float(available), float(allocation)
            if not math.isfinite(available) or not math.isfinite(allocation):
                raise ValueError("invalid upstream balance amount")
            info = {
                "available": round(available, 4),
                "monthly_allocation": allocation,
                "used_month": round(max(0.0, allocation - available), 4) if allocation else 0.0,
                "reset_date": org.get("resetDate"),
                "is_low": org.get("isLow", False),
                "session_used": round(self.session_used, 4),
            }
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc) or type(exc).__name__
                self._cached_at = time.monotonic()
                if self._balance:
                    return dict(self._balance, stale=True, stale_error=self.last_error)
                return {"available": None, "error": self.last_error, "session_used": self.session_used}
        with self._lock:
            self._balance = dict(info)
            self._cached_at = time.monotonic()
            self.last_error = None
        return dict(info, stale=False)

    def record_turn(self, turn_id, cost):
        if not cost:
            return
        with self._lock:
            if turn_id and turn_id in self._seen_turns:
                return
            if turn_id:
                self._seen_turns.add(turn_id)
            self.session_used += cost
            self._history.append({"ts": time.time(), "turn_id": turn_id, "credits": round(cost, 5)})
            if len(self._history) > HISTORY_MAX:
                del self._history[: len(self._history) - HISTORY_MAX]
            if self._balance and self._balance.get("available") is not None:
                self._balance["available"] = round(self._balance["available"] - cost, 4)

    def usage(self):
        with self._lock:
            total = sum(item["credits"] for item in self._history)
            return {"session_used": round(self.session_used, 4), "turns": len(self._history),
                    "recent": list(self._history[-50:]), "recent_total": round(total, 4),
                    "available": (self._balance or {}).get("available")}
