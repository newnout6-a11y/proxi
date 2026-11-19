"""Relay-side authentication and upstream (Retool) auth headers."""
import collections
import fnmatch
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import threading
import time

import apierr
from apierr import unauthorized

# What Retool's frontend sends as X-Retool-Client-Version: RETOOL_VERSION_STRING of its entry script (4.99.0-dd22f13, Build
# 424025, in the public bundle of 2026-11-18; live answers name the same in x-retool-api-version). The relay sent
# 4.84.0-16f3123 until then, fifteen minor versions behind every browser. The catalog look reads the version of the
# current frontend (retool_catalog.client_version) and set_client_version puts it here; this one serves until then.
CLIENT_VERSION = "4.99.0-dd22f13 (Build 424025)"
_CLIENT_VERSION_SHAPE = re.compile(r"\d+\.\d+\.\d+-[0-9a-f]{4,40} \(Build \d+\)")


def set_client_version(version) -> bool:
    """Send `version` as X-Retool-Client-Version from now on, if it has the shape Retool's frontend gives it."""
    global CLIENT_VERSION
    if not isinstance(version, str) or not _CLIENT_VERSION_SHAPE.fullmatch(version):
        return False
    CLIENT_VERSION = version
    return True


def cloudflare_challenge(status, headers) -> bool:
    """True for Cloudflare's challenge in front of Retool: a 403 with `cf-mitigated: challenge`, as Retool's frontend tells
    it from a refusal of the session (isCloudflareChallenge, CloudflareChallengeError). It concerns the connection (its
    address, its client), not the account's cookies."""
    if status != 403 or not headers:
        return False
    try:
        items = headers.items()
    except AttributeError:
        return False
    return any(str(name).lower() == "cf-mitigated" and str(value).strip().lower() == "challenge" for name, value in items)


DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
_CHROME_MAJOR = re.compile(r"\bChrome/(\d+)")
# Checked in this order: an Android user agent names Linux too, and Chrome OS names X11.
_PLATFORMS = (("Windows", '"Windows"'), ("Android", '"Android"'), ("CrOS", '"Chrome OS"'),
              ("Macintosh", '"macOS"'), ("Mac OS X", '"macOS"'), ("Linux", '"Linux"'))

# Always-on budget for wrong-key attempts (rate_limit_rps is off by default and covers only completions). Memory is bounded:
# at most one entry per source IP, and an entry keeps the last _FAILURE_LIMIT + 1 failures, as far back as the budget looks.
# It used to keep every failure of the window and filter them all under the lock on each new one: a flood levelled off near
# 30,000 stamps, at 2 ms of CPU a wrong key. A source past the budget is refused before its key is compared
# (auth_failure_wait): looked at only after a wrong key, the budget changed the status of the answer and nothing else, and a
# right guess was still let in. Loopback has no budget: every local client shares 127.0.0.1, so once any local process had
# spent it, a local client with a stale key got a 429 with Retry-After, which SDKs retry, instead of the final 401.
_FAILURE_WINDOW = 60.0
_FAILURE_LIMIT = 200
_FAILURES = {}
_FAILURES_LOCK = threading.Lock()


def is_loopback(client_ip) -> bool:
    """127.0.0.0/8 or ::1, also as an IPv4-mapped address (::ffff:127.0.0.1 from a dual-stack listener)."""
    try:
        address = ipaddress.ip_address(str(client_ip).split("%", 1)[0])
    except ValueError:
        return False
    return (getattr(address, "ipv4_mapped", None) or address).is_loopback


def _budget_wait(stamps, now):
    """Seconds until the oldest of the last _FAILURE_LIMIT + 1 failures leaves the window, or None while fewer than that
    fall within it. Counted from the oldest stamp of the window, a wait that a source honoured ended too early."""
    if not stamps or len(stamps) <= _FAILURE_LIMIT:
        return None
    waited = now - stamps[len(stamps) - _FAILURE_LIMIT - 1]
    return max(1.0, _FAILURE_WINDOW - waited) if waited < _FAILURE_WINDOW else None


def auth_failure_wait(client_ip):
    """The seconds a source past its budget must still wait before a key it presents is compared at all, else None."""
    if is_loopback(client_ip):
        return None
    with _FAILURES_LOCK:
        return _budget_wait(_FAILURES.get(client_ip), time.time())


