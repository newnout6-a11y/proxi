"""Account dispatcher: staggered renewal, per-account balance and routing health.

One scheduler thread replaces a keepalive thread per account. First checks are
spread out, at most `account_refresh_parallel` checks run at once and a network
failure pauses every account together, so 20-30 accounts never renew in one
burst. An account whose renewal keeps failing with 401/403, or whose confirmed
balance is below `min_account_balance`, leaves the routing pool until a later
check succeeds. An account that spent Retool's hourly managed-key quota is parked
for `account_rate_limit_cooldown` seconds and returns by itself. A Minara account
whose Bearer token has expired leaves the pool without a request to Minara. An
account whose Retool plan refused a model is held back from that model alone
(`refused_models` on the backend) and keeps serving every other one: for five
minutes, and for an hour when the refusal comes within an hour after the last
hold of that pair ended.
"""
import math
import collections
import random
import threading
import time

import apierr
import console_log
import journal
import minara_accounts
from minara_catalog import CREDITS_PER_USD
from io_workers import DaemonIOExecutor

DEAD_AFTER = 3            # consecutive auth failures before an account leaves the pool
FORCED_CHECK_GAP = 15.0   # at most one on-demand balance fetch per account per gap
TOKEN_RECHECK = 120.0     # seconds between balance reads of a Minara account whose token was rejected: a brief
                          # 401 of Minara's own took every account out for ten minutes
STARTUP_SPREAD = 2.0      # seconds between accounts' first checks
STARTUP_PARALLEL = 16     # balances read at a time in the first round of a start (check_all)
NETWORK_PAUSE_MAX = 300.0
AUTH_RETRY_MAX = 600.0
PRIORITY_MAX = 99
LABEL_MAX = 40
RATE_LIMIT_COOLDOWN = 900.0  # default pause after Retool's hourly managed-key limit
RATE_LIMIT_MAX = 3600.0      # the limit is a sliding hour: parking an account longer never helps
RATE_LIMIT_STEP = 300.0      # a limit right after the return adds five minutes to the pause ...
RATE_LIMIT_CEILING = 1200.0  # ... up to twenty minutes, or the cooldown itself when that is longer
EXPIRY_WARN_EVERY = 86400.0  # a token about to expire is announced at most once a day per account
PLAN_REFUSAL_FIRST = 300.0   # the first refusal of a model by an account's Retool plan holds that pair for five minutes,
PLAN_REFUSAL_HOLD = 3600.0   # one within an hour after the pair's last hold ended holds it for an hour


def _local_time(epoch):
    """A moment as the console shows dates: 11.11.2026 14:05 local time."""
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(epoch))
    except (OverflowError, OSError, ValueError):
        return "%d" % epoch


