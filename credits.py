"""Thread-safe credits tracking and usage history."""
import collections
import json
import math
import threading
import time
import urllib.request

CACHE_TTL = 30.0
HISTORY_MAX = 200
# A cache write worth this much alone is called out (a `[WRITE COSTLY …]` marker in the `[cache]` line and a
# cache.write_expensive event): one turn of 484,380 written tokens at Fable 5.2's $10/M is 1065 Minara credits, more than
# a fresh account's 300-credit plan, and it took an account from +500 to -875 at once. A warning only: the turn is
# billed by the time its usage arrives, so refusing it here would change nothing.
WRITE_WARN = 300.0
# A Minara Claude reply that carried cache marks, read the cache, wrote nothing and came back with at least this many input
# tokens is called out (cache.tail_unwritten, and a red line under its `[cache]` line): Minara bills a reply that writes
# nothing at the input price for its whole prompt, the part it read from the cache included. 15 of 36 turns of one Fable
# 5.2 session were such replies, 6,972 of its 9,876 credits (2026-11-13; minara.TOOL_RESULTS_END). A warning only.
TAIL_UNWRITTEN_INPUT = 8192
# The turn ids a manager remembers to count each billed attempt once. Every id is new (a uuid per attempt, per keepwarm
# ping), so a repeat can only come from the same attempt reported twice, moments apart: a few thousand cover that, where
# the set kept every id of the process's life (about 120 bytes each, some 2,000 a day).
SEEN_TURNS_MAX = 4096

# Retool rates measured by a least-squares fit over 350 journal turns (2026-10-31).
# Retool passes Anthropic pricing through: cache read 0.05x, cache write ~1.25x and
# output ~5x of the base input rate. The tariff is flat — no long-context premium
# up to at least 460k tokens (fit predicted 46.52 vs 46.53 actual on the record turn).
# They stay the rates of a model MODEL_RATES does not name.
RATE_INPUT = 0.000075
RATE_CACHE_READ = 0.000004
RATE_CACHE_WRITE = 0.000100
RATE_OUTPUT = 0.000401
# Per-model rates, an exact least-squares fit (largest error 0.0000 credits) of the reported credits of every attempt in
# the live journal of 2026-11-06: 753 attempts of Sonnet 5.5 and 120 of Opus 5.5. Sonnet costs half of Opus for input,
# output and a cache write; a cache read is 4e-6 for both.
# The others, the same fit of the journal of 2026-11-07 (largest error 0.0000 credits): gpt-6.1-sol over 781 attempts, GLM-5.3
# over 321 and GLM-5.3-Flash over 224; their keepwarm pings were billed within 1.3% of what these rates give. gpt-6.1-terra
# costs half of sol (5 attempts and 3 pings), claude-sonnet-5 1.5 times Sonnet 5.5 and gpt-5.5 is the fit of its 3 attempts:
# each of them matched to the last digit. GLM and gpt-5.5 report no cache write: a prefix they do not read is billed as
# plain input, and their cache_write says so.
MODEL_RATES = {
    "claude-sonnet-5-5": {"input": 0.00004, "cache_read": 0.000004, "cache_write": 0.00005, "output": 0.0002},
    "claude-opus-5-5": {"input": 0.00008, "cache_read": 0.000004, "cache_write": 0.0001, "output": 0.0004},
    "claude-sonnet-5": {"input": 0.00006, "cache_read": 0.000006, "cache_write": 0.000075, "output": 0.0003},
    "gpt-6.1-sol": {"input": 0.00004, "cache_read": 0.000002, "cache_write": 0.00005, "output": 0.0002},
    "gpt-6.1-terra": {"input": 0.00002, "cache_read": 0.000001, "cache_write": 0.000025, "output": 0.0001},
    "gpt-5.5": {"input": 0.0001, "cache_read": 0.00001, "cache_write": 0.0001, "output": 0.0006},
    "zai-org/GLM-5.3": {"input": 0.000112, "cache_read": 0.0000112, "cache_write": 0.000112, "output": 0.000352},
    "zai-org/GLM-5.3-Flash": {"input": 0.00006, "cache_read": 0.000012, "cache_write": 0.00006, "output": 0.0002},
}
_DEFAULT_RATES = {"input": RATE_INPUT, "cache_read": RATE_CACHE_READ, "cache_write": RATE_CACHE_WRITE,
                  "output": RATE_OUTPUT}


