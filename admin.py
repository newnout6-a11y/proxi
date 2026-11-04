"""Local administration: public build assets and authenticated, bounded APIs."""
import contextlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

from urllib.parse import parse_qs

import account_report
import apierr
import auth
import config
import journal
import models
from redact import redact
import sse
import validate


BUILD_ROOT = Path(__file__).resolve().parent / "web" / "dist"
LIVE_SETTINGS = {"rate_limit_rps", "rate_limit_burst"}
STREAM_MAX_SECONDS = 600
STREAM_HEARTBEAT = 10.0


def _int_param(query, name, default, low, high):
    try:
        value = int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        raise apierr.bad_request(f"'{name}' must be an integer", param=name)
    return max(low, min(high, value))


def _list_param(query, name):
    values = []
    for raw in query.get(name) or []:
        values += [item.strip() for item in raw.split(",") if item.strip()]
    return set(values) or None


def _schema():
    fields = {
        "model": {"type": "string", "label": "Модель по умолчанию",
                  "enum": [entry["id"] for entry in models.catalog()]},
        "reasoning_effort": {"type": "string", "label": "Уровень мышления",
                             "enum": ["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"]},
        "session_mode": {"type": "string", "label": "Режим сессий",
                         "enum": ["per_request", "per_conversation", "shared"]},
        "exec_policy": {"type": "string", "label": "Политика локальных команд",
                        "enum": ["off", "allowlist", "allow_all"]},
    }
    for key, label in {
        "enable_chat": "Chat Completions API", "enable_responses": "Responses API",
        "enable_anthropic": "Anthropic API", "strict_models": "Проверять имена моделей",
        "show_unrouted_models": "Показывать модели без маршрута",
        "show_thinking": "Передавать мышление модели", "project_context": "Контекст проекта",
        "show_exec_output": "Вывод локальных команд",
        "cache_keepwarm_during_turns": "Прогрев кэша во время длинных ходов",
        "allow_effort_downgrade": "Разрешать приближение неподдерживаемого effort",
    }.items():
        fields[key] = {"type": "boolean", "label": label}
    for key, label, minimum, maximum in [
        ("max_workers", "Параллельные запросы", 1, 512),
        ("max_conversations", "Сохранённые диалоги", 1, 4096),
        ("conversation_idle_timeout", "Хранение неактивного диалога, с", 1, 86400),
        ("turn_timeout", "Ожидание ответа, с", 1, 86400),
        ("recv_timeout", "Ожидание WebSocket, с", 1, 3600),
        ("upstream_timeout", "Ожидание соединения, с", 1, 3600),
        ("session_refresh_interval", "Обновление авторизации, с", 0, 86400),
        ("command_timeout", "Ожидание локальной команды, с", 1, 86400),
        ("command_output_limit", "Вывод команды, байт (0 — без лимита)", 0, 100_000_000),
        ("max_exec_turns", "Циклы инструментов (0 — без лимита)", 0, 1000),
        ("context_max_bytes", "Контекст проекта, байт", 1, 10_000_000),
        ("max_prompt_bytes", "Размер запроса, байт (0 — без лимита)", 0, 1_073_741_824),
        ("max_body_bytes", "Размер HTTP body, байт", 1, 1_073_741_824),
        ("max_frame_bytes", "Размер WebSocket frame, байт", 1, 1_073_741_824),
        ("cache_keepwarm_turn_offset", "Прогрев кэша во время хода, с от начала", 30, 285),
    ]:
        fields[key] = {"type": "integer", "label": label, "min": minimum, "max": maximum}
    fields["rate_limit_rps"] = {"type": "number", "label": "Запросов в секунду (0 — без лимита)",
                                "min": 0, "max": 10_000}
    fields["rate_limit_burst"] = {"type": "number", "label": "Запас запросов", "min": 1,
                                  "max": 100_000}
    for key, field in fields.items():
        field["restart_required"] = key not in LIVE_SETTINGS
        field["description"] = ("Применяется к новым запросам сразу после сохранения."
                                if key in LIVE_SETTINGS else
                                "Сохраняется в конфигурации. Применится после перезапуска relay.")
    return fields


def is_admin_path(path):
    return path == "/admin" or path.startswith("/admin/")


