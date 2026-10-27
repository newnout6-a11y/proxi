"""Credits management and tracking for Retool AI agent."""
import json, time, urllib.request, urllib.error

class CreditsManager:
    def __init__(self, base_url, headers_fn):
        self.base_url = base_url
        self.headers_fn = headers_fn
        self._cached_balance = None
        self._cache_time = 0
        self.session_used = 0.0

    def get_balance(self, force_refresh=False):
        now = time.time()
        if not force_refresh and self._cached_balance and (now - self._cache_time < 30):
            return self._cached_balance

        try:
            req = urllib.request.Request(
                self.base_url + "/api/credits/balance",
                headers=self.headers_fn(),
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
                org = data.get("org") or {}
                avail = org.get("balance", 0.0)
                alloc = org.get("monthlyAllocation", 0.0)
                reset_date = org.get("resetDate", "")
                is_low = org.get("isLow", False)
                res = {
                    "available": round(avail, 4),
                    "monthly_allocation": alloc,
                    "used_month": round(max(0.0, alloc - avail), 4) if alloc else 0.0,
                    "reset_date": reset_date,
                    "is_low": is_low,
                    "session_used": round(self.session_used, 4)
                }
                self._cached_balance = res
                self._cache_time = now
                return res
        except Exception as e:
            if self._cached_balance:
                return self._cached_balance
            return {
                "available": None,
                "monthly_allocation": None,
                "error": str(e),
                "session_used": round(self.session_used, 4)
            }

    def record_turn_cost(self, credits_used: float):
        if not credits_used:
            return
        self.session_used += credits_used
        if self._cached_balance and self._cached_balance.get("available") is not None:
            self._cached_balance["available"] = round(self._cached_balance["available"] - credits_used, 4)
            self._cached_balance["session_used"] = round(self.session_used, 4)
            avail_str = f"{self._cached_balance['available']:.4f}"
        else:
            avail_str = "unknown"
        print(f"[Retool Credits] Turn cost: {credits_used:.4f} | Session total: {self.session_used:.4f} | Est. balance: {avail_str}", flush=True)
