"""Relay-side authentication and upstream (Retool) auth headers."""
import hmac
import os

from apierr import unauthorized

CLIENT_VERSION = "4.84.0-16f3123 (Build 409813)"


def extract_key(headers) -> str:
    """Pull a relay key from Authorization, x-api-key or ?key= (handled by caller)."""
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth and not auth.lower().startswith(("basic ", "digest ")):
        # Anthropic clients send the key in x-api-key; some send raw Authorization.
        return auth.strip()
    return (headers.get("x-api-key") or "").strip()


def verify(headers, query_key, expected: str) -> None:
    """Constant-time check of the relay key. Raises 401."""
    presented = extract_key(headers) or (query_key or "").strip()
    if not presented:
        raise unauthorized()
    if not hmac.compare_digest(presented, expected):
        raise unauthorized("invalid relay API key")


def upstream_headers(cfg) -> dict:
    """Retool session headers. Cookie/xsrf stay the source of upstream auth."""
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
    for segment in cfg["cookie"].split("; "):
        if "=" in segment:
            key, value = segment.split("=", 1)
            jar[key] = value
    changed = False
    dirty = False
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
            cfg["xsrf"] = value
            changed = True
    if dirty:
        cfg["cookie"] = "; ".join(key + "=" + value for key, value in jar.items())
        cfg["cookie_values"] = list(jar.values())
    return changed


def mask(key: str) -> str:
    if not key:
        return "(none)"
    return key[:8] + "…" + key[-4:] if len(key) > 12 else key


def relay_key_source() -> str:
    return "RELAY_API_KEY env" if os.environ.get("RELAY_API_KEY") else "relay_key.txt/config"
