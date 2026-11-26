"""Cost guard: what a Minara request will cost, worked out before it is sent, and ceilings on it (design_routing_errors.md §6).

Minara lets a request through on any positive balance and debits it afterwards. On 2026-11-17/18 test agents that continued
the owner's multi-megabyte sessions in new conversations spent about 11,360 Minara credits in 35 minutes: single requests
cost 500 to 3,533 credits, each fork writing its whole prompt to a cold cache or sending it uncached, and nine accounts went
negative. The owner's own requests cost at most 1,195 credits (p99 250): a prompt of 352k tokens costs 1.6 credits when the
cache is warm. What tells a dear request from a cheap one is a cold cache and the model's price, not the prompt's size.

The estimate prices a hypothetical usage of the request with minara.estimated_cost, the function minara.normalize_usage
prices every reply with: the estimate and the debit are one formula, price tiers (tiered_expr) included.
- Prompt tokens: adapters.count_input_tokens, the relay's own count_tokens, scaled from the dialog's last real count when the
  session has one.
- Cache: warm when the relay has seen a priced turn of this dialog. A Claude cache is shared by all Minara accounts and read
  for under an hour (HEAD_WARM); any other channel's cache is the account's own (a fork on a new account wrote 458k-473k
  tokens there, each). A session that brings a prefix of its own (a turn served here, or one restored from disk after a
  restart) is warm. Anything else is a first contact: the whole prompt is written (an explicit cache) or billed as input.
- Output: the request's max_tokens, at most what its effort level spends (LEVEL_OUTPUT).

The ceilings (config.DEFAULTS, the admin applies them at once): a request (cost_guard_request_minara, 1,500 credits: above
the owner's largest, under the three dearest of the agents', 65% of their spend), a sliding hour over all Minara accounts
(cost_guard_hour_minara, 12,000: a guard against loops, the owner's busiest hour was 6,373), and a client key's own
max_request_minara and max_day_minara. A request above one of them is refused before anything is sent: HTTP 400
cost_limit_exceeded (final in every dialect: Codex gets invalid_prompt) or 429 with Retry-After for the hour, its text naming
the estimate and how it was made. A request may go through up to a sum the client names in x-proxi-cost-confirm (the master
key, or a client key with allow_cost_confirm). Retool turns are not guarded: an organization's hourly Copilot limit bounds
them (the owner's dearest Retool request was 96 credits). A key's daily spend is kept in cost_guard.auto.json beside
config.json, so a restart does not reset it.
"""
import collections
import json
import math
import os
import tempfile
import threading
import time

import apierr

CONFIRM_HEADER = "x-proxi-cost-confirm"
STATE_NAME = "cost_guard.auto.json"
MODES = ("off", "warn", "block")
HOUR = 3600.0
# How long a dialog's cache is read after its last turn: Minara's Claude marks hold an hour (minara.HEAD_WARM, 3300 s), any
# other channel's cache is taken as warm for half an hour, and only on the account that wrote it.
WARM_SHARED = 3300.0
WARM_OWN = 1800.0
# What an effort level spends on its reply, at most (the request's max_tokens caps it): the reasoning and the answer.
LEVEL_OUTPUT = {"none": 2000, "minimal": 2000, "low": 2000, "medium": 8000, "high": 16000, "xhigh": 32000, "max": 32000}
DEFAULT_OUTPUT = 8000
MAX_DIALOGS = 4096  # dialogs whose last priced turn is remembered (the oldest go first)
REPLACE_ATTEMPTS = 4

Estimate = collections.namedtuple(
    "Estimate", "credits usd prompt_tokens cache_read written output_tokens warm basis")


def output_budget(max_tokens, effort):
    """The output tokens a request is priced for: its effort level's budget, at most its max_tokens."""
    budget = LEVEL_OUTPUT.get(effort, DEFAULT_OUTPUT)
    if type(max_tokens) is int and max_tokens > 0:
        budget = min(budget, max_tokens)
    return budget


def shared_cache(entry):
    """Whether the model's cache on Minara is read from every account (Claude) rather than the account's own."""
    entry = entry or {}
    text = " ".join(str(entry.get(name) or "") for name in ("provider", "upstream_model", "id")).lower()
    return "anthropic" in text or "claude" in text


