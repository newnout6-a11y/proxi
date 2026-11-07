"""Configuration loading, environment overrides and validation."""
import json
from contextlib import contextmanager, nullcontext
import hashlib
import math
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time

from apierr import service_unavailable
import models
import web_search

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
    "relay_system_prompt": None,  # null uses the default role; a string supplies the user's policy
    "relay_system_prompt_mode": "standalone",  # standalone | append | replace client instructions
    # Prompt control (profiles, rules, relay texts). "" is prompts.json next to config.json, a relative path
    # starts there too; load() replaces it with the absolute path. Without the file nothing changes.
    "prompts_file": "",
    "provider_access": {},  # resource-bound BYOK/retention facts, scoped to each account
    "max_body_bytes": 32 * 1024 * 1024,
    "max_frame_bytes": 16 * 1024 * 1024,
    "max_workers": 32,
    "max_conversations": 128,
    "conversation_idle_timeout": 900,
    # A new conversation that finds the pool full of conversations that cannot go now (busy, or the parent of a running
    # subagent) waits up to this many seconds for a slot before it gets 503. 0 answers 503 at once.
    "conversation_wait": 10,
    # The journal (journal.jsonl) starts a new file at this size and keeps this many old ones (journal.jsonl.1 is the
    # newest), so at most (journal_keep_files + 1) * journal_max_file_bytes stay on disk. 0 keeps no old file.
    "journal_max_file_bytes": 50 * 1024 * 1024,
    "journal_keep_files": 5,
    # Conversations survive a relay restart: after every turn that moved one, its signed history, cache marks and
    # account are written to a private directory (empty = <state-dir>/conversations) and read back, onto the
    # same account, when the conversation's next request creates its session.
    "conversation_state": True,
    "conversation_state_dir": "",
    # 200 were too few: the tests of six agents filled them in an hour, and the stored states of the owner's idle chats
    # (Claude Desktop, Codex Desktop) were dropped 43-55 minutes after their last turn, while the store held 22 MB of
    # its 512 MiB (journal watch of 2026-11-07). A state is about 110 KB compressed on average.
    "conversation_state_max_conversations": 1000,
    "conversation_state_ttl": 86400,  # seconds a stored conversation is kept after its last turn
    "conversation_state_max_file_bytes": 32 * 1024 * 1024,   # one conversation, as stored (compressed)
    "conversation_state_max_total_bytes": 512 * 1024 * 1024,  # the whole directory
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
    "allow_effort_downgrade": False,
    # Retool's Claude routes have no xhigh. A request for it is served at this level instead of failing: high (what the
    # nearest-level rule picks too) or max. Read from the config like allow_effort_downgrade, so a change needs a restart.
    "claude_xhigh_effort": "high",
    # WebSearch of Claude Code (a server tool of the provider that Retool cannot run): "" refuses it with a 422, "exa" lets the
    # relay run the model's searches on Exa (POST /search). The key is read from the environment variable named here and is never
    # kept in config.json; a missing variable leaves the refusal in place and the start prints a warning.
    "web_search_provider": "",
    "web_search_key_env": "EXA_API_KEY",
    "web_search_endpoint": "https://api.exa.ai",
    "web_search_results": 5,      # pages the provider returns per search (the model reads them all)
    "web_search_timeout": 20,     # seconds one search may take
    # A client's compaction (the request that summarizes its history so that the client can replace it) runs on the effort
    # of the signed branch it resends: another effort restarts that history and the provider's cached messages, and the
    # whole conversation is written again. False lets the effort the client sends decide, as for any other request.
    "compaction_keep_effort": True,
    # Codex's memory jobs: the extraction of memories from an old thread, one tool-less request per thread for a strict schema
    # (client_task "background"), about 5 credits each on gpt-6.1-terra. False answers them with a 400 before anything is
    # spent, so Codex makes no memories until they are switched on again; a switch in the admin, applied at once.
    "codex_memory_jobs": True,
    # Codex's compaction goes out with no tools, so it shared nothing with the chat's cached prefix and wrote the whole history again
    # (31,943 tokens, 1.65 credits where the chat's last request wrote 9,807; about 31 credits at 330k tokens). True declares the chat's
    # tools again (compaction_tools), so the summary reads the chat's cache; False serves the request as Codex sent it. A switch in
    # the admin, applied at once.
    "codex_compaction_cache": True,
    # A request with tools and no conversation identity (DeepSeek Harness sends no session header) takes the conversation
    # whose request its history extends, so its signed history continues; False gives each such request a session of its own.
    "infer_conversations": True,
    # Parts of a client's system prompt that change between requests with no change in the conversation, which rewrite
    # the whole cache (prompt_stabilizer): the location of a skill that exists in two folders (OpenCode 1.x) is held at
    # the first one seen. stable_prompt_date also holds OpenCode's "Today's date" at the date a conversation began while
    # it stays active, so a chat that crosses midnight is not rewritten; the model is then told that date, which is why
    # it is off by default.
    "stabilize_client_prompts": True,
    "stable_prompt_date": False,
    # Codex declares its MCP tools late (request 1: 44-46 tools, request 2: 50-60), and so does Claude Code when an MCP server
    # connects after its first request (59 -> 90 tools, 107k tokens written); each change rewrites the cache. A client of these
    # families is given the union of the tools it declared before and this request's, per project (and per kind of agent),
    # kept in conversation_state_dir between runs, so the list the model sees stops changing; [] switches it off. A tool not
    # declared for tool_union_ttl seconds is dropped.
    "tool_union_clients": ["codex", "claude-code"],
    "tool_union_ttl": 6 * 3600,
    # Claude Code (2.1.305 and later) defers its MCP tools behind ToolSearch when the base URL is a gateway, and every tool the
    # model loads changes the tool list, which rewrites the whole cached conversation. "eager" answers a request that carries
    # tool search with a 400, which makes Claude Code send it again with every tool loaded up front; "native" serves
    # the deferral as the client sends it.
    "claude_tool_search": "eager",
    # "direct" = plain copilotStreamGPT POST with cached context (fast), "agent" = sandbox WebSocket.
    "transport": "direct",
    "direct_cache": True,
    "upstream_trace_dir": "",  # explicit private raw request/response traces; disabled by default
    "upstream_empty_retries": 1,           # extra identical POSTs after an empty/thinking-only answer (paid)
    # Retool answers "models are rate limited right now, try again shortly" (429) before it generates anything: the same body is
    # sent again on the same account after a short pause (about 2, 4 and 8 s in all), inside the 5-POST budget of a step. Free,
    # so it is not counted as paid; 0 passes the 429 on to the client at once.
    "upstream_rate_limit_retries": 3,
    "upstream_error_max_dumps": 50,        # newest private request-body dump sets kept; 0 = unlimited
    "upstream_attempt_summaries_max": 200,  # newest per-attempt *-response.json kept; 0 = unlimited
    "client_trace_dir": "",    # explicit private client request/response traces; disabled by default
    "trace_max_folders": 200,  # newest trace folders kept in each trace directory; 0 = unlimited
    # Provider caches expire in minutes; re-read the prefix to hold it open.
    "cache_keepwarm_seconds": 240,
    # Idle warming stops this long after a conversation's last real request. A ping reads
    # the cached prefix (4e-6 credits/token), a miss writes it again (1e-4). Under 1% of the
    # main chats whose warming ended at 900 s came back in the next 15 minutes (live journal,
    # 2026-11-06/07), so longer warming of a main chat does not pay.
    "cache_keepwarm_idle": 900,
    # A subagent that answered with a tool call is still expected back; one that answered
    # with plain text is done and is not warmed at all. 0 = no idle warming for subagents.
    # One waiting for a tool always came back in the live journal (4 of 4), two of them after
    # a 15-minute test run, when 600 s of warming had let the cache expire: 46 credits of
    # rewrites that two more pings each (7.7 credits) would have saved. 1800 s covers a tool
    # of about half an hour; its seven pings cost less than one rewrite of the prefix on the
    # measured rates of Claude, GPT and GLM-5.3 (credits.MODEL_RATES).
    "cache_keepwarm_subagent_idle": 1800,
    "cache_keepwarm_during_turns": True,
    "cache_keepwarm_turn_offset": 200,
    # Accounts below this confirmed balance leave the routing pool.
    "min_account_balance": 20,
    "account_balance_interval": 300,
    "account_refresh_parallel": 3,
    # Pause after Retool's hourly managed-key limit; a limit right after the return adds 5 minutes
    # to it, up to max(this, 20 minutes).
    "account_rate_limit_cooldown": 900,
    # Seconds a request that has a warm conversation waits on an account that answered the hourly limit, and then asks
    # that account once more, before the conversation moves to another account (which writes its whole prefix again).
    # With a Retry-After from Retool it waits that long if that fits; 0 moves the conversation at once.
    "account_rate_limit_wait": 0,
    # A conversation that has to change account (the account ran out, was signed out, reached the hourly limit or lost the route)
    # leaves its signed thinking history behind: its next body differs from the cached one and the whole prefix is written again
    # (79k to 580k tokens, 8 to 58 credits, 38 s to the first token), although the provider's cache is shared by the accounts.
    # true takes the signed branches along, marked as read back from disk: a refusal that names the blocks forgets them and the
    # next request goes unsigned, as every move did before. Off until a move has been seen to be accepted on the live relay.
    "account_move_keeps_history": False,
    "agent_mode": 2,
    "model": "claude-opus-5-5",
    "google_provider_resource_name": "",
    "retool_fast_provider_resource_name": "",
}

