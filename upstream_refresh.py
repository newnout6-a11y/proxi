"""Shared Retool authentication: keepalive, rotation persistence and one auth retry."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from auth import merge_set_cookie, upstream_headers
from config import _account_record, persist_auth, reload_auth, renewal_lock
import journal
from redact import redact

MIN_INTERVAL = 20.0


class Refresher:
    def __init__(self, cfg, http_refresh=None):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._last = float("-inf")
        self._last_result = False
        self._http_refresh = http_refresh or self._default
        self.last_error = None
        self.persistence_error = None
        self.last_success_at = None
        self._stop = threading.Event()
        self._thread = None
        self.last_failure_kind = None
        # Set by AccountDispatcher: it schedules renewals instead of this thread.
        self.keepalive_owner = None

    def _default(self):
        req = urllib.request.Request(self.cfg["base"] + "/api/refreshtoken",
                                     data=b"{}", headers=upstream_headers(self.cfg), method="POST")
        with urllib.request.urlopen(req, timeout=min(self.cfg["upstream_timeout"], 15)) as resp:
            # PROTOCOL.md specifies an empty body: an empty or non-JSON reply is a
            # successful renewal, and its Set-Cookie headers must not be lost to a
            # strict parser.
            raw = resp.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            if body.get("error") or body.get("success") is False:
                raise ValueError("Retool rejected session renewal")
            return resp.headers.get_all("set-cookie") or []

    def _persist(self):
        try:
            persist_auth(self.cfg)
            self.persistence_error = None
        except Exception as exc:
            # Keep the working in-memory session; retry persistence next keepalive.
            self.persistence_error = "cannot save renewed session: " + type(exc).__name__

    def refresh(self, force=False, *, rejected_cookie=None) -> bool:
        try:
            with renewal_lock(self.cfg):
                return self._refresh_locked(force, rejected_cookie=rejected_cookie)
        except OSError as exc:
            self.last_error = "cannot coordinate session renewal: " + type(exc).__name__
            self.last_failure_kind = "local"
            self._last_result = False
            return False

    def _refresh_locked(self, force=False, *, rejected_cookie=None) -> bool:
        """Single-flight renewal. A concurrent caller reuses the new credentials."""
        # Multi-account: journal events must say WHICH tenant renewed/failed.
        account_tag = journal.tag(self.cfg.get("account_id") or self.cfg.get("page_uuid"))
        base = self.cfg.get("base")
        # Hostname only: credentials, paths or queries in the URL stay out of logs.
        tenant = (urllib.parse.urlsplit(base).hostname or "") if isinstance(base, str) else ""
        with self._lock:
            if rejected_cookie is not None and self.cfg["cookie"] != rejected_cookie:
                return True
            try:
                if reload_auth(self.cfg):
                    self._last_result = True
                    return True
            except Exception as exc:
                self.persistence_error = "cannot read session configuration: " + type(exc).__name__
            now = time.monotonic()
            if not force and now - self._last < MIN_INTERVAL:
                return self._last_result if rejected_cookie is not None else False
            self._last = now
            try:
                headers = self._http_refresh()
            except urllib.error.HTTPError as exc:
                self.last_error = f"session renewal failed: HTTP {exc.code}"
                self.last_failure_kind = "auth" if exc.code in (401, 403) else "upstream"
                self._last_result = False
                exc.close()
                journal.emit("auth.refresh", "Продление сессии Retool не удалось: HTTP %s" % exc.code, "error",
                             ok=False, status=exc.code, forced=force,
                             account=account_tag, tenant=tenant)
                return False
            except Exception as exc:
                self.last_error = redact(str(exc), cookie_values=self.cfg.get("cookie_values", []))
                self.last_failure_kind = ("network" if isinstance(exc, (urllib.error.URLError, OSError))
                                          else "upstream")
                self._last_result = False
                journal.emit("auth.refresh", "Продление сессии Retool не удалось", "error",
                             ok=False, error=self.last_error, forced=force,
                             account=account_tag, tenant=tenant)
                return False
            merge_set_cookie(self.cfg, headers)
            if "_account_ref" in self.cfg and isinstance(self.cfg["_account_ref"], dict):
                self.cfg["_account_ref"]["cookie"] = self.cfg["cookie"]
                self.cfg["_account_ref"]["xsrf"] = self.cfg["xsrf"]
            elif isinstance(self.cfg.get("accounts"), list):
                try:
                    record = _account_record(self.cfg, self.cfg)
                except ValueError:
                    # Ambiguous in-memory mirror: the renewed root session still works.
                    record = None
                if record is not None:
                    record.update(cookie=self.cfg["cookie"], xsrf=self.cfg["xsrf"])
            self._persist()
            journal.emit("auth.refresh", "Сессия Retool продлена",
                         "warn" if self.persistence_error else "success", ok=True, forced=force,
                         rotated=bool(headers), persistence_error=self.persistence_error,
                         account=account_tag, tenant=tenant)
            self.last_error = None
            self.last_failure_kind = None
            self.last_success_at = time.time()
            self._last_result = True
            # Retool may say "Authenticated, but token was not refreshed" until
            # rotation is due. This is still a successful authentication check.
            return True

    def call(self, request):
        """Run an HTTP operation and replay once after recovering 401/403."""
        cookie = self.cfg["cookie"]
        try:
            return request()
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403) or not self.refresh(rejected_cookie=cookie):
                raise
            exc.close()
        return request()

    def start(self):
        """Keep the session alive even when no client sends model requests."""
        interval = self.cfg.get("session_refresh_interval", 900)
        if not interval or (self._thread and self._thread.is_alive()) or self.keepalive_owner is not None:
            return
        self._stop.clear()

        def keepalive():
            delay = MIN_INTERVAL
            while not self._stop.is_set():
                try:
                    ok = self.refresh()
                except Exception as exc:  # noqa: BLE001 - one failure must not end keepalive
                    self.last_error = "session renewal failed: " + type(exc).__name__
                    ok = False
                wait = interval if ok else delay
                delay = MIN_INTERVAL if ok else min(delay * 2, 300)
                if self._stop.wait(wait):
                    break

        self._thread = threading.Thread(target=keepalive, name="retool-session-keepalive", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=16)

    def status(self):
        owner = self.keepalive_owner
        return {"keepalive": bool(self._thread and self._thread.is_alive()) or bool(
                    owner is not None and owner.alive() and owner.interval > 0),
                "last_success_at": self.last_success_at, "error": self.last_error,
                "persistence_error": self.persistence_error}


def describe(error) -> str:
    return json.dumps({"refresh_error": error}) if isinstance(error, str) else ""