def register_auth_failure(client_ip):
    """Record a failed key attempt for the source address. Returns the seconds a
    repeat attempt must wait when the budget is exhausted, else None. Loopback records nothing."""
    if is_loopback(client_ip):
        return None
    now = time.time()
    with _FAILURES_LOCK:
        stamps = _FAILURES.get(client_ip)
        if stamps is None:
            stamps = _FAILURES[client_ip] = collections.deque()
        stamps.append(now)
        while len(stamps) > _FAILURE_LIMIT + 1:
            stamps.popleft()
        if len(_FAILURES) > 1024:
            for ip in [ip for ip, s in _FAILURES.items() if not s or now - s[-1] > _FAILURE_WINDOW]:
                _FAILURES.pop(ip, None)
        while len(_FAILURES) > 4096:
            # Absolute bound: a flood of fresh IPs must not grow the table forever.
            oldest = min(_FAILURES.items(), key=lambda item: item[1][-1])[0]
            _FAILURES.pop(oldest, None)
        return _budget_wait(stamps, now)


def _configured_relay_key(path):
    """relay_api_key of the config at `path`, read as plain JSON; None for none, an unreadable file or the placeholder."""
    try:
        with open(path, encoding="utf-8-sig") as source:
            saved = json.load(source)
    except (OSError, ValueError):
        return None
    value = saved.get("relay_api_key") if isinstance(saved, dict) else None
    # config.example.json describes the value in angle brackets, and the relay ignores that text (config._placeholder).
    if not isinstance(value, str) or (value.strip().startswith("<") and value.strip().endswith(">")):
        return None
    return value


def client_headers(key=None, config_path=None):
    """Load the local relay credential without requiring upstream config.

    Without `key` it is found where the relay finds its own (config._ensure_relay_key): RELAY_API_KEY, then the
    config's relay_api_key, then relay_key.txt beside that config. The config is config.json beside this file unless
    `config_path` names the one the relay was started with. Nothing is validated or written, and no key is generated.
    """
    if key is None:
        config_path = os.path.abspath(config_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                  "config.json"))
        key = os.environ.get("RELAY_API_KEY") or _configured_relay_key(config_path)
        if not key:
            path = os.path.join(os.path.dirname(config_path), "relay_key.txt")
            try:
                with open(path, encoding="utf-8") as source:
                    key = source.read().strip()
            except FileNotFoundError:
                key = None
    if not isinstance(key, str) or not key.strip():
        raise ValueError("missing relay API key: set RELAY_API_KEY, relay_api_key in the relay's config or "
                         "relay_key.txt beside it (name that config with --config), or pass --key")
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


SHORT_KEY = 32


def short_key_warning(key):
    """The startup warning for a relay key short enough to guess, else None.

    Wrong keys from 127.0.0.1 have no attempt budget (every local client shares that address, and one with a stale key
    used to lock the others out); a tunnel or reverse proxy on this machine makes every remote client 127.0.0.1 too, so
    only the length of the key stands between them and the relay. The generated key (relay_key.txt) is long enough."""
    length = len(key.strip()) if isinstance(key, str) else 0
    if 0 < length < SHORT_KEY:
        return ("the relay key has only %d characters: wrong keys from 127.0.0.1 are not limited, and a tunnel or reverse "
                "proxy on this machine makes every client 127.0.0.1; set one of %d or more in RELAY_API_KEY or "
                "relay_api_key, or remove both to use the generated relay_key.txt" % (length, SHORT_KEY))
    return None


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


def generate_client_key() -> str:
    """Generate a fresh virtual client API key (`sk-proxi-...`)."""
    return "sk-proxi-" + secrets.token_urlsafe(24)