REQUIRED = ("base", "page_uuid", "ua")
PROMPTS_FILE = "prompts.json"
PROMPTS_FILE_MAX_CHARS = 4096
# The counts and sizes of the stored conversations: whole positive numbers.
STATE_NUMBERS = ("conversation_state_max_conversations", "conversation_state_ttl",
                 "conversation_state_max_file_bytes", "conversation_state_max_total_bytes")
# The size and the number of the journal's files: whole numbers, checked before int() would turn true into 1.
JOURNAL_NUMBERS = ("journal_max_file_bytes", "journal_keep_files")
# How long a new conversation waits for a slot of a full pool, and how long a request waits on an account that is at its
# hourly limit: whole numbers of seconds, checked the same way.
POOL_NUMBERS = ("conversation_wait", "account_rate_limit_wait")

# Cookie values that are just host names are not secrets.
HOSTNAME = re.compile(r"[A-Za-z0-9.\-]+\.(com|app|io|dev|net|org)")

_STORAGE_GUARD = threading.Lock()
_STORAGE_LOCKS = {}
_STORAGE_LOCAL = threading.local()


@contextmanager
def storage_lock(path, timeout=30, best_effort=False):
    """Serialize the entire config transaction across threads and processes.

    Keep the sidecar: deleting it after unlock can split concurrent writers
    between different inodes. OS locks are released if the process exits.
    `best_effort` runs without the OS lock when the sidecar cannot be created
    (read-only directory); the in-process lock is still held.
    """
    path = os.path.normcase(os.path.realpath(os.fspath(path)))
    with _STORAGE_GUARD:
        lock = _STORAGE_LOCKS.setdefault(path, threading.RLock())
    with lock:
        held = getattr(_STORAGE_LOCAL, "held", set())
        if path in held:
            yield
            return
        try:
            sidecar = open(path + ".lock", "a+b")
        except OSError:
            if not best_effort:
                raise
            sidecar = None
        if sidecar is None:
            yield
            return
        with sidecar as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("configuration storage is busy") from None
                    time.sleep(0.01)
            _STORAGE_LOCAL.held = held | {path}
            try:
                yield
            finally:
                _STORAGE_LOCAL.held = held
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_file(path, saved):
    """Atomic replace; callers hold storage_lock around read/merge/write."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path),
                                         prefix=".session-", suffix=".tmp", delete=False) as fh:
            temporary = fh.name
            try:
                json.dump(saved, fh, ensure_ascii=False, indent=2, allow_nan=False)
            except ValueError as exc:
                raise service_unavailable("config contains values that cannot be saved as JSON: %s" % exc)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            # A read-only temp (copied from a read-only target) cannot be unlinked on Windows.
            try:
                os.chmod(temporary, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            os.unlink(temporary)


def renewal_lock(cfg):
    """One renewal owner per persisted account, without locking other accounts' IO."""
    path = cfg.get("_config_path")
    if not path or not cfg.get("_persist_auth", False):
        return nullcontext()
    identity = cfg.get("account_id") or (cfg.get("base", "") + "/" + cfg.get("page_uuid", ""))
    tag = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return storage_lock(os.fspath(path) + ".auth-" + tag,
                        timeout=max(30, cfg.get("upstream_timeout", 120) + 5))