def _time_left(seconds):
    """3 дн 5 ч, 7 ч, 40 мин."""
    hours = max(0, int(seconds // 3600))
    if hours >= 24:
        return "%d дн %d ч" % (hours // 24, hours % 24)
    if hours:
        return "%d ч" % hours
    return "%d мин" % max(1, math.ceil(seconds / 60))


def enabled(backend):
    """An operator can switch an account off in the admin panel without deleting it."""
    return (getattr(backend, "cfg", None) or {}).get("enabled", True) is not False


def priority(backend):
    """Lower is preferred for new conversations; 0 when unset."""
    value = (getattr(backend, "cfg", None) or {}).get("priority", 0)
    return value if type(value) is int else 0


UNNAMED = "account"  # what a report, a console line or a message calls an account that has no identity


def cfg_identity(cfg):
    """The raw identifier that names an account config: `account_id`, else `page_uuid`; None when it has neither.

    The one place that says which field names an account. State that must find its account again after a
    restart (a stored conversation) keeps this value, never a cookie."""
    cfg = cfg or {}
    return cfg.get("account_id") or cfg.get("page_uuid") or None


def cfg_tag(cfg, default=None):
    """Journal label of an account config: a short hash, never the raw ID (see cfg_identity).

    `default` is what an account without an identity gets. Journal fields keep None, so that the event names
    no account; anything a person reads (reports, status rows, console lines, messages) passes UNNAMED."""
    return journal.tag(cfg_identity(cfg)) or default


def account_tag(backend, default=None):
    """`cfg_tag` of a backend's config."""
    return cfg_tag(getattr(backend, "cfg", None), default)


def priced_in_dollars(cfg):
    """A universal provider's credential: its balance and costs are US dollars, neither Retool's nor Minara's credits."""
    return (cfg or {}).get("upstream", "retool") not in ("retool", "minara")


def usd_floor(cfg):
    """The dollar balance below which a universal provider's credential takes no new turn: min_balance_usd, else 0.

    min_account_balance is a figure in Retool credits (20 by default): a provider with $15 left was judged against it,
    left the pool as "exhausted" and served only as a reserve (audit A07-04)."""
    value = (cfg or {}).get("min_balance_usd")
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0.0


def idle_block(backend):
    """Why background traffic, such as idle cache warmups, must skip this account; None when it may run.

    Any account the operator switched off, or the dispatcher took out of routing (`backend.unavailable`
    is set, whatever its reason), only serves requests a client actually sends. The reason is passed
    through, never matched against a list, so a reason added later is covered without a change here."""
    if not enabled(backend):
        return "disabled"
    unavailable = getattr(backend, "unavailable", None)
    if unavailable is None:
        return None
    reason = unavailable.get("reason") if isinstance(unavailable, dict) else None
    return reason or "unavailable"


class UnmeteredAccount:
    """Provider balance is unavailable, not zero and not Retool credits. No network calls."""
    supports_balance = False

    def __init__(self):
        self.last_error = None
        self.last_fetched_at = None
        self.session_used = None
        self._lock = threading.RLock()
        self._attempts = collections.OrderedDict()
        self._active = {}
        self._turn_count = 0
        self._attempt_count = 0
        self._priced_count = 0
        self._currencies = {}
        self._currency_counts = {}
        self._currency_overflow = False
        self._unknown_cost_count = 0
        self._only_unknown_cost = None

    def start_provider_turn(self):
        with self._lock:
            self._turn_count += 1

    def _account(self, row, sign=1):
        cost = row.get('provider_cost')
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            return
        self._priced_count += sign
        currency = row.get('provider_cost_currency')
        if currency is None:
            self._unknown_cost_count += sign
            # On removal the remaining singleton is another row, never the removed cost.
            # Its historical row may already be evicted: return unknown conservatively.
            self._only_unknown_cost = cost if sign > 0 and self._unknown_cost_count == 1 else None
        elif currency in self._currencies or len(self._currencies) < 16:
            self._currency_counts[currency] = self._currency_counts.get(currency, 0) + sign
            self._currencies[currency] = self._currencies.get(currency, 0.0) + sign * cost
            if self._currency_counts[currency] == 0:
                self._currency_counts.pop(currency, None)
                self._currencies.pop(currency, None)
        else:
            self._currency_overflow = True

    def record_provider_usage(self, attempt_id, turn_id, usage, *, final=False):
        """Bounded active/recent dedup plus lifetime totals; IDs are not retained forever."""
        with self._lock:
            row = self._active.get(attempt_id) or self._attempts.get(attempt_id)
            if row is None:
                row = {'attempt_id': attempt_id, 'turn_id': turn_id, 'ts': time.time()}
                self._attempt_count += 1
                self._active[attempt_id] = row
            else:
                self._account(row, -1)
            row.update(usage)
            self._account(row)
            if final:
                self._active.pop(attempt_id, None)
                self._attempts[attempt_id] = row
                self._attempts.move_to_end(attempt_id)
                while len(self._attempts) > 50:
                    self._attempts.popitem(last=False)

    def finish_provider_attempt(self, attempt_id, turn_id):
        self.record_provider_usage(attempt_id, turn_id, {}, final=True)

    def get_balance(self, force_refresh=False):
        return {'available': None, 'session_used': None, 'balance_supported': False,
                'unit': None, 'stale': False, 'unreported_usage': True}

    def balance_snapshot(self):
        return self.get_balance()

    def usage(self):
        from credits import provider_cost_summary
        with self._lock:
            rows = [*self._attempts.values(), *self._active.values()]
            recent = rows[-50:]
            summary = provider_cost_summary(recent)
            total = None
            currency = None
            if not self._currency_overflow:
                if len(self._currencies) == 1 and not self._unknown_cost_count:
                    currency, total = next(iter(self._currencies.items()))
                elif not self._currencies and self._unknown_cost_count == 1:
                    total = self._only_unknown_cost
            complete = self._attempt_count == self._priced_count
            overflow = total is not None and not math.isfinite(total)
            summary.update(provider_cost=round(total, 10) if total is not None and not overflow else None,
                           provider_cost_overflow=overflow,
                           provider_cost_currency=currency, provider_cost_complete=complete)
            return {'session_used': None, 'turns': self._turn_count, 'attempts': self._attempt_count,
                    'recent': [dict(row) for row in recent], 'recent_total': None,
                    'available': None, 'unreported_usage': not complete,
                    'unit': None, **summary}

    def mark_usage_unknown(self):
        pass  # begin-attempt records an unpriced attempt, including network failures

    def record_turn(self, turn_id, cost):
        pass  # Upstream monetary cost must never be debited as Retool credits.


def _minara_network_get(url, **kwargs):
    from curl_cffi import requests
    return requests.get(url, **kwargs)


class MinaraAccount(UnmeteredAccount):
    """A Minara account's credits, read from GET /auth/me (creditBalanceBreakdown: plan + extra).

    Minara debits a reply's usage.cost at 220 credits per US dollar as soon as it ends, and lets the balance go below zero
    (a turn is charged after it ran); at zero or below it answers 402. Between server reads the reported costs are taken
    off the last balance, so an account near its end leaves the pool before its next reply would overdraw it.

    The Bearer token is a JWT that lives 30 days: every balance answer carries `expires_at` (epoch seconds, None when the
    token is not a readable JWT), and an expired token is never sent (Minara would answer 401)."""
    supports_balance = True
    keeps_local_debit = True  # account_report reads its snapshot, not the network, for passive reports
    CACHE_SECONDS = 20.0

    def __init__(self, cfg, fetch=None, backend=None):
        super().__init__()
        self.cfg = cfg
        self._fetch = fetch
        self.backend = backend
        self._balance = None
        self._fetched = float("-inf")
        self._spent_since = 0.0
        # The balance at which Minara last answered 402: whether its bonus ("extra") credits pay for API calls is not
        # known (an account at plan -102 and extra 63 was refused), so a refusal holds until the balance grows.
        self._refused_at = None
        self._raw = None  # plan + extra of the last read, before a refusal holds it at 0
        # One read at a time; a failed read is answered from memory for CACHE_SECONDS, as CreditsManager does. Without
        # them every caller read /auth/me again, and an account whose read failed was asked on every call.
        self._fetch_lock = threading.Lock()
        self._failed_at = float("-inf")
        self._failure = None
        self._token_read = None  # the token whose expiry _expiry holds: read again only when the token changes
        self._expiry = None

    def expires_at(self):
        """When the account's token stops working (epoch seconds, from the JWT payload); None when unknown."""
        token = self.cfg.get("bearer_token")
        with self._lock:
            if token != self._token_read:
                self._token_read, self._expiry = token, minara_accounts.expires_at(token)
            return self._expiry

    def expired(self, now=None):
        expiry = self.expires_at()
        return expiry is not None and (time.time() if now is None else now) >= expiry

    def _stamped(self, snapshot):
        snapshot["expires_at"] = self.expires_at()
        return snapshot

    def note_refusal(self):
        """Minara answered 402: the account takes nothing until a later read shows more credits than now.

        The mark is the server's balance (not the one shown, which a refusal holds at 0 or below), so a second 402 never
        lowers it and lets the account back in at an unchanged balance. Before any read it is unknown: the first read
        then sets it, so a top-up still brings the account back (it stayed out until a restart)."""
        with self._lock:
            raw = self._raw
            current = (raw - self._spent_since) if raw is not None else float("inf")
            self._refused_at = current if self._refused_at is None else max(self._refused_at, current)

    def _origin(self):
        base = str(self.cfg.get("base") or "https://api.minara.ai/v1").rstrip("/")
        return base[:-3] if base.endswith("/v1") else base

    def _read(self):
        if self._fetch is not None:
            return self._fetch()
        # config._check_upstream resolves bearer_token_env into bearer_token in memory.
        token = self.cfg.get("bearer_token") or ""
        get = getattr(self.backend, "_minara_get", None) or _minara_network_get
        response = get(self._origin() + "/auth/me", impersonate="chrome131", timeout=30, allow_redirects=False,
                       headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        if response.status_code in (401, 403):
            raise apierr.upstream_auth_failed("Minara rejected the account token")
        if response.status_code != 200:
            raise apierr.upstream_error("Minara balance HTTP %s" % response.status_code)
        return response.json()

    def get_balance(self, force_refresh=False):
        if self.expired():
            # The dispatcher takes such an account out of the pool (check_expiry); asking Minara would only cost a 401.
            return self._failed("token_expired")
        asked_at = time.monotonic()
        with self._lock:
            fresh = asked_at - self._fetched < self.CACHE_SECONDS
            if self._balance is not None and fresh and not force_refresh:
                return self._stamped(dict(self._balance))
            if not force_refresh and asked_at - self._failed_at < self.CACHE_SECONDS and self._failure:
                return self._failed(self._failure, stamp=False)
        with self._fetch_lock:
            with self._lock:
                # Another caller read it while this one waited: its answer is this one's too.
                if self._fetched >= asked_at and self._balance is not None:
                    return self._stamped(dict(self._balance))
                if self._failed_at >= asked_at and self._failure:
                    return self._failed(self._failure, stamp=False)
            return self._fetch_balance()

    def _fetch_balance(self):
        with self._lock:
            # What was spent until now; costs recorded while the read is under way stay on the local debit (Minara may
            # answer the read before it debits them). They were zeroed with the rest.
            spent_before = self._spent_since
        try:
            data = self._read()
            parts = data.get("creditBalanceBreakdown") or {}
            plan, extra = float(parts.get("plan")), float(parts.get("extra", 0) or 0)
            if not (math.isfinite(plan) and math.isfinite(extra)):
                raise ValueError("balance is not a number")
            total = (data.get("creditBalance") or {}).get("total")
            reward = (data.get("creditBalance") or {}).get("reward")
            capacity = sum(float(value) for value in (total, reward) if value is not None) or None
        except apierr.ApiError as error:
            return self._failed(error.code)
        except Exception as error:  # noqa: BLE001 - a balance read must never fail a turn
            return self._failed(type(error).__name__)
        with self._lock:
            available = self._raw = plan + extra
            if self._refused_at == float("inf"):
                self._refused_at = available  # refused before the first read: this read is the mark
            if self._refused_at is not None:
                if available > self._refused_at + 1:
                    self._refused_at = None  # topped up since the refusal
                else:
                    available = min(available, 0.0)
            self._balance = {"available": round(available, 4), "plan": round(plan, 4), "extra": round(extra, 4),
                             "monthly_allocation": capacity, "unit": "minara_credits", "balance_supported": True,
                             "usd": round(available / CREDITS_PER_USD, 4), "session_used": None,
                             "refused": self._refused_at is not None,
                             "stale": False, "unreported_usage": False}
            self._fetched = time.monotonic()
            self._spent_since = max(0.0, self._spent_since - spent_before)
            self.last_fetched_at = time.time()
            self.last_error = None
            return self._stamped(dict(self._balance))

    def _failed(self, reason, stamp=True):
        with self._lock:
            if stamp:
                self._failed_at, self._failure = time.monotonic(), reason
            self.last_error = reason
            snapshot = dict(self._balance) if self._balance else {
                "available": None, "unit": "minara_credits", "balance_supported": True, "session_used": None,
                "unreported_usage": True}
            snapshot.update(error=reason, stale=self._balance is not None)
            return self._stamped(snapshot)

    def balance_snapshot(self):
        """The last server balance less the costs reported since: no network."""
        with self._lock:
            if self._balance is None:
                return self._stamped({"available": None, "unit": "minara_credits", "balance_supported": True})
            snapshot = dict(self._balance)
            snapshot["available"] = round(snapshot["available"] - self._spent_since, 4)
            return self._stamped(snapshot)

    def _account(self, row, sign=1):
        super()._account(row, sign)
        cost = row.get("provider_cost")
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            self._spent_since += sign * cost * CREDITS_PER_USD


class AccountDispatcher:
    def __init__(self, backends, cfg, managers, *, clock=time.monotonic, rng=None, wall=time.time):
        self.backends = list(backends)
        self.managers = dict(managers)
        self.interval = float(cfg.get("session_refresh_interval", 900) or 0)
        self.threshold = float(cfg.get("min_account_balance", 20))
        # Minara credits are another unit (220 per US dollar): a turn on a long Claude context costs 5-50 of them.
        # 0: an account takes turns until Minara refuses one (402), and that request moves to another account at once
        # (mark_minara_refusal). A floor of 15 left up to 15 credits on each of 51 accounts.
        self.minara_threshold = float(cfg.get("minara_min_credits", 0))
        self.balance_interval = float(cfg.get("account_balance_interval", 300) or 0)
        self.rate_limit_cooldown = min(max(float(cfg.get("account_rate_limit_cooldown") or RATE_LIMIT_COOLDOWN),
                                           1.0), RATE_LIMIT_MAX)
        self.plan_refusal_first = PLAN_REFUSAL_FIRST
        self.plan_refusal_hold = PLAN_REFUSAL_HOLD
        self.parallel = max(1, int(cfg.get("account_refresh_parallel", 3) or 1))
        self.clock = clock
        self.wall = wall  # token expiry is a moment in epoch seconds, not on the monotonic clock
        self.rng = rng or random.Random()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._executor = None
        self._busy = 0
        self._network_failures = 0
        self.pause_until = float("-inf")
        now = clock()
        self.state = {}
        for index, backend in enumerate(self.backends):
            offset = min(index * STARTUP_SPREAD, 60.0)
            self.state[backend] = {"next_refresh": now + offset, "next_balance": now + offset + 1.0,
                                   "auth_failures": 0, "soft_failures": 0, "running": set(), "reasons": {},
                                   "last_forced": float("-inf"), "forced": None, "available": None,
                                   "expiry_warned": None, "plan_refusals": {}, "plan_released": {}}
            backend.unavailable = None
            backend.refused_models = {}
            if self.managers.get(backend) is not None:
                backend.credits = self.managers[backend]
            refresher = getattr(backend, "refresher", None)
            if refresher is not None:
                refresher.keepalive_owner = self

    def add_account(self, backend, manager):
        """Take one account into the live routing pool, its state built exactly as __init__ builds the rest.

        Its first check is due at once (a balance read, or the expiry its token names), so a wrong sign-in or an empty
        account shows on the status page within seconds instead of after a restart."""
        with self._lock:
            if backend in self.state:
                return
            now = self.clock()
            self.state[backend] = {"next_refresh": now, "next_balance": now, "auth_failures": 0,
                                   "soft_failures": 0, "running": set(), "reasons": {},
                                   "last_forced": float("-inf"), "forced": None, "available": None,
                                   "expiry_warned": None, "plan_refusals": {}, "plan_released": {}}
            backend.unavailable = None
            backend.refused_models = {}
            if manager is not None:
                self.managers[backend] = manager
                backend.credits = manager
            refresher = getattr(backend, "refresher", None)
            if refresher is not None:
                refresher.keepalive_owner = self
            # A new list: reports read it without the lock (account_report.account_managers).
            self.backends = [*self.backends, backend]

    def check_soon(self, backend):
        """Judge this account again now: its token's expiry at once, its balance at the next tick (the regular check, so
        the admin's request does not wait for Minara). For an account that has just got a new token."""
        self.check_expiry([backend])
        with self._lock:
            state = self.state.get(backend)
            if state is not None:
                state["next_balance"] = min(state["next_balance"], self.clock())

    def remove_account(self, backend):
        """Take one account out of routing; True when it was there. Nothing of its state is read afterwards."""
        with self._lock:
            if backend not in self.state:
                return False
            self.backends = [item for item in self.backends if item is not backend]
            self.state.pop(backend, None)
            self.managers.pop(backend, None)
        return True

    def threshold_of(self, backend):
        """The balance below which this account takes no new turn, in its own unit."""
        cfg = getattr(backend, "cfg", None) or {}
        if cfg.get("upstream") == "minara":
            return self.minara_threshold
        if priced_in_dollars(cfg):
            return usd_floor(cfg)
        return self.threshold

    # -- lifecycle -----------------------------------------------------------
    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.alive():
            return
        with self._lock:
            # Claims of a previous run (jobs cancelled by stop()) must not block scheduling.
            self._busy = 0
            for state in self.state.values():
                state["running"].clear()
        self._stop.clear()
        # Daemon workers: a ThreadPoolExecutor is joined at interpreter exit, and a balance or renewal call under way
        # (10-15 s network timeouts) kept a stopped relay alive after Ctrl+C.
        self._executor = DaemonIOExecutor(self.parallel, thread_name_prefix="account-check")
        self._thread = threading.Thread(target=self._loop, name="account-dispatcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _loop(self):
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(1.0)

    def _tick(self):
        try:
            self.revive_renewed()
            self.release_rate_limited()
            self.release_plan_refusals()
            self.check_expiry()
            for backend, kind in self.due():
                try:
                    self._executor.submit(self._run, backend, kind)
                except Exception as exc:  # noqa: BLE001 - executor shut down: release the claim
                    self._unclaim(backend, kind)
                    journal.emit("account.dispatcher", "Не удалось запустить проверку: %s" % type(exc).__name__,
                                 "warn")
        except Exception as exc:  # noqa: BLE001 - the scheduler must outlive one bad tick
            journal.emit("account.dispatcher", "Ошибка планировщика аккаунтов: %s" % type(exc).__name__, "error")

    def _unclaim(self, backend, kind):
        with self._lock:
            state = self.state.get(backend)
            if state is not None:
                state["running"].discard(kind)
            # Always given back, also for an account removed meanwhile: a slot lost here was lost for every account.
            self._busy = max(0, self._busy - 1)

    # -- scheduling ----------------------------------------------------------
    def _tag(self, backend):
        """The account as a person reads it (status rows, console lines); journal fields use account_tag."""
        return account_tag(backend, UNNAMED)

    def _jitter(self, seconds):
        return seconds * self.rng.uniform(0.9, 1.1)

    def due(self, now=None):
        """Claim due jobs without exceeding the parallel limit."""
        now = self.clock() if now is None else now
        jobs = []
        with self._lock:
            for backend in self.backends:
                if self._busy >= self.parallel:
                    break
                state = self.state[backend]
                if state["running"] or now < self.pause_until or not enabled(backend):
                    # A switched-off account is only checked on an explicit admin request.
                    continue
                manager = self.managers.get(backend)
                snapshot = getattr(manager, "balance_snapshot", None)
                if (snapshot and "exhausted" not in state["reasons"] and "auth_dead" not in state["reasons"]
                        and now - state["last_forced"] >= FORCED_CHECK_GAP):
                    # A local debit crossed the threshold: confirm with the server now.
                    local = console_log.number(snapshot().get("available"), signed=True)
                    if local is not None and (local <= 0 or local < self.threshold_of(backend)):
                        state["next_balance"] = min(state["next_balance"], now)
                        state["last_forced"] = now
                if (getattr(backend, "refresher", None) is not None
                        and self.interval > 0 and now >= state["next_refresh"]):
                    kind = "refresh"
                elif (manager is not None and getattr(manager, "supports_balance", True)
                      and now >= state["next_balance"]
                      and not ("auth_dead" in state["reasons"] and self.interval > 0
                               and state["reasons"]["auth_dead"].get("error") != "token_rejected")):
                    # A signed-out account's balance fetch meets a 401 and renews the session itself (Refresher.call): one
                    # POST /api/refreshtoken a minute, past the backoff of the renewals above. Its balance is checked
                    # again as soon as a renewal brings it back (next_balance is in the past by then).
                    kind = "balance"
                else:
                    continue
                state["running"].add(kind)
                self._busy += 1
                jobs.append((backend, kind))
        return jobs

    def _run(self, backend, kind):
        try:
            if kind == "refresh":
                self.refresh(backend)
            else:
                self.check_balance(backend)
        except Exception as exc:  # noqa: BLE001 - one account must not stop the others
            with self._lock:
                state = self.state.get(backend)
                if state is not None:
                    state["next_refresh" if kind == "refresh" else "next_balance"] = self.clock() + 60.0
            if state is not None:  # an account removed while its check ran has nothing left to report
                journal.emit("account.dispatcher", "Проверка аккаунта не удалась: %s" % type(exc).__name__, "warn",
                             account=account_tag(backend), job=kind)
        finally:
            self._unclaim(backend, kind)

    # -- state ---------------------------------------------------------------
    def _set_reason(self, backend, reason, details):
        """Returns True when the reason the account is out of the pool changed: it left, came back, or is out for another
        reason now. Caller holds the lock."""
        state = self.state[backend]
        before = backend.unavailable
        if details is None:
            state["reasons"].pop(reason, None)
        else:
            state["reasons"][reason] = dict(details, reason=reason)
        reasons = state["reasons"]
        # A parked account is never a reserve candidate, even when its balance is also low; nor is a signed-out one, whose
        # requests all fail. Below the balance threshold too, a signed-out account used to show as "exhausted": it stayed a
        # reserve, the richest one took every turn (a 401, then a failover logged as an empty balance), the alert that it
        # needs a new sign-in never came, and a lone such account answered 402 "top up" instead of 503.
        backend.unavailable = reasons.get("rate_limited") or reasons.get("auth_dead") or reasons.get("exhausted")
        return (before or {}).get("reason") != (backend.unavailable or {}).get("reason")

    def _announce(self, backend, changed, reason=None):
        """`reason` is the state captured under the lock that changed it."""
        if not changed:
            return
        label, tag = self._tag(backend), account_tag(backend)
        if reason is None:
            text = "[account] %s снова в пуле" % label
            journal.emit("account.available", text, "success", account=tag)
        elif reason["reason"] == "rate_limited":
            return  # mark_rate_limited says it itself, with the time the account comes back
        elif reason["reason"] == "exhausted":
            threshold = self.threshold_of(backend)
            cfg = getattr(backend, "cfg", None) or {}
            unit = (" кредитов Minara" if cfg.get("upstream") == "minara" else " USD" if priced_in_dollars(cfg) else "")
            text = "[account] %s исключён: баланс %s < %s%s" % (
                label, console_log.amount(reason.get("available"), signed=True), console_log.amount(threshold), unit)
            journal.emit("account.exhausted", text, "warn", account=tag, available=reason.get("available"),
                         threshold=threshold)
        elif reason.get("error") == "token_rejected":
            text = "[account] %s исключён: Minara отклонила токен (истёк или отозван), нужен новый токен" % label
            journal.emit("account.auth_dead", text, "error", account=tag, error="token_rejected",
                         status=reason.get("status"))
        elif reason.get("error") == "token_expired":
            text = "[account] %s исключён: токен Minara истёк %s, нужен новый токен" % (
                label, _local_time(reason["expires_at"]))
            journal.emit("account.auth_dead", text, "error", account=tag, error="token_expired",
                         expires_at=reason["expires_at"])
        else:
            text = "[account] %s исключён: сессия не продлевается, нужен новый вход" % label
            journal.emit("account.auth_dead", text, "error", account=tag, error=reason.get("error"))
        console_log.write(text)

    def check_expiry(self, backends=None, now=None):
        """Judge the Minara accounts' Bearer tokens by the expiry written in them; returns the accounts whose token expired.

        An expired token takes its account out of the pool as auth_dead with no request to Minara (it would answer 401),
        and the account comes back if it gets a token that has not expired. A token with less than three days left is
        announced in the journal and on the console, at most once a day per account. A token whose expiry cannot be read
        changes nothing."""
        now = self.wall() if now is None else now
        expired, changed, expiring = [], [], []
        with self._lock:
            for backend in self.backends if backends is None else backends:
                state = self.state.get(backend)
                read = getattr(self.managers.get(backend), "expires_at", None)
                if state is None or read is None:
                    continue
                expiry = read()
                if type(expiry) not in (int, float) or not math.isfinite(expiry):
                    expiry = None  # unknown: nothing is judged by it
                dead = state["reasons"].get("auth_dead")
                if expiry is not None and now >= expiry:
                    expired.append(backend)
                    if dead is None or dead.get("expires_at") != expiry:
                        if self._set_reason(backend, "auth_dead", {"error": "token_expired", "expires_at": expiry}):
                            changed.append((backend, backend.unavailable))
                    continue
                if dead is not None and dead.get("error") == "token_expired":
                    # The account has a token that is still valid now: back to the pool, its balance read at once.
                    state["next_balance"] = min(state["next_balance"], self.clock())
                    if self._set_reason(backend, "auth_dead", None):
                        changed.append((backend, backend.unavailable))
                warned = state["expiry_warned"]
                if (expiry is not None and expiry - now < minara_accounts.WARN_SECONDS and enabled(backend)
                        and (warned is None or now - warned >= EXPIRY_WARN_EVERY)):
                    state["expiry_warned"] = now
                    expiring.append((backend, expiry))
        for backend, reason in changed:
            self._announce(backend, True, reason)
        for backend, expiry in expiring:
            text = "[account] %s: токен Minara истекает %s (через %s), нужен новый токен" % (
                self._tag(backend), _local_time(expiry), _time_left(expiry - now))
            journal.emit("account.token_expiring", text, "warn", account=account_tag(backend), expires_at=expiry,
                         days_left=round((expiry - now) / 86400, 2))
            console_log.write(text)
        return expired

    def refresh(self, backend):
        refresher = backend.refresher
        detailed = getattr(refresher, "refresh_detailed", None)
        if detailed is not None:
            ok, kind, error = detailed(force=True)
        else:
            ok = refresher.refresh(force=True)
            kind, error = getattr(refresher, "last_failure_kind", None), getattr(refresher, "last_error", None)
        now = self.clock()
        changed = False
        with self._lock:
            state = self.state[backend]
            if ok:
                self._network_failures = 0
                state["auth_failures"] = 0
                state["soft_failures"] = 0
                state["next_refresh"] = now + self._jitter(self.interval)
                changed = self._set_reason(backend, "auth_dead", None)
            elif kind == "network":
                # The network is down, not this account's session: every account waits
                # together and nobody is marked signed out.
                if now >= self.pause_until:
                    # Parallel checks failing together count as one outage.
                    self._network_failures += 1
                pause = min(20.0 * 2 ** min(max(self._network_failures, 1) - 1, 10), NETWORK_PAUSE_MAX)
                self.pause_until = max(self.pause_until, now + pause)
                state["next_refresh"] = self.pause_until
            elif kind != "auth":
                # Retool (5xx) or local storage failed for this account only: back off
                # this account and keep renewing the others.
                state["soft_failures"] += 1
                pause = min(20.0 * 2 ** min(state["soft_failures"] - 1, 10), NETWORK_PAUSE_MAX)
                state["next_refresh"] = now + self._jitter(pause)
            else:
                state["auth_failures"] += 1
                failures = state["auth_failures"]
                state["next_refresh"] = now + self._jitter(min(20.0 * 2 ** min(failures - 1, 10), AUTH_RETRY_MAX))
                if failures >= DEAD_AFTER:
                    changed = self._set_reason(backend, "auth_dead",
                                               {"error": error, "failures": failures,
                                                "renewed_at": getattr(refresher, "last_success_at", None)})
            reason = backend.unavailable
        self._announce(backend, changed, reason)
        return ok

    def revive_renewed(self):
        """A request-path renewal (401 retry, new sign-in in config.json) also
        proves the session is alive: return such accounts to the pool at once."""
        revived = []
        with self._lock:
            for backend in self.backends:
                dead = self.state[backend]["reasons"].get("auth_dead")
                renewed = getattr(getattr(backend, "refresher", None), "last_success_at", None)
                if dead and renewed is not None and renewed != dead.get("renewed_at"):
                    self.state[backend]["auth_failures"] = 0
                    if self._set_reason(backend, "auth_dead", None):
                        revived.append((backend, backend.unavailable))
        for backend, reason in revived:
            self._announce(backend, True, reason)
        return bool(revived)

    def mark_rate_limited(self, backend, retry_after=None, request_id=None, sent_at=None):
        """Park an account that spent Retool's hourly managed-key quota; returns when it comes back.

        The pause is `Retry-After` when Retool sends one, else `account_rate_limit_cooldown`. The limit slides, so an
        account that fails again within as long as it was parked (the first real request after a return is its
        probe) waits five minutes longer each time, up to the larger of the cooldown and twenty minutes. A hint is
        honoured as it is and neither advances nor resets that ladder. Turns that fail together share one pause and
        one journal line. A limit reported by a request that was sent (`sent_at`, on this dispatcher's clock)
        before the account's last return is old news: it changes nothing and returns None.
        """
        if backend not in self.state:
            return None
        now = self.clock()
        with self._lock:
            state = self.state[backend]
            hold = state["reasons"].get("rate_limited")
            if hold is not None and now < hold["until"]:
                return hold["until_at"]
            # A hold the scheduler has not released yet still ended at its own deadline.
            returned = state.get("rl_released", float("-inf")) if hold is None else hold["until"]
            if type(sent_at) in (int, float) and sent_at < returned:
                return None
            previous = state.get("rl_pause", 0.0)
            hint = retry_after if isinstance(retry_after, (int, float)) and 0 < retry_after < math.inf else None
            if hint is not None:
                pause, source = min(max(float(hint), 1.0), RATE_LIMIT_MAX), "retry_after"
            elif previous and now - returned < previous:
                ceiling = max(self.rate_limit_cooldown, RATE_LIMIT_CEILING)
                pause, source = min(previous + RATE_LIMIT_STEP, ceiling), "escalated"
            else:
                pause, source = self.rate_limit_cooldown, "cooldown"
            if source != "retry_after":
                state["rl_pause"] = pause
            until_at = time.time() + pause
            self._set_reason(backend, "rate_limited", {"until": now + pause, "until_at": until_at,
                                                       "pause": pause, "source": source})
        text = "[account] %s исключён: часовой лимит Retool до %s (пауза %s%s)" % (
            self._tag(backend), time.strftime("%H:%M:%S", time.localtime(until_at)), apierr.wait_text(pause),
            ", повторный лимит подряд" if source == "escalated" else "")
        journal.emit("account.rate_limited", text, "warn", account=account_tag(backend), until=round(until_at, 3),
                     retry_in=pause, source=source, **({"request_id": request_id} if request_id else {}))
        console_log.write(text)
        return until_at

    def release_rate_limited(self, now=None):
        """Return accounts whose pause is over to the pool; the scheduler calls this every tick."""
        now = self.clock() if now is None else now
        released = []
        with self._lock:
            for backend in self.backends:
                state = self.state[backend]
                hold = state["reasons"].get("rate_limited")
                if hold is None or now < hold["until"]:
                    continue
                state["rl_released"] = now
                if self._set_reason(backend, "rate_limited", None):
                    # Back in the pool, or out for another reason it had meanwhile (a low balance, a dead session).
                    released.append((backend, backend.unavailable))
        for backend, reason in released:
            self._announce(backend, True, reason)
        return bool(released)

    def mark_plan_refusal(self, backend, model, request_id=None):
        """Hold this account back from `model` alone after Retool's 402 "This model is not available for your plan."; returns
        when the account is asked for that model again (epoch seconds), None for an account or a model it cannot hold.

        The account keeps serving every other model: a client that asks for a model no plan offers takes no account out of
        the pool for anything else. The pool reads the holds from `backend.refused_models` (SessionPool._plan_hold): a
        conversation of this model bound to the account moves at its next request, and when every account that serves the
        model is held, the request gets the pool's settled 404. Turns that fail together share one hold and one journal
        line. The hold ends by itself (release_plan_refusals, every tick): the next request of the model asks the account
        again. Its length is a ladder of two steps: `plan_refusal_first` for a pair (account, model), and
        `plan_refusal_hold` for a refusal that comes within that long a time after the pair's last hold ended, and for each
        one after it. A fault of Retool's that refuses every account for a few minutes so holds the pool for five minutes,
        not an hour. Nothing of it outlives a restart.
        """
        if not isinstance(model, str) or not model:
            return None
        now = self.clock()
        with self._lock:
            state = self.state.get(backend)  # read under the lock: the admin may remove the account meanwhile
            if state is None:
                return None
            holds, ended_at = state["plan_refusals"], state["plan_released"]
            hold = holds.get(model)
            if hold is not None and now < hold["until"]:
                return hold["until_at"]
            # When the pair's last hold ended: one the scheduler has not released yet still ended at its own deadline.
            ended = hold["until"] if hold is not None else ended_at.get(model)
            repeated = ended is not None and now - ended <= self.plan_refusal_hold
            pause = self.plan_refusal_hold if repeated else self.plan_refusal_first
            until_at = time.time() + pause
            ended_at.pop(model, None)
            holds[model] = {"until": now + pause, "until_at": until_at, "pause": pause, "repeated": repeated}
            self._publish_plan_refusals(backend)
        text = "[account] %s исключён для %s: модель не входит в тариф Retool (402) до %s (пауза %s%s)" % (
            self._tag(backend), model, time.strftime("%H:%M:%S", time.localtime(until_at)), apierr.wait_text(pause),
            ", повторный отказ подряд" if repeated else "")
        journal.emit("account.model_refused", text, "warn", account=account_tag(backend), model=model,
                     until=round(until_at, 3), retry_in=pause, repeated=repeated,
                     **({"request_id": request_id} if request_id else {}))
        console_log.write(text)
        return until_at

    def release_plan_refusals(self, now=None):
        """End the holds whose time is over, so that the next request of the model asks the account again; the scheduler
        calls this every tick. True when a hold ended.

        When a hold ended stays in memory (`plan_released`, one entry per pair) for as long as a refusal then counts as a
        repeated one (mark_plan_refusal), and no longer."""
        now = self.clock() if now is None else now
        released = []
        with self._lock:
            for backend in self.backends:
                state = self.state[backend]
                holds, ended_at = state["plan_refusals"], state["plan_released"]
                ended = [model for model, hold in holds.items() if now >= hold["until"]]
                for model in ended:
                    ended_at[model] = holds.pop(model)["until"]
                    released.append((backend, model))
                for model in [model for model, at in ended_at.items() if now - at > self.plan_refusal_hold]:
                    del ended_at[model]
                if ended:
                    self._publish_plan_refusals(backend)
        for backend, model in released:
            journal.emit("account.model_retry",
                         "[account] %s: пауза для %s закончилась, следующий запрос этой модели снова спросит аккаунт" % (
                             self._tag(backend), model), "info", account=account_tag(backend), model=model)
        return bool(released)

    def _publish_plan_refusals(self, backend):
        """Caller holds the lock. The pool and the admin read a new dict each time, never one that changes under them."""
        backend.refused_models = {model: {"until_at": hold["until_at"], "pause": hold["pause"]}
                                  for model, hold in self.state[backend]["plan_refusals"].items()}

    def confirm_minara_token(self, backend):
        """A chat request of this Minara account was answered 401/403: read /auth/me now (check_balance marks the account
        token_rejected when it refuses the token too). True when the account is out of the pool afterwards."""
        if backend not in self.state:
            return False
        self.check_balance(backend)
        return getattr(backend, "unavailable", None) is not None

    def mark_minara_refusal(self, backend, error):
        """Minara refused a request of this account: 402 takes it out as exhausted, 401/403 as auth_dead (the token was
        revoked or expired before the date written in it). True when it is out of the pool now, for the caller to move
        the request. No balance read is needed: the regular one brings the account back once it shows more credits than
        at the refusal (MinaraAccount.note_refusal), or once its token is accepted again."""
        if backend not in self.state:
            return False
        rejected = getattr(error, "code", None) == "upstream_auth_error"
        manager = self.managers.get(backend)
        if not rejected and getattr(manager, "note_refusal", None) is not None:
            manager.note_refusal()  # the transport did it already; again is harmless and covers another transport
        with self._lock:
            state = self.state[backend]
            if rejected:
                changed = self._set_reason(backend, "auth_dead", {"error": "token_rejected",
                                                                  "status": getattr(error, "upstream_status", None)})
                state["next_balance"] = self.clock() + self._jitter(TOKEN_RECHECK)
            else:
                available = state["available"]
                changed = self._set_reason(backend, "exhausted", {
                    "available": min(available, 0.0) if isinstance(available, (int, float)) else 0.0,
                    "threshold": self.threshold_of(backend), "refused": True})
            reason = backend.unavailable
        self._announce(backend, changed, reason)
        return True

    def recheck_exhausted(self, entry=None):
        """Before answering 402, confirm excluded balances (a top-up is adopted at once).

        Only the accounts of the request's provider are read (`entry`, the model entry: a minara/* request never reads
        Retool's accounts, nor the reverse), several at once: one after another, 51 spent Minara accounts took 15 s
        before the 402 could be answered."""
        minara = (entry or {}).get("upstream") == "minara" if entry is not None else None
        with self._lock:
            exhausted = [b for b in self.backends if "exhausted" in self.state[b]["reasons"] and enabled(b)
                         and (minara is None or ((getattr(b, "cfg", None) or {}).get("upstream") == "minara") == minara)]
        if not exhausted:
            return False
        if len(exhausted) == 1:
            return not self.check_balance_now(exhausted[0])
        from io_workers import DaemonIOExecutor
        pool = DaemonIOExecutor(min(8, len(exhausted)), thread_name_prefix="recheck")
        try:
            # every read finishes before the answer (a top-up of any of them is adopted), at once rather than in turn
            return not all(list(pool.map(self.check_balance_now, exhausted)))
        finally:
            pool.shutdown(wait=False)

    def check_balance(self, backend):
        """Fetch this account's balance; True when it is confirmed below the threshold."""
        manager = self.managers.get(backend)
        if manager is None:
            return False
        if backend in self.check_expiry([backend]):
            # An expired token is never sent: the answer would be a 401 and the balance would stay what it was.
            with self._lock:
                state = self.state[backend]
                state["next_balance"] = self.clock() + self._jitter(3600.0)
                return "exhausted" in state["reasons"]
        info = manager.get_balance(force_refresh=True)
        available = console_log.number(info.get("available"), signed=True)
        fresh = available is not None and not info.get("error") and not info.get("stale_error")
        # A Bearer token has no renewal (no refresher): Minara's 401/403 means a new token is needed.
        rejected = (info.get("error") == "upstream_auth_error" and getattr(backend, "refresher", None) is None
                    and (getattr(backend, "cfg", None) or {}).get("upstream") == "minara")
        now = self.clock()
        changed = False
        with self._lock:
            state = self.state[backend]
            state["next_balance"] = now + self._jitter(self.balance_interval if fresh and self.balance_interval > 0
                                                       else TOKEN_RECHECK if rejected
                                                       else 60.0 if not fresh else 3600.0)
            dead = state["reasons"].get("auth_dead") or {}
            if rejected and dead.get("error") != "token_rejected":
                changed = self._set_reason(backend, "auth_dead", {"error": "token_rejected", "status": None})
            elif fresh and dead.get("error") == "token_rejected":
                changed = self._set_reason(backend, "auth_dead", None)  # the token is accepted again
            if fresh:
                state["available"] = available
                # A balance of 0 buys nothing whatever the threshold (min_account_balance 0 left such an account usable).
                if available <= 0 or available < self.threshold_of(backend):
                    changed = self._set_reason(backend, "exhausted",
                                               {"available": available, "threshold": self.threshold_of(backend)}) or changed
                else:
                    changed = self._set_reason(backend, "exhausted", None) or changed
            exhausted = "exhausted" in state["reasons"]
            reason = backend.unavailable
        self._announce(backend, changed, reason)
        return exhausted

    def check_balance_now(self, backend):
        """On-demand confirmation after a failed request, rate-limited per account."""
        if backend not in self.state or self.managers.get(backend) is None:
            return False
        with self._lock:
            state = self.state[backend]
            pending = state["forced"]
            if pending is None:
                if self.clock() - state["last_forced"] < FORCED_CHECK_GAP:
                    return "exhausted" in state["reasons"]
                state["last_forced"] = self.clock()
                pending = state["forced"] = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            # Another request is already confirming this balance: share its result.
            pending.wait(30.0)
            with self._lock:
                return "exhausted" in state["reasons"]
        try:
            return self.check_balance(backend)
        finally:
            with self._lock:
                state["forced"] = None
            pending.set()

    def check_all(self, parallel=None):
        """Read every enabled account's balance now, `parallel` at a time (STARTUP_PARALLEL), and judge each one as the
        regular check does: the first round of a start. Returns how many accounts it checked.

        The regular round reads `account_refresh_parallel` (3) accounts at a time and spreads the first reads over a
        minute: with 483 accounts the empty ones (most of the Minara pool) left the pool one by one for three minutes after
        a start, and the start itself waited a minute more for the banner's own read of every balance (2026-11-13). An
        account claimed here is left alone by the dispatcher's loop until its check is done; the relay stopping ends the
        round between two accounts."""
        claimed = []
        with self._lock:
            for backend in self.backends:
                state = self.state.get(backend)
                manager = self.managers.get(backend)
                if (state is None or state["running"] or not enabled(backend) or manager is None
                        or not getattr(manager, "supports_balance", True)):
                    continue
                state["running"].add("balance")
                claimed.append(backend)

        def check(backend):
            try:
                if not self._stop.is_set():
                    self.check_balance(backend)
            except Exception as exc:  # noqa: BLE001 - one account must not stop the others
                with self._lock:
                    state = self.state.get(backend)
                    if state is not None:
                        state["next_balance"] = self.clock() + 60.0
                journal.emit("account.dispatcher", "Проверка аккаунта не удалась: %s" % type(exc).__name__, "warn",
                             account=account_tag(backend), job="balance")
            finally:
                with self._lock:
                    state = self.state.get(backend)
                    if state is not None:
                        state["running"].discard("balance")

        if claimed:
            workers = DaemonIOExecutor(min(max(1, int(parallel or STARTUP_PARALLEL)), len(claimed)),
                                       thread_name_prefix="account-start")
            try:
                list(workers.map(check, claimed))
            finally:
                workers.shutdown(wait=False)
        return len(claimed)

    def status(self):
        with self._lock:
            return [{"account": self._tag(backend),
                     "state": (backend.unavailable or {}).get("reason", "active"),
                     "available": self.state[backend]["available"],
                     "auth_failures": self.state[backend]["auth_failures"],
                     "rate_limited_until": (self.state[backend]["reasons"].get("rate_limited") or {}).get("until_at")}
                    for backend in self.backends]