def security_headers(handler):
    for key, value in {
        "cache-control": "no-store", "pragma": "no-cache",
        "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
        "x-frame-options": "DENY", "cross-origin-resource-policy": "same-origin",
        "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                   "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
                                   "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    }.items():
        handler.send_header(key, value)


class Admin:
    def __init__(self, runtime):
        self.runtime = runtime
        self.cfg = runtime.cfg
        self.lock = threading.Lock()
        self.schema = _schema()

    def _account_secrets(self):
        """Credentials of every account; short values are skipped so they cannot mangle ordinary text."""
        secrets = []
        for backend in getattr(self.runtime.accounts, "backends", None) or []:
            cfg = getattr(backend, "cfg", None) or {}
            secrets += [cfg.get("cookie"), cfg.get("xsrf")]
            secrets += [part.partition("=")[2].strip() for part in str(cfg.get("cookie") or "").split(";")]
            secrets += cfg.get("cookie_values") or []
        secrets = [secret for secret in secrets if isinstance(secret, str) and len(secret) >= 8]
        for backend in getattr(self.runtime.accounts, "backends", None) or []:
            cfg = getattr(backend, "cfg", None) or {}
            secrets += [str(cfg[key]) for key in ("account_id", "page_uuid", "thread_id", "base")
                        if cfg.get(key) and len(str(cfg[key])) >= 5]
        return sorted(set(secrets), key=len, reverse=True)

    def _safe(self, value):
        if isinstance(value, str):
            value = redact(value, cookie_values=self.cfg.get("cookie_values", []))
            secrets = [self.cfg.get(key) for key in ("relay_api_key", "cookie", "xsrf")]
            secrets += self.cfg.get("cookie_values", [])
            secrets += self._account_secrets()
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, dict):
            return {key: self._safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._safe(item) for item in value]
        return value

    def _saved(self):
        path = self.cfg.get("_config_path")
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as source:
                saved = json.load(source)
            if not isinstance(saved, dict):
                raise ValueError("configuration is not an object")
            return saved
        except (OSError, ValueError):
            raise apierr.service_unavailable("Cannot read relay settings")

    def _settings(self, saved=None):
        saved = self._saved() if saved is None else saved
        effective = {key: self.cfg.get(key, config.DEFAULTS.get(key)) for key in self.schema}
        desired = {key: saved.get(key, effective[key]) for key in self.schema}
        # Environment overrides remain authoritative after restarting.
        if os.environ.get("RELAY_SESSION_MODE"):
            desired["session_mode"] = effective["session_mode"]
        pending = [key for key in self.schema if desired[key] != effective[key]]
        return {"settings": desired, "effective": effective, "schema": self.schema,
                "pending_restart": pending, "restart_required": bool(pending),
                "writable": bool(self.cfg.get("_config_path"))}

    def _routable(self, entry):
        # Mirror proxy._model_routable so the admin catalog cannot disagree with
        # /v1/models (previously: empty configuration rendered every model as
        # "no resource", and show_unrouted_models / the default model were ignored).
        if self.cfg.get("show_unrouted_models"):
            return True
        if not any(self.cfg.get(key) for key in models.PROVIDER_RESOURCE_KEY.values()):
            return True  # calls would fail with a clear 503; still show the catalog
        if entry["id"] == self.cfg.get("model"):
            return True  # the configured default stays visible
        key = models.PROVIDER_RESOURCE_KEY.get(entry["provider"])
        return bool(key and self.cfg.get(key))

    def _models(self):
        return {"data": [{**entry, "routable": self._routable(entry), "available_verified": None}
                         for entry in models.catalog()], "default_model": self.cfg.get("model")}

    def _accounts(self, entries, usage):
        accounts = []
        for entry, per_account in zip(entries, usage["accounts"]):
            info, backend = entry["info"], entry["backend"]
            error = info.get("error") or info.get("stale_error")
            accounts.append({
                "account": account_report.account_tag(backend), "state": entry["state"],
                "routable": entry["routable"], "reserve": entry["reserve"],
                "available": entry["available"],
                "monthly_allocation": account_report.known(info.get("monthly_allocation")),
                "used_month": account_report.known(info.get("used_month")),
                "reset_date": info.get("reset_date"), "stale": bool(info.get("stale")),
                "session_used": per_account.get("session_used", 0.0), "turns": per_account.get("turns", 0),
                "auth_failures": entry["auth_failures"],
                "last_renewed_at": getattr(getattr(backend, "refresher", None), "last_success_at", None),
                "error": self._safe(str(error))[:200] if error else None,
            })
        return accounts

    def _overview(self, handler):
        runtime = self.runtime
        entries = account_report.collect(runtime)
        summary = account_report.summarize(entries)
        usage = account_report.usage_report(runtime, [(entry["backend"], entry["manager"]) for entry in entries])
        accounts = self._accounts(entries, usage)
        balance = account_report.aggregate_balance([entry["info"] for entry in entries])
        errors = [item["error"] for item in accounts if item["error"]]
        if balance["available"] is None:
            balance["error"] = errors[0] if errors else None
        ok = summary["ok"]
        catalog = self._models()["data"]
        # Counters are sampled without iterating or exposing conversation keys.
        pool = runtime.pool
        port = handler.server.server_address[1]
        host = self.cfg.get("host", "127.0.0.1")
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1" if host == "0.0.0.0" else "::1"
        authority = ("[" + host + "]" if ":" in host else host) + ":" + str(port)
        base_url = "http://" + authority
        return self._safe({
            "health": {"ok": ok, "upstream": "ok" if ok else "degraded", "error": errors[0] if errors else None},
            "uptime_seconds": round(max(0, time.time() - runtime.started), 1),
            "balance": balance, "usage": usage,
            "accounts": accounts,
            "accounts_summary": {key: summary[key] for key in ("total", "routable", "reserve", "states")},
            "limits": {"min_account_balance": self.cfg.get("min_account_balance"),
                       "account_balance_interval": self.cfg.get("account_balance_interval"),
                       "upstream_empty_retries": self.cfg.get("upstream_empty_retries", 1),
                       "max_workers": self.cfg["max_workers"],
                       "max_conversations": self.cfg["max_conversations"]},
            "session": runtime.backend.refresher.status(),
            "sessions": {"active": len(pool._active), "cached": len(pool._conversations),
                         "max_workers": self.cfg["max_workers"],
                         "max_conversations": self.cfg["max_conversations"], "mode": pool.mode},
            "models": {"total": len(catalog), "routable": sum(item["routable"] for item in catalog),
                       "available_verified": None, "default": self.cfg.get("model")},
            "resources": {key: self.cfg[key] for key, field in self.schema.items()
                          if field["type"] in ("integer", "number")},
            "connection": {"openai_base_url": base_url + "/v1", "anthropic_base_url": base_url},
        })

    def _validate(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise apierr.bad_request("'settings' must be a non-empty object")
        for key, value in changes.items():
            field = self.schema.get(key)
            if field is None:
                raise apierr.bad_request("Unknown or protected setting", param=key)
            kind = field["type"]
            valid = ((kind == "boolean" and isinstance(value, bool)) or
                     (kind == "integer" and type(value) is int) or
                     (kind == "number" and type(value) in (int, float) and math.isfinite(value)) or
                     (kind == "string" and isinstance(value, str)))
            if not valid:
                raise apierr.bad_request(f"'{key}' must be {kind}", param=key)
            if "enum" in field and value not in field["enum"]:
                raise apierr.bad_request(f"Unsupported value for '{key}'", param=key)
            if "min" in field and (value < field["min"] or
                                   (field.get("exclusive_minimum") and value == field["min"])):
                raise apierr.bad_request(f"'{key}' is below its minimum", param=key)
            if "max" in field and value > field["max"]:
                # Without an upper bound 1e308 would silently disable the limiter.
                raise apierr.bad_request(f"'{key}' is above its maximum", param=key)
        if "session_mode" in changes and os.environ.get("RELAY_SESSION_MODE"):
            raise apierr.bad_request("session_mode is controlled by RELAY_SESSION_MODE", param="session_mode")

    def _update(self, changes):
        self._validate(changes)
        path = self.cfg.get("_config_path")
        if not path:
            raise apierr.service_unavailable("Relay settings have no writable configuration file")
        # Coordinate with renewal's read/merge/write to preserve rotated credentials.
        with self.lock, contextlib.ExitStack() as locks:
            # Every account's refresher persists rotated cookies through the same
            # file: hold all their locks, not only account 0's.
            backends = getattr(self.runtime.pool, "backends", None) or [self.runtime.backend]
            for refresher_lock in {id(r._lock): r._lock for r in
                                   (getattr(b, "refresher", None) for b in backends) if r}.values():
                locks.enter_context(refresher_lock)
            try:
                locks.enter_context(config.storage_lock(path))
            except TimeoutError:
                raise apierr.service_unavailable("Relay settings are busy: configuration storage is locked")
            saved = self._saved()
            desired = self._settings(saved)["settings"]
            candidate = {**desired, **changes}
            if candidate["session_mode"] == "shared" and not (saved.get("thread_id") or self.cfg.get("thread_id")):
                raise apierr.bad_request("shared mode requires a configured thread_id", param="session_mode")
            if candidate["reasoning_effort"] != "auto":
                models.resolve_tolerant(candidate["model"], candidate["reasoning_effort"], strict=True,
                                        allow_downgrade=candidate["allow_effort_downgrade"])
            changed = [key for key, value in changes.items() if desired[key] != value]
            temporary = None
            try:
                saved.update(changes)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path),
                                                 prefix=".admin-", suffix=".tmp", delete=False) as output:
                    temporary = output.name
                    json.dump(saved, output, ensure_ascii=False, indent=2, allow_nan=False)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
                os.replace(temporary, path)
            except (OSError, ValueError):
                raise apierr.service_unavailable("Cannot save relay settings")
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            applied = [key for key in changes if key in LIVE_SETTINGS]
            journal.emit("admin.settings", "Настройки сохранены: %s" % (", ".join(changed) or "без изменений"),
                         "success", changed={key: {"from": desired[key], "to": changes[key]} for key in changed},
                         applied=applied, restart_required=[key for key in changed if key not in LIVE_SETTINGS])
            if applied:
                limiter = self.runtime.limiter
                with limiter.lock:
                    self.cfg.update({key: changes[key] for key in applied})
                    limiter.rate = self.cfg["rate_limit_rps"]
                    limiter.burst = self.cfg["rate_limit_burst"]
                    limiter.tokens = min(limiter.tokens, limiter.burst)
            return {**self._settings(saved), "saved": True, "changed": changed, "applied": applied}

    def _mutation_guard(self, handler):
        if handler.headers.get("x-admin-request") != "1":
            raise apierr.ApiError(403, "forbidden", "X-Admin-Request: 1 is required")
        if (handler.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
            raise apierr.ApiError(415, "unsupported_media_type", "content-type must be application/json")
        if handler.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise apierr.ApiError(403, "forbidden", "Cross-site administration is blocked")
        origin = handler.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ("http", "https") or parsed.netloc.lower() !=
                    (handler.headers.get("host") or "").lower() or parsed.path or parsed.query or parsed.fragment):
                raise apierr.ApiError(403, "forbidden", "Administration requires a same-origin request")

    def _static(self, handler, path):
        relative = "index.html" if path in ("/admin", "/admin/") else unquote(path[len("/admin/"):])
        suffix_types = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                        ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2",
                        ".woff": "font/woff"}
        if relative not in ("index.html", "favicon.svg") and (not relative.startswith("assets/") or
                                                              Path(relative).suffix.lower() not in suffix_types):
            raise apierr.not_found()
        root = BUILD_ROOT.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or "\\" in relative or "\x00" in relative:
            raise apierr.not_found()
        try:
            body = target.read_bytes()
        except (OSError, ValueError):
            raise apierr.not_found("Admin UI build is unavailable")
        handler._status = 200
        handler.send_response(200)
        handler.send_header("content-type", "text/html; charset=utf-8" if relative == "index.html"
                            else suffix_types[target.suffix.lower()])
        handler.send_header("content-length", str(len(body)))
        handler.send_header("x-request-id", handler.request_id)
        security_headers(handler)
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)

    def _events(self, query):
        data = self.runtime.journal.query(
            since=_int_param(query, "since", 0, 0, 2 ** 62),
            limit=_int_param(query, "limit", 300, 1, 2000),
            levels=_list_param(query, "level"), categories=_list_param(query, "category"),
            text=(query.get("q") or [""])[0][:200] or None,
            turn=(query.get("turn") or [""])[0] or None,
            before=_int_param(query, "before", 0, 0, 2 ** 62) or None)
        last_seq = data[-1]["seq"] if data else self.runtime.journal.last_seq
        return {"data": data, "last_seq": last_seq}

    def _stream(self, handler, query):
        """Server-sent journal events; the key travels in headers via fetch streaming."""
        events = self.runtime.journal
        since = _int_param(query, "since", events.last_seq, 0, 2 ** 62)
        handler._status = 200
        handler.close_connection = True
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream; charset=utf-8")
        handler.send_header("x-accel-buffering", "no")
        handler.send_header("connection", "close")
        handler.send_header("x-request-id", handler.request_id)
        security_headers(handler)
        handler.end_headers()
        writer = sse.StreamWriter(handler)
        writer.event("hello", {"last_seq": events.last_seq, "server_time": time.time()})
        deadline = time.monotonic() + STREAM_MAX_SECONDS
        last_write = time.monotonic()
        while not writer.broken and time.monotonic() < deadline:
            if events.last_seq < since:
                # The buffer was cleared: restart numbering for this follower.
                writer.event("reset", {"last_seq": events.last_seq})
                since = 0
            if events.wait(since, 1.0):
                batch = events.query(since=since, limit=500, order="asc")
                for event in batch:
                    writer.event("journal", event)
                if batch:
                    since = batch[-1]["seq"]
                last_write = time.monotonic()
            elif time.monotonic() - last_write >= STREAM_HEARTBEAT:
                writer.comment("tick")
                last_write = time.monotonic()
            if sse.client_gone(handler):
                break

    def handle(self, handler, path):
        if not path.startswith("/admin/api/"):
            if handler.command not in ("GET", "HEAD"):
                raise apierr.method_not_allowed()
            self._static(handler, path)
            return
        # Credentials stay in headers, never URLs, static markup or responses.
        try:
            auth.verify(handler.headers, "", self.cfg["relay_api_key"])
        except apierr.ApiError as exc:
            # Only a PRESENTED wrong key counts as brute-forcing; missing-key
            # probes stay unthrottled.
            if exc.message == "invalid relay API key":
                wait = auth.register_auth_failure(handler.client_address[0])
                if wait:
                    raise apierr.too_many_requests("too many auth failures", round(wait, 1))
            journal.emit("admin.auth_failed", "Отклонён вход в админку: неверный ключ", "warn",
                         path=path, request_id=handler.request_id)
            raise
        query = parse_qs(getattr(handler, "_query", "") or "")
        if handler.command == "GET":
            if path == "/admin/api/overview":
                payload = self._overview(handler)
            elif path == "/admin/api/models":
                payload = self._models()
            elif path == "/admin/api/settings":
                with self.lock:
                    payload = self._settings()
            elif path == "/admin/api/events":
                payload = self._events(query)
            elif path == "/admin/api/events/stream":
                self._stream(handler, query)
                return
            elif path == "/admin/api/turns":
                payload = {"data": self.runtime.journal.turns(_int_param(query, "limit", 60, 1, 500))}
            elif path == "/admin/api/metrics":
                window = _int_param(query, "window", 3600, 300, 86400)
                auto = max(10, window // 60) if window <= 3600 else 300 if window <= 21600 else 900
                # At most 1440 buckets keep the response small whatever 'bucket' asks for.
                bucket = _int_param(query, "bucket", auto, max(10, -(-window // 1440)), window)
                payload = self.runtime.journal.metrics(window=window, bucket=bucket)
            else:
                raise apierr.not_found()
        elif handler.command == "POST" and path == "/admin/api/session":
            handler.close_connection = True
            self._mutation_guard(handler)
            data = validate.load_json(handler._read_body())
            action = data.get("action")
            if action not in ("login", "logout"):
                raise apierr.bad_request("'action' must be login or logout", param="action")
            journal.emit("admin." + action, "Оператор вошёл в админку" if action == "login"
                         else "Оператор вышел из админки", "info",
                         client=(handler.headers.get("user-agent") or "")[:120])
            payload = {"ok": True}
        elif handler.command == "POST" and path == "/admin/api/events/clear":
            handler.close_connection = True
            self._mutation_guard(handler)
            validate.load_json(handler._read_body())
            self.runtime.journal.reset()
            journal.emit("admin.journal_cleared", "Журнал в памяти очищен оператором", "warn")
            payload = {"ok": True, "last_seq": self.runtime.journal.last_seq}
        elif handler.command == "PATCH" and path == "/admin/api/settings":
            # Guard failures must close a connection whose body was not consumed.
            handler.close_connection = True
            self._mutation_guard(handler)
            data = validate.load_json(handler._read_body())
            if set(data) != {"settings"}:
                raise apierr.bad_request("Expected only a 'settings' object")
            payload = self._update(data["settings"])
        elif path in ("/admin/api/overview", "/admin/api/models", "/admin/api/settings", "/admin/api/events",
                      "/admin/api/turns", "/admin/api/metrics", "/admin/api/session",
                      "/admin/api/events/clear", "/admin/api/events/stream"):
            raise apierr.method_not_allowed()
        else:
            raise apierr.not_found()
        handler._json(200, payload)