def validate_client_key_entry(entry: dict, where: str = "client_keys") -> dict:
    """Validate and normalize one `client_keys[]` entry."""
    if not isinstance(entry, dict):
        raise ValueError(f"{where} entry must be an object")
    kid = str(entry.get("id") or "").strip()
    if not kid or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", kid) or kid == "master":
        raise ValueError(f"{where}: invalid or reserved client key id '{kid}'")
    raw_key = str(entry.get("key") or "").strip()
    if len(raw_key) < 12 or any(ord(c) < 33 or ord(c) > 126 for c in raw_key):
        raise ValueError(f"{where} ({kid}): 'key' must be at least 12 printable ASCII characters")
    label = str(entry.get("label") or kid).strip()[:64]
    enabled = entry.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError(f"{where} ({kid}): 'enabled' must be a boolean")
    allowed = entry.get("allowed_models")
    if allowed is not None:
        if not isinstance(allowed, list) or not all(isinstance(x, str) and x.strip() for x in allowed):
            raise ValueError(f"{where} ({kid}): 'allowed_models' must be a list of model patterns")
        allowed = [x.strip() for x in allowed]
    rpm = entry.get("rpm_limit")
    if rpm is not None:
        if type(rpm) is not int or rpm < 1 or rpm > 100_000:
            raise ValueError(f"{where} ({kid}): 'rpm_limit' must be an integer from 1 to 100000 (or null)")
    budget = entry.get("max_budget_usd")
    if budget is not None:
        if type(budget) not in (int, float) or isinstance(budget, bool) or not math.isfinite(budget) or budget < 0:
            raise ValueError(f"{where} ({kid}): 'max_budget_usd' must be a non-negative number (or null)")
        budget = round(float(budget), 6)
    # The cost guard's ceilings of this key, in Minara credits (cost_guard): one request, and the key's day.
    minara_limits = {}
    for name in ("max_request_minara", "max_day_minara"):
        value = entry.get(name)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError(f"{where} ({kid}): '{name}' must be a non-negative number (or null)")
        minara_limits[name] = None if value is None else round(float(value), 4)
    confirm = entry.get("allow_cost_confirm", False)
    if type(confirm) is not bool:
        raise ValueError(f"{where} ({kid}): 'allow_cost_confirm' must be a boolean")
    return {
        "id": kid,
        "label": label,
        "key": raw_key,
        "enabled": enabled,
        "allowed_models": allowed,
        "rpm_limit": rpm,
        "max_budget_usd": budget,
        **minara_limits,
        "allow_cost_confirm": confirm,
    }


def model_allowed_for_profile(profile: dict | None, *model_names: str | None) -> bool:
    """True when `profile` is master, has no `allowed_models` restriction, or matches one of `model_names`."""
    if not profile or profile.get("master"):
        return True
    patterns = profile.get("allowed_models")
    if not patterns:
        return True
    candidates = [m.strip().lower() for m in model_names if isinstance(m, str) and m.strip()]
    if not candidates:
        return True
    for pat in patterns:
        low_pat = pat.lower()
        if any(fnmatch.fnmatchcase(c, low_pat) for c in candidates):
            return True
    return False


def conversation_namespace(profile, master_key):
    """Internal partition for a non-master credential. Reusing its label or ID does not inherit old state."""
    if not profile or profile.get("master"):
        return None
    value = json.dumps(["proxi-dialog-v1", profile["id"], profile["key"]]).encode("utf-8")
    return hmac.new(master_key.encode("utf-8"), value, "sha256").hexdigest()[:32]


