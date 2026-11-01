"""Relay-side authentication and upstream (Retool) auth headers."""
import hmac
import os
import threading
import time

from apierr import unauthorized

CLIENT_VERSION = "4.84.0-16f3123 (Build 409813)"

# Always-on budget for wrong-key attempts (rate_limit_rps is off by default and
# covers only completions). Memory is bounded: at most one entry per source IP.
_FAILURE_WINDOW = 60.0
_FAILURE_LIMIT = 200
_FAILURES = {}
_FAILURES_LOCK = threading.Lock()


def register_auth_failure(client_ip):
    """Record a failed key attempt for the source address. Returns the seconds a
    repeat attempt must wait when the budget is exhausted, else None."""
    now = time.time()
    with _FAILURES_LOCK:
        stamps = [t for t in _FAILURES.get(client_ip, []) if now - t < _FAILURE_WINDOW]
        stamps.append(now)
        _FAILURES[client_ip] = stamps
        if len(_FAILURES) > 1024:
            for ip in [ip for ip, s in _FAILURES.items() if not s or now - s[-1] > _FAILURE_WINDOW]:
                _FAILURES.pop(ip, None)
        while len(_FAILURES) > 4096:
            # Absolute bound: a flood of fresh IPs must not grow the table forever.
            oldest = min(_FAILURES.items(), key=lambda item: item[1][-1])[0]
            _FAILURES.pop(oldest, None)
        if len(stamps) > _FAILURE_LIMIT:
            return max(1.0, _FAILURE_WINDOW - (now - stamps[0]))
    return None


def client_headers(key=None):
    """Load the local relay credential without requiring upstream config."""
    if key is None:
        key = os.environ.get("RELAY_API_KEY")
        if not key:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay_key.txt")
            try:
                with open(path, encoding="utf-8") as source:
                    key = source.read().strip()
            except FileNotFoundError:
                key = None
    if not isinstance(key, str) or not key.strip():
        raise ValueError("missing relay API key: set RELAY_API_KEY or create relay_key.txt")
    return {"content-type": "application/json",
            "authorization": "Bearer " + key.strip().encode("utf-8").decode("latin-1")}


def extract_key(headers) -> str:
    """Pull a relay key from Authorization, x-api-key or ?key= (handled by caller)."""
    headers = {key.lower(): value for key, value in headers.items()}
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth and not auth.lower().startswith(("basic ", "digest ")):
        # Anthropic clients send the key in x-api-key; some send raw Authorization.
        return auth.strip()
    return (headers.get("x-api-key") or "").strip()


def verify(headers, query_key, expected: str) -> None:
    """Constant-time check of the relay key. Raises 401."""
    presented = extract_key(headers)
    from_header = bool(presented)
    presented = presented or (query_key or "").strip()
    if not presented:
        raise unauthorized()
    # compare_digest refuses non-ASCII str; headers arrive latin-1 decoded, so
    # round-trip back to the original bytes and compare those.
    encoded = presented.encode("latin-1", "replace") if from_header else presented.encode("utf-8")
    if not hmac.compare_digest(encoded, expected.encode("utf-8")):
        raise unauthorized("invalid relay API key")


def upstream_headers(cfg) -> dict:
    """Retool session headers. Cookie/xsrf stay the source of upstream auth."""
    cfg = dict(cfg)  # read cookie and xsrf from the same credential snapshot
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": cfg["base"],
        "referer": cfg["base"] + "/",
        "user-agent": cfg["ua"],
        "cookie": cfg["cookie"],
        "x-xsrf-token": cfg["xsrf"],
        "x-retool-client-version": CLIENT_VERSION,
    }


def merge_set_cookie(cfg, headers) -> bool:
    """Apply Set-Cookie values from a refresh response onto the in-memory config.

    Returns True when the access token (or xsrf token) changed.
    """
    jar = {}
    for segment in cfg["cookie"].split(";"):
        if "=" in segment:
            key, value = segment.strip().split("=", 1)
            jar[key] = value
    changed = False
    dirty = False
    xsrf = cfg["xsrf"]
    xsrf_set = False
    for raw in headers or []:
        pair = raw.split(";")[0]
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        if value == "":
            if jar.pop(key, None) is not None:
                dirty = True
                if key in ("xsrfToken", "__Host-xsrfToken") and not xsrf_set:
                    # The deleted cookie must not keep a stale xsrf header alive
                    # (unless this same batch already set the other name).
                    xsrf = jar.get("__Host-xsrfToken") or jar.get("xsrfToken") or ""
            continue
        if jar.get(key) != value:
            jar[key] = value
            changed = True
            dirty = True
        if key in ("xsrfToken", "__Host-xsrfToken"):
            dirty = dirty or xsrf != value
            changed = changed or xsrf != value
            xsrf = value
            xsrf_set = True
    if dirty:
        cfg.update(cookie="; ".join(key + "=" + value for key, value in jar.items()),
                   cookie_values=list(jar.values()), xsrf=xsrf)
    return changed


def mask(key: str) -> str:
    if not key:
        return "(none)"
    return key[:8] + "…" + key[-4:] if len(key) > 12 else key


def relay_key_source() -> str:
    return "RELAY_API_KEY env" if os.environ.get("RELAY_API_KEY") else "relay_key.txt/config"
