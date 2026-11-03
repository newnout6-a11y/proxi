"""Account dispatcher: staggered renewal, per-account balance and routing health.

One scheduler thread replaces a keepalive thread per account. First checks are
spread out, at most `account_refresh_parallel` checks run at once and a network
failure pauses every account together, so 20-30 accounts never renew in one
burst. An account whose renewal keeps failing with 401/403, or whose confirmed
balance is below `min_account_balance`, leaves the routing pool until a later
check succeeds.
"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import console_log
import journal

DEAD_AFTER = 3            # consecutive auth failures before an account leaves the pool
FORCED_CHECK_GAP = 15.0   # at most one on-demand balance fetch per account per gap
STARTUP_SPREAD = 2.0      # seconds between accounts' first checks
NETWORK_PAUSE_MAX = 300.0
AUTH_RETRY_MAX = 600.0


class AccountDispatcher:
    def __init__(self, backends, cfg, managers, *, clock=time.monotonic, rng=None):
        self.backends = list(backends)
        self.managers = dict(managers)
        self.interval = float(cfg.get("session_refresh_interval", 900) or 0)
        self.threshold = float(cfg.get("min_account_balance", 20))
        self.balance_interval = float(cfg.get("account_balance_interval", 300) or 0)
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
                                   "auth_failures": 0, "running": set(), "reasons": {},
                                   "last_forced": float("-inf"), "available": None}
            backend.unavailable = None
            if self.managers.get(backend) is not None:
                backend.credits = self.managers[backend]
            refresher = getattr(backend, "refresher", None)
            if refresher is not None:
                refresher.keepalive_owner = self

    # -- lifecycle -----------------------------------------------------------
    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.alive():
            return
        self._stop.clear()
        self._executor = ThreadPoolExecutor(max_workers=self.parallel, thread_name_prefix="account-check")
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
            try:
                self.revive_renewed()
                for backend, kind in self.due():
                    self._executor.submit(self._run, backend, kind)
            except Exception as exc:  # noqa: BLE001 - the scheduler must outlive one bad tick
                journal.emit("account.dispatcher", "Ошибка планировщика аккаунтов: %s" % type(exc).__name__, "error")
            self._stop.wait(1.0)

    # -- scheduling ----------------------------------------------------------
    def _tag(self, backend):
        cfg = getattr(backend, "cfg", None) or {}
        return journal.tag(cfg.get("account_id") or cfg.get("page_uuid")) or "account"

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
                if state["running"] or now < self.pause_until:
                    continue
                manager = self.managers.get(backend)
                snapshot = getattr(manager, "balance_snapshot", None)
                if snapshot and "exhausted" not in state["reasons"] and now - state["last_forced"] >= FORCED_CHECK_GAP:
                    # A local debit crossed the threshold: confirm with the server now.
                    local = console_log.number(snapshot().get("available"), signed=True)
                    if local is not None and local < self.threshold:
                        state["next_balance"] = min(state["next_balance"], now)
                        state["last_forced"] = now
                if self.interval > 0 and now >= state["next_refresh"]:
                    kind = "refresh"
                elif manager is not None and now >= state["next_balance"]:
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
                         account=self._tag(backend), job=kind)
        finally:
            with self._lock:
                self.state[backend]["running"].discard(kind)
                self._busy -= 1

    # -- state ---------------------------------------------------------------
    def _set_reason(self, backend, reason, details):
        """Returns True when routing eligibility changed. Caller holds the lock."""
        state = self.state[backend]
        before = backend.unavailable
        if details is None:
            state["reasons"].pop(reason, None)
        else:
            state["reasons"][reason] = dict(details, reason=reason)
        reasons = state["reasons"]
        backend.unavailable = reasons.get("exhausted") or reasons.get("auth_dead")
        return (before is None) != (backend.unavailable is None)

    def _announce(self, backend, changed, reason=None):
        """`reason` is the state captured under the lock that changed it."""
        if not changed:
            return
        tag = self._tag(backend)
        if reason is None:
            text = "[account] %s снова в пуле" % tag
            journal.emit("account.available", text, "success", account=tag)
        elif reason["reason"] == "exhausted":
            text = "[account] %s исключён: баланс %s < %s" % (
                tag, console_log.amount(reason.get("available"), signed=True), console_log.amount(self.threshold))
            journal.emit("account.exhausted", text, "warn", account=tag, available=reason.get("available"),
                         threshold=self.threshold)
        else:
            text = "[account] %s исключён: сессия не продлевается, нужен новый вход" % tag
            journal.emit("account.auth_dead", text, "error", account=tag, error=reason.get("error"))
        console_log.write(text)

    def refresh(self, backend):
        refresher = backend.refresher
        ok = refresher.refresh(force=True)
        kind = getattr(refresher, "last_failure_kind", None)
        now = self.clock()
        changed = False
        with self._lock:
            state = self.state[backend]
            if ok:
                self._network_failures = 0
                state["auth_failures"] = 0
                state["next_refresh"] = now + self._jitter(self.interval)
                changed = self._set_reason(backend, "auth_dead", None)
            elif kind != "auth":
                # The network, Retool (5xx) or local storage failed, not this account's
                # session: every account waits together and nobody is marked signed out.
                if now >= self.pause_until:
                    # Parallel checks failing together count as one outage.
                    self._network_failures += 1
                pause = min(20.0 * 2 ** (max(self._network_failures, 1) - 1), NETWORK_PAUSE_MAX)
                self.pause_until = max(self.pause_until, now + pause)
                state["next_refresh"] = self.pause_until
            else:
                state["auth_failures"] += 1
                failures = state["auth_failures"]
                state["next_refresh"] = now + self._jitter(min(20.0 * 2 ** (failures - 1), AUTH_RETRY_MAX))
                if failures >= DEAD_AFTER:
                    changed = self._set_reason(backend, "auth_dead",
                                               {"error": getattr(refresher, "last_error", None), "failures": failures,
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

    def recheck_exhausted(self):
        """Before answering 402, confirm excluded balances (a top-up is adopted at once)."""
        with self._lock:
            exhausted = [b for b in self.backends if "exhausted" in self.state[b]["reasons"]]
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
                if available < self.threshold:
                    changed = self._set_reason(backend, "exhausted",
                                               {"available": available, "threshold": self.threshold})
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
            if self.clock() - state["last_forced"] < FORCED_CHECK_GAP:
                return "exhausted" in state["reasons"]
            state["last_forced"] = self.clock()
        return self.check_balance(backend)

    def status(self):
        with self._lock:
            return [{"account": self._tag(backend),
                     "state": (backend.unavailable or {}).get("reason", "active"),
                     "available": self.state[backend]["available"],
                     "auth_failures": self.state[backend]["auth_failures"]}
                    for backend in self.backends]
