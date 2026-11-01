"""OpenAI/Anthropic compatible relay over the Retool AI agent WebSocket."""
import asyncio
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

import adapters
import apierr
import auth
from redact import redact
import config as config_module
import models as model_catalog
import sse
import validate
from credits import CreditsManager
from orchestrator import TurnRunner
from session import Backend, SessionPool

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
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.backend = Backend(cfg)
        self.pool = SessionPool(self.backend)
        self.credits = CreditsManager(cfg["base"], lambda: auth.upstream_headers(cfg), self.backend.refresher)
        self.runner = TurnRunner(cfg, self.credits, self.pool)
        self.limiter = RateLimiter(cfg["rate_limit_rps"], cfg["rate_limit_burst"])
        self.started = time.time()


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

        def log_message(self, fmt, *args):
            return

        # -- helpers ------------------------------------------------------
        def _cors(self):
            self.send_header("access-control-allow-origin", "*")
            self.send_header("access-control-allow-headers",
                             "authorization, x-api-key, anthropic-version, anthropic-beta, content-type, "
                             "x-request-id, x-session-affinity, x-session-id, x-opencode-session")
            self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
            self.send_header("access-control-expose-headers", "x-request-id, retry-after")

        def _json(self, status, payload, extra=None):
            self._status = status
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("x-request-id", self.request_id)
            if extra:
                for key, value in extra.items():
                    self.send_header(key, str(value))
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    self.close_connection = True

        def _dialect(self):
            return ROUTES.get((self.command, self.path), "")

        # -- entry --------------------------------------------------------
        def do_OPTIONS(self):
            self.request_id = uuid.uuid4().hex[:12]
            self.send_response(204)
            self.send_header("x-request-id", self.request_id)
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

        def _log(self, note=""):
            status = getattr(self, "_status", 0) or getattr(self, "_stream_status", 0)
            took = (time.time() - self._started) * 1000
            model = getattr(self, "_model", "-")
            print("relay %s %s %s %s %s %.0fms %s" % (
                self.request_id, self.command, redact(self.path), status, model, took, note), flush=True)

        def _dispatch(self):
            self.request_id = uuid.uuid4().hex[:12]
            self._started = time.time()
            # Keep-alive reuses the handler: a stale status from the previous
            # request (e.g. a 404 probe) must not leak into this log line.
            self._status = 0
            self._stream_status = 0
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            self.path = path
            route = ROUTES.get((self.command, path))
            dialect = "anthropic" if route == "anthropic" else "chat"
            try:
                self._log_route = route
                if route is None:
                    if any(method_path[1] == path for method_path in ROUTES):
                        raise apierr.method_not_allowed()
                    raise apierr.not_found()
                if route != "health":
                    query = parse_qs(parsed_url.query)
                    auth.verify(self.headers, (query.get("key") or [""])[0], cfg["relay_api_key"])
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
                    balance = runtime.credits.get_balance(force_refresh=True)
                    status = 200 if balance.get("available") is not None or balance.get("stale") else 503
                    self._json(status, balance)
                elif route == "usage":
                    self._json(200, runtime.credits.usage())
                elif route == "health":
                    self._health()
            except apierr.ApiError as err:
                if err.status >= 400:
                    print("[err] %s %s %s" % (err.status, err.code, (err.message or '')[:160]), flush=True)
                if getattr(self, "_streaming", False):
                    writer = self._writer
                    writer.event("error", adapters.error_payload(dialect, err))
                    writer.done()
                else:
                    self._error_response(err, dialect)
            except Exception as exc:  # never leak a bare traceback
                print("relay %s EXC %s: %s" % (self.request_id, type(exc).__name__, str(exc)[:200]), flush=True)
                err = apierr.internal(str(exc) or exc.__class__.__name__)
                if getattr(self, "_streaming", False):
                    writer = self._writer
                    writer.event("error", adapters.error_payload(dialect, err))
                    writer.done()
                else:
                    self._error_response(err, dialect)
            finally:
                self._log()

        def _error_response(self, err, dialect):
            body = adapters.error_payload(dialect, err)
            extra = {"retry-after": err.retry_after} if err.retry_after else None
            self._json(err.status, body, extra)

        def _count_tokens(self):
            """Claude Code asks for a token estimate before sending a turn."""
            data = validate.load_json(self._read_body())
            # The real count_tokens endpoint does not require max_tokens.
            data.setdefault("max_tokens", 1)
            parsed = validate.parse_anthropic(data, cfg)
            text = parsed.system + "\n" + parsed.prompt
            self._json(200, {"input_tokens": adapters.approx_tokens(text)})

        def _models(self):
            data = [{
                "id": model["id"],
                "object": "model",
                "owned_by": model["provider"],
                "label": model["label"],
                "context_window": model["context"],
                "max_output_tokens": model.get("max_output"),
                **{key: model[key] for key in ("metadata_checked_at", "metadata_source", "metadata_status", "metadata_notes")
                   if key in model},
                "capabilities": {"reasoning": model["reasoning"], "tools": model["tools"],
                                 "vision": False,
                                 "reasoning_effort_levels": model.get("reasoning_effort_levels", []),
                                 "default_reasoning_effort": model.get("default_reasoning_effort"),
                                 "reasoning_effort_control": model.get("reasoning_effort_control", "selectable")},
            } for model in model_catalog.catalog()
                if self._model_routable(model)]
            self._json(200, {"object": "list", "data": data})

        def _model_routable(self, model) -> bool:
            """Hide models whose provider resource is not configured."""
            if cfg.get("show_unrouted_models"):
                return True
            if not any(cfg.get(k) for k in model_catalog.PROVIDER_RESOURCE_KEY.values()):
                return True  # nothing configured yet: list everything, calls fail with a clear 503
            if model["id"] == cfg.get("model"):
                return True  # the configured default stays visible
            key = model_catalog.PROVIDER_RESOURCE_KEY.get(model["provider"])
            return bool(key and cfg.get(key))

        def _health(self):
            balance = runtime.credits.get_balance()
            ok = balance.get("available") is not None and not balance.get("stale")
            payload = {
                "ok": ok,
                "uptime_seconds": round(time.time() - runtime.started, 1),
                "session_mode": cfg["session_mode"],
                "models": len(model_catalog.catalog()),
                "upstream": "ok" if ok else "degraded",
                "error": balance.get("error") or balance.get("stale_error"),
                "session": runtime.backend.refresher.status(),
            }
            self._json(200 if ok else 503, payload)

        # -- completions --------------------------------------------------
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
                raise apierr.ApiError(415, "unsupported_media_type", "content-type must be application/json")
            return self.rfile.read(length)

        def _completion(self, dialect):
            data = validate.load_json(self._read_body())
            if dialect == "anthropic":
                parsed = validate.parse_anthropic(data, cfg)
            elif dialect == "responses":
                parsed = validate.parse_responses(data, cfg)
            else:
                parsed = validate.parse_chat(data, cfg)
            affinity = (self.headers.get("x-session-affinity") or
                        self.headers.get("x-session-id") or
                        self.headers.get("x-opencode-session"))
            parsed.conversation_key = validate.conversation_key(data, affinity)

            wait = runtime.limiter.take()
            if wait:
                raise apierr.too_many_requests("relay rate limit reached", wait)

            events = queue.Queue()
            holder = {"reason": "stop", "session": None, "cancel_event": threading.Event()}
            fut = asyncio.run_coroutine_threadsafe(runtime.runner.run(parsed, events, holder), runtime.loop)

            prompt_text = parsed.system + "\n" + parsed.prompt
            if not parsed.stream:
                adapter = adapters.for_dialect(dialect, parsed)
                reason = self._collect(fut, events, adapter, prompt_text, holder)
                payload = adapter.end(reason, prompt_text)
                self._json(200, payload)
                return

            writer = sse.StreamWriter(self)
            self._writer = writer
            self._streaming = True
            writer.start({"x-request-id": self.request_id})
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
                kind = event.get("type")
                if kind == "error":
                    raise event["error"]
                if kind == "end":
                    reason = event.get("reason", "stop")
                    continue
                adapter.feed(event)
                if sse.client_gone(self):
                    self._cancel_turn(fut, holder_session)
                    return "cancelled"
            return reason

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
                    if now - last_write >= sse.HEARTBEAT_EVERY:
                        writer.comment()
                        last_write = now
                    if sse.client_gone(self):
                        self._cancel_turn(fut, holder)
                        return "cancelled"
                    if fut.done() and events.empty():
                        break
                    continue
                if event is None:
                    break
                if event.get("type") == "error":
                    raise event["error"]
                if event.get("type") == "end":
                    reason = event.get("reason", "stop")
                    holder["reason"] = reason
                    continue
                adapter.feed(event)
                last_write = time.time()
                # Check for a disconnect on every event, not only when the stream idles.
                if sse.client_gone(self):
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

    def process_request(self, request, client_address):
        self.slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    cfg = config_module.load()
    cfg["command_log"] = os.path.join(HERE, "commands.log")
    runtime = Runtime(cfg)
    handler = make_handler(runtime)
    slots = threading.BoundedSemaphore(cfg["max_workers"])
    server = BoundedServer((cfg["host"], cfg["port"]), handler, slots)

    runtime.backend.refresher.start()
    balance = runtime.credits.get_balance(force_refresh=True)
    print("=" * 64)
    print(" Retool AI relay")
    print(" listen:        http://" + cfg["host"] + ":" + str(cfg["port"]) + "/v1")
    print(" relay key:     " + auth.mask(cfg["relay_api_key"]) + "  (from " + auth.relay_key_source() + ")")
    print(" model default: " + cfg["model"] + " | effort: " + cfg["reasoning_effort"])
    print(" session mode:  " + cfg["session_mode"] + " | workspace: " + cfg["workspace_root"])
    print(" exec policy:   " + cfg["exec_policy"] + " | max turns: " + str(cfg["max_exec_turns"]))
    if balance.get("available") is not None:
        print(" balance:       " + str(balance["available"]) + " / " + str(balance.get("monthly_allocation")))
    else:
        print(" upstream:      DEGRADED " + str(balance.get("error")))
    print("=" * 64)

    def stop(*_):
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
                print("serve_forever crashed:", exc, flush=True)
                time.sleep(0.5)
    finally:
        try:
            runtime.backend.refresher.stop()
            runtime.loop.call_soon_threadsafe(runtime.loop.stop)
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
