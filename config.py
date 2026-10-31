"""Configuration loading, environment overrides and validation."""
import json
import math
import os
import re
import secrets
import tempfile

from apierr import service_unavailable

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
KEY_PATH = os.path.join(HERE, "relay_key.txt")

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8787,
    # Keep one upstream thread for one client conversation.  OpenCode sends
    # x-session-affinity/X-Session-Id, which SessionPool uses as the key.
    "session_mode": "per_conversation",
    "strict_models": True,
    "show_unrouted_models": False,
    "max_body_bytes": 32 * 1024 * 1024,
    "max_frame_bytes": 16 * 1024 * 1024,
    "max_workers": 32,
    "max_conversations": 128,
    "conversation_idle_timeout": 900,
    "rate_limit_rps": 0.0,       # optional local quota; upstream limits still apply
    "rate_limit_burst": 64.0,
    "turn_timeout": 900,
    "recv_timeout": 120,
    "upstream_timeout": 120,
    "session_refresh_interval": 900,
    "command_timeout": 300,
    "command_output_limit": 64000,
    "max_exec_turns": 0,          # 0 = без ограничения
    "exec_policy": "allowlist",
    "exec_allowlist": [
        r"^powershell\b", r"^pwsh\b", r"^cmd\b", r"^git\b", r"^ping\b", r"^more\b", r"^dir\b", r"^type\b", r"^findstr\b", r"^where\b",
        r"^rg\b", r"^Get-ChildItem\b", r"^Get-Content\b", r"^Select-String\b",
        r"^echo\b", r"^cls\b",
    ],
    "project_context": True,
    # Sent every turn, so it is capped tighter than a one-off prompt would be.
    "context_max_bytes": 40000,
    "max_prompt_bytes": 0,       # no relay-imposed prompt cap
    "show_exec_output": True,
    "enable_anthropic": True,
    "enable_chat": True,
    "show_thinking": True,
    # OpenAI-compatible clients open a reasoning view only on non-empty text; Opus 5.5
    # reasons silently, so this label is shown when thinking starts ("" disables it).
    "thinking_placeholder": "Модель думает…\n",
    "enable_responses": True,
    "reasoning_effort": "auto",
    # "direct" = plain copilotStreamGPT POST with cached context (fast), "agent" = sandbox WebSocket.
    "transport": "direct",
    "direct_cache": True,
    # Provider caches expire in minutes; re-read the prefix to hold it open.
    "cache_keepwarm_seconds": 240,
    "cache_keepwarm_idle": 1800,
    "agent_mode": 2,
    "model": "claude-opus-5-5",
    "google_provider_resource_name": "",
    "retool_fast_provider_resource_name": "",
}

REQUIRED = ("base", "page_uuid", "ua")

# Cookie values that are just host names are not secrets.
HOSTNAME = re.compile(r"[A-Za-z0-9.\-]+\.(com|app|io|dev|net|org)")


def _read_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise service_unavailable(f"config not found: {path} (copy config.example.json to config.json)")
    except json.JSONDecodeError as exc:
        raise service_unavailable(f"config is not valid JSON: {exc}")


