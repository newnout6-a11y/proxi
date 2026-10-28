"""Upstream session refresh (Retool /api/refreshtoken)."""
import json
import threading
import time
import urllib.error
import urllib.request

from auth import merge_set_cookie, upstream_headers

MIN_INTERVAL = 20.0


class Refresher:
    def __init__(self, cfg, http_refresh=None):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._last = 0.0
        self._http_refresh = http_refresh or self._default
        self.last_error = None

    def _default(self):
        req = urllib.request.Request(self.cfg["base"] + "/api/refreshtoken",
                                     data=b"{}", headers=upstream_headers(self.cfg), method="POST")
        with urllib.request.urlopen(req, timeout=self.cfg["upstream_timeout"]) as resp:
            return resp.headers.get_all("set-cookie") or []

    def refresh(self, force=False) -> bool:
        """Try to renew the session. Returns True when cookies changed."""
        with self._lock:
            now = time.time()
            if not force and now - self._last < MIN_INTERVAL:
                return False
            self._last = now
            try:
                headers = self._http_refresh()
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read()[:160].decode("utf-8", "replace")
                except Exception:
                    pass
                self.last_error = f"HTTP {exc.code}: {body}"
                return False
            except Exception as exc:
                self.last_error = str(exc)
                return False
            self.last_error = None
            return merge_set_cookie(self.cfg, headers)


def describe(error) -> str:
    return json.dumps({"refresh_error": error}) if isinstance(error, str) else ""
