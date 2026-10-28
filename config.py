"""Configuration loading, environment overrides and validation."""
import json
import os
import re
import secrets

from apierr import service_unavailable

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
KEY_PATH = os.path.join(HERE, "relay_key.txt")

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8787,
    "session_mode": "per_request",
    "strict_models": True,
    "max_body_bytes": 32 * 1024 * 1024,
    "max_frame_bytes": 16 * 1024 * 1024,
    "max_workers": 16,
    "rate_limit_rps": 8.0,
    "rate_limit_burst": 16.0,
    "turn_timeout": 60,
    "recv_timeout": 30,
    "upstream_timeout": 120,
    "command_timeout": 40,
    "command_output_limit": 4000,
    "max_exec_turns": 5,
    "exec_policy": "allowlist",
    "exec_allowlist": [
        r"^powershell\b", r"^pwsh\b", r"^cmd\b", r"^git\b", r"^ping\b", r"^more\b", r"^dir\b", r"^type\b", r"^findstr\b", r"^where\b", r"^python\b",
        r"^pytest\b", r"^npm\b", r"^node\b", r"^pip\b", r"^dotnet\b", r"^cargo\b",
        r"^rg\b", r"^Get-ChildItem\b", r"^Get-Content\b", r"^Select-String\b",
        r"^curl\b", r"^echo\b", r"^cls\b", r"^mkdir\b",
    ],
    "project_context": True,
    "context_max_bytes": 2500,
    "show_exec_output": True,
    "enable_anthropic": True,
    "enable_chat": True,
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


def load(path=CONFIG_PATH):
    """Load config, apply env overrides, fill defaults, validate."""
    cfg = dict(DEFAULTS)
    cfg.update(_read_file(path))

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
                    "command_timeout", "command_output_limit", "max_exec_turns", "context_max_bytes"):
        try:
            cfg[num_key] = int(cfg[num_key])
        except (TypeError, ValueError):
            raise service_unavailable(f"config value '{num_key}' must be an integer")

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
    if cfg["session_mode"] not in ("per_request", "shared"):
        raise service_unavailable("session_mode must be 'per_request' or 'shared'")
    if cfg["session_mode"] == "shared" and not cfg.get("thread_id"):
        raise service_unavailable("session_mode 'shared' requires 'thread_id'")
    if cfg["exec_policy"] not in ("allow_all", "allowlist"):
        raise service_unavailable("exec_policy must be 'allow_all' or 'allowlist'")

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
    return cfg