def _legacy_match(records, cfg):
    """A record an external sign-in rewrote without account_id: accepted only when it is the
    single record for this tenant/page. Another account on the same page makes it ambiguous."""
    same_page = [item for item in records
                 if str(item.get("base", "")).rstrip("/") == cfg["base"].rstrip("/")
                 and item.get("page_uuid") == cfg.get("page_uuid")]
    if len(same_page) == 1 and not same_page[0].get("account_id"):
        return same_page[0]
    return None


def _account_record(saved, cfg):
    """Exact stable ID, or an unambiguous legacy tenant/page. Never fall back."""
    records = saved.get("accounts")
    if isinstance(records, list):
        records = [item for item in records if isinstance(item, dict)]
        if cfg.get("account_id"):
            matches = [item for item in records if item.get("account_id") == cfg["account_id"]]
        else:
            matches = [item for item in records
                       if item.get("base", "").rstrip("/") == cfg["base"].rstrip("/")
                       and item.get("page_uuid") == cfg.get("page_uuid")]
        if len(matches) > 1:
            raise ValueError("ambiguous account identity; reload configuration to assign account_id")
        record = matches[0] if matches else None
        if record is None and cfg.get("account_id"):
            record = _legacy_match(records, cfg)
    else:
        record = saved
        if cfg.get("account_id") and record.get("account_id") != cfg["account_id"]:
            record = _legacy_match([record], cfg)
    if record is None or (record.get("base") or saved.get("base", "")).rstrip("/") != cfg["base"].rstrip("/"):
        return None
    return record