def rates(model=None):
    """The credit rates per token of `model` ({"input", "cache_read", "cache_write", "output"}); the default fit for any other."""
    return MODEL_RATES.get(model, _DEFAULT_RATES)


def token_counter(value):
    """A reported token count is a nonnegative finite integer, including zero."""
    if type(value) is int:
        return value if value >= 0 else None
    if type(value) is float and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return None


def cost_breakdown(usage, model=None):
    """Per-category credit cost and cache hit ratio for one turn's usage, at the rates of `model` (see rates)."""
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    read = int(usage.get("cache_read") or 0)
    write = int(usage.get("cache_write") or 0)
    prompt = inp + read + write
    rate = rates(model)
    if isinstance(model, str) and model.startswith("minara/"):
        return {"cost_input": None, "cost_cache_read": None, "cost_cache_write": None,
                "cost_output": None, "prompt_tokens": prompt,
                "cache_hit_ratio": round(read / prompt, 4) if prompt else None}
    return {
        "cost_input": round(inp * rate["input"], 5),
        "cost_cache_read": round(read * rate["cache_read"], 5),
        "cost_cache_write": round(write * rate["cache_write"], 5),
        "cost_output": round(out * rate["output"], 5),
        "prompt_tokens": prompt,
        "cache_hit_ratio": round(read / prompt, 4) if prompt else None,
    }


def minara_credits(usage):
    """Minara's own credits for a turn already priced in US dollars (220 per dollar), None when unpriced.

    Minara debits its own price list, so a turn carries provider_cost in USD and this is that figure in the currency its
    balance speaks. It is never added to `credits`, which is Retool's unit: the two are kept apart everywhere
    (account_report.aggregate_balance sums each in its own field)."""
    cost = usage.get("provider_cost") if isinstance(usage, dict) else None
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
        return None
    if (usage.get("provider_cost_currency") or "USD") != "USD":
        return None
    import minara_catalog
    return round(cost * minara_catalog.CREDITS_PER_USD, 4)


