"""OpenAI/Anthropic compatible relay over the Retool AI agent WebSocket."""
import asyncio
import argparse
import concurrent.futures
import json
import os
import queue
import signal
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import account_report
import adapters
import admin as admin_module
import apierr
import auth
import journal
import client_context
import console_log
from redact import redact
import config as config_module
import models as model_catalog
import sse
import models
import validate
from accounts import AccountDispatcher
from credits import CreditsManager
from orchestrator import TurnRunner
from session import Backend, SessionPool
from upstream_diagnostics import WireTrace
from io_workers import DaemonIOExecutor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))


class RateLimiter:
    def __init__(self, rate, burst):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.stamp = time.monotonic()
        self.lock = threading.Lock()

    def take(self):
        if self.rate <= 0:
            return 0.0
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now
            if self.tokens < 1.0:
                return max(0.05, (1.0 - self.tokens) / self.rate)
            self.tokens -= 1.0
            return 0.0


class Runtime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.loop = asyncio.new_event_loop()
        self._lifecycle_lock = threading.Lock()
        self._closing = False
        # Each direct turn holds one executor thread for its whole stream; the
        # default pool (~min(32, cpu+4)) silently capped concurrent turns below
        # max_workers and starved keepwarm pings / refreshes behind them.
        self._io_executor = DaemonIOExecutor(max(32, int(cfg.get("max_workers", 32)) * 2 + 8))
        self.loop._relay_io_executor = self._io_executor
        self._loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._loop_thread.start()
        self.backend = Backend(cfg)
        self.pool = SessionPool(self.backend)
        if getattr(self.pool, "backends", None):
            self.backend = self.pool.backend
        backends = getattr(self.pool, "backends", None) or [self.backend]
        managers = {}
        for backend in backends:
            if not hasattr(backend, "refresher"):
                continue
            managers[backend] = CreditsManager(
                backend.cfg.get("base", cfg.get("base")),
                (lambda b=backend: auth.upstream_headers(b.cfg)), backend.refresher)
        self.credits = managers.get(self.backend) or CreditsManager(
            self.backend.cfg.get("base", cfg.get("base")),
            lambda: auth.upstream_headers(self.backend.cfg), getattr(self.backend, "refresher", None))
        self.accounts = AccountDispatcher(list(managers), cfg, managers)
        self.runner = TurnRunner(cfg, self.credits, self.pool)
        self.runner.accounts = self.accounts
        self.limiter = RateLimiter(cfg["rate_limit_rps"], cfg["rate_limit_burst"])
        self.started = time.time()
        self.journal = journal.JOURNAL
        self.journal.configure(cfg, cfg.get("journal_path"))
        self.admin = admin_module.Admin(self)

    def submit(self, coroutine):
        with self._lifecycle_lock:
            if self._closing:
                coroutine.close()
                raise apierr.service_unavailable("relay is shutting down")
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    async def _shutdown_tasks(self):
        self.loop._relay_closing = True
        sessions = {record[0] for record in self.pool._active.values()}
        sessions.update(self.pool._conversations.values())
        if self.pool._shared is not None:
            sessions.add(self.pool._shared)
        for session in sessions:
            session.cancel()
        if sessions:
            await asyncio.gather(*(session.aclose() for session in sessions), return_exceptions=True)
        # Session cleanup can finish turn generators; drain those tasks as well.
        while True:
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            if not tasks:
                break
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.loop.shutdown_asyncgens()

    def close(self):
        with self._lifecycle_lock:
            if self._closing:
                return
            self._closing = True
        future = asyncio.run_coroutine_threadsafe(self._shutdown_tasks(), self.loop)
        try:
            future.result(timeout=10)
        except concurrent.futures.TimeoutError:
            future.cancel()
            journal.emit("relay.shutdown", "Истекло время завершения задач relay", "warn")
        finally:
            self._io_executor.shutdown(wait=False, cancel_futures=True)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._loop_thread.join(timeout=2)
            if not self._loop_thread.is_alive():
                self.loop.close()