class ClientKeyManager:
    """Runtimeauthenticator, RPM rate limiter, USD budget enforcer, and usage tracker for `client_keys[]`."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._keys: dict[str, dict] = {}
        self._usage: dict[str, dict] = {}
        self._rpm_stamps: dict[str, collections.deque] = {}
        self._counted: dict[str, str] = {}  # the last request each key was counted for (enforce_request)
        self.reload(cfg.get("client_keys") or [])

    def reload(self, raw_entries: list) -> None:
        normalized: dict[str, dict] = {}
        for idx, item in enumerate(raw_entries or []):
            norm = validate_client_key_entry(item, f"client_keys[{idx}]")
            normalized[norm["id"]] = norm
        with self._lock:
            self._keys = normalized
            for kid in normalized:
                self._usage.setdefault(
                    kid,
                    {"requests": 0, "input_tokens": 0, "output_tokens": 0, "spent_usd": 0.0, "last_used_at": None},
                )

    def authenticate(self, headers, query_key: str | None, master_key: str) -> dict:
        """Verify master `relay_api_key` or an enabled virtual key from `client_keys[]` in constant time."""
        presented = extract_key(headers)
        from_header = bool(presented)
        presented = presented or (query_key or "").strip()
        if not presented:
            raise unauthorized()
        encoded = presented.encode("latin-1", "replace") if from_header else presented.encode("utf-8")

        if hmac.compare_digest(encoded, master_key.encode("utf-8")):
            return {"id": "master", "label": "master", "master": True}

        with self._lock:
            entries = list(self._keys.values())

        matched = None
        for entry in entries:
            if hmac.compare_digest(encoded, entry["key"].encode("utf-8")):
                matched = entry
        if matched is None:
            raise unauthorized("invalid relay API key")
        if not matched.get("enabled", True):
            raise unauthorized("client API key is disabled")
        return dict(matched, master=False)

    def enforce_request(self, profile: dict | None, resolved_model: str, requested_model: str | None = None) -> None:
        """Check `allowed_models`, `max_budget_usd`, and `rpm_limit` for a virtual client key before dispatch."""
        if not profile or profile.get("master"):
            return
        kid = profile["id"]
        if not model_allowed_for_profile(profile, resolved_model, requested_model):
            raise apierr.ApiError(
                403,
                "forbidden",
                f"Client key '{profile.get('label') or kid}' is not permitted to access model '{requested_model or resolved_model}'",
            )

        now_mono = time.monotonic()
        now_wall = time.time()
        # A client request may check several models (a prompt suggestion tries its candidates after the request's own
        # check): each took an RPM slot and counted a request, so one suggestion used two slots or more (audit A07-05).
        import client_context  # imports no project module
        request_id = (client_context.current() or {}).get("request_id")
        with self._lock:
            live = self._keys.get(kid, profile)
            stats = self._usage.setdefault(
                kid,
                {"requests": 0, "input_tokens": 0, "output_tokens": 0, "spent_usd": 0.0, "last_used_at": None},
            )
            max_budget = live.get("max_budget_usd")
            if max_budget is not None and stats["spent_usd"] >= max_budget:
                raise apierr.ApiError(
                    402,
                    "insufficient_credits",
                    f"Client key '{live.get('label') or kid}' has reached its USD budget (${max_budget:.4f})",
                )
            if request_id and self._counted.get(kid) == request_id:
                return  # this request was counted when it was first checked
            rpm = live.get("rpm_limit")
            if rpm is not None and rpm > 0:
                window = self._rpm_stamps.setdefault(kid, collections.deque())
                while window and (now_mono - window[0]) >= 60.0:
                    window.popleft()
                if len(window) >= rpm:
                    wait = max(0.1, 60.0 - (now_mono - window[0]))
                    raise apierr.too_many_requests(
                        f"Client key '{live.get('label') or kid}' exceeded RPM limit ({rpm}/min)",
                        retry_after=wait,
                    )
                window.append(now_mono)
            stats["requests"] += 1
            stats["last_used_at"] = round(now_wall, 3)
            if request_id:
                self._counted[kid] = request_id

    def record_usage(self, key_id: str | None, *, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float | None = None) -> None:
        if not key_id or key_id == "master":
            return
        with self._lock:
            stats = self._usage.get(key_id)
            if stats is None:
                return
            stats["input_tokens"] += max(0, int(input_tokens or 0))
            stats["output_tokens"] += max(0, int(output_tokens or 0))
            if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool) and math.isfinite(cost_usd) and cost_usd > 0:
                stats["spent_usd"] = round(stats["spent_usd"] + float(cost_usd), 8)

    def list_public(self) -> list[dict]:
        """Return safe masked metadata + live usage counters for Admin UI."""
        with self._lock:
            out = []
            for kid, entry in self._keys.items():
                stats = self._usage.get(kid) or {}
                raw_k = entry["key"]
                masked_k = f"{raw_k[:9]}…{raw_k[-4:]}" if len(raw_k) > 16 else mask(raw_k)
                out.append({
                    "id": kid,
                    "label": entry["label"],
                    "key_preview": masked_k,
                    "enabled": entry["enabled"],
                    "allowed_models": entry["allowed_models"],
                    "rpm_limit": entry["rpm_limit"],
                    "max_budget_usd": entry["max_budget_usd"],
                    "max_request_minara": entry.get("max_request_minara"),
                    "max_day_minara": entry.get("max_day_minara"),
                    "allow_cost_confirm": entry.get("allow_cost_confirm", False),
                    "requests": stats.get("requests", 0),
                    "input_tokens": stats.get("input_tokens", 0),
                    "output_tokens": stats.get("output_tokens", 0),
                    "spent_usd": round(stats.get("spent_usd", 0.0), 6),
                    "last_used_at": stats.get("last_used_at"),
                })
            return out
