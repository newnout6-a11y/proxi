"""Relay-side authentication and upstream (Retool) auth headers."""
import hmac
import os

from apierr import unauthorized

CLIENT_VERSION = "4.84.0-16f3123 (Build 409813)"


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
    for raw in headers or []:
        pair = raw.split(";")[0]
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        if value == "":
            if jar.pop(key, None) is not None:
                dirty = True
            continue
        if jar.get(key) != value:
            jar[key] = value
            changed = True
            dirty = True
        if key in ("xsrfToken", "__Host-xsrfToken"):
            dirty = dirty or xsrf != value
            changed = changed or xsrf != value
            xsrf = value
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