def _derived_account_id(record, saved, occurrence):
    """The id an account without one gets: the same on every start, saved or not.

    It used to be random, and where it could not be saved (secrets in RETOOL_COOKIE/RETOOL_XSRF, a read-only config.json)
    every start made a new one: the stored conversations, bound to the account's id, never came back after a restart and
    each wrote its whole prefix again. Derived from the tenant and the page, it is stable, and a later writable start
    saves the very same value."""
    base = str(record.get("base") or saved.get("base") or "").rstrip("/").lower()
    page = str(record.get("page_uuid") or saved.get("page_uuid") or "").lower()
    return hashlib.sha256(("%s|%s|%d" % (base, page, occurrence)).encode("utf-8")).hexdigest()[:32]


def _assign_account_ids(saved):
    records = saved.get("accounts") if isinstance(saved.get("accounts"), list) else [saved]
    seen = set()
    changed = False
    for record in records:
        value = record.get("account_id")
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) or value in seen:
                raise service_unavailable("account_id must be a unique, nonempty identifier")
            seen.add(value)
    occurrences = {}
    for record in records:
        if not record.get("account_id"):
            place = (str(record.get("base") or saved.get("base") or "").rstrip("/").lower(),
                     str(record.get("page_uuid") or saved.get("page_uuid") or "").lower())
            occurrence = occurrences.get(place, 0)
            value = _derived_account_id(record, saved, occurrence)
            while value in seen:  # an explicit id of another record that happens to be this one
                occurrence += 1
                value = _derived_account_id(record, saved, occurrence)
            occurrences[place] = occurrence + 1
            record["account_id"] = value
            seen.add(value)
            changed = True
    return changed


def _read_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise service_unavailable(f"config not found: {path} (copy config.example.json to config.json)")
    except json.JSONDecodeError as exc:
        raise service_unavailable(f"config is not valid JSON: {exc}")


def _prompts_path(value, config_path):
    """Absolute path of the prompts file: "" is prompts.json beside config.json, a relative path starts there too."""
    if not isinstance(value, str):
        raise service_unavailable("prompts_file must be a string")
    if "\x00" in value:
        raise service_unavailable("prompts_file must not contain NUL characters")
    if len(value) > PROMPTS_FILE_MAX_CHARS:
        raise service_unavailable(f"prompts_file must be at most {PROMPTS_FILE_MAX_CHARS} characters")
    return os.path.abspath(os.path.join(os.path.dirname(config_path), value.strip() or PROMPTS_FILE))


def _placeholder(value):
    """A value copied unchanged from config.example.json, which describes what goes there in angle brackets."""
    return isinstance(value, str) and value.strip().startswith("<") and value.strip().endswith(">")


