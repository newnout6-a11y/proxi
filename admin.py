"""Local administration: public build assets and authenticated, bounded APIs."""
import contextlib
import json
import math
import os
from pathlib import Path
import re
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
from accounts import enabled as account_enabled, priority as account_priority
from redact import redact
import sse
import validate
from context import DEFAULT_RELAY_SYSTEM_PROMPT


BUILD_ROOT = Path(__file__).resolve().parent / "web" / "dist"
LIVE_SETTINGS = {"rate_limit_rps", "rate_limit_burst", "min_account_balance", "account_balance_interval"}
STREAM_MAX_SECONDS = 600
STREAM_HEARTBEAT = 10.0
POOL_FIELDS = {"enabled", "priority", "label"}
NEW_ACCOUNT_REQUIRED = ("base", "page_uuid", "cookie", "xsrf")
NEW_ACCOUNT_OPTIONAL = ("label", "provider_resource_name", "openai_provider_resource_name")
# Moved into accounts[0] when a single-account configuration becomes a pool.
ACCOUNT_RECORD_KEYS = ("base", "page_uuid", "cookie", "xsrf", "account_id", "thread_id", "label", "enabled",
                       "priority", *models.PROVIDER_RESOURCE_KEY.values())
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
HOSTNAME = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
ACCOUNT_PATH = re.compile(r"/admin/api/accounts/([A-Za-z0-9_-]{1,64})(?:/(balance|renew))?")
SETTING_GROUPS = ("routing", "model", "instructions", "retries", "cache", "exec", "limits", "api")


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


def _plain(value, *, ascii_only=False):
    """No control characters; `ascii_only` also requires printable ASCII (values sent as HTTP headers)."""
    return all(32 <= ord(char) < (127 if ascii_only else 0x110000) and ord(char) != 127 for char in value)


