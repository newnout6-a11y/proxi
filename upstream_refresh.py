"""Shared Retool authentication: keepalive, rotation persistence and one auth retry."""
import json
import threading
import time
import urllib.error
import urllib.request

from auth import merge_set_cookie, upstream_headers
from config import persist_auth, reload_auth
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
        """Single-flight renewal. A concurrent caller reuses the new credentials."""
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
                self._last_result = False
                exc.close()
                journal.emit("auth.refresh", "Продление сессии Retool не удалось: HTTP %s" % exc.code, "error",
                             ok=False, status=exc.code, forced=force)
                return False
            except Exception as exc:
                self.last_error = redact(str(exc), cookie_values=self.cfg.get("cookie_values", []))
                self._last_result = False
                journal.emit("auth.refresh", "Продление сессии Retool не удалось", "error",
                             ok=False, error=self.last_error, forced=force)
                return False
            merge_set_cookie(self.cfg, headers)
            if "_account_ref" in self.cfg and isinstance(self.cfg["_account_ref"], dict):
                self.cfg["_account_ref"]["cookie"] = self.cfg["cookie"]
                self.cfg["_account_ref"]["xsrf"] = self.cfg["xsrf"]
            if isinstance(self.cfg.get("accounts"), list):
                for acc in self.cfg["accounts"]:
                    if isinstance(acc, dict) and acc.get("page_uuid") == self.cfg.get("page_uuid"):
                        acc["cookie"] = self.cfg["cookie"]
                        acc["xsrf"] = self.cfg["xsrf"]
                        break
            self._persist()
            journal.emit("auth.refresh", "Сессия Retool продлена",
                         "warn" if self.persistence_error else "success", ok=True, forced=force,
                         rotated=bool(headers), persistence_error=self.persistence_error)
            self.last_error = None
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
        if not interval or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()

        def keepalive():
            delay = MIN_INTERVAL
            while not self._stop.is_set():
                ok = self.refresh()
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
        return {"keepalive": bool(self._thread and self._thread.is_alive()),
                "last_success_at": self.last_success_at, "error": self.last_error,
                "persistence_error": self.persistence_error}


def describe(error) -> str:
    return json.dumps({"refresh_error": error}) if isinstance(error, str) else ""