ROUTES = {
    ("POST", "/v1/chat/completions"): "chat",
    ("POST", "/v1/responses"): "responses",
    ("POST", "/v1/messages"): "anthropic",
    ("POST", "/v1/messages/count_tokens"): "count_tokens",
    ("GET", "/v1/models"): "models",
    ("GET", "/v1/models/"): "models",
    ("GET", "/codex/models"): "codex_models",
    ("GET", "/v1/balance"): "balance",
    ("GET", "/v1/usage"): "usage",
    ("GET", "/health"): "health",
}


def make_handler(runtime: Runtime):
    cfg = runtime.cfg

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "retool-relay/2"
        # Idle keep-alive reads die after 75s: without this, 32 silent connections
        # hold every worker slot forever (slowloris on a single-process relay).
        # SSE heartbeats (10-15s) keep legitimate streams well below it.
        timeout = 75

        def log_message(self, fmt, *args):
            return

        # -- helpers ------------------------------------------------------
        def _cors(self):
            if admin_module.is_admin_path(urlparse(self.path).path):
                admin_module.security_headers(self)
                return
            self.send_header("access-control-allow-origin", "*")
            self.send_header("access-control-allow-headers",
                             "authorization, x-api-key, anthropic-version, anthropic-beta, content-type, "
                             "x-request-id, x-session-affinity, x-session-id, x-opencode-session, "
                             "x-proxi-client, x-proxi-session-id, x-proxi-chat-title, x-proxi-agent, "
                             "x-proxi-parent-session-id, x-opencode-session-id, x-opencode-parent-session-id, "
                             "x-claude-code-session-id, x-claude-code-agent-id")
            self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
            self.send_header("access-control-expose-headers", "x-request-id, request-id, retry-after, "
                             "x-proxi-ignored-parameters, x-proxi-system-messages, "
                             "x-proxi-thinking-display, x-proxi-output-format")

        def _json(self, status, payload, extra=None):
            self._status = status
            self._response_started = True
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("x-request-id", self.request_id)
            self.send_header("request-id", self.request_id)
            if extra:
                for key, value in extra.items():
                    self.send_header(key, str(value))
            self._cors()
            self.end_headers()
            trace = getattr(self, "_client_trace", None)
            if trace:
                trace.event("client_response_headers", http_status=status, content_type="application/json")
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                    if trace:
                        trace.raw(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    if trace:
                        trace.event("client_write_failed")
                    self.close_connection = True

        # -- entry --------------------------------------------------------
        def do_OPTIONS(self):
            self.request_id = uuid.uuid4().hex[:12]
            self.send_response(204)
            self.send_header("x-request-id", self.request_id)
            self.send_header("request-id", self.request_id)
            self._cors()
            self.send_header("content-length", "0")
            self.end_headers()

        def do_GET(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def do_HEAD(self):
            self._dispatch()

        def do_PUT(self):
            self._dispatch()

        def do_PATCH(self):
            self._dispatch()

        def do_DELETE(self):
            self._dispatch()

        def do_TRACE(self):
            self._dispatch()

        def _log(self, note=""):
            status = getattr(self, "_status", 0) or getattr(self, "_stream_status", 0)
            took = (time.time() - self._started) * 1000
            model = getattr(self, "_model", "-")
            effort = getattr(self, "_effort_str", "")
            effort_tag = f" [{effort}]" if effort and model != "-" else ""
            route = getattr(self, "_log_route", None)
            local_note = {"count_tokens": "[local-token-count]", "models": "[local-model-catalog]"}.get(route, "")
            console_log.write("relay %s %s %s %s %s%s %.0fms %s" % (
                self.request_id, self.command, redact(self.path), status, model, effort_tag, took,
                " ".join(part for part in (client_context.label(), local_note, note) if part)))
            if route in ("chat", "responses", "anthropic") and getattr(self, "_model_called", False):
                backend, manager = self._serving_account()
                snapshot = getattr(manager, "balance_snapshot", None)
                if snapshot:
                    tag = account_report.account_tag(backend) if backend is not None and len(runtime.accounts.backends) > 1 else None
                    console_log.write(console_log.balance_line(self.request_id, snapshot(), self._credits_used, account=tag))
            # Admin polling and live streams would drown the journal in its own traffic.
            if route == "admin" and self.command in ("GET", "HEAD") and 0 < status < 400:
                return
            level = "error" if status >= 500 or status == 0 else "warn" if status >= 400 else "success"
            journal.emit("http.request", "%s %s → %s" % (self.command, self.path, status or "—"), level,
                         request_id=self.request_id, method=self.command, path=self.path, route=route,
                         status=status, duration_ms=round(took), model=None if model == "-" else model,
                         stream=bool(getattr(self, "_streaming", False)),
                         user_agent=self.headers.get("user-agent", "")[:120] if self.headers else "",
                         error=getattr(self, "_error_code", None))

        def _serving_account(self):
            """The account that answered this request, else the primary one."""
            lease = (getattr(self, "_holder", None) or {}).get("session")
            backend = getattr(getattr(lease, "session", lease), "backend", None)
            if backend is None:
                return None, runtime.credits
            if backend is runtime.backend:
                return backend, runtime.credits
            manager = getattr(backend, "credits", None) or runtime.accounts.managers.get(backend)
            return backend, manager or runtime.credits

        def _dispatch(self):
            self.request_id = uuid.uuid4().hex[:12]
            context_token = client_context.bind({"request_id": self.request_id})
            self._started = time.time()
            # Keep-alive reuses the handler: a stale status from the previous
            # request (e.g. a 404 probe) must not leak into this log line.
            self._status = 0
            self._stream_status = 0
            self._streaming = False
            self._model = "-"
            self._effort_str = ""
            self._credits_used = None
            self._model_called = False
            self._holder = None
            self._error_code = None
            self._response_started = False
            self._client_trace = None
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            self.path = path
            self._query = parsed_url.query
            route = ROUTES.get((self.command, path))
            # /v1/responses clients need their own error envelope: a chat-shaped
            # error without type:"error" leaves a strict SDK waiting forever.
            dialect = {"anthropic": "anthropic", "responses": "responses"}.get(route, "chat")
            try:
                self._log_route = route
                if admin_module.is_admin_path(path):
                    self._log_route = "admin"
                    runtime.admin.handle(self, path)
                    return
                if route is None:
                    if any(method_path[1] == path for method_path in ROUTES):
                        raise apierr.method_not_allowed()
                    raise apierr.not_found()
                if route != "health":
                    query = parse_qs(parsed_url.query)
                    try:
                        auth.verify(self.headers, (query.get("key") or [""])[0], cfg["relay_api_key"])
                    except apierr.ApiError as exc:
                        # A missing key is a routine probe; a PRESENTED wrong key is
                        # brute-forcing and gets the always-on per-source budget.
                        if exc.message == "invalid relay API key":
                            self._throttle_auth_failure()
                        raise
                if route == "chat":
                    if not cfg["enable_chat"]:
                        raise apierr.not_found("chat API disabled")
                    self._completion("chat")
                elif route == "responses":
                    if not cfg["enable_responses"]:
                        raise apierr.not_found("responses API disabled")
                    self._completion("responses")
                elif route == "anthropic":
                    if not cfg["enable_anthropic"]:
                        raise apierr.not_found("anthropic API disabled")
                    self._completion("anthropic")
                elif route == "count_tokens":
                    if not cfg["enable_anthropic"]:
                        raise apierr.not_found("anthropic API disabled")
                    self._count_tokens()
                elif route == "models":
                    self._models()
                elif route == "codex_models":
                    self._json(200, model_catalog.codex_catalog(
                        [entry for entry in model_catalog.catalog() if self._model_routable(entry)]))
                elif route == "balance":
                    report = self._balance_report()
                    self._json(200 if report["available"] is not None else 503, report)
                elif route == "usage":
                    self._json(200, self._usage_report())
                elif route == "health":
                    self._health()
            except apierr.ApiError as err:
                self._error_code = err.code
                self._log_error(err)
                if err.status >= 400:
                    console_log.write("[err] %s %s %s" % (err.status, err.code, (err.message or '')[:160]))
                if getattr(self, "_streaming", False):
                    writer = self._writer
                    writer.event("error", self._error_payload(dialect, err))
                    # Journal must not report a mid-stream failure as 200/success.
                    self._stream_status = err.status
                    writer.done()
                else:
                    self._respond_error(err, dialect)
            except Exception as exc:  # never leak a bare traceback
                console_log.write("relay %s EXC %s: %s" % (self.request_id, type(exc).__name__, str(exc)[:200]))
                self._error_code = "internal_error"
                journal.emit("http.exception", "%s: %s" % (type(exc).__name__, str(exc)[:300]), "error",
                             request_id=self.request_id, path=self.path)
                err = apierr.internal(str(exc) or exc.__class__.__name__)
                if getattr(self, "_streaming", False):
                    writer = self._writer
                    writer.event("error", self._error_payload(dialect, err))
                    self._stream_status = err.status
                    writer.done()
                else:
                    self._respond_error(err, dialect)
            finally:
                try:
                    self._log()
                finally:
                    trace = self._client_trace
                    if trace:
                        http_status = self._status or (200 if self._streaming and self._response_started
                                                       else self._stream_status)
                        trace.event("client_end", http_status=http_status,
                                    stream_status=self._stream_status, error=self._error_code)
                        trace.close()
                        if trace.failed:
                            journal.emit("client.trace_failed", "Не удалось сохранить подробную трассу клиента",
                                         "warn", request_id=self.request_id)
                    client_context.reset(context_token)

        def _log_error(self, err):
            journal.emit("http.error", err.message, "error" if err.status >= 500 else "warn",
                         status=err.status, code=err.code, param=err.param, model=self._model,
                         effort=self._effort_str or None,
                         stage=getattr(err, "stage", "upstream" if hasattr(err, "upstream_status") else "relay"),
                         upstream_status=getattr(err, "upstream_status", None),
                         upstream_attempts=getattr(err, "upstream_attempts", None),
                         upstream_request_id=getattr(err, "upstream_request_id", None),
                         diagnostic_id=getattr(err, "diagnostic_id", None))

        def _error_payload(self, dialect, err):
            body = adapters.error_payload(dialect, err)
            body["request_id"] = self.request_id
            if getattr(err, "diagnostic_id", None):
                body["diagnostic_id"] = err.diagnostic_id
            if hasattr(err, "upstream_attempts"):
                body["upstream_attempts"] = err.upstream_attempts
            return body

        def _respond_error(self, err, dialect):
            if getattr(self, "_response_started", False):
                # A status line already went out (a failed _json/_static write or a
                # finished SSE): appending a second one corrupts the stream.
                self.close_connection = True
                return
            self._error_response(err, dialect)

        def _error_response(self, err, dialect):
            # Non-stream HTTP errors use the standard OpenAI envelope for both chat
            # and responses routes; the flat responses shape is for SSE events only.
            body = self._error_payload("chat" if dialect == "responses" else dialect, err)
            extra = {"retry-after": err.retry_after} if err.retry_after else None
            self._json(err.status, body, extra)

        def _throttle_auth_failure(self):
            # rate_limit_rps is off by default and covers only completions, so key
            # brute-forcing gets its own always-on budget per source address.
            wait = auth.register_auth_failure(self.client_address[0])
            if wait:
                raise apierr.too_many_requests("too many auth failures", round(wait, 1))

        def _count_tokens(self):
            """Claude Code asks for a token estimate before sending a turn."""
            data = validate.load_json(self._read_body())
            self._bind_client(data)
            client_context.bind({**client_context.current(), "request_purpose": "local-token-count"})
            # The real count_tokens endpoint does not require max_tokens.
            data.setdefault("max_tokens", 1)
            parsed = validate.parse_anthropic(data, cfg)
            text = parsed.system + "\n" + parsed.prompt
            if parsed.tools:
                text += "\n" + json.dumps(parsed.tools, ensure_ascii=False, separators=(",", ":"))
            self._json(200, {"input_tokens": adapters.approx_tokens(text)}, {"x-proxi-token-count": "estimated"})

        def _models(self):
            entries = [model for model in model_catalog.catalog() if self._model_routable(model)]
            data = [{
                "id": model["id"],
                "object": "model",
                "owned_by": model["provider"],
                "label": model["label"],
                "context_window": model["context"],
                "max_output_tokens": model.get("max_output"),
                **model_catalog.desktop_metadata(model, entries),
                **{key: model[key] for key in ("metadata_checked_at", "metadata_source", "metadata_status", "metadata_notes")
                   if key in model},
                "capabilities": {"reasoning": model["reasoning"], "tools": model["tools"],
                                 "vision": bool(model.get("vision", False)),
                                 "reasoning_effort_levels": model.get("reasoning_effort_levels", []),
                                 "default_reasoning_effort": model.get("default_reasoning_effort"),
                                 "reasoning_effort_control": model.get("reasoning_effort_control", "selectable")},
            } for model in entries]
            self._json(200, {"object": "list", "data": data})

        def _model_routable(self, model) -> bool:
            return model_catalog.catalog_routable(model, cfg, runtime.accounts.backends)

        def _balance_report(self):
            return account_report.balance_report(account_report.collect(runtime, force_refresh=True))

        def _usage_report(self):
            return account_report.usage_report(runtime)

        def _health(self):
            # An account counts when the dispatcher routes to it and its own balance
            # check is fresh; one healthy account keeps the relay serving.
            entries = account_report.collect(runtime)
            summary = account_report.summarize(entries)
            ok = summary["ok"]
            # /health is the only unauthenticated route and carries CORS *: it must
            # expose readiness booleans, not organization financials or raw
            # upstream error text — those live behind /v1/balance with the key.
            refresher = getattr(account_report.serving_entry(entries)["backend"], "refresher", None)
            session = {key: value for key, value in (refresher or runtime.backend.refresher).status().items()
                       if not isinstance(value, str)}
            payload = {
                "ok": ok,
                "uptime_seconds": round(time.time() - runtime.started, 1),
                "session_mode": cfg["session_mode"],
                "models": sum(1 for model in model_catalog.catalog() if self._model_routable(model)),
                "upstream": "ok" if ok else "degraded",
                "balance_known": summary["balance_known"],
                "session": session,
                "accounts": {"total": summary["total"], "routable": summary["routable"],
                             "reserve": summary["reserve"], "states": summary["states"]},
            }
            self._json(200 if ok else 503, payload)

        # -- completions --------------------------------------------------
        def _bind_client(self, data):
            client_context.bind({**client_context.describe(data, self.headers, cfg), "request_id": self.request_id})
            self._model = client_context.clean(data.get("model"), 100) or "-"
            output = data.get("output_config")
            reasoning = data.get("reasoning")
            effort = (output.get("effort") if isinstance(output, dict) else None) or data.get("reasoning_effort")
            if not effort and isinstance(reasoning, dict):
                effort = reasoning.get("effort")
            self._effort_str = client_context.clean(effort, 30)

        def _read_body(self):
            raw_len = self.headers.get("content-length")
            if raw_len is None:
                self.close_connection = True
                raise apierr.bad_request("Content-Length header is required")
            try:
                length = int(raw_len)
            except (TypeError, ValueError):
                # Body length is unknown, so the connection cannot be reused.
                self.close_connection = True
                raise apierr.bad_request("Content-Length must be an integer")
            if length <= 0:
                raise apierr.bad_request("empty request body")
            if length > cfg["max_body_bytes"]:
                self.close_connection = True
                raise apierr.payload_too_large(cfg["max_body_bytes"])
            ctype = (self.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype and ctype != "application/json":
                # The body stays unread: keep-alive would parse it as the next
                # request line (request-smuggling style desync).
                self.close_connection = True
                raise apierr.ApiError(415, "unsupported_media_type", "content-type must be application/json")
            try:
                return self.rfile.read(length)
            except Exception:
                # The body was not fully consumed: keep-alive would parse the
                # remainder as the next request line.
                self.close_connection = True
                raise

        def _completion(self, dialect):
            raw = self._read_body()
            if cfg.get("client_trace_dir"):
                trace_id = time.strftime("%Y%m%d-%H%M%S") + "-" + self.request_id + "-client"
                self._client_trace = WireTrace(cfg, trace_id, raw, root_key="client_trace_dir",
                                               response_file="response-body.bin")
                self._client_trace.request(self.headers)
                self._client_trace.event("client_request", method=self.command, path=self.path, dialect=dialect)
            data = validate.load_json(raw)
            self._bind_client(data)
            if dialect == "anthropic":
                parsed = validate.parse_anthropic(data, cfg)
            elif dialect == "responses":
                parsed = validate.parse_responses(data, cfg)
            else:
                parsed = validate.parse_chat(data, cfg)
            parsed.conversation_key, parsed.parent_conversation_key = validate.conversation_affinity(
                data, self.headers, client_context.current())
            parsed.request_id = self.request_id
            parsed.thinking_placeholder = cfg.get("thinking_placeholder", "")
            parsed.dialect = dialect
            self._model = parsed.model
            refusal = runtime.pool.model_refusal(parsed.entry)
            if refusal is not None:
                raise refusal

            effective_effort = models.effective_effort(parsed.entry, parsed.effort, cfg.get("reasoning_effort"))
            self._effort_str = effective_effort or (
                "provider" if parsed.entry.get("reasoning") and
                parsed.entry.get("reasoning_effort_control") in ("provider-managed", "accepted-ignored") else "none")

            parsed.request_purpose = client_context.request_kind(parsed)
            compatibility_headers = {}
            if parsed.structured_title:
                compatibility_headers['x-proxi-output-format'] = 'validated-session-title'
            if 'thinking.display' in parsed.declared_ignored:
                compatibility_headers['x-proxi-thinking-display'] = 'summarized'
            if parsed.declared_ignored:
                compatibility_headers['x-proxi-ignored-parameters'] = ','.join(parsed.declared_ignored)
                journal.emit('request.compatibility',
                             'Параметры не поддерживаются маршрутом и не передаются: ' + ', '.join(parsed.declared_ignored),
                             'warn', parameters=parsed.declared_ignored)
            if parsed.system_messages_normalized:
                compatibility_headers['x-proxi-system-messages'] = 'merged-into-head'
                journal.emit('request.compatibility',
                             'Системные напоминания объединены в один head: Retool не принимает несколько system. '
                             'Изменение head сбрасывает прежний подписанный префикс.', 'warn',
                             request_id=self.request_id, control='mid_conversation_system',
                             handling='merged_into_head')
            if self._client_trace:
                self._client_trace.event("validated_request", parsed=vars(parsed), effective_effort=self._effort_str)
            client_context.bind({**client_context.current(), "request_purpose": parsed.request_purpose})
            console_log.write(f"[{self.request_id}] {client_context.label()} {self.command} {self.path} | MODEL: {parsed.model} | EFFORT: {self._effort_str.upper()} | PURPOSE: {parsed.request_purpose}")

            wait = runtime.limiter.take()
            if wait:
                journal.emit("http.rate_limited", "Локальный лимит запросов: ждать %.2f с" % wait, "warn",
                             request_id=self.request_id, retry_after=round(wait, 2))
                raise apierr.too_many_requests("relay rate limit reached", wait)

            events = queue.Queue()
            holder = {"reason": "stop", "session": None, "cancel_event": threading.Event()}
            self._holder = holder
            self._model_called = True
            fut = runtime.submit(runtime.runner.run(parsed, events, holder))

            prompt_text = parsed.system + "\n" + parsed.prompt
            if not parsed.stream:
                adapter = adapters.for_dialect(dialect, parsed)
                reason = self._collect(fut, events, adapter, prompt_text, holder)
                payload = adapter.end(reason, prompt_text)
                self._json(200, payload, compatibility_headers)
                return

            writer = sse.StreamWriter(self)
            self._writer = writer
            self._streaming = True
            writer.start({"x-request-id": self.request_id, "request-id": self.request_id,
                          **compatibility_headers})
            self._response_started = True
            self._model = parsed.model
            self._stream_status = 200
            adapter = adapters.for_dialect(dialect, parsed, writer)
            adapter.begin()
            reason = self._pump(fut, events, adapter, holder, writer)
            adapter.end(reason, prompt_text)

        def _collect(self, fut, events, adapter, prompt_text, holder_session=None):
            holder_session = holder_session or {}
            reason = "stop"
            while True:
                try:
                    event = events.get(timeout=0.1)
                except queue.Empty:
                    # Non-stream turns can be abandoned as well: cancel upstream on disconnect.
                    if sse.client_gone(self):
                        self._cancel_turn(fut, holder_session)
                        return "cancelled"
                    if fut.done() and events.empty():
                        break
                    continue
                if event is None:
                    break
                self._trace_relay_event(event)
                kind = event.get("type")
                self._track_credits(event)
                if kind == "error":
                    raise event["error"]
                if kind == "end":
                    reason = event.get("reason", "stop")
                    continue
                try:
                    adapter.feed(event)
                except apierr.ApiError:
                    self._cancel_turn(fut, holder_session)
                    raise
                if sse.client_gone(self):
                    self._cancel_turn(fut, holder_session)
                    return "cancelled"
            return reason

        def _track_credits(self, event):
            if event.get("type") == "credits":
                cost = console_log.number(event.get("value"))
                if cost is not None:
                    self._credits_used = (self._credits_used or 0.0) + cost

        def _trace_relay_event(self, event):
            trace = self._client_trace
            if trace:
                item = dict(event)
                error = item.get("error")
                if isinstance(error, Exception):
                    item["error"] = {"exception_type": type(error).__name__, "message": str(error), **vars(error)}
                trace.event("relay_event", event=item)

        def _cancel_turn(self, fut, holder):
            event = holder.get("cancel_event")
            if event is not None:
                event.set()
            session = holder.get("session")
            if session is not None:
                session.cancel()
            fut.cancel()

        def _pump(self, fut, events, adapter, holder, writer):
            """Stream events to the client, sending heartbeats and honouring disconnects."""
            last_write = time.time()
            reason = "stop"
            while True:
                try:
                    event = events.get(timeout=0.25)
                except queue.Empty:
                    now = time.time()
                    writer_last = getattr(writer, "last_write", last_write)
                    if now - writer_last >= sse.HEARTBEAT_EVERY:
                        writer.comment()
                        last_write = now
                    if sse.client_gone(self) or getattr(writer, "broken", False):
                        self._cancel_turn(fut, holder)
                        return "cancelled"
                    if fut.done() and events.empty():
                        break
                    continue
                if event is None:
                    break
                self._trace_relay_event(event)
                if event.get("type") == "error":
                    raise event["error"]
                self._track_credits(event)
                if event.get("type") == "end":
                    reason = event.get("reason", "stop")
                    holder["reason"] = reason
                    continue
                try:
                    adapter.feed(event)
                except apierr.ApiError:
                    self._cancel_turn(fut, holder)
                    raise
                last_write = time.time()
                # Check for a disconnect on every event, not only when the stream idles.
                if sse.client_gone(self) or getattr(writer, "broken", False):
                    self._cancel_turn(fut, holder)
                    return "cancelled"
            return holder.get("reason", reason)

    return Handler


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR permits two relay processes to own the same port,
    # making clients randomly hit old code after a restart.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, handler, slots):
        super().__init__(address, handler)
        self.slots = slots
        self._stopping = threading.Event()

    def shutdown(self):
        self._stopping.set()
        super().shutdown()

    def process_request(self, request, client_address):
        while not self._stopping.is_set():
            if self.slots.acquire(timeout=0.1):
                break
        else:
            self.shutdown_request(request)
            return
        if self._stopping.is_set():
            self.slots.release()
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def handle_error(self, request, client_address):
        # A client dropping the connection mid-request is routine (Ctrl+C,
        # page reload); keep the console clean and let real errors through.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def startup_config(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=config_module.CONFIG_PATH, help="Path to the existing relay config")
    parser.add_argument("--state-dir", help="Directory for journal, command log and rejected upstream bodies")
    args = parser.parse_args(argv)
    cfg = config_module.load(os.path.abspath(args.config))
    state = os.path.abspath(args.state_dir or HERE)
    os.makedirs(state, exist_ok=True)
    for key, relative in (("command_log", "commands.log"), ("journal_path", "journal.jsonl"),
                          ("upstream_error_dir", os.path.join("logs", "upstream-400"))):
        if args.state_dir or not cfg.get(key):
            cfg[key] = os.path.join(state, relative)
    return cfg


def startup_balance(runtime):
    """Use the same account pool and unknown-balance semantics as /v1/balance."""
    return account_report.balance_report(account_report.collect(runtime, force_refresh=True))


def startup_warnings(runtime):
    """Conditions that make requests fail later without any other hint, in banner order."""
    backends = getattr(runtime.accounts, "backends", None)
    hidden = model_catalog.default_model_warning(runtime.cfg, backends)
    return ([hidden] if hidden else []) + list(runtime.cfg.get("config_warnings") or [])


def banner_lines(cfg, balance, warnings=()):
    lines = ["=" * 64, " Retool AI relay", " listen:        http://" + cfg["host"] + ":" + str(cfg["port"]) + "/v1"]
    if cfg["host"] not in ("127.0.0.1", "localhost", "::1"):
        lines.append(" WARNING:       non-loopback address: /health and admin static are reachable from outside")
    lines += [" relay key:     " + auth.mask(cfg["relay_api_key"]) + "  (from " + auth.relay_key_source() + ")",
              " model default: " + cfg["model"] + " | effort: " + cfg["reasoning_effort"],
              " session mode:  " + cfg["session_mode"] + " | workspace: " + cfg["workspace_root"],
              " exec policy:   " + cfg["exec_policy"] + " | max turns: " + str(cfg["max_exec_turns"])]
    if balance.get("available") is not None:
        line = " balance:       " + str(balance["available"]) + " / " + str(balance.get("monthly_allocation"))
        if balance.get("partial"):
            unknown = balance.get("unknown_accounts") or 0
            line += (("  (partial: %d of %d accounts unknown)" % (unknown, balance.get("accounts_total", unknown)))
                     if unknown else "  (partial: monthly allocation unknown for some accounts)")
        lines.append(line)
    else:
        lines.append(" upstream:      DEGRADED " + str(balance.get("error")))
    lines += [" WARNING:       " + text for text in warnings]
    lines.append("=" * 64)
    return lines


def main(argv=None):
    cfg = startup_config(argv)
    runtime = Runtime(cfg)
    journal.emit("relay.start", "Relay запущен на %s:%s" % (cfg["host"], cfg["port"]), "success",
                 host=cfg["host"], port=cfg["port"], model=cfg["model"], transport=cfg.get("transport"),
                 session_mode=cfg["session_mode"], exec_policy=cfg["exec_policy"])
    warnings = startup_warnings(runtime)
    for text in warnings:
        journal.emit("relay.warning", text, "warn", model=cfg["model"])
    handler = make_handler(runtime)
    slots = threading.BoundedSemaphore(cfg["max_workers"])
    server = BoundedServer((cfg["host"], cfg["port"]), handler, slots)

    backends = getattr(runtime.pool, "backends", None) or [runtime.backend]
    runtime.accounts.start()
    refreshers = list({id(b.refresher): b.refresher for b in backends if getattr(b, "refresher", None)}.values())
    for r in refreshers:
        r.start()  # no-op for accounts the dispatcher schedules
    balance = startup_balance(runtime)
    for line in banner_lines(cfg, balance, warnings):
        print(line)

    stop_requested = threading.Event()

    def stop(*_):
        if stop_requested.is_set():
            return
        stop_requested.set()
        print("\nshutting down…")
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            pass

    def _thread_crash(args):
        print("thread crash:", args.exc_type.__name__, args.exc_value, flush=True)

    threading.excepthook = _thread_crash

    try:
        while True:
            try:
                server.serve_forever()
                break
            except Exception as exc:  # never lose the listener on a stray error
                if stop_requested.is_set():
                    break
                print("serve_forever crashed:", exc, flush=True)
                time.sleep(0.5)
    finally:
        try:
            server.server_close()
            runtime.close()
        finally:
            backends = getattr(runtime.pool, "backends", None) or [runtime.backend]
            refreshers = list({id(b.refresher): b.refresher for b in backends if getattr(b, "refresher", None)}.values())
            for r in refreshers:
                r.stop()
            runtime.accounts.stop()


if __name__ == "__main__":
    main()