def _schema():
    catalog_ids = [entry["id"] for entry in models.catalog()]
    # (key, type, label, group, description, unit, min, max, enum)
    rows = [
        ("session_mode", "string", "Режим сессий", "routing",
         "Как диалоги клиентов привязываются к тредам Retool: свой тред на диалог, новый на каждый запрос "
         "или один общий", None, None, None, ["per_request", "per_conversation", "shared"]),
        ("min_account_balance", "number", "Порог резерва", "routing",
         "Аккаунт с балансом ниже порога уходит в резерв и получает запросы, только когда ниже порога все",
         "кредитов", 0, 1_000_000, None),
        ("account_balance_interval", "integer", "Проверка баланса", "routing",
         "Как часто спрашивать у Retool баланс каждого включённого аккаунта; 0 — раз в час",
         "с", 0, 86400, None),
        ("session_refresh_interval", "integer", "Продление сессии", "routing",
         "Как часто продлевать сессию Retool каждого включённого аккаунта; 0 — не продлевать по таймеру",
         "с", 0, 86400, None),
        ("account_refresh_parallel", "integer", "Проверок одновременно", "routing",
         "Сколько аккаунтов можно проверять или продлевать одновременно", None, 1, 32, None),
        ("max_conversations", "integer", "Открытые диалоги", "routing",
         "Сколько диалогов relay держит открытыми; при переполнении закрывается самый давний неактивный",
         None, 1, 4096, None),
        ("conversation_idle_timeout", "integer", "Хранение неактивного диалога", "routing",
         "Через сколько секунд простоя диалог закрывается и следующий запрос начнёт новый тред Retool",
         "с", 1, 86400, None),
        ("model", "string", "Модель по умолчанию", "model",
         "Модель для запросов, в которых клиент не указал свою", None, None, None, catalog_ids),
        ("reasoning_effort", "string", "Уровень мышления", "model",
         "Уровень мышления, если клиент его не передал; auto оставляет выбор модели", None, None, None,
         ["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"]),
        ("allow_effort_downgrade", "boolean", "Подбирать ближайший effort", "model",
         "Если модель не поддерживает запрошенный уровень мышления, взять ближайший вместо ошибки",
         None, None, None, None),
        ("show_thinking", "boolean", "Передавать мышление модели", "model",
         "Отдавать клиенту блоки мышления модели, когда Retool их присылает",
         None, None, None, None),
        ("strict_models", "boolean", "Проверять имена моделей", "model",
         "Отклонять запрос с неизвестной моделью вместо подстановки похожей", None, None, None, None),
        ("show_unrouted_models", "boolean", "Показывать модели без маршрута", "model",
         "Перечислять в /v1/models модели, для которых у аккаунтов нет ресурса Retool",
         None, None, None, None),
        ("project_context", "boolean", "Контекст проекта", "model",
         "Добавлять к запросу краткое описание рабочей папки relay", None, None, None, None),
        ("context_max_bytes", "integer", "Размер контекста проекта", "model",
         "Предел описания рабочей папки, которое добавляется к каждому запросу", "байт", 1, 10_000_000, None),
        ("relay_system_prompt_mode", "string", "Применение инструкций", "instructions",
         "Где применяется собственная роль relay; служебные запросы IDE сохраняют свои инструкции",
         None, None, None, ["standalone", "append", "replace"]),
        ("relay_system_prompt", "string", "Инструкции для модели", "instructions",
         "Роль, стиль ответа и рабочие границы. null возвращает стандартную роль, пустая строка убирает собственную роль",
         None, None, None, None),
        ("upstream_empty_retries", "integer", "Повторы пустого ответа", "retries",
         "Сколько раз повторить запрос, если Retool вернул пустой ответ или одно мышление; каждый повтор платный",
         None, 0, 4, None),
        ("upstream_error_max_dumps", "integer", "Дампы ошибок", "retries",
         "Сколько последних наборов диагностики с телом запроса хранить после ошибок; 0 — все",
         None, 0, 10_000, None),
        ("upstream_attempt_summaries_max", "integer", "Сводки попыток", "retries",
         "Сколько последних сводок попыток обращения к Retool хранить на диске; 0 — все", None, 0, 100_000, None),
        ("cache_keepwarm_seconds", "integer", "Интервал прогрева", "cache",
         "Как часто перечитывать начало диалога, чтобы кэш промпта не остыл; 0 — прогрев выключен",
         "с", 0, 3600, None),
        ("cache_keepwarm_idle", "integer", "Прогрев после запроса", "cache",
         "Сколько секунд после последнего запроса диалог ещё прогревают", "с", 1, 86400, None),
        ("cache_keepwarm_during_turns", "boolean", "Прогрев во время хода", "cache",
         "Прогревать кэш и во время длинного хода, пока модель ещё отвечает", None, None, None, None),
        ("cache_keepwarm_turn_offset", "integer", "Первый прогрев в ходе", "cache",
         "Через сколько секунд от начала хода отправить первый прогрев", "с", 30, 285, None),
        ("exec_policy", "string", "Локальные команды", "exec",
         "Какие команды модель может выполнить на этой машине: никакие, из списка разрешённых или любые",
         None, None, None, ["off", "allowlist", "allow_all"]),
        ("show_exec_output", "boolean", "Показывать вывод команд", "exec",
         "Передавать клиенту вывод выполненных команд", None, None, None, None),
        ("command_timeout", "integer", "Ожидание команды", "exec",
         "Сколько ждать завершения одной команды", "с", 1, 86400, None),
        ("command_output_limit", "integer", "Вывод команды", "exec",
         "Сколько вывода команды вернуть модели; 0 — без ограничения", "байт", 0, 100_000_000, None),
        ("max_exec_turns", "integer", "Циклы команд в ходе", "exec",
         "Сколько циклов «команда — ответ» допускается в одном ходе; 0 — без ограничения", None, 0, 1000, None),
        ("max_workers", "integer", "Параллельные запросы", "limits",
         "Сколько запросов relay обслуживает одновременно; остальные ждут", None, 1, 512, None),
        ("rate_limit_rps", "number", "Запросов в секунду", "limits",
         "Сколько запросов в секунду принимать от клиентов; 0 — без ограничения", None, 0, 10_000, None),
        ("rate_limit_burst", "number", "Запас запросов", "limits",
         "Сколько запросов подряд можно принять сверх темпа, прежде чем включится ограничение",
         None, 1, 100_000, None),
        ("turn_timeout", "integer", "Ожидание ответа", "limits",
         "Максимальная длительность одного хода модели", "с", 1, 86400, None),
        ("upstream_timeout", "integer", "Ожидание Retool", "limits",
         "Сколько ждать соединения и ответа на HTTP-запрос к Retool", "с", 1, 3600, None),
        ("recv_timeout", "integer", "Ожидание WebSocket", "limits",
         "Сколько ждать следующего сообщения по WebSocket Retool", "с", 1, 3600, None),
        ("max_prompt_bytes", "integer", "Размер промпта", "limits",
         "Отклонять запросы с промптом больше этого размера; 0 — без ограничения", "байт", 0, 1_073_741_824, None),
        ("max_body_bytes", "integer", "Размер HTTP body", "limits",
         "Максимальный размер тела HTTP-запроса от клиента", "байт", 1, 1_073_741_824, None),
        ("max_frame_bytes", "integer", "Размер WebSocket frame", "limits",
         "Максимальный размер одного кадра WebSocket от Retool", "байт", 1, 1_073_741_824, None),
        ("enable_chat", "boolean", "Chat Completions API", "api",
         "Принимать запросы OpenAI Chat Completions на /v1/chat/completions", None, None, None, None),
        ("enable_responses", "boolean", "Responses API", "api",
         "Принимать запросы OpenAI Responses на /v1/responses", None, None, None, None),
        ("enable_anthropic", "boolean", "Anthropic API", "api",
         "Принимать запросы Anthropic Messages на /v1/messages", None, None, None, None),
    ]
    fields = {}
    for key, kind, label, group, description, unit, minimum, maximum, enum in rows:
        field = {"type": kind, "label": label, "group": group, "description": description,
                 "restart_required": key not in LIVE_SETTINGS}
        if unit:
            field["unit"] = unit
        if minimum is not None:
            field.update(min=minimum, max=maximum)
        if enum is not None:
            field["enum"] = enum
        fields[key] = field
    fields['relay_system_prompt']['nullable'] = True
    fields['relay_system_prompt']['multiline'] = True
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

    def _safe(self, value, secrets=None):
        # Collected once per response, not once per string in it.
        if secrets is None:
            secrets = [self.cfg.get(key) for key in ("relay_api_key", "cookie", "xsrf")]
            secrets += self.cfg.get("cookie_values", [])
            secrets += self._account_secrets()
            secrets = [secret for secret in secrets if secret]
        if isinstance(value, str):
            value = redact(value, cookie_values=self.cfg.get("cookie_values", []))
            for secret in secrets:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, dict):
            return {key: self._safe(item, secrets) for key, item in value.items()}
        if isinstance(value, list):
            return [self._safe(item, secrets) for item in value]
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
                "default_system_prompt": DEFAULT_RELAY_SYSTEM_PROMPT,
                "pending_restart": pending, "restart_required": bool(pending),
                "writable": bool(self.cfg.get("_config_path"))}

    def _routable(self, entry):
        runtime = getattr(self, 'runtime', None)
        backends = getattr(getattr(runtime, 'accounts', None), 'backends', None)
        return models.catalog_routable(entry, self.cfg, backends)

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

    # -- account pool ------------------------------------------------------------
    @staticmethod
    def _records(saved):
        if isinstance(saved.get("accounts"), list):
            return [record for record in saved["accounts"] if isinstance(record, dict)]
        return [saved] if saved.get("base") or saved.get("page_uuid") else []

    @staticmethod
    def _same_account(record, cfg):
        if record.get("account_id") and cfg.get("account_id"):
            return record["account_id"] == cfg["account_id"]
        return (str(record.get("base") or "").rstrip("/") == str(cfg.get("base") or "").rstrip("/")
                and record.get("page_uuid") == cfg.get("page_uuid"))

    @staticmethod
    def _record_tag(record):
        return journal.tag(record.get("account_id") or record.get("page_uuid")) or "account"

    def _live_backends(self):
        return getattr(self.runtime.pool, "backends", None) or [self.runtime.backend]

    def _pool(self):
        """Loaded accounts (with live state) plus accounts saved but not loaded until a restart."""
        runtime = self.runtime
        entries = account_report.collect(runtime)
        usage = account_report.usage_report(runtime, [(entry["backend"], entry["manager"]) for entry in entries])
        records = self._records(self._saved()) if self.cfg.get("_config_path") else None
        pool = []
        for entry, item in zip(entries, self._accounts(entries, usage)):
            backend = entry["backend"]
            removed = records is not None and not any(self._same_account(r, backend.cfg) for r in records)
            pool.append({**item, "label": backend.cfg.get("label") or "", "enabled": account_enabled(backend),
                         "priority": account_priority(backend),
                         "last_balance_at": getattr(entry["manager"], "last_fetched_at", None),
                         "pending": "removed" if removed else None, "_backend": backend})
        loaded = [entry["backend"].cfg for entry in entries]
        for record in records or []:
            if any(self._same_account(record, cfg) for cfg in loaded):
                continue
            pool.append({
                "account": self._record_tag(record), "label": record.get("label") or "",
                "enabled": record.get("enabled", True) is not False,
                "priority": record["priority"] if type(record.get("priority")) is int else 0,
                "state": "pending", "routable": False, "reserve": False, "available": None,
                "monthly_allocation": None, "used_month": None, "reset_date": None, "stale": False,
                "session_used": 0.0, "turns": 0, "auth_failures": 0, "last_renewed_at": None,
                "last_balance_at": None, "error": None, "pending": "added", "_record": record})
        return pool

    @staticmethod
    def _public(account):
        return {key: value for key, value in account.items() if not key.startswith("_")}

    def _find(self, tag, pool=None):
        for account in self._pool() if pool is None else pool:
            if account["account"] == tag:
                return account
        raise apierr.not_found("Unknown account")

    def _account_response(self, tag, **extra):
        return self._safe({**extra, "account": self._public(self._find(tag))})

    def _pool_response(self):
        pool = self._pool()
        live = [account for account in pool if account["pending"] != "removed"]
        states = {}
        for account in live:
            states[account["state"]] = states.get(account["state"], 0) + 1
        return self._safe({
            "accounts": [self._public(account) for account in pool],
            "summary": {"total": len(live), "enabled": sum(account["enabled"] for account in live),
                        "routable": sum(account["routable"] for account in live),
                        "reserve": sum(account["reserve"] for account in live), "states": states},
            "limits": {key: self.cfg.get(key, config.DEFAULTS.get(key))
                       for key in ("min_account_balance", "account_balance_interval", "account_refresh_parallel")},
            "restart_required": any(account["pending"] for account in pool),
        })

    def _saved_record(self, saved, account):
        backend = account.get("_backend")
        for record in self._records(saved):
            if (self._same_account(record, backend.cfg) if backend is not None
                    else self._record_tag(record) == account["account"]):
                return record
        raise apierr.conflict("The account changed in config.json; reload the page")

    def _other_enabled(self, saved, backend):
        """Loaded, still configured and enabled accounts other than `backend`."""
        records = self._records(saved)
        return [other for other in self._live_backends() if other is not backend and account_enabled(other)
                and any(self._same_account(record, other.cfg) for record in records)]

    @staticmethod
    def _write(path, saved):
        try:
            config._write_file(path, saved)
        except (OSError, ValueError):
            raise apierr.service_unavailable("Cannot save relay settings")

    @staticmethod
    def _apply_live(backend, changes):
        backend.cfg.update(changes)
        reference = backend.cfg.get("_account_ref")
        if isinstance(reference, dict) and reference is not backend.cfg:
            reference.update(changes)

    def _patch_account(self, tag, changes):
        if not changes:
            raise apierr.bad_request("Expected enabled, priority or label")
        for key, value in changes.items():
            if key not in POOL_FIELDS:
                raise apierr.bad_request("Unknown or protected account field", param=key)
            valid = ((key == "enabled" and type(value) is bool) or
                     (key == "priority" and type(value) is int and 0 <= value <= 99) or
                     (key == "label" and isinstance(value, str) and len(value.strip()) <= 40 and _plain(value)))
            if not valid:
                raise apierr.bad_request(f"Invalid value for '{key}'", param=key)
        if "label" in changes:
            changes["label"] = changes["label"].strip()
        account = self._find(tag)
        if account["pending"] == "removed":
            raise apierr.conflict("The account is removed from config.json and leaves after a restart")
        backend = account.get("_backend")
        with self._transaction() as (path, saved):
            record = self._saved_record(saved, account)
            if changes.get("enabled") is False and backend is not None and account_enabled(backend):
                if not self._other_enabled(saved, backend):
                    raise apierr.conflict("The last enabled account cannot be switched off")
                if self.runtime.pool.mode == "shared" and backend is self.runtime.pool.backend:
                    raise apierr.conflict("Shared session mode serves only the first account")
            record.update(changes)
            self._write(path, saved)
            if backend is not None:
                self._apply_live(backend, changes)
        journal.emit("admin.account", "Аккаунт %s изменён: %s" % (tag, ", ".join(sorted(changes))), "info",
                     account=tag, changed=changes)
        return self._account_response(tag)

    def _loaded(self, tag):
        account = self._find(tag)
        backend = account.get("_backend")
        if backend is None or backend not in self.runtime.accounts.state:
            raise apierr.conflict("The account is not loaded yet; restart the relay")
        return backend

    def _check_balance(self, tag):
        backend = self._loaded(tag)
        self.runtime.accounts.check_balance(backend)
        journal.emit("admin.account", "Баланс аккаунта %s запрошен вручную" % tag, "info", account=tag)
        return self._account_response(tag)

    def _renew(self, tag):
        backend = self._loaded(tag)
        if getattr(backend, "refresher", None) is None:
            raise apierr.conflict("The account has no session to renew")
        error = None
        try:
            ok = bool(self.runtime.accounts.refresh(backend))
        except Exception as exc:  # noqa: BLE001 - a failed renewal is an answer, not a server error
            ok, error = False, type(exc).__name__
        if not ok:
            error = getattr(backend.refresher, "last_error", None) or error or "session renewal failed"
        journal.emit("admin.account", "Сессия аккаунта %s %s вручную" % (tag, "продлена" if ok else "не продлена"),
                     "success" if ok else "warn", account=tag)
        return self._account_response(tag, ok=ok, error=self._safe(str(error))[:200] if error else None)

    @staticmethod
    def _new_account(data):
        unknown = set(data) - set(NEW_ACCOUNT_REQUIRED) - set(NEW_ACCOUNT_OPTIONAL)
        if unknown:
            raise apierr.bad_request("Unknown account field", param=sorted(unknown)[0])
        record = {}
        for key in NEW_ACCOUNT_REQUIRED + NEW_ACCOUNT_OPTIONAL:
            value = data.get(key)
            if value is None and key in NEW_ACCOUNT_OPTIONAL:
                continue
            if not isinstance(value, str) or not value.strip():
                if key in NEW_ACCOUNT_OPTIONAL and isinstance(value, str):
                    continue
                raise apierr.bad_request(f"'{key}' must be a non-empty string", param=key)
            value = value.strip()
            # Everything but the label becomes part of an HTTP request: printable ASCII only,
            # so a control character cannot split a header and a header cannot fail to encode.
            if len(value) > 16384 or not _plain(value, ascii_only=key != "label"):
                raise apierr.bad_request(f"Invalid value for '{key}'", param=key)
            record[key] = value
        parsed = urlsplit(record["base"])
        try:
            port = parsed.port
        except ValueError:
            raise apierr.bad_request("'base' has an invalid port", param="base")
        if (parsed.scheme != "https" or not HOSTNAME.fullmatch(parsed.hostname or "") or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise apierr.bad_request("'base' must be an https origin such as https://tenant.retool.com", param="base")
        # Rebuilt from its parts: no trailing ':' or default port survives into the config
        record["base"] = "https://" + parsed.hostname + (f":{port}" if port and port != 443 else "")
        if not UUID.fullmatch(record["page_uuid"]):
            raise apierr.bad_request("'page_uuid' must be a UUID", param="page_uuid")
        record["page_uuid"] = record["page_uuid"].lower()
        if len(record.get("label", "")) > 40:
            raise apierr.bad_request("'label' is longer than 40 characters", param="label")
        record.update(enabled=True, priority=0)
        return record

    def _add_account(self, data):
        record = self._new_account(data)
        with self._transaction() as (path, saved):
            for existing in self._records(saved):
                if (str(existing.get("base") or saved.get("base") or "").rstrip("/").lower() == record["base"]
                        and str(existing.get("page_uuid") or "").lower() == record["page_uuid"]):
                    raise apierr.conflict("This Retool app is already in the pool")
            if not isinstance(saved.get("accounts"), list):
                # A single-account configuration becomes a pool; the account keeps its id and session.
                # Its session must be in the file: RETOOL_COOKIE/RETOOL_XSRF are not, and a pool
                # record without them would stop the relay from starting.
                if not (saved.get("cookie") and saved.get("xsrf")) or not self.cfg.get("_persist_auth", True):
                    raise apierr.conflict("The current account's session comes from RETOOL_COOKIE/RETOOL_XSRF, "
                                          "not config.json; move it into the file before adding accounts")
                first = {key: saved[key] for key in ACCOUNT_RECORD_KEYS if key in saved}
                if not first.get("account_id") and self.cfg.get("account_id"):
                    first["account_id"] = self.cfg["account_id"]
                for key in ("cookie", "xsrf"):
                    saved.pop(key, None)
                saved["accounts"] = [first]
            saved["accounts"].append(record)
            for item in saved["accounts"]:
                if isinstance(item, dict) and not all(item.get(key) for key in ("cookie", "xsrf", "base", "page_uuid")):
                    raise apierr.conflict("An account in config.json has no stored session; fix the file first")
            config._assign_account_ids(saved)
            self._write(path, saved)
        tag = self._record_tag(record)
        journal.emit("admin.account", "Добавлен аккаунт %s, подключится после перезапуска" % tag, "success",
                     account=tag, label=record.get("label", ""))
        return self._account_response(tag, restart_required=True)

    def _delete_account(self, tag):
        account = self._find(tag)
        if account["pending"] == "removed":
            raise apierr.conflict("The account is already removed and leaves after a restart")
        backend = account.get("_backend")
        with self._transaction() as (path, saved):
            record = self._saved_record(saved, account)
            if not isinstance(saved.get("accounts"), list) or len(self._records(saved)) <= 1:
                raise apierr.conflict("The last account cannot be removed")
            if backend is not None and account_enabled(backend) and not self._other_enabled(saved, backend):
                raise apierr.conflict("The last enabled account cannot be removed")
            if backend is not None and self.runtime.pool.mode == "shared" and backend is self.runtime.pool.backend:
                raise apierr.conflict("Shared session mode serves only the first account")
            remaining = [item for item in saved["accounts"] if item is not record]
            if not any(isinstance(item, dict) and item.get("enabled", True) is not False for item in remaining):
                raise apierr.conflict("No enabled account would remain")
            saved["accounts"] = remaining
            if self._same_account(saved, record) and (saved.get("base") or saved.get("page_uuid")):
                self._scrub_root(saved, record, remaining)
            self._write(path, saved)
            if backend is not None:
                self._apply_live(backend, {"enabled": False})
        journal.emit("admin.account", "Аккаунт %s удалён из config.json" % tag, "warn", account=tag)
        return {"ok": True, "pending": "removed" if backend is not None else None,
                "restart_required": backend is not None}

    def _scrub_root(self, saved, record, remaining):
        """The root mirrored the removed account: drop its identity and session there (a
        rotated cookie lives only in accounts[i], so the root copy may be older and differ);
        config.load fills base/page_uuid/cookie/xsrf from the first remaining account."""
        for key in ("base", "page_uuid", "cookie", "xsrf", "account_id"):
            saved.pop(key, None)
        if (saved.get("session_mode") or self.cfg.get("session_mode")) != "shared":
            saved.pop("thread_id", None)
        base = str(record.get("base") or "").rstrip("/")
        for key in models.PROVIDER_RESOURCE_KEY.values():
            if key not in saved:
                continue
            value = saved.pop(key)
            # Same-tenant accounts without their own resource inherited the root's: give it to
            # them explicitly, so nothing depends on which account becomes the new root.
            for item in remaining:
                if isinstance(item, dict) and key not in item and str(item.get("base") or "").rstrip("/") == base:
                    item[key] = value
        # The root now mirrors the first remaining account, its resources included, so model
        # listings that read the root (/v1/models) keep seeing the routes that exist.
        first = next((item for item in remaining if isinstance(item, dict)), {})
        for key in models.PROVIDER_RESOURCE_KEY.values():
            if first.get(key):
                saved[key] = first[key]

    def _pool_route(self, handler, path, match):
        allowed = ("GET", "POST") if match is None else ("POST",) if match.group(2) else ("PATCH", "DELETE")
        if handler.command not in allowed:
            raise apierr.method_not_allowed()
        if handler.command == "GET":
            return self._pool_response()
        # Guard failures must close a connection whose body was not consumed.
        handler.close_connection = True
        self._mutation_guard(handler)
        data = validate.load_json(handler._read_body())
        if match is None:
            return self._add_account(data)
        tag, action = match.groups()
        if action == "balance":
            return self._check_balance(tag)
        if action == "renew":
            return self._renew(tag)
        if handler.command == "PATCH":
            return self._patch_account(tag, data)
        return self._delete_account(tag)

    def _validate(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise apierr.bad_request("'settings' must be a non-empty object")
        for key, value in changes.items():
            field = self.schema.get(key)
            if field is None:
                raise apierr.bad_request("Unknown or protected setting", param=key)
            kind = field["type"]
            if value is None and field.get('nullable'):
                continue
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

    @contextlib.contextmanager
    def _transaction(self):
        """Read/merge/write of config.json, serialized with renewal's cookie rotation."""
        path = self.cfg.get("_config_path")
        if not path:
            raise apierr.service_unavailable("Relay settings have no writable configuration file")
        with self.lock, contextlib.ExitStack() as locks:
            # Every account's refresher persists rotated cookies through the same
            # file: hold all their locks, not only account 0's. A renewal holds its lock
            # for its HTTP round trip, so an admin write may wait for one to finish (up to
            # upstream_timeout); storage_lock alone would keep the file consistent, the
            # refresher locks keep a renewal from reading the file mid-transaction.
            backends = getattr(self.runtime.pool, "backends", None) or [self.runtime.backend]
            for refresher_lock in {id(r._lock): r._lock for r in
                                   (getattr(b, "refresher", None) for b in backends) if r}.values():
                locks.enter_context(refresher_lock)
            try:
                locks.enter_context(config.storage_lock(path))
            except TimeoutError:
                raise apierr.service_unavailable("Relay settings are busy: configuration storage is locked")
            yield path, self._saved()

    def _update(self, changes):
        self._validate(changes)
        with self._transaction() as (path, saved):
            desired = self._settings(saved)["settings"]
            candidate = {**desired, **changes}
            # Judge what the next start will see: the file or RETOOL_THREAD_ID, not the running cfg
            # (a deleted account's root thread_id may still be in memory).
            if candidate["session_mode"] == "shared" and not (saved.get("thread_id") or os.environ.get("RETOOL_THREAD_ID")):
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
                dispatcher = self.runtime.accounts
                if {"min_account_balance", "account_balance_interval"} & set(applied):
                    with dispatcher._lock:
                        dispatcher.threshold = float(self.cfg["min_account_balance"])
                        dispatcher.balance_interval = float(self.cfg["account_balance_interval"] or 0)
                        # Re-judge every account against the new threshold at the next tick.
                        now = dispatcher.clock()
                        for state in dispatcher.state.values():
                            state["next_balance"] = min(state["next_balance"], now)
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
        account_match = ACCOUNT_PATH.fullmatch(path)
        if path == "/admin/api/accounts" or account_match:
            handler._json(200, self._pool_route(handler, path, account_match))
            return
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