def _ensure_relay_key(cfg):
    key = os.environ.get("RELAY_API_KEY") or cfg.get("relay_api_key")
    if key:
        return key
    key_path = os.path.join(os.path.dirname(cfg["_config_path"]), "relay_key.txt") if cfg.get("_config_path") else KEY_PATH
    if os.path.exists(key_path):
        with open(key_path, encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    key = "relay-" + secrets.token_urlsafe(24)
    with open(key_path, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")
    return key


def persist_auth(cfg):
    """Merge only this account's rotation under a shared storage transaction.

    Returns True when config.json was rewritten and False when it already held these
    values: a renewal that rotated nothing must not rewrite the whole file."""
    path = cfg.get("_config_path")
    if not path or not cfg.get("_persist_auth", False):
        return False
    with storage_lock(path):
        saved = _read_file(path)
        record = _account_record(saved, cfg)
        if record is None:
            raise ValueError("session account or origin changed")
        disk_auth = (record.get("cookie"), record.get("xsrf"))
        if disk_auth != cfg.get("_disk_auth", disk_auth) and disk_auth != (cfg["cookie"], cfg["xsrf"]):
            raise ValueError("session configuration changed during renewal")
        auth = (cfg["cookie"], cfg["xsrf"])
        needs_id = bool(cfg.get("account_id")) and not record.get("account_id")
        if disk_auth == auth and not needs_id:
            cfg["_disk_auth"] = auth
            return False
        record.update(cookie=cfg["cookie"], xsrf=cfg["xsrf"])
        if needs_id:
            record["account_id"] = cfg["account_id"]
        _write_file(path, saved)
        cfg["_disk_auth"] = auth
        return True


def reload_auth(cfg):
    """Adopt a newer local session, including an externally completed sign-in."""
    if not cfg.get("_config_path") or not cfg.get("_persist_auth", False):
        return False
    with storage_lock(cfg["_config_path"]):
        saved = _read_file(cfg["_config_path"])
        target = _account_record(saved, cfg)
        if target is None:
            return False
        cookie, xsrf = target.get("cookie"), target.get("xsrf")
        if not cookie or not xsrf or (cookie, xsrf) == cfg.get("_disk_auth", (cfg["cookie"], cfg["xsrf"])):
            return False
        cfg.update(cookie=cookie, xsrf=xsrf, _disk_auth=(cookie, xsrf),
                   cookie_values=[p.strip().split("=", 1)[1] for p in cookie.split(";") if "=" in p])
        if isinstance(cfg.get("_account_ref"), dict):
            cfg["_account_ref"].update(cookie=cookie, xsrf=xsrf)
        return True


def _check_provider_access(access, name):
    """Resource-bound BYOK/retention facts; every error names the provider and the field."""
    if not isinstance(access, dict):
        raise service_unavailable(f"{name}: 'provider_access' must be an object")
    for provider, details in access.items():
        where = f"{name}: provider_access.{provider}"
        key = models.PROVIDER_RESOURCE_KEY.get(provider)
        if key is None:
            raise service_unavailable(f"{name}: provider_access has an unknown provider '{provider}' "
                                      f"(known: {', '.join(sorted(models.PROVIDER_RESOURCE_KEY))})")
        if not isinstance(details, dict):
            raise service_unavailable(f"{where} must be an object")
        unknown = sorted(set(details) - {'resource_name', 'byok', 'data_retention_enabled'})
        if unknown:
            raise service_unavailable(f"{where} has unknown field(s): {', '.join(map(str, unknown))} "
                                      "(allowed: resource_name, byok, data_retention_enabled)")
        resource = details.get('resource_name')
        if not isinstance(resource, str) or not resource:
            raise service_unavailable(f"{where}.resource_name must be a non-empty string "
                                      f"(the same value as {key})")
        for flag in ('byok', 'data_retention_enabled'):
            if flag in details and type(details[flag]) is not bool:
                raise service_unavailable(f"{where}.{flag} must be a boolean")
        if details.get('byok') is True and resource == '__managed__':
            raise service_unavailable(f"{where}.byok cannot be true when resource_name is '__managed__': "
                                      "Retool-managed keys are never BYOK")


def _provider_access_warnings(cfg, record, name):
    """A descriptor only applies to the exact resource it names; say so when it names another."""
    access = record.get('provider_access')
    if not isinstance(access, dict):
        return []
    effective = models.account_config(cfg, record) if isinstance(cfg.get('accounts'), list) else cfg
    warnings = []
    for provider, details in access.items():
        key = models.PROVIDER_RESOURCE_KEY[provider]
        if details['resource_name'] != effective.get(key):
            warnings.append(f"{name}: provider_access.{provider}.resource_name differs from {key}, "
                            "so the descriptor is ignored until they are identical")
    return warnings


def _check_web_search(cfg):
    """The WebSearch settings of a config: a provider the relay knows, the name of the variable that holds its key, and bounds."""
    if cfg["web_search_provider"] not in ("",) + web_search.PROVIDERS:
        raise service_unavailable("web_search_provider must be one of: \"\", " + ", ".join(web_search.PROVIDERS))
    if not isinstance(cfg["web_search_key_env"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}",
                                                                        cfg["web_search_key_env"]):
        raise service_unavailable("web_search_key_env must be the name of an environment variable (the key itself "
                                  "does not belong in config.json)")
    endpoint = cfg["web_search_endpoint"]
    if not isinstance(endpoint, str) or not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", endpoint):
        raise service_unavailable("web_search_endpoint must be an http(s) address")
    results, timeout = _whole_number(cfg["web_search_results"]), cfg["web_search_timeout"]
    if results is None or not 1 <= results <= 20:
        raise service_unavailable("web_search_results must be a whole number from 1 to 20")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise service_unavailable("web_search_timeout must be a number of seconds from 1 to 120")
    cfg["web_search_results"] = results


def _whole_number(value):
    """The integer a setting says: a whole number or its text; None for true/false, a fraction or anything else."""
    if type(value) is int:
        return value
    if type(value) is float:
        return int(value) if math.isfinite(value) and value == int(value) else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _check_pool_fields(record, name):
    """Admin pool controls: enabled (bool), priority (0..99, lower first), label (short text)."""
    if "enabled" in record and type(record["enabled"]) is not bool:
        raise service_unavailable(f"{name}: 'enabled' must be a boolean")
    if "priority" in record and (type(record["priority"]) is not int or not 0 <= record["priority"] <= 99):
        raise service_unavailable(f"{name}: 'priority' must be an integer from 0 to 99")
    if "label" in record and (not isinstance(record["label"], str) or len(record["label"]) > 40):
        raise service_unavailable(f"{name}: 'label' must be text of at most 40 characters")
    _check_provider_access(record.get('provider_access', {}), name)


def load(path=CONFIG_PATH):
    """Load config, apply env overrides, fill defaults, validate."""
    if not os.path.isfile(path):
        # Fail before storage_lock: it would create a stray .lock next to a missing file.
        _read_file(path)
    with storage_lock(path, best_effort=True):
        return _load(path)


def _load(path):
    cfg = dict(DEFAULTS)
    saved = _read_file(path)
    cfg.update(saved)
    # null selects the default role; "" and blank text mean no role of our own (context.relay_policy).
    if cfg.get('relay_system_prompt') is not None and not isinstance(cfg['relay_system_prompt'], str):
        raise service_unavailable("relay_system_prompt must be a string or null")
    if cfg.get('relay_system_prompt_mode') not in ('standalone', 'append', 'replace'):
        raise service_unavailable("relay_system_prompt_mode must be one of standalone, append, replace")
    cfg["_config_path"] = os.path.abspath(path)
    cfg["prompts_file"] = _prompts_path(cfg.get("prompts_file"), cfg["_config_path"])
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

    # Before the loop below, which would turn true into 1, 1.9 into 1 and crash on infinity.
    for num_key in STATE_NUMBERS + JOURNAL_NUMBERS + POOL_NUMBERS:
        if _whole_number(cfg[num_key]) is None:
            raise service_unavailable(f"config value '{num_key}' must be an integer (a whole number: "
                                      "not true/false, not a fraction)")

    for num_key in ("port", "agent_mode", "max_body_bytes", "max_frame_bytes", "max_workers",
                    "turn_timeout", "recv_timeout", "upstream_timeout",
                    "session_refresh_interval", "max_conversations", "conversation_idle_timeout",
                    "conversation_wait",
                    "command_timeout", "command_output_limit", "max_exec_turns", "context_max_bytes",
                    "max_prompt_bytes", "cache_keepwarm_seconds", "cache_keepwarm_idle",
                    "cache_keepwarm_subagent_idle",
                    "cache_keepwarm_turn_offset", "account_balance_interval", "account_refresh_parallel",
                    "account_rate_limit_cooldown", "account_rate_limit_wait",
                    "upstream_empty_retries", "upstream_rate_limit_retries", "upstream_error_max_dumps",
                    "upstream_attempt_summaries_max",
                    "trace_max_folders", "conversation_state_max_conversations", "conversation_state_ttl",
                    "conversation_state_max_file_bytes", "conversation_state_max_total_bytes",
                    "journal_max_file_bytes", "journal_keep_files"):
        try:
            cfg[num_key] = int(cfg[num_key])
        except (TypeError, ValueError):
            raise service_unavailable(f"config value '{num_key}' must be an integer")

    if cfg["session_refresh_interval"] < 0:
        raise service_unavailable("session_refresh_interval must be non-negative (0 disables keepalive)")
    try:
        cfg["min_account_balance"] = float(cfg["min_account_balance"])
    except (TypeError, ValueError):
        raise service_unavailable("min_account_balance must be a number")
    if not math.isfinite(cfg["min_account_balance"]) or cfg["min_account_balance"] < 0:
        raise service_unavailable("min_account_balance must be a finite non-negative number")
    if cfg["account_balance_interval"] < 0 or cfg["account_refresh_parallel"] < 1:
        raise service_unavailable("account_balance_interval must be non-negative and account_refresh_parallel positive")
    if not 1 <= cfg["account_rate_limit_cooldown"] <= 3600:
        raise service_unavailable("account_rate_limit_cooldown must be between 1 and 3600 seconds")
    if not 0 <= cfg["account_rate_limit_wait"] <= 120:
        raise service_unavailable("account_rate_limit_wait must be between 0 and 120 seconds (0 moves the conversation at once)")
    if type(cfg["allow_effort_downgrade"]) is not bool:
        raise service_unavailable("allow_effort_downgrade must be a boolean")
    if type(cfg["account_move_keeps_history"]) is not bool:
        raise service_unavailable("account_move_keeps_history must be a boolean")
    if cfg["claude_xhigh_effort"] not in models.XHIGH_REPLACEMENTS:
        raise service_unavailable("claude_xhigh_effort must be one of: " + ", ".join(models.XHIGH_REPLACEMENTS))
    _check_web_search(cfg)
    if type(cfg["compaction_keep_effort"]) is not bool:
        raise service_unavailable("compaction_keep_effort must be a boolean")
    if type(cfg["codex_memory_jobs"]) is not bool:
        raise service_unavailable("codex_memory_jobs must be a boolean")
    if type(cfg["codex_compaction_cache"]) is not bool:
        raise service_unavailable("codex_compaction_cache must be a boolean")
    if type(cfg["infer_conversations"]) is not bool:
        raise service_unavailable("infer_conversations must be a boolean")
    for key in ("stabilize_client_prompts", "stable_prompt_date"):
        if type(cfg[key]) is not bool:
            raise service_unavailable(f"{key} must be a boolean")
    if (not isinstance(cfg["tool_union_clients"], list)
            or not all(isinstance(item, str) for item in cfg["tool_union_clients"])):
        raise service_unavailable("tool_union_clients must be a list of client families (for example [\"codex\"]; [] is off)")
    if type(cfg["tool_union_ttl"]) is not int or cfg["tool_union_ttl"] < 1:
        raise service_unavailable("tool_union_ttl must be a positive whole number of seconds")
    if cfg["claude_tool_search"] not in ("eager", "native"):
        raise service_unavailable("claude_tool_search must be \"eager\" or \"native\"")
    for key in ("max_exec_turns", "max_prompt_bytes", "command_output_limit",
                "upstream_empty_retries", "upstream_error_max_dumps", "upstream_attempt_summaries_max",
                "trace_max_folders"):
        if cfg[key] < 0:
            raise service_unavailable(f"{key} must be non-negative (0 disables the cap)")
    if not 0 <= cfg["upstream_rate_limit_retries"] <= 4:
        raise service_unavailable("upstream_rate_limit_retries must be from 0 (pass a 429 on at once) to 4: a step has "
                                  "at most 5 POSTs")
    if cfg["cache_keepwarm_subagent_idle"] < 0:
        raise service_unavailable("cache_keepwarm_subagent_idle must be non-negative (0 = no idle warming for subagents)")
    for key in ("turn_timeout", "recv_timeout", "upstream_timeout", "command_timeout", "max_workers",
                "max_body_bytes", "max_frame_bytes"):
        if cfg[key] < 1:
            raise service_unavailable(f"{key} must be positive")
    if cfg["max_conversations"] < 1 or cfg["conversation_idle_timeout"] < 1:
        raise service_unavailable("max_conversations and conversation_idle_timeout must be positive")
    if not 0 <= cfg["conversation_wait"] <= 300:
        raise service_unavailable("conversation_wait must be between 0 and 300 seconds (0 answers 503 at once)")
    if not 1024 * 1024 <= cfg["journal_max_file_bytes"] <= 1024 ** 3:
        raise service_unavailable("journal_max_file_bytes must be between 1 MiB and 1 GiB (1048576 to 1073741824)")
    if not 0 <= cfg["journal_keep_files"] <= 50:
        raise service_unavailable("journal_keep_files must be between 0 and 50 (0 keeps no old file)")
    if type(cfg["conversation_state"]) is not bool:
        raise service_unavailable("conversation_state must be a boolean")
    if not isinstance(cfg["conversation_state_dir"], str):
        raise service_unavailable("conversation_state_dir must be a string (empty = <state-dir>/conversations)")
    for key in STATE_NUMBERS:
        if cfg[key] < 1:
            raise service_unavailable(f"{key} must be positive (set conversation_state to false to switch it off)")
    if cfg["conversation_state_max_file_bytes"] > cfg["conversation_state_max_total_bytes"]:
        raise service_unavailable("conversation_state_max_file_bytes must not exceed "
                                  "conversation_state_max_total_bytes")

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
    warnings = []
    if accounts is not None:
        if not isinstance(accounts, list) or not accounts:
            raise service_unavailable("'accounts' must be a non-empty array")
        for i, acc in enumerate(accounts):
            if not isinstance(acc, dict):
                raise service_unavailable(f"account #{i} must be an object")
            acc_missing = [k for k in ("cookie", "xsrf", "base", "page_uuid") if not acc.get(k)]
            if acc_missing:
                raise service_unavailable(f"account #{i} is missing required credentials: " + ", ".join(acc_missing))
            _check_pool_fields(acc, f"account #{i}")
        if "provider_access" in saved:
            _check_provider_access(saved["provider_access"], "top-level")
        if not cfg.get("base") and accounts:
            cfg["base"] = accounts[0]["base"]
        if not cfg.get("page_uuid") and accounts:
            cfg["page_uuid"] = accounts[0]["page_uuid"]
        if not cfg.get("cookie") and accounts:
            cfg["cookie"] = accounts[0]["cookie"]
        if not cfg.get("xsrf") and accounts:
            cfg["xsrf"] = accounts[0]["xsrf"]
        if saved.get("provider_access"):
            warnings.append("top-level provider_access is ignored when accounts[] is present; move it into the "
                            "entry of the account that owns the resource")
        for i, acc in enumerate(accounts):
            warnings.extend(_provider_access_warnings(cfg, acc, f"account #{i}"))
    else:
        _check_pool_fields(cfg, "the account")
        warnings.extend(_provider_access_warnings(cfg, cfg, "the account"))

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
    if cfg.get("transport") != "direct":
        # README: the legacy WebSocket sandbox ("agent") path is disabled. Accepting
        # it silently would revive every unmaintained WS-only code path.
        raise service_unavailable("transport must be 'direct' (the legacy 'agent' WebSocket transport is disabled)")
    if cfg["exec_policy"] not in ("allow_all", "allowlist", "off"):
        raise service_unavailable("exec_policy must be 'allow_all', 'allowlist' or 'off'")
    if cfg.get("reasoning_effort") not in (None, "auto"):
        models.resolve_tolerant(cfg["model"], cfg["reasoning_effort"],
                                strict=cfg["strict_models"], allow_downgrade=cfg["allow_effort_downgrade"],
                                xhigh_as=cfg["claude_xhigh_effort"])

    workspace = cfg.get("workspace_root") or os.getcwd()
    # Resolve symlinks/junctions so escapes are visible.
    cfg["workspace_root"] = os.path.realpath(workspace)
    if not os.path.isdir(cfg["workspace_root"]):
        raise service_unavailable(f"workspace_root is not a directory: {cfg['workspace_root']}")

    if _placeholder(cfg.get("relay_api_key")):
        # config.example.json carries a description in angle brackets here; copied unchanged it was the relay key itself,
        # a string anyone can read in the repository.
        warnings.append("relay_api_key holds the placeholder of config.example.json and is ignored: the key comes from "
                        "RELAY_API_KEY or relay_key.txt")
        cfg["relay_api_key"] = None
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
    search_key = web_search.key_of(cfg)
    if search_key and search_key not in cookie_vals:
        cookie_vals.append(search_key)  # masked wherever the relay redacts secrets, should it ever show up in a message
    if cfg["web_search_provider"] and not search_key:
        warnings.append("web_search_provider is %s, but the environment variable %s holds no key: WebSearch stays refused"
                        % (cfg["web_search_provider"], cfg["web_search_key_env"]))
    cfg["_disk_auth"] = (cfg["cookie"], cfg["xsrf"])
    cfg["config_warnings"] = warnings
    for text in warnings:
        print("warning: " + text, file=sys.stderr)
    changed = _assign_account_ids(saved)
    if not isinstance(saved.get("accounts"), list):
        cfg["account_id"] = saved["account_id"]
    if changed and cfg["_persist_auth"]:
        try:
            _write_file(path, saved)
        except OSError as exc:
            # Read-only config: the ids derived from base and page_uuid are the same at every start until one can save them.
            print("warning: cannot save account_id to %s (%s); ids are kept in memory" % (
                path, type(exc).__name__), file=sys.stderr)
    return cfg