def write_cost(usage, model=None, pricing=None):
    """Credits one turn paid for writing its prefix into the cache, or None when it cannot be told apart.

    A cache write is the one line item that can cost more than a whole account: a 484,380-token write at Fable 5.2's
    $10/M is 1065 Minara credits ($4.84), more than the 300-credit plan of a fresh account. Minara's own price list
    (`pricing`, USD per million tokens) is what it debits, so the share is read from there rather than guessed from the
    dollar total, which mixes input, output and reads together."""
    if not isinstance(usage, dict) or not isinstance(pricing, dict):
        return None
    tokens = usage.get("cache_write")
    if type(tokens) not in (int, float) or not math.isfinite(tokens) or tokens <= 0:
        return None
    try:
        usd_per_million = float(pricing.get("cache_write", pricing["prompt"]) or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(usd_per_million) or usd_per_million <= 0:
        return None
    usd = tokens * usd_per_million / 1e6
    if isinstance(model, str) and model.startswith("minara/"):
        import minara_catalog
        return round(usd * minara_catalog.CREDITS_PER_USD, 2)
    # Retool bills its own fitted rate for a write (credits per token), not Minara's dollar price list.
    return round(tokens * rates(model)["cache_write"], 5)


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
        # Billed turns since start: _history keeps only the last HISTORY_MAX of them, and its length was shown as this count.
        self.turns = 0
        self._history = []
        self._seen_turns = collections.OrderedDict()  # the newest SEEN_TURNS_MAX turn ids, oldest first
        self.last_error = None
        self._balance_estimated = False
        self._unreported_usage = False
        self._usage_revision = 0
        self._refresh_raced = False
        self.last_fetched_at = None  # wall clock of the last successful server balance

    def get_balance(self, force_refresh=False):
        # Coalesce simultaneous cache misses, including a failed auth refresh.
        with self._fetch_lock:
            return self._get_balance(force_refresh)

    def _get_balance(self, force_refresh):
        now = time.monotonic()
        with self._lock:
            usage_revision = self._usage_revision
            if not force_refresh and now - self._cached_at < CACHE_TTL:
                if self._balance:
                    return dict(self._balance, stale=bool(self.last_error or self._unreported_usage or self._refresh_raced),
                                unreported_usage=self._unreported_usage,
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
            # An allocation Retool leaves out (or sends as null) is unknown, not zero; an explicit 0 is a real zero.
            allocation = org.get("monthlyAllocation")
            if isinstance(available, bool) or isinstance(allocation, bool):
                raise ValueError("invalid upstream balance amount")
            available = float(available)
            allocation = None if allocation is None else float(allocation)
            if not math.isfinite(available) or (allocation is not None and not math.isfinite(allocation)):
                raise ValueError("invalid upstream balance amount")
            info = {
                "available": round(available, 4),
                "monthly_allocation": allocation,
                "used_month": (None if allocation is None
                               else round(max(0.0, allocation - available), 4) if allocation else 0.0),
                "reset_date": org.get("resetDate"),
                "is_low": org.get("isLow", False),
                "session_used": round(self.session_used, 4),
            }
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc) or type(exc).__name__
                self._cached_at = time.monotonic()
                if self._balance:
                    return dict(self._balance, stale=True, stale_error=self.last_error,
                                unreported_usage=self._unreported_usage)
                return {"available": None, "error": self.last_error, "session_used": self.session_used}
        with self._lock:
            # A debit concurrent with this server snapshot may already be included.
            # Do not subtract it twice or call the result fully reconciled.
            self._refresh_raced = self._usage_revision != usage_revision
            if not self._refresh_raced:
                self._unreported_usage = False
            info["session_used"] = round(self.session_used, 4)
            self._balance = dict(info)
            self._cached_at = time.monotonic()
            self.last_fetched_at = time.time()
            self.last_error = None
            self._balance_estimated = False
            return dict(info, stale=self._refresh_raced or self._unreported_usage,
                        unreported_usage=self._unreported_usage)

    def record_turn(self, turn_id, cost):
        if isinstance(cost, bool):
            return
        try:
            cost = float(cost)
        except (ValueError, TypeError, OverflowError):
            return
        if not math.isfinite(cost) or cost <= 0:
            return
        with self._lock:
            if turn_id and turn_id in self._seen_turns:
                return
            if turn_id:
                self._seen_turns[turn_id] = None
                if len(self._seen_turns) > SEEN_TURNS_MAX:
                    self._seen_turns.popitem(last=False)
            self._usage_revision += 1
            self.session_used += cost
            self.turns += 1
            self._history.append({"ts": time.time(), "turn_id": turn_id, "credits": round(cost, 5)})
            if len(self._history) > HISTORY_MAX:
                del self._history[: len(self._history) - HISTORY_MAX]
            if self._balance and self._balance.get("available") is not None:
                self._balance["available"] = round(self._balance["available"] - cost, 4)
                self._balance_estimated = True

    def mark_usage_unknown(self):
        """A completed inference without creditsUsed is not a free inference."""
        with self._lock:
            self._unreported_usage = True
            self._usage_revision += 1

    def balance_snapshot(self):
        """Nonblocking console view; keep the local debit distinct from a fetch."""
        with self._lock:
            return dict(self._balance or {},
                        stale=bool(self.last_error or self._unreported_usage or self._refresh_raced)
                              or time.monotonic() - self._cached_at >= CACHE_TTL,
                        unreported_usage=self._unreported_usage,
                        estimated=self._balance_estimated)

    def usage(self):
        with self._lock:
            total = sum(item["credits"] for item in self._history)
            return {"session_used": round(self.session_used, 4), "turns": self.turns,
                    "recent": list(self._history[-50:]), "recent_total": round(total, 4),
                    "unreported_usage": self._unreported_usage,
                    "available": (self._balance or {}).get("available")}


def provider_cost_summary(records):
    """Separate reported money from Retool credits; never infer a currency or sum unlike money."""
    records = list(records)
    priced = [r for r in records if type(r.get('provider_cost')) in (int, float)
              and math.isfinite(r['provider_cost']) and r['provider_cost'] >= 0]
    currencies = {r.get('provider_cost_currency') for r in priced}
    summable = len(priced) == 1 or (bool(priced) and len(currencies) == 1 and None not in currencies)
    total = sum(r['provider_cost'] for r in priced) if summable else None
    overflow = total is not None and not math.isfinite(total)
    return {'provider_cost': round(total, 10) if total is not None and not overflow else None,
            'provider_cost_overflow': overflow,
            'provider_cost_currency': next(iter(currencies)) if len(currencies) == 1 else None,
            'provider_cost_complete': bool(records) and len(priced) == len(records),
            'provider_cost_records': [dict(r) for r in records]}
