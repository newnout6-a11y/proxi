"""Relay-side authentication and upstream (Retool) auth headers."""
import hmac
import os
import re
import threading
import time

from apierr import unauthorized

CLIENT_VERSION = "4.84.0-16f3123 (Build 409813)"
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
_CHROME_MAJOR = re.compile(r"\bChrome/(\d+)")
# Checked in this order: an Android user agent names Linux too, and Chrome OS names X11.
_PLATFORMS = (("Windows", '"Windows"'), ("Android", '"Android"'), ("CrOS", '"Chrome OS"'),
              ("Macintosh", '"macOS"'), ("Mac OS X", '"macOS"'), ("Linux", '"Linux"'))

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


def client_hints(ua, brands=None) -> dict:
    """The Chromium client hints a browser with this User-Agent sends: the same major version, platform and mobile flag.

    `brands` (the account's sec_ch_ua) replaces the brand list. A user agent that is not Chromium (Firefox, Safari)
    sends no client hints at all, so none are made up for it."""
    match = _CHROME_MAJOR.search(ua or "")
    if not brands:
        if match is None:
            return {}
        major = match.group(1)
        brands = '"Google Chrome";v="%s", "Chromium";v="%s", "Not_A Brand";v="24"' % (major, major)
    platform = next((value for needle, value in _PLATFORMS if needle in (ua or "")), '"Windows"')
    return {"sec-ch-ua": brands, "sec-ch-ua-mobile": "?1" if "Mobile" in (ua or "") else "?0",
            "sec-ch-ua-platform": platform}


# A cookie or xsrf token read with its line break (Get-Content -Raw, a hand edit of config.json) made http.client refuse the
# header with a ValueError that quoted the value whole, the session JWT included, and that text reached the startup banner,
# /v1/balance and the completion errors. The ends are trimmed; a control character inside is refused without the value.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _header_secret(value, name, variable):
    if not isinstance(value, str):
        return value
    value = value.strip()
    if _CONTROL.search(value) or any(ord(char) > 0xFF for char in value):
        raise ValueError("the Retool %s holds a line break, a control character or a character outside Latin-1: "
                         "correct it in config.json or %s" % (name, variable))
    return value


def upstream_headers(cfg) -> dict:
    """Retool session headers. Cookie/xsrf stay the source of upstream auth."""
    cfg = dict(cfg)  # read cookie and xsrf from the same credential snapshot
    ua = cfg.get("ua") or DEFAULT_UA
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": cfg["base"],
        "referer": cfg["base"] + "/",
        "user-agent": ua,
        "cookie": _header_secret(cfg["cookie"], "cookie", "RETOOL_COOKIE"),
        "x-xsrf-token": _header_secret(cfg["xsrf"], "xsrf", "RETOOL_XSRF"),
        "x-retool-client-version": CLIENT_VERSION,
        # The version in the hints is the one the User-Agent names (an account with Chrome/141 used to send v="131").
        **client_hints(ua, cfg.get("sec_ch_ua")),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "accept-language": "en-US,en;q=0.9",
        "priority": "u=1, i",
    }


# Cookie values that are just host names (lastSubdomain=tenant.retool.com) are not secrets: masking them hides the tenant.
HOST_NAME = re.compile(r"[A-Za-z0-9.\-]+\.(com|app|io|dev|net|org)")


def secret_values(cookie, extra=()):
    """What the relay masks for a session: every cookie value but a host name, then the other secrets config.load found
    (cfg["_extra_secrets"]: the web search key). A renewal or a new sign-in rebuilds the list with this; it used to keep
    only the raw cookie values, so the search key stopped being masked and the tenant's host name started to be."""
    values = []
    for segment in (cookie or "").split(";"):
        if "=" not in segment:
            continue
        value = segment.split("=", 1)[1].strip()
        if value and not HOST_NAME.fullmatch(value) and value not in values:
            values.append(value)
    values += [value for value in extra or () if isinstance(value, str) and value and value not in values]
    return values


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
        cookie = "; ".join(key + "=" + value for key, value in jar.items())
        cfg.update(cookie=cookie, cookie_values=secret_values(cookie, cfg.get("_extra_secrets", ())), xsrf=xsrf)
    return changed


def mask(key: str) -> str:
    """What the startup banner shows of the relay key (the supervisor keeps stdout in relay.log): its length, and the ends
    of a long key only. It printed a key of up to 12 characters whole and 12 characters of a longer one, so a short key
    stood in the log in clear and one of up to 20 characters left 1 to 8 to guess."""
    if not key:
        return "(none)"
    if len(key) >= 32:
        return "%s…%s (%d characters)" % (key[:4], key[-4:], len(key))
    return "(set, %d characters)" % len(key)


def relay_key_source() -> str:
    return "RELAY_API_KEY env" if os.environ.get("RELAY_API_KEY") else "relay_key.txt/config"