def _ensure_relay_key(cfg):
    key = os.environ.get("RELAY_API_KEY") or cfg.get("relay_api_key")
    if key:
        return key
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    key = "relay-" + secrets.token_urlsafe(24)
    with open(KEY_PATH, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")
    return key


def persist_auth(cfg):
    """Atomically save rotated credentials without writing runtime defaults/secrets."""
    path = cfg.get("_config_path")
    if not path or not cfg.get("_persist_auth", False):
        return
    saved = _read_file(path)
    acc_match = None
    if isinstance(saved.get("accounts"), list):
        for acc in saved["accounts"]:
            if isinstance(acc, dict):
                if cfg.get("page_uuid") and acc.get("page_uuid") == cfg.get("page_uuid"):
                    acc_match = acc
                    break
                elif not cfg.get("page_uuid") and acc.get("base", "").rstrip("/") == cfg["base"].rstrip("/"):
                    acc_match = acc
                    break
    expected_base = (acc_match.get("base") if acc_match and acc_match.get("base") else saved.get("base", "")).rstrip("/")
    if expected_base != cfg["base"].rstrip("/"):
        raise ValueError("session configuration origin changed")
    disk_auth = (acc_match.get("cookie"), acc_match.get("xsrf")) if acc_match else (saved.get("cookie"), saved.get("xsrf"))
    if disk_auth != cfg.get("_disk_auth", disk_auth):
        raise ValueError("session configuration changed during renewal")
    if not saved.get("accounts") or (not acc_match and cfg.get("page_uuid") == saved.get("page_uuid")):
        saved.update(cookie=cfg["cookie"], xsrf=cfg["xsrf"])
    if isinstance(saved.get("accounts"), list):
        for acc in saved["accounts"]:
            if isinstance(acc, dict):
                if cfg.get("page_uuid") and acc.get("page_uuid") == cfg.get("page_uuid"):
                    acc["cookie"] = cfg["cookie"]
                    acc["xsrf"] = cfg["xsrf"]
                    break
                elif not cfg.get("page_uuid") and acc.get("base", "").rstrip("/") == cfg["base"].rstrip("/"):
                    acc["cookie"] = cfg["cookie"]
                    acc["xsrf"] = cfg["xsrf"]
                    break
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path),
                                         prefix=".session-", suffix=".tmp", delete=False) as fh:
            temporary = fh.name
            json.dump(saved, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        cfg["_disk_auth"] = (cfg["cookie"], cfg["xsrf"])
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def reload_auth(cfg):
    """Adopt a newer local session, including an externally completed sign-in."""
    if not cfg.get("_config_path") or not cfg.get("_persist_auth", False):
        return False
    saved = _read_file(cfg["_config_path"])
    acc_match = None
    if isinstance(saved.get("accounts"), list):
        for acc in saved["accounts"]:
            if isinstance(acc, dict):
                if cfg.get("page_uuid") and acc.get("page_uuid") == cfg.get("page_uuid"):
                    acc_match = acc
                    break
                elif not cfg.get("page_uuid") and acc.get("base", "").rstrip("/") == cfg["base"].rstrip("/"):
                    acc_match = acc
                    break
    target = acc_match if acc_match is not None else saved
    base = target.get("base") or saved.get("base", "")
    cookie = target.get("cookie")
    xsrf = target.get("xsrf")
    if (base.rstrip("/") != cfg["base"].rstrip("/")
            or not cookie or not xsrf
            or (cookie, xsrf) == cfg.get("_disk_auth", (cfg["cookie"], cfg["xsrf"]))):
        return False
    cfg.update(cookie=cookie, xsrf=xsrf, _disk_auth=(cookie, xsrf),
               cookie_values=[p.strip().split("=", 1)[1] for p in cookie.split(";") if "=" in p])
    return True


def load(path=CONFIG_PATH):
    """Load config, apply env overrides, fill defaults, validate."""
    cfg = dict(DEFAULTS)
    cfg.update(_read_file(path))
    cfg["_config_path"] = os.path.abspath(path)
    cfg["_persist_auth"] = not (os.environ.get("RETOOL_COOKIE") or os.environ.get("RETOOL_XSRF"))

    # Secrets may live outside the repo: env wins over file.
    env_map = {
        "base": "RETOOL_BASE",
        "cookie": "RETOOL_COOKIE",
        "xsrf": "RETOOL_XSRF",
        "page_uuid": "RETOOL_PAGE_UUID",
        "thread_id": "RETOOL_THREAD_ID",
        "host": "RELAY_HOST",
        "port": "RELAY_PORT",
        "session_mode": "RELAY_SESSION_MODE",
        "transport": "RELAY_TRANSPORT",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value:
            cfg[key] = value

    for num_key in ("port", "agent_mode", "max_body_bytes", "max_frame_bytes", "max_workers",
                    "turn_timeout", "recv_timeout", "upstream_timeout",
                    "session_refresh_interval", "max_conversations", "conversation_idle_timeout",
                    "command_timeout", "command_output_limit", "max_exec_turns", "context_max_bytes",
                    "max_prompt_bytes", "cache_keepwarm_seconds", "cache_keepwarm_idle"):
        try:
            cfg[num_key] = int(cfg[num_key])
        except (TypeError, ValueError):
            raise service_unavailable(f"config value '{num_key}' must be an integer")

    if cfg["session_refresh_interval"] < 0:
        raise service_unavailable("session_refresh_interval must be non-negative (0 disables keepalive)")
    for key in ("max_exec_turns", "max_prompt_bytes", "command_output_limit"):
        if cfg[key] < 0:
            raise service_unavailable(f"{key} must be non-negative (0 disables the cap)")
    for key in ("turn_timeout", "recv_timeout", "upstream_timeout", "command_timeout", "max_workers",
                "max_body_bytes", "max_frame_bytes"):
        if cfg[key] < 1:
            raise service_unavailable(f"{key} must be positive")
    if cfg["max_conversations"] < 1 or cfg["conversation_idle_timeout"] < 1:
        raise service_unavailable("max_conversations and conversation_idle_timeout must be positive")

    for float_key in ("rate_limit_rps", "rate_limit_burst"):
        try:
            cfg[float_key] = float(cfg[float_key])
        except (TypeError, ValueError):
            raise service_unavailable(f"config value '{float_key}' must be a number")
        if not math.isfinite(cfg[float_key]):
            raise service_unavailable(f"config value '{float_key}' must be finite")
    if cfg["rate_limit_rps"] < 0 or cfg["rate_limit_burst"] < 1:
        # burst < 1 can never reach the 1.0 token threshold: every completion
        # would be 429 forever.
        raise service_unavailable("rate_limit_rps must be non-negative and rate_limit_burst must be at least 1")

    accounts = cfg.get("accounts")
    if accounts is not None:
        if not isinstance(accounts, list) or not accounts:
            raise service_unavailable("'accounts' must be a non-empty array")
        for i, acc in enumerate(accounts):
            if not isinstance(acc, dict):
                raise service_unavailable(f"account #{i} must be an object")
            acc_missing = [k for k in ("cookie", "xsrf", "base", "page_uuid") if not acc.get(k)]
            if acc_missing:
                raise service_unavailable(f"account #{i} is missing required credentials: " + ", ".join(acc_missing))
        if not cfg.get("base") and accounts:
            cfg["base"] = accounts[0]["base"]
        if not cfg.get("page_uuid") and accounts:
            cfg["page_uuid"] = accounts[0]["page_uuid"]
        if not cfg.get("cookie") and accounts:
            cfg["cookie"] = accounts[0]["cookie"]
        if not cfg.get("xsrf") and accounts:
            cfg["xsrf"] = accounts[0]["xsrf"]

    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        raise service_unavailable("config is missing required keys: " + ", ".join(missing))
    if not cfg.get("cookie"):
        raise service_unavailable("config is missing 'cookie' (or RETOOL_COOKIE)")
    if not cfg.get("xsrf"):
        raise service_unavailable("config is missing 'xsrf' (or RETOOL_XSRF)")
    if cfg["session_mode"] not in ("per_request", "per_conversation", "shared"):
        raise service_unavailable("session_mode must be 'per_request', 'per_conversation' or 'shared'")
    if cfg["session_mode"] == "shared" and not cfg.get("thread_id"):
        raise service_unavailable("session_mode 'shared' requires 'thread_id'")
    if cfg.get("transport") not in ("direct", "agent"):
        raise service_unavailable("transport must be 'direct' or 'agent'")
    if cfg["exec_policy"] not in ("allow_all", "allowlist", "off"):
        raise service_unavailable("exec_policy must be 'allow_all', 'allowlist' or 'off'")

    workspace = cfg.get("workspace_root") or os.getcwd()
    # Resolve symlinks/junctions so escapes are visible.
    cfg["workspace_root"] = os.path.realpath(workspace)
    if not os.path.isdir(cfg["workspace_root"]):
        raise service_unavailable(f"workspace_root is not a directory: {cfg['workspace_root']}")

    cfg["relay_api_key"] = _ensure_relay_key(cfg)
    # Mask cookie secrets, but not plain host names (else the subdomain gets hidden).
    cookie_strings = []
    if cfg.get("cookie"):
        cookie_strings.append(cfg["cookie"])
    if isinstance(cfg.get("accounts"), list):
        for acc in cfg["accounts"]:
            if isinstance(acc, dict) and acc.get("cookie"):
                cookie_strings.append(acc["cookie"])
    cookie_vals = []
    for c_str in cookie_strings:
        for seg in c_str.split("; "):
            if "=" in seg:
                val = seg.split("=", 1)[1]
                if val and not HOSTNAME.fullmatch(val) and val not in cookie_vals:
                    cookie_vals.append(val)
    cfg["cookie_values"] = cookie_vals
    cfg["_disk_auth"] = (cfg["cookie"], cfg["xsrf"])
    return cfg