def estimate(entry, *, prompt_tokens, basis, warm_prefix, max_tokens, effort):
    """The Estimate of one Minara request, None when the catalog price of the model cannot be read.

    warm_prefix: the prompt tokens of the dialog's last turn when its cache is warm, else 0 (the whole prompt is new)."""
    import minara
    import minara_catalog
    pricing = (entry or {}).get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt = max(0, int(prompt_tokens or 0))
    read = min(prompt, max(0, int(warm_prefix or 0)))
    new = prompt - read
    output = output_budget(max_tokens, effort)
    # An explicit cache (the relay's marks) writes what is new; an automatic one bills a cold prompt as input.
    explicit = (entry or {}).get("cache") == "explicit"
    usage = {"input_tokens": 0 if explicit else new, "cache_read": read, "cache_write": new if explicit else 0,
             "output_tokens": output}
    usd = minara.estimated_cost(pricing, usage)
    if usd is None or not math.isfinite(usd):
        return None
    return Estimate(round(usd * minara_catalog.CREDITS_PER_USD, 2), round(usd, 6), prompt, read, new, output,
                    read > 0, basis)


def confirmed(value):
    """The credits a client allowed with x-proxi-cost-confirm (a positive number), else None."""
    try:
        number = float(str(value).strip().replace(",", "")) if value not in (None, "") else None
    except ValueError:
        return None
    return number if number is not None and math.isfinite(number) and number > 0 else None


def _limit(value):
    """A ceiling from the config or a key: a positive number, None for none (0, null, nonsense)."""
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def _amount(value):
    return "{:,.0f}".format(value) if value >= 100 else "{:,.2f}".format(value)


def explain(est, entry, account):
    """How an estimate was made, for the client and the journal: the cache it assumed and what it was priced for."""
    if est.warm:
        cache = "about {:,} prompt tokens read from the dialog's warm cache and {:,} new".format(est.cache_read, est.written)
    elif (entry or {}).get("cache") == "explicit":
        cache = "about {:,} prompt tokens would be written to a cold cache".format(est.written)
    else:
        cache = "about {:,} prompt tokens would be sent without a cache".format(est.written)
    return "{}, up to {:,} output tokens (model {}, account {})".format(
        cache, est.output_tokens, (entry or {}).get("id") or "?", account or "?")


