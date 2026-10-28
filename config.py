"""Configuration loading, environment overrides and validation."""
import json
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
    "rate_limit_rps": 32.0,
    "rate_limit_burst": 64.0,
    "turn_timeout": 300,
    "recv_timeout": 120,
    "upstream_timeout": 120,
    "session_refresh_interval": 900,
    "command_timeout": 40,
    "command_output_limit": 64000,
    "max_exec_turns": 0,          # 0 = без ограничения
    "exec_policy": "allowlist",
    "exec_allowlist": [
        r"^powershell\b", r"^pwsh\b", r"^cmd\b", r"^git\b", r"^ping\b", r"^more\b", r"^dir\b", r"^type\b", r"^findstr\b", r"^where\b", r"^python\b",
        r"^pytest\b", r"^npm\b", r"^node\b", r"^pip\b", r"^dotnet\b", r"^cargo\b",
        r"^rg\b", r"^Get-ChildItem\b", r"^Get-Content\b", r"^Select-String\b",
        r"^curl\b", r"^echo\b", r"^cls\b", r"^mkdir\b",
    ],
    "project_context": True,
    "context_max_bytes": 120000,
    "max_prompt_bytes": 128000,
    "show_exec_output": True,
    "enable_anthropic": True,
    "enable_chat": True,
    "show_thinking": True,
    "enable_responses": True,
    "reasoning_effort": "high",
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
    if saved.get("base", "").rstrip("/") != cfg["base"].rstrip("/"):
        raise ValueError("session configuration origin changed")
    disk_auth = (saved.get("cookie"), saved.get("xsrf"))
    if disk_auth != cfg.get("_disk_auth", disk_auth):
        raise ValueError("session configuration changed during renewal")
    saved.update(cookie=cfg["cookie"], xsrf=cfg["xsrf"])
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
    if (saved.get("base", "").rstrip("/") != cfg["base"].rstrip("/")
            or not saved.get("cookie") or not saved.get("xsrf")
            or (saved["cookie"], saved["xsrf"]) == cfg.get("_disk_auth", (cfg["cookie"], cfg["xsrf"]))):
        return False
    cfg.update(cookie=saved["cookie"], xsrf=saved["xsrf"], _disk_auth=(saved["cookie"], saved["xsrf"]),
               cookie_values=[p.strip().split("=", 1)[1] for p in saved["cookie"].split(";") if "=" in p])
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
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value:
            cfg[key] = value

    for num_key in ("port", "agent_mode", "max_body_bytes", "max_frame_bytes", "max_workers",
                    "turn_timeout", "recv_timeout", "upstream_timeout",
                    "session_refresh_interval",
                    "command_timeout", "command_output_limit", "max_exec_turns", "context_max_bytes",
                    "max_prompt_bytes"):
        try:
            cfg[num_key] = int(cfg[num_key])
        except (TypeError, ValueError):
            raise service_unavailable(f"config value '{num_key}' must be an integer")

    if cfg["session_refresh_interval"] < 0:
        raise service_unavailable("session_refresh_interval must be non-negative (0 disables keepalive)")

    for float_key in ("rate_limit_rps", "rate_limit_burst"):
        try:
            cfg[float_key] = float(cfg[float_key])
        except (TypeError, ValueError):
            raise service_unavailable(f"config value '{float_key}' must be a number")

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
    if cfg["exec_policy"] not in ("allow_all", "allowlist", "off"):
        raise service_unavailable("exec_policy must be 'allow_all', 'allowlist' or 'off'")

    workspace = cfg.get("workspace_root") or os.getcwd()
    # Resolve symlinks/junctions so escapes are visible.
    cfg["workspace_root"] = os.path.realpath(workspace)
    if not os.path.isdir(cfg["workspace_root"]):
        raise service_unavailable(f"workspace_root is not a directory: {cfg['workspace_root']}")

    cfg["relay_api_key"] = _ensure_relay_key(cfg)
    # Mask cookie secrets, but not plain host names (else the subdomain gets hidden).
    cfg["cookie_values"] = [
        seg.split("=", 1)[1]
        for seg in cfg["cookie"].split("; ")
        if "=" in seg and not HOSTNAME.fullmatch(seg.split("=", 1)[1])
    ]
    cfg["_disk_auth"] = (cfg["cookie"], cfg["xsrf"])
    return cfg
