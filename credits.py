"""Thread-safe credits tracking and usage history."""
import json
import math
import threading
import time
import urllib.request

CACHE_TTL = 30.0
HISTORY_MAX = 200


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