class Guard:
    """The Minara spend the ceilings are kept against: the last hour of all accounts, a key's day, each dialog's last turn.

    Thread-safe; `clock` is wall time (a key's day is the local calendar day)."""

    def __init__(self, path=None, clock=time.time):
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._clock = clock
        self._path = path
        self._hour = collections.deque()  # (at, credits) of each Minara debit of the last hour
        self._dialogs = collections.OrderedDict()  # conversation -> {"at", "prompt", "account", "model"}
        self._keys = {}  # client key id -> {"day": "YYYY-MM-DD", "credits": spent that day}
        self._load()

    # -- what was spent -------------------------------------------------------------------------------------------------
    def spent(self, credits, *, key_id=None, conversation=None, account=None, model=None, prompt_tokens=None):
        """Record a Minara debit of a turn (its share, as the journal adds it once) and the dialog's prompt."""
        now = self._clock()
        changed = False
        with self._lock:
            if type(credits) in (int, float) and math.isfinite(credits) and credits > 0:
                self._hour.append((now, float(credits)))
                if key_id:
                    day = self._day(now)
                    record = self._keys.get(key_id)
                    if record is None or record.get("day") != day:
                        record = self._keys[key_id] = {"day": day, "credits": 0.0}
                    record["credits"] = round(record["credits"] + float(credits), 4)
                    changed = True
            if conversation and type(prompt_tokens) is int and prompt_tokens > 0:
                self._dialogs.pop(conversation, None)
                self._dialogs[conversation] = {"at": now, "prompt": prompt_tokens, "account": account, "model": model}
                while len(self._dialogs) > MAX_DIALOGS:
                    self._dialogs.popitem(last=False)
        if changed:
            self._save()

    def hour_spent(self):
        """(Minara credits debited in the last hour, seconds until the oldest of them leaves the hour)."""
        now = self._clock()
        with self._lock:
            while self._hour and now - self._hour[0][0] >= HOUR:
                self._hour.popleft()
            total = sum(credits for _, credits in self._hour)
            return total, (HOUR - (now - self._hour[0][0]) if self._hour else 0.0)

    def _hour_wait(self, need):
        """Seconds until `need` credits have left the hour."""
        now = self._clock()
        with self._lock:
            freed = 0.0
            for at, credits in self._hour:
                freed += credits
                if freed >= need:
                    return max(1.0, HOUR - (now - at))
        return HOUR

    def key_spent(self, key_id):
        """Minara credits a client key spent today."""
        with self._lock:
            record = self._keys.get(key_id)
            return record["credits"] if record and record.get("day") == self._day(self._clock()) else 0.0

    def warm_prefix(self, conversation, *, model, account, shared, session_prefix=0):
        """The prompt tokens of the dialog's last turn when its cache is warm for this request, else 0."""
        record = None
        if conversation:
            with self._lock:
                record = self._dialogs.get(conversation)
        if record is not None:
            if record.get("model") != model:
                return 0
            age = self._clock() - record["at"]
            if age >= (WARM_SHARED if shared else WARM_OWN):
                return 0
            if not shared and record.get("account") != account:
                return 0
            return record["prompt"]
        # A session with a prefix of its own served this dialog on this account (now, or before a restart).
        return max(0, int(session_prefix or 0))

    # -- the ceilings ---------------------------------------------------------------------------------------------------
    def check(self, cfg, est, *, entry, account, key=None, confirm=None):
        """(error to answer with or None, whether to warn): the ceilings of cfg and of the client `key` for `est`."""
        mode = cfg.get("cost_guard", "block")
        if mode == "off" or est is None:
            return None, False
        warn_at = _limit(cfg.get("cost_guard_warn_minara"))
        warn = warn_at is not None and est.credits >= warn_at
        allowed = confirmed(confirm)
        key = key or {}
        how = explain(est, entry, account)
        refusal = None
        limits = [(_limit(cfg.get("cost_guard_request_minara")), "cost_guard_request_minara")]
        if key.get("id"):
            limits.append((_limit(key.get("max_request_minara")), "max_request_minara of client key '%s'" % key["id"]))
        for ceiling, name in limits:
            if ceiling is not None and est.credits > ceiling and not (allowed and allowed >= est.credits):
                refusal = apierr.cost_limit_exceeded(
                    "Estimated cost {} Minara credits (~${:.2f}) is above {} = {}: {}. Raise the limit in the admin "
                    "(Settings) or repeat with the header {}: {}.".format(
                        _amount(est.credits), est.usd, name, _amount(ceiling), how, CONFIRM_HEADER,
                        int(math.ceil(est.credits / 100.0) * 100)), est)
                break
        day = _limit(key.get("max_day_minara")) if key.get("id") else None
        if refusal is None and day is not None:
            spent = self.key_spent(key["id"])
            if spent + est.credits > day:
                refusal = apierr.cost_limit_exceeded(
                    "Client key '{}' has spent {} of its {} Minara credits for today (max_day_minara); this request is "
                    "estimated at {}: {}. The day's spend resets at midnight.".format(
                        key["id"], _amount(spent), _amount(day), _amount(est.credits), how), est)
        hour = _limit(cfg.get("cost_guard_hour_minara"))
        if refusal is None and hour is not None:
            spent, _ = self.hour_spent()
            if spent + est.credits > hour:
                wait = self._hour_wait(spent + est.credits - hour)
                refusal = apierr.ApiError(
                    429, "rate_limit_error",
                    "Minara spent {} credits in the last hour; this request (estimated at {}) would pass "
                    "cost_guard_hour_minara = {}. Retry in {} or raise the limit in the admin.".format(
                        _amount(spent), _amount(est.credits), _amount(hour), apierr.wait_text(wait)),
                    retry_after=math.ceil(wait))
                refusal.cost_estimate = est
        if mode == "warn":
            return None, warn or refusal is not None
        return refusal, warn

    # -- the day's spend of the keys, on disk ---------------------------------------------------------------------------
    @staticmethod
    def _day(now):
        return time.strftime("%Y-%m-%d", time.localtime(now))

    def _load(self):
        if not self._path:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError):
            return
        keys = saved.get("keys") if isinstance(saved, dict) else None
        for key_id, record in (keys or {}).items():
            if (isinstance(key_id, str) and isinstance(record, dict) and isinstance(record.get("day"), str)
                    and type(record.get("credits")) in (int, float) and math.isfinite(record["credits"])):
                self._keys[key_id] = {"day": record["day"], "credits": float(record["credits"])}

    def _save(self):
        if not self._path:
            return
        with self._save_lock:
            with self._lock:
                payload = {"keys": {key_id: dict(record) for key_id, record in self._keys.items()}}
            temporary = None
            try:
                directory = os.path.dirname(os.path.abspath(self._path))
                fd, temporary = tempfile.mkstemp(prefix=".cost_guard-", suffix=".tmp", dir=directory)
                try:
                    handle = os.fdopen(fd, "w", encoding="utf-8")
                except BaseException:
                    os.close(fd)
                    raise
                with handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                for attempt in range(REPLACE_ATTEMPTS):
                    try:
                        os.replace(temporary, self._path)
                        temporary = None
                        break
                    except PermissionError:
                        # Windows: a scanner or indexer may hold the target for a moment.
                        if attempt + 1 == REPLACE_ATTEMPTS:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            except OSError:
                pass  # a key's day only: the guard keeps working from memory
            finally:
                if temporary is not None and os.path.exists(temporary):
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass


def state_path(cfg):
    """cost_guard.auto.json beside config.json, None for a config that was not loaded from a file."""
    path = (cfg or {}).get("_config_path")
    return os.path.join(os.path.dirname(path), STATE_NAME) if path else None
