"""Account dispatcher: staggered renewal, per-account balance and routing health.

One scheduler thread replaces a keepalive thread per account. First checks are
spread out, at most `account_refresh_parallel` checks run at once and a network
failure pauses every account together, so 20-30 accounts never renew in one
burst. An account whose renewal keeps failing with 401/403, or whose confirmed
balance is below `min_account_balance`, leaves the routing pool until a later
check succeeds. An account that spent Retool's hourly managed-key quota is parked
for `account_rate_limit_cooldown` seconds and returns by itself.
"""
import math
import collections
import random
import threading
import time

import apierr
import console_log
import journal
from minara_catalog import CREDITS_PER_USD
from io_workers import DaemonIOExecutor

DEAD_AFTER = 3            # consecutive auth failures before an account leaves the pool
FORCED_CHECK_GAP = 15.0   # at most one on-demand balance fetch per account per gap
STARTUP_SPREAD = 2.0      # seconds between accounts' first checks
NETWORK_PAUSE_MAX = 300.0
AUTH_RETRY_MAX = 600.0
PRIORITY_MAX = 99
LABEL_MAX = 40
RATE_LIMIT_COOLDOWN = 900.0  # default pause after Retool's hourly managed-key limit
RATE_LIMIT_MAX = 3600.0      # the limit is a sliding hour: parking an account longer never helps
RATE_LIMIT_STEP = 300.0      # a limit right after the return adds five minutes to the pause ...
RATE_LIMIT_CEILING = 1200.0  # ... up to twenty minutes, or the cooldown itself when that is longer


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
    off the last balance, so an account near its end leaves the pool before its next reply would overdraw it."""
    supports_balance = True
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

    def note_refusal(self):
        """Minara answered 402: the account takes nothing until a later read shows more credits than now."""
        with self._lock:
            total = (self._balance or {}).get("available")
            self._refused_at = (total - self._spent_since) if total is not None else float("inf")

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
        with self._lock:
            fresh = time.monotonic() - self._fetched < self.CACHE_SECONDS
            if self._balance is not None and fresh and not force_refresh:
                return dict(self._balance)
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
            available = plan + extra
            if self._refused_at is not None:
                if available > self._refused_at + 1 and self._refused_at != float("inf"):
                    self._refused_at = None  # topped up since the refusal
                else:
                    available = min(available, 0.0)
            self._balance = {"available": round(available, 4), "plan": round(plan, 4), "extra": round(extra, 4),
                             "monthly_allocation": capacity, "unit": "minara_credits", "balance_supported": True,
                             "usd": round(available / CREDITS_PER_USD, 4), "session_used": None,
                             "refused": self._refused_at is not None,
                             "stale": False, "unreported_usage": False}
            self._fetched = time.monotonic()
            self._spent_since = 0.0
            self.last_fetched_at = time.time()
            self.last_error = None
            return dict(self._balance)

    def _failed(self, reason):
        with self._lock:
            self.last_error = reason
            snapshot = dict(self._balance) if self._balance else {
                "available": None, "unit": "minara_credits", "balance_supported": True, "session_used": None,
                "unreported_usage": True}
            snapshot.update(error=reason, stale=self._balance is not None)
            return snapshot

    def balance_snapshot(self):
        """The last server balance less the costs reported since: no network."""
        with self._lock:
            if self._balance is None:
                return {"available": None, "unit": "minara_credits", "balance_supported": True}
            snapshot = dict(self._balance)
            snapshot["available"] = round(snapshot["available"] - self._spent_since, 4)
            return snapshot

    def _account(self, row, sign=1):
        super()._account(row, sign)
        cost = row.get("provider_cost")
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            self._spent_since += sign * cost * CREDITS_PER_USD


class AccountDispatcher:
    def __init__(self, backends, cfg, managers, *, clock=time.monotonic, rng=None):
        self.backends = list(backends)
        self.managers = dict(managers)
        self.interval = float(cfg.get("session_refresh_interval", 900) or 0)
        self.threshold = float(cfg.get("min_account_balance", 20))
        # Minara credits are another unit (220 per US dollar): a turn on a long Claude context costs 5-50 of them.
        self.minara_threshold = float(cfg.get("minara_min_credits", 15))
        self.balance_interval = float(cfg.get("account_balance_interval", 300) or 0)
        self.rate_limit_cooldown = min(max(float(cfg.get("account_rate_limit_cooldown") or RATE_LIMIT_COOLDOWN),
                                           1.0), RATE_LIMIT_MAX)
        self.parallel = max(1, int(cfg.get("account_refresh_parallel", 3) or 1))
        self.clock = clock
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
                                   "last_forced": float("-inf"), "forced": None, "available": None}
            backend.unavailable = None
            if self.managers.get(backend) is not None:
                backend.credits = self.managers[backend]
            refresher = getattr(backend, "refresher", None)
            if refresher is not None:
                refresher.keepalive_owner = self

    def threshold_of(self, backend):
        """The balance below which this account takes no new turn, in its own unit."""
        if (getattr(backend, "cfg", None) or {}).get("upstream") == "minara":
            return self.minara_threshold
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
            self.state[backend]["running"].discard(kind)
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
                if snapshot and "exhausted" not in state["reasons"] and now - state["last_forced"] >= FORCED_CHECK_GAP:
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
                      and not ("auth_dead" in state["reasons"] and self.interval > 0)):
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
                key = "next_refresh" if kind == "refresh" else "next_balance"
                self.state[backend][key] = self.clock() + 60.0
            journal.emit("account.dispatcher", "Проверка аккаунта не удалась: %s" % type(exc).__name__, "warn",
                         account=account_tag(backend), job=kind)
        finally:
            with self._lock:
                self.state[backend]["running"].discard(kind)
                self._busy = max(0, self._busy - 1)

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
            unit = " кредитов Minara" if (getattr(backend, "cfg", None) or {}).get("upstream") == "minara" else ""
            text = "[account] %s исключён: баланс %s < %s%s" % (
                label, console_log.amount(reason.get("available"), signed=True), console_log.amount(threshold), unit)
            journal.emit("account.exhausted", text, "warn", account=tag, available=reason.get("available"),
                         threshold=threshold)
        else:
            text = "[account] %s исключён: сессия не продлевается, нужен новый вход" % label
            journal.emit("account.auth_dead", text, "error", account=tag, error=reason.get("error"))
        console_log.write(text)

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

    def recheck_exhausted(self):
        """Before answering 402, confirm excluded balances (a top-up is adopted at once)."""
        with self._lock:
            exhausted = [b for b in self.backends if "exhausted" in self.state[b]["reasons"] and enabled(b)]
        restored = False
        for backend in exhausted:
            if not self.check_balance_now(backend):
                restored = True
        return restored

    def check_balance(self, backend):
        """Fetch this account's balance; True when it is confirmed below the threshold."""
        manager = self.managers.get(backend)
        if manager is None:
            return False
        info = manager.get_balance(force_refresh=True)
        available = console_log.number(info.get("available"), signed=True)
        fresh = available is not None and not info.get("error") and not info.get("stale_error")
        now = self.clock()
        changed = False
        with self._lock:
            state = self.state[backend]
            state["next_balance"] = now + self._jitter(self.balance_interval if fresh and self.balance_interval > 0
                                                       else 60.0 if not fresh else 3600.0)
            if fresh:
                state["available"] = available
                # A balance of 0 buys nothing whatever the threshold (min_account_balance 0 left such an account usable).
                if available <= 0 or available < self.threshold_of(backend):
                    changed = self._set_reason(backend, "exhausted",
                                               {"available": available, "threshold": self.threshold_of(backend)})
                else:
                    changed = self._set_reason(backend, "exhausted", None)
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

    def status(self):
        with self._lock:
            return [{"account": self._tag(backend),
                     "state": (backend.unavailable or {}).get("reason", "active"),
                     "available": self.state[backend]["available"],
                     "auth_failures": self.state[backend]["auth_failures"],
                     "rate_limited_until": (self.state[backend]["reasons"].get("rate_limited") or {}).get("until_at")}
                    for backend in self.backends]
