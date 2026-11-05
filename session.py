"""Upstream Retool transport: thread creation, WebSocket turns, retries, cancellation."""
import asyncio
from collections import deque
import datetime as _datetime
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid

import direct
import native_history

import websockets

import apierr
import journal
import models as model_catalog
from accounts import UNNAMED, account_tag, enabled as account_enabled, priority as account_priority
from auth import upstream_headers
from upstream_refresh import Refresher

ISO_FMT = "%Y-%m-%dT%H:%M:%S"

MAX_FRAME_DEFAULT = 16 * 1024 * 1024
MAX_PENDING_FRAMES = 256


def _iso(ts: float) -> str:
    return _datetime.datetime.fromtimestamp(ts, _datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _created_timestamp(value):
    if not value:
        return 0.0
    try:
        return _datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _tool_call_from_event(event):
    """Normalise Retool WS/history tool-call shapes to relay events."""
    content = event.get("content") if isinstance(event.get("content"), dict) else event
    name = content.get("toolName") or content.get("name")
    call_id = content.get("toolCallId") or content.get("call_id") or content.get("id")
    arguments = content.get("toolInput", content.get("input", content.get("arguments")))
    if arguments is None:
        arguments = content.get("partialInputs")
    if not name or not call_id or arguments is None:
        return None
    if isinstance(arguments, str):
        encoded = arguments
    else:
        encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return {"id": str(call_id), "name": str(name), "arguments": encoded}


def _merge_tool_call(call, buffers):
    previous = buffers.get(call["id"])
    current = call["arguments"]
    if previous is None:
        buffers[call["id"]] = current
        return call
    merged = current if current.startswith(previous) else previous + current
    if merged == previous:
        return None
    buffers[call["id"]] = merged
    return {**call, "arguments": merged}


class StartResult:
    def __init__(self, url, sandbox_id):
        self.url = url
        self.sandbox_id = sandbox_id


class Backend:
    """Owns HTTP calls to Retool. Injectable for offline tests."""

    def __init__(self, cfg, *, http_post=None, ws_connect=None, refresher=None):
        self.cfg = cfg
        self._http_post = http_post or self._default_post
        self._ws_connect = ws_connect or websockets.connect
        self.refresher = refresher if refresher is not None else Refresher(cfg)

    # -- HTTP ---------------------------------------------------------------
    def headers(self):
        """Current upstream credential headers (rebuilt after a refresh)."""
        return upstream_headers(self.cfg)

    async def new_thread_id(self) -> str:
        """Direct transport needs no Retool thread; keep the cheap local id."""
        if self.cfg.get("transport") == "direct":
            return "direct-" + uuid.uuid4().hex
        return await self.create_thread()

    def _default_post(self, path, payload, timeout):
        data = json.dumps(payload, ensure_ascii=True).encode("ascii")
        req = urllib.request.Request(self.cfg["base"] + path, data=data,
                                     headers=upstream_headers(self.cfg), method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def _call(self, path, payload, *, attempts=3, timeout=None):
        timeout = timeout or self.cfg["upstream_timeout"]
        last = None
        for attempt in range(attempts):
            try:
                return await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.refresher.call(lambda: self._http_post(path, payload, timeout)))
            except urllib.error.HTTPError as exc:
                body = exc.read()[:300].decode("utf-8", "replace") if exc.fp else ""
                if exc.code in (401, 403):
                    detail = body or (self.refresher.last_error or "")
                    last = apierr.upstream_auth_failed(detail)
                    raise last
                if exc.code == 429:
                    retry_after = exc.headers.get("retry-after") if exc.headers else None
                    last = apierr.too_many_requests("Retool rate limit", float(retry_after or 2))
                    if attempt + 1 < attempts:
                        await asyncio.sleep(min(float(retry_after or 2), 5) * (attempt + 1))
                    continue
                if 500 <= exc.code < 600:
                    last = apierr.upstream_error(f"Retool HTTP {exc.code}{(': ' + body) if body else ''}")
                    if attempt + 1 < attempts:
                        await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                raise apierr.upstream_error(f"Retool HTTP {exc.code}{(': ' + body) if body else ''}")
            except TimeoutError:
                last = apierr.upstream_timeout(f"Retool did not answer within {timeout}s")
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.4 * (attempt + 1))
            except urllib.error.URLError as exc:
                last = apierr.upstream_error(f"Retool unreachable: {exc.reason}")
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.4 * (attempt + 1))
        raise last if last is not None else apierr.upstream_error("upstream call failed")

    # -- API ----------------------------------------------------------------
    async def get_messages(self, thread_id):
        """Fetch persisted messages (used to explain silent turns)."""
        path = f"/api/pages/{self.cfg['page_uuid']}/sandpack/threads/{thread_id}/messages"

        def call():
            req = urllib.request.Request(self.cfg["base"] + path, headers=upstream_headers(self.cfg), method="GET")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # Direct-transport threads persist no sandpack messages.
                    return {"messages": []}
                raise

        return await asyncio.get_running_loop().run_in_executor(None, lambda: self.refresher.call(call))

    async def create_thread(self) -> str:
        data = await self._call(f"/api/pages/{self.cfg['page_uuid']}/sandpack/threads", {})
        thread_id = data.get("id") if isinstance(data, dict) else None
        if not thread_id:
            raise apierr.upstream_error("Retool did not return a thread id")
        return thread_id

    async def start_run(self, thread_id) -> StartResult:
        data = await self._call("/api/ai/retool-next-agent/start", {
            "threadId": thread_id,
            "pageUuid": self.cfg["page_uuid"],
            "environmentName": "production",
        })
        if not isinstance(data, dict) or not data.get("url") or not data.get("sandboxToken"):
            raise apierr.upstream_error("Retool start returned no websocket url")
        url = data["url"]
        sep = "&" if "?" in url else "?"
        if "sandboxToken=" not in url:
            url = url + sep + "sandboxToken=" + data["sandboxToken"]
        return StartResult(url, data.get("sandboxId"))

    async def connect(self, url):
        return await self._ws_connect(url, origin=self.cfg["base"],
                                      max_size=self.cfg.get("max_frame_bytes") or MAX_FRAME_DEFAULT,
                                      ping_interval=None, open_timeout=self.cfg["upstream_timeout"])


async def _answer_control(ws, raw):
    try:
        event = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(event, dict):
        return False
    if event.get("type") == "ping":
        await ws.send('{"type":"pong"}')
        return True
    ack = event.get("ack")
    if isinstance(ack, dict) and ack.get("id"):
        await ws.send(json.dumps({"type": "client_ack", "replyTo": ack["id"]}))
    return False


class _SocketReader:
    """One receiver; backlog <= 256 frames and one configured max-frame byte budget."""

    def __init__(self, ws, byte_limit):
        self.ws = ws
        self.frames = deque()
        self.bytes = 0
        self.byte_limit = byte_limit
        self.ready = asyncio.Event()
        self.error = None
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        try:
            while True:
                try:
                    raw = await self.ws.recv()
                except asyncio.TimeoutError:
                    # Injectable transports may expose an idle read timeout.
                    await asyncio.sleep(0)
                    continue
                if await _answer_control(self.ws, raw):
                    continue
                size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
                if len(self.frames) >= MAX_PENDING_FRAMES or self.bytes + size > self.byte_limit:
                    raise apierr.upstream_error("websocket application buffer overflow; connection closed")
                self.frames.append((raw, size))
                self.bytes += size
                self.ready.set()
                # Let the active consumer run when recv() has buffered frames.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
        finally:
            if self.error is None:
                self.error = RuntimeError("websocket reader closed")
            self.ready.set()
            try:
                await self.ws.close()
            except Exception:
                pass

    async def recv(self):
        while True:
            # Overflow cannot turn into a successful, silently truncated turn.
            if isinstance(self.error, apierr.ApiError):
                raise self.error
            if self.frames:
                raw, size = self.frames.popleft()
                self.bytes -= size
                return raw
            if self.error is not None:
                raise self.error
            self.ready.clear()
            await self.ready.wait()

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.frames.clear()
        self.bytes = 0
        self.error = RuntimeError("websocket reader closed")
        self.ready.set()
        try:
            await self.ws.close()
        except Exception:
            pass


class RelaySession:
    """One WebSocket turn stream bound to a single Retool thread."""

    def __init__(self, backend: Backend, thread_id):
        self.backend = backend
        self.cfg = backend.cfg
        self.thread_id = thread_id
        self.ws = None
        self._reader = None
        self._cancelled = False
        self._needs_drain = False
        self._drain_task = None
        self._loop = None
        self.sandbox_id = None
        self._direct = None
        self._registered_tools = {}
        self._cached_head = None
        self._warm_body = None
        self._warm_at = 0
        self._warm_task = None
        self._last_request_at = time.time()
        self._active_direct_abort = None
        self._active_warm_abort = None
        self._cached_turn_ends = []
        self._native_history = native_history.History()
        self._native_client_messages = None
        self._native_conversation_key = None

    def set_direct(self, head, tools, tool_choice, max_tokens, chunks=None):
        """Direct transport: cacheable head, chunked history and native client tools."""
        prior = self._direct or {}
        if head:
            if self._cached_head and self._cached_head != head:
                # Head churn invalidates the cached prefix, so every history
                # breakpoint misses and the whole prompt is written again.
                journal.emit("cache.head_changed", "Head изменился — кэш префикса будет перезаписан", "info",
                             old_sha=hashlib.sha256(self._cached_head.encode("utf-8")).hexdigest()[:12],
                             new_sha=hashlib.sha256(head.encode("utf-8")).hexdigest()[:12],
                             old_chars=len(self._cached_head), new_chars=len(head),
                             thread=journal.tag(getattr(self, "thread_id", None)))
            self._cached_head = head
        if tools:
            if prior.get("tools") and prior.get("tools") != tools:
                journal.emit("cache.tools_changed", "Набор инструментов клиента изменился", "info",
                             old_count=len(prior["tools"]), new_count=len(tools),
                             thread=journal.tag(getattr(self, "thread_id", None)))
            self._registered_tools = {t["name"]: t for t in tools if isinstance(t, dict) and t.get("name")}
        self._direct = {"head": head or "", "tools": tools, "tool_choice": tool_choice,
                        "max_tokens": max_tokens, "chunks": chunks}

    def register_tools(self, tools):
        """Register tools into the session's canonical tool schema."""
        if tools:
            self._registered_tools = {t["name"]: t for t in tools if isinstance(t, dict) and t.get("name")}

    def get_canonical_tools(self):
        """Return the normalized canonical tools registered in this session."""
        return [self._registered_tools[name] for name in sorted(self._registered_tools)]

    def has_tools(self):
        return bool(self._registered_tools)

    def begin_request(self):
        """Reset cancellation only when the pool grants a new request lease."""
        self._cancelled = False
        self._last_request_at = time.time()

    def cancel(self):
        """Thread-safe cancellation: close the old socket to isolate its frames."""
        self._cancelled = True
        self._needs_drain = True
        abort = getattr(self, "_active_direct_abort", None)
        if callable(abort):
            try:
                abort()
            except Exception:
                pass
        warm_abort = getattr(self, "_active_warm_abort", None)
        if callable(warm_abort):
            try:
                warm_abort()
            except Exception:
                pass
        loop = self._loop
        if self.ws is not None and self._drain_task is None and loop is not None and not loop.is_closed():
            self._drain_task = asyncio.run_coroutine_threadsafe(self.aclose(), loop)

    async def open(self):
        self._loop = asyncio.get_running_loop()
        if self.ws is not None:
            reader = await self._ensure_reader()
            if reader.error is None and not reader.task.done():
                return
            error = reader.error
            await self.aclose()
            if isinstance(error, apierr.ApiError):
                raise error
        rejected_cookie = self.cfg.get("cookie")
        try:
            started = await self.backend.start_run(self.thread_id)
            self.ws = await self.backend.connect(started.url)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in (401, 403) or "401" in str(exc):
                refresh_options = {"rejected_cookie": rejected_cookie} if rejected_cookie is not None else {}
                refreshed = await asyncio.to_thread(self.backend.refresher.refresh, **refresh_options)
                if not refreshed:
                    raise apierr.upstream_auth_failed(self.backend.refresher.last_error or str(exc))
                started = await self.backend.start_run(self.thread_id)
                self.ws = await self.backend.connect(started.url)
            else:
                raise
        self.sandbox_id = started.sandbox_id
        await self._ensure_reader()

    async def _ensure_reader(self):
        if self._reader is not None and self._reader.ws is not self.ws:
            reader, self._reader = self._reader, None
            await reader.close()
        if self._reader is None:
            if self.ws is None:
                raise RuntimeError("websocket is not connected")
            self._reader = _SocketReader(self.ws, self.cfg.get("max_frame_bytes") or MAX_FRAME_DEFAULT)
        return self._reader

    async def _recv(self):
        return await (await self._ensure_reader()).recv()

    async def _maybe_pong(self, raw):
        await _answer_control(self.ws, raw)

    async def aclose(self):
        abort = getattr(self, "_active_direct_abort", None)
        if callable(abort):
            try:
                abort()
            except Exception:
                pass
        warm_abort = getattr(self, "_active_warm_abort", None)
        if callable(warm_abort):
            try:
                warm_abort()
            except Exception:
                pass
        task, self._warm_task = getattr(self, "_warm_task", None), None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ws, self.ws = self.ws, None
        reader, self._reader = self._reader, None
        if reader is not None:
            await reader.close()
        if ws is not None and (reader is None or reader.ws is not ws):
            try:
                await ws.close()
            except Exception:
                pass

    async def stream(self, prompt, model_provider, tool_names=None):
        """Keep one request's cancellation sticky across all local tool turns."""
        self._loop = asyncio.get_running_loop()
        if self._drain_task is not None:
            try:
                await asyncio.shield(asyncio.wrap_future(self._drain_task))
            except Exception:
                pass
            self._drain_task = None
        if self._needs_drain:
            await self.aclose()
            self._needs_drain = False
        if self._cancelled:
            yield {"type": "end", "reason": "cancelled"}
            return
        completed = False
        self._output_started = False
        turn = self._stream_turn(prompt, model_provider, tool_names)
        try:
            async for event in turn:
                if event.get("type") in ("text", "thinking", "tool_call"):
                    self._output_started = True
                if event.get("type") == "end" and event.get("reason") in ("stop", "tool_calls"):
                    completed = True
                yield event
        finally:
            await turn.aclose()
            if not completed and self.cfg.get("transport") != "direct":
                # A timed-out, abandoned or failed turn cannot be read again by
                # the next request sharing this Retool thread.
                self._needs_drain = True
                await self.aclose()

    async def _stream_turn(self, prompt, model_provider, tool_names=None):
        """Yield normalised provider events, cleaning its poller on every exit."""
        if self.cfg.get("transport") == "direct":
            args = self._direct or {}
            async for event in direct.iter_events(
                    self.cfg, self, model_provider, prompt, args.get("head"), args.get("tools"),
                    args.get("tool_choice"), args.get("max_tokens"), args.get("chunks")):
                yield event
            return
        try:
            await self.open()
        except Exception as exc:
            raise exc if isinstance(exc, apierr.ApiError) else apierr.upstream_error(str(exc))
        if self._cancelled:
            yield {"type": "end", "reason": "cancelled"}
            return
        turn_start = time.time()
        frame = json.dumps({
                "type": "user_prompt",
                "prompt": prompt,
                "modelProvider": model_provider,
                "attachments": [],
                "taggedResourceNames": [],
                "taggedResourceIds": [],
                "r2OnboardingTemplateId": None,
                "messageId": str(uuid.uuid4()),
                "expectedThreadId": self.thread_id,
                "agentMode": int(self.cfg.get("agent_mode", 2)),
                "testingEnabled": False,
                "requestOrigin": "Browser",
            }, ensure_ascii=True)
        try:
            await self.ws.send(frame)
        except Exception as exc:
            raise apierr.upstream_error(f"websocket send failed: {exc}")

        deadline = turn_start + self.cfg["turn_timeout"]
        tool_names = set(tool_names or ())
        state = {"thinking": "", "tools": {}, "signatures": set(), "activity": asyncio.Event()}
        think_q = asyncio.Queue() if (self.cfg.get("show_thinking") or tool_names) else None
        poller = (asyncio.ensure_future(self._poll_thinking(turn_start, think_q, tool_names, state))
                  if think_q is not None else None)
        last_text = ""
        full_text = ""
        block_id = None
        thinking_text = ""
        thinking_id = None
        last_thinking = ""
        restart_hint = False
        reconnects = 0
        tool_buffers = state["tools"]
        receive_task = None
        credits_received = False
        try:
            while True:
                while think_q is not None and not think_q.empty():
                    pending = think_q.get_nowait()
                    if isinstance(pending, dict):
                        yield pending
                    else:
                        yield {"type": "thinking", "delta": pending}
                if self._cancelled:
                    self.cancel()
                    if poller is not None:
                        poller.cancel()
                    yield {"type": "end", "reason": "cancelled"}
                    return
                remaining = deadline - time.time()
                if remaining <= 0:
                    if poller is not None:
                        poller.cancel()
                    yield {"type": "end", "reason": "timeout"}
                    return
                try:
                    # Revisit persisted thinking promptly without cancelling an
                    # in-flight socket read each time the history queue ticks.
                    receive_wait = min(remaining, self.cfg["recv_timeout"], 0.25 if think_q is not None else remaining)
                    if receive_task is None:
                        receive_task = asyncio.create_task(self._recv())
                    done, _ = await asyncio.wait({receive_task}, timeout=receive_wait)
                    if not done:
                        continue
                    ready, receive_task = receive_task, None
                    raw = ready.result()
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    if poller is not None:
                        poller.cancel()
                    raise
                except apierr.ApiError:
                    raise
                except Exception as exc:
                    if self._cancelled:
                        yield {"type": "end", "reason": "cancelled"}
                        return
                    if reconnects >= 1 or self._output_started:
                        raise apierr.upstream_error(f"websocket closed: {exc}")
                    reconnects += 1
                    await self.aclose()
                    try:
                        await self.open()
                        if self._cancelled:
                            yield {"type": "end", "reason": "cancelled"}
                            return
                        await self.ws.send(frame)
                    except Exception as inner:
                        if poller is not None:
                            poller.cancel()
                        raise apierr.upstream_error(f"websocket reconnect failed: {inner}")
                    continue

                if self._cancelled:
                    yield {"type": "end", "reason": "cancelled"}
                    return
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")

                if kind == "text":
                    content = event.get("content") or ""
                    # Text frames are cumulative *per messageId*; a new id starts a new block.
                    if event.get("messageId") != block_id:
                        # A new id may restart the same text (overlap) or open a new block.
                        if block_id is not None and last_text and content.startswith(last_text):
                            block_id = event.get("messageId")
                            delta = content[len(last_text):]
                            last_text = content
                            full_text += delta
                            if delta:
                                state["activity"].set()
                                yield {"type": "text", "delta": delta}
                            continue
                        if full_text.strip():
                            full_text = full_text.rstrip("\n") + "\n"
                        block_id = event.get("messageId")
                        last_text = ""
                    if content.startswith(last_text):
                        delta = content[len(last_text):]
                    elif restart_hint or len(content) < len(last_text):
                        # Buffer restarted: the frame carries the text from scratch.
                        delta = content
                    else:
                        delta = content[len(last_text):]
                    restart_hint = False
                    last_text = content
                    full_text += delta
                    if delta:
                        state["activity"].set()
                        yield {"type": "text", "delta": delta}
                elif kind == "thinking":
                    content = event.get("content") or ""
                    if event.get("messageId") != thinking_id:
                        if thinking_text.strip():
                            thinking_text += "\n"
                        thinking_id = event.get("messageId")
                        last_thinking = ""
                    if content.startswith(last_thinking):
                        delta = content[len(last_thinking):]
                    elif len(content) >= len(last_thinking):
                        delta = content[len(last_thinking):]
                    else:
                        delta = content
                    last_thinking = content
                    thinking_text += delta
                    sent = state["thinking"]
                    if thinking_text.startswith(sent):
                        delta = thinking_text[len(sent):]
                        state["thinking"] = thinking_text
                    elif sent.startswith(thinking_text):
                        delta = ""
                    else:
                        state["thinking"] += delta
                    if delta:
                        state["activity"].set()
                        yield {"type": "thinking", "delta": delta}
                    signature = event.get("signature")
                    if signature and signature not in state["signatures"]:
                        state["activity"].set()
                        state["signatures"].add(signature)
                        yield {"type": "thinking_signature", "signature": signature}
                elif kind == "thinking_signature":
                    signature = event.get("signature")
                    if signature and signature not in state["signatures"]:
                        state["activity"].set()
                        state["signatures"].add(signature)
                        yield {"type": "thinking_signature", "signature": signature}
                elif kind == "clear_streaming":
                    # UI signal (usually before a tool call): the next frame may restart
                    # the same text, so we only mark that the buffer may be replaced.
                    restart_hint = True
                elif kind == "run_credits_persisted":
                    value = event.get("creditsUsed")
                    if isinstance(value, (int, float)):
                        credits_received = True
                        yield {"type": "credits", "value": float(value)}
                elif kind == "hitl":
                    yield {"type": "hitl", "question": _hitl_text(event)}
                elif kind == "tool_call":
                    call = _tool_call_from_event(event)
                    if call is not None and call["name"] in tool_names:
                        state["activity"].set()
                        merged = _merge_tool_call(call, tool_buffers)
                        if merged is not None:
                            yield {"type": "tool_call", **merged}
                elif kind == "agent_loop_done":
                    if poller is not None:
                        poller.cancel()
                        await asyncio.gather(poller, return_exceptions=True)
                        # Fast turns may finish before the first polling tick.
                        # Fetch the final persisted reasoning while it still
                        # belongs to this turn; this never invents reasoning.
                        try:
                            await asyncio.wait_for(self._queue_history(turn_start, think_q, tool_names, state), timeout=3)
                        except Exception:
                            pass
                        while think_q is not None and not think_q.empty():
                            pending = think_q.get_nowait()
                            if isinstance(pending, dict):
                                yield pending
                            else:
                                yield {"type": "thinking", "delta": pending}
                    if not credits_received:
                        for trail in await self._trailing():
                            yield trail
                    yield {"type": "final_text", "text": full_text}
                    yield {"type": "end", "reason": "stop"}
                    return
                elif kind == "error":
                    if poller is not None:
                        poller.cancel()
                    raise apierr.upstream_error(str(event)[:300])
                elif os.environ.get("DEBUG_EVENTS"):
                    print("dbg-unknown:", json.dumps(event, ensure_ascii=False)[:400], flush=True)
        finally:
            if receive_task is not None:
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
            if poller is not None:
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)

    async def _queue_history(self, turn_start, queue_, tool_names, state):
        data = await self.backend.get_messages(self.thread_id)
        before = queue_.qsize()
        parts = []
        signatures = []
        for message in reversed(data.get("messages", [])):
            created = message.get("createdAt")
            if not created or _created_timestamp(created) < turn_start:
                continue
            content = message.get("content") or {}
            if not isinstance(content, dict):
                continue
            if content.get("type") == "tool_call":
                call = _tool_call_from_event(content)
                if call and call["name"] in tool_names:
                    merged = _merge_tool_call(call, state["tools"])
                    if merged is not None:
                        queue_.put_nowait({"type": "tool_call", **merged})
                continue
            if content.get("type") != "thinking" or not self.cfg.get("show_thinking"):
                continue
            parts.append(content.get("thinking") or "")
            signature = content.get("signature")
            if signature and signature not in state["signatures"]:
                state["signatures"].add(signature)
                signatures.append(signature)
        text = "\n".join(p for p in parts if p)
        sent = state["thinking"]
        if text.startswith(sent) and len(text) > len(sent):
            queue_.put_nowait({"type": "thinking", "delta": text[len(sent):]})
            state["thinking"] = text
        for signature in signatures:
            queue_.put_nowait({"type": "thinking_signature", "signature": signature})
        return queue_.qsize() > before

    async def _poll_thinking(self, turn_start, queue_, tool_names=(), state=None):
        """Poll only actual provider messages correlated to this turn."""
        if state is None:
            state = {"thinking": "", "tools": {}, "signatures": set()}
        activity = state.setdefault("activity", asyncio.Event())
        interval = 2.0
        while True:
            try:
                try:
                    await asyncio.wait_for(activity.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
                else:
                    # WS activity already supplies new content. Resume the
                    # normal cadence without adding a redundant history GET.
                    activity.clear()
                    interval = 2.0
                    continue
                changed = await self._queue_history(turn_start, queue_, tool_names, state)
                interval = 2.0 if changed else min(interval * 2, 15.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                interval = min(interval * 2, 15.0)
                continue

    async def _trailing(self):
        """Collect a trailing credits frame, but never block the turn for long."""
        end = time.time() + 0.8
        out = []
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(self._recv(), timeout=0.25)
            except asyncio.TimeoutError:
                break
            except apierr.ApiError:
                raise
            except Exception:
                break
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "run_credits_persisted":
                value = event.get("creditsUsed")
                if isinstance(value, (int, float)):
                    out.append({"type": "credits", "value": float(value)})
                break
        return out


def _hitl_text(event) -> str:
    hitl = event.get("hitl") or {}
    questions = (hitl.get("hitlProps") or {}).get("questions") or []
    texts = [q.get("question") for q in questions if isinstance(q, dict) and q.get("question")]
    if texts:
        return "\n".join(texts)
    return hitl.get("message") or f"[hitl: {hitl.get('hitlType', 'askUser')}]"


class SessionLease:
    """A unique request handle, even when the underlying socket is reused."""

    def __init__(self, session):
        self.session = session
        self.released = False

    def __getattr__(self, name):
        return getattr(self.session, name)

    def cancel(self):
        # The released flag is flipped on the pool loop. From an HTTP thread the
        # check must run on that same loop, or a stale cancel lands after the next
        # begin_request() and silently cancels the NEXT lease's turn. From the loop
        # thread itself (in-turn backend callbacks) cancel stays synchronous —
        # deferring it would break the immediate socket-closed contract.
        session = self.session
        loop = getattr(session, "_loop", None)
        on_loop = False
        try:
            on_loop = loop is not None and loop.is_running() and asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            if not self.released:
                session.cancel()
            return
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._cancel_checked)
        else:
            self._cancel_checked()

    def _cancel_checked(self):
        if not self.released:
            self.session.cancel()

    async def stream(self, prompt, model_provider, tool_names=None):
        if self.released:
            yield {"type": "end", "reason": "cancelled"}
            return
        async for event in self.session.stream(prompt, model_provider, tool_names):
            yield event


class _Dependent:
    """One in-flight request of a child conversation that pins its ancestors' warm cache.

    The scheduler judges it by what the child session really did, never by the lease
    count alone, so a release that never comes cannot keep an ancestor warm forever."""
    __slots__ = ("since", "session")

    def __init__(self):
        self.since = time.time()
        self.session = None


class SessionPool:
    """Bounded persistent conversations with one exclusive lease per request."""

    def __init__(self, backend: Backend):
        self.backend = backend
        self.cfg = backend.cfg
        self.backends = [backend]
        if isinstance(self.cfg.get("accounts"), list) and self.cfg["accounts"]:
            self.backends = []
            custom_post = getattr(backend, "_http_post", None)
            if custom_post == getattr(backend, "_default_post", None):
                custom_post = None
            custom_ws = getattr(backend, "_ws_connect", None)
            if custom_ws == getattr(websockets, "connect", None):
                custom_ws = None
            for acc in self.cfg["accounts"]:
                acc_cfg = dict(self.cfg)
                if acc.get("base", self.cfg.get("base", "")).rstrip("/") != self.cfg.get("base", "").rstrip("/"):
                    for resource_key in model_catalog.PROVIDER_RESOURCE_KEY.values():
                        if resource_key not in acc:
                            acc_cfg.pop(resource_key, None)
                acc_cfg.update(acc)
                # Pool controls belong to the account record, never inherited from the root.
                acc_cfg.update(enabled=acc.get("enabled", True), priority=acc.get("priority", 0),
                               label=acc.get("label", ""), provider_access=acc.get('provider_access', {}))
                acc_cfg["cookie_values"] = self.cfg.get("cookie_values", [])
                acc_cfg["_account_ref"] = acc
                acc_cfg["_disk_auth"] = (acc.get("cookie"), acc.get("xsrf"))
                acc_backend = Backend(acc_cfg, http_post=custom_post, ws_connect=custom_ws)
                self.backends.append(acc_backend)
            if self.backends:
                self.backend = self.backends[0]
        self._shared = None
        self._lock = asyncio.Lock()
        self._conversations = {}
        self._conversation_locks = {}
        self._conversation_refs = {}
        self._last_used = {}
        self._slots = asyncio.BoundedSemaphore(max(1, self.cfg["max_workers"]))
        self._active = {}

    @staticmethod
    def _route_refusal(backend, model_entry):
        """Why this account can never serve the model (a settled fact, unlike its health), or None."""
        if model_entry is None:
            return None
        return model_catalog.refusal_reasons(model_entry, [backend.cfg])[0]

    @staticmethod
    def _check_route(backend, model_entry):
        reason = SessionPool._route_refusal(backend, model_entry)
        if reason:
            raise model_catalog.model_unavailable(model_entry, [reason])

    @staticmethod
    def _routable(backend, model_entry):
        try:
            SessionPool._check_route(backend, model_entry)
        except apierr.ApiError:
            return False
        return True

    @staticmethod
    def _usable(backend, exclude=()):
        """The dispatcher marks exhausted, signed-out or rate-limited accounts unavailable; the
        operator can switch an account off."""
        return (backend not in exclude and getattr(backend, "unavailable", None) is None
                and account_enabled(backend))

    @staticmethod
    def _reserve(backends, exclude=()):
        """min_account_balance is a failover reserve: accounts below it still serve
        while their confirmed balance is positive (unknown counts as usable)."""
        spare = []
        for backend in backends:
            reason = getattr(backend, "unavailable", None) or {}
            if backend in exclude or reason.get("reason") != "exhausted" or not account_enabled(backend):
                continue
            available = reason.get("available")
            if available is None or available > 0:
                spare.append(backend)
        return spare

    @staticmethod
    def _unusable_error(backends):
        backends = [b for b in backends if account_enabled(b)]
        if not backends:
            return apierr.service_unavailable("no enabled account: every account is switched off")
        reasons = [getattr(b, "unavailable", None) or {} for b in backends]
        limited = [r for r in reasons if r.get("reason") == "rate_limited"]
        if limited:
            # The hourly limit clears by itself: say when, instead of a 402/503 that implies a manual fix.
            wait = min(max(0.0, r.get("until_at", 0.0) - time.time()) for r in limited)
            return apierr.accounts_rate_limited(wait, len(limited), len(backends))
        exhausted = [(account_tag(b, UNNAMED), r.get("available"))
                     for b, r in zip(backends, reasons) if r.get("reason") == "exhausted"]
        if exhausted:
            threshold = next(r.get("threshold") for r in reasons if r.get("reason") == "exhausted")
            return apierr.insufficient_credits(threshold or 0, exhausted)
        return apierr.service_unavailable("no usable account: Retool sessions need a new sign-in")

    def _on_shared_session(self, isolated=False, context_scope=None):
        """True for a request that runs on the one shared session: shared mode, no isolation, no context scope."""
        return self.mode == "shared" and not isolated and not context_scope

    def _serving(self, isolated=False, context_scope=None):
        """The accounts a request of this kind can be routed to (models.serving over the pool's accounts)."""
        return model_catalog.serving(self.backends, self.cfg, self._on_shared_session(isolated, context_scope))

    def model_refusal(self, model_entry, isolated=False, context_scope=None):
        """The settled 404 when no account that serves this request can serve the model, else None.

        The HTTP layer calls this before a response starts, so a stream gets a real status instead of
        an SSE error. `isolated` and `context_scope` are acquire()'s and decide which accounts serve (_serving).
        With no provider resource on any of them, only the model's own route facts count and acquire() explains
        the missing resource, the same way discovery treats an unconfigured setup.
        """
        if model_entry is None:
            return None
        configs = [backend.cfg for backend in self._serving(isolated, context_scope)]
        refusals = model_catalog.refusal_reasons(model_entry, configs, model_catalog.pool_configured(configs))
        return model_catalog.model_unavailable(model_entry, refusals) if refusals and all(refusals) else None

    def _candidates(self, backends, model_entry, exclude=(), configured=True):
        """The accounts a request can go to right now as (kind, accounts); acquire()'s error when there are none.

        kind is "ready" (usable accounts), "reserve" (every one is below min_account_balance: the richest serves)
        or "signed_out" (a session that needs a new sign-in is still tried). Reads state only: nothing is
        reserved, awaited or counted. `configured` is refusal_reasons': acquire() always judges the resource.
        """
        reasons = (model_catalog.refusal_reasons(model_entry, [backend.cfg for backend in backends], configured)
                   if model_entry is not None else [None] * len(backends))
        routable = [backend for backend, reason in zip(backends, reasons) if not reason]
        refusals = [reason for reason in reasons if reason]
        if refusals and not routable:
            # Every account refuses this model by configuration (unsupported, retired, gated, no
            # resource): a settled 4xx that names the reason, never a retryable 503.
            raise model_catalog.model_unavailable(model_entry, refusals)
        if not any(account_enabled(b) for b in routable) and any(account_enabled(b) for b in backends):
            raise apierr.service_unavailable("no enabled account has a route for the requested model")
        eligible = [backend for backend in routable if self._usable(backend, exclude)]
        if eligible:
            return "ready", eligible
        spare = self._reserve(routable, exclude)
        if spare:
            return "reserve", spare
        # A signed-out account is still better than no account: its 401 makes the
        # refresher re-read config.json, so a fresh sign-in is adopted at once.
        signed_out = [backend for backend in routable if backend not in exclude and account_enabled(backend)
                      and (getattr(backend, "unavailable", None) or {}).get("reason") == "auth_dead"]
        if signed_out:
            return "signed_out", signed_out
        raise self._unusable_error(routable)

    def _check_shared(self, model_entry, exclude=(), configured=True):
        """What the shared session needs of its one account; acquire()'s error when it cannot serve."""
        reasons = (model_catalog.refusal_reasons(model_entry, [self.backend.cfg], configured)
                   if model_entry is not None else [None])
        if reasons[0]:
            raise model_catalog.model_unavailable(model_entry, reasons)
        if not account_enabled(self.backend):
            raise apierr.service_unavailable("the shared-mode account is switched off")
        self._candidates([self.backend], model_entry, exclude, configured)

    def unavailable_error(self, model_entry, isolated=False, context_scope=None, exclude=()):
        """The error acquire() raises right now because no account can take this request, or None.

        Reads state only (see _candidates). The HTTP layer asks before a stream starts, so the client gets the real
        status and Retry-After instead of an error event inside a 200. Like model_refusal, a pool without any provider
        resource leaves the explanation to acquire(). Room in the conversation pool is not part of the answer, and an
        account that fails between this check and acquire() still ends in an error event.
        """
        configured = model_catalog.pool_configured([b.cfg for b in self._serving(isolated, context_scope)])
        try:
            if self._on_shared_session(isolated, context_scope):
                self._check_shared(model_entry, exclude, configured)
            else:
                self._candidates(self.backends, model_entry, exclude, configured)
        except apierr.ApiError as error:
            return error
        return None

    def _next_backend(self, model_entry=None, exclude=()) -> Backend:
        kind, candidates = self._candidates(self.backends, model_entry, exclude)
        if kind == "reserve":
            # Every account is below the reserve: spend the richest one down to zero.
            return max(candidates, key=lambda b: float("inf") if b.unavailable.get("available") is None
                       else b.unavailable["available"])
        counts = {b: 0 for b in self.backends}
        for s in self._conversations.values():
            if getattr(s, "backend", None) in counts:
                counts[s.backend] += 1
        for session, _, _, _ in self._active.values():
            if getattr(session, "backend", None) in counts:
                counts[session.backend] += 2
        idx = getattr(self, "_rr_index", 0)
        self._rr_index = (idx + 1) % len(self.backends)
        # Operator priority first (lower wins), then the least loaded, then round-robin.
        return min(candidates, key=lambda b: (account_priority(b), counts[b],
                                              (self.backends.index(b) - idx) % len(self.backends)))

    @property
    def mode(self):
        return self.cfg["session_mode"]

    async def _evict_idle(self, make_room=False):
        now = time.monotonic()
        ttl = self.cfg.get("conversation_idle_timeout", 900)
        capacity = max(1, self.cfg.get("max_conversations", 128))
        idle = sorted((key for key in self._conversation_locks
                       if not self._conversation_refs.get(key)
                       and not getattr(self._conversations.get(key), "_cache_dependents", 0)),
                      key=lambda key: self._last_used.get(key, 0))
        closing = []
        for key in idle:
            expired = ttl > 0 and now - self._last_used.get(key, now) >= ttl
            full = make_room and len(self._conversation_locks) >= capacity
            if not expired and not full:
                continue
            session = self._conversations.pop(key, None)
            self._conversation_locks.pop(key, None)
            self._conversation_refs.pop(key, None)
            self._last_used.pop(key, None)
            if session is not None:
                session._warm_stop_reason = "evicted"
                closing.append(session)
            journal.emit("session.evict", "Диалог вытеснен (%s)" % ("простой" if expired else "нет места"),
                         "debug", conversation=journal.tag(key), reason="idle" if expired else "capacity")
        for session in closing:
            await session.aclose()

    async def _new_thread_id(self, backend=None) -> str:
        """Test backends may only implement create_thread()."""
        b = backend or self.backend
        maker = getattr(b, "new_thread_id", None)
        return await maker() if maker is not None else await b.create_thread()

    def _cache_parents(self, parent_key):
        """Pin only existing ancestors; never create a conversation for a label."""
        parents, seen = [], set()
        pending = [parent_key] if parent_key else []
        while pending:
            parent = pending.pop()
            candidates = [(k, s) for k, s in self._conversations.items()
                          if getattr(s, "_client_key", k) == parent]
            if self._shared is not None and getattr(self._shared, "_client_key", None) == parent:
                candidates.append((None, self._shared))
            for key, session in candidates:
                if id(session) in seen:
                    continue
                seen.add(id(session))
                parents.append((key, session))
                ancestor = getattr(session, "_parent_key", None)
                if ancestor:
                    pending.append(ancestor)
        return parents

    def _touch_cache_parents(self, parents, delta, dependent=None):
        for key, session in parents:
            # Never below zero: an unbalanced release must not corrupt the next request's pin.
            session._cache_dependents = max(0, getattr(session, "_cache_dependents", 0) + delta)
            if dependent is not None:
                records = getattr(session, "_cache_dependent_records", None)
                if records is None:
                    records = session._cache_dependent_records = set()
                if delta > 0:
                    records.add(dependent)
                else:
                    records.discard(dependent)
            session._last_request_at = time.time()
            if key in self._conversations:
                self._last_used[key] = time.monotonic()
            if session._warm_body and self._usable(session.backend):
                direct._ensure_keepwarm(session, session.cfg)

    async def acquire(self, conversation_key=None, *, isolated=False, model_entry=None, context_scope=None,
                      exclude=(), parent_key=None) -> SessionLease:
        """Take one slot and return an independently releasable request handle.

        `exclude` lists accounts that already failed this request (failover).
        """
        await self._slots.acquire()
        lock = None
        locked = False
        key = None
        referenced = False
        keep = False
        parents = self._cache_parents(parent_key) if parent_key != conversation_key else []
        dependent = _Dependent() if parents else None
        self._touch_cache_parents(parents, 1, dependent)
        client_key = conversation_key
        try:
            affinity_parent = (self._shared if self.mode == "shared" else
                               self._conversations.get(conversation_key) or
                               self._conversations.get((conversation_key, "text")))
            affinity_backend = (self.backend if self.mode == "shared" else
                                affinity_parent.backend if affinity_parent is not None else None)
            if affinity_backend is not None and not (self._usable(affinity_backend, exclude)
                                                     and self._routable(affinity_backend, model_entry)):
                # Affinity is strict only while the account can still serve this model.
                affinity_backend = None
            if context_scope and (conversation_key or self.mode == "shared"):
                conversation_key = (None if self.mode == "shared" else conversation_key, context_scope)
            if self._on_shared_session(isolated, context_scope):
                self._check_shared(model_entry, exclude)
                lock = self._lock
                await lock.acquire()
                locked = True
                if self._shared is None:
                    self._shared = RelaySession(self.backend, self.cfg["thread_id"])
                session = self._shared
                keep = True
            elif not isolated and conversation_key and (self.mode == "per_conversation" or
                                                        (self.mode == "shared" and context_scope)):
                key = conversation_key
                await self._evict_idle(make_room=key not in self._conversation_locks)
                capacity = max(1, self.cfg.get("max_conversations", 128))
                if key not in self._conversation_locks and len(self._conversation_locks) >= capacity:
                    raise apierr.service_unavailable("conversation pool is full; retry after an active conversation finishes")
                lock = self._conversation_locks.setdefault(key, asyncio.Lock())
                self._conversation_refs[key] = self._conversation_refs.get(key, 0) + 1
                referenced = True
                if lock.locked() and (getattr(self._conversations.get(key), "_direct", None)
                                      or getattr(self._conversations.get(key), "_cached_head", None)
                                      or self.cfg.get("transport") == "direct"
                                      or len(self.backends) > 1):
                    parent = self._conversations.get(key)
                    backend = (parent.backend if parent is not None and self._usable(parent.backend, exclude)
                               and self._routable(parent.backend, model_entry)
                               else affinity_backend or self._next_backend(model_entry, exclude))
                    self._check_route(backend, model_entry)
                    session = RelaySession(backend, await self._new_thread_id(backend))
                    if parent is not None:
                        if parent.backend is backend:
                            # Immutable snapshots are shared only by branches of
                            # this same conversation/account. Child replies must
                            # remain available after its temporary lease closes.
                            session._native_history = parent._native_history
                        if getattr(parent, "_cached_head", None):
                            session._cached_head = parent._cached_head
                        if getattr(parent, "_registered_tools", None):
                            session._registered_tools = dict(parent._registered_tools)
                            # Remember the inherited snapshot: on release only
                            # child-ADDED tools transfer back, so a narrowed or
                            # replaced client set cannot be resurrected by a union.
                            session._inherited_tools = set(parent._registered_tools)
                        if getattr(parent, "_direct", None):
                            session._direct = dict(parent._direct)
                        if getattr(parent, "_cached_turn_ends", None):
                            session._cached_turn_ends = list(parent._cached_turn_ends)
                    keep = False
                    lock = None
                else:
                    await lock.acquire()
                    locked = True
                    session = self._conversations.get(key)
                    if session is not None and not (self._usable(session.backend, exclude)
                                                    and self._routable(session.backend, model_entry)):
                        # The account ran out, was signed out or has no route for this
                        # model: move the conversation.
                        # Resolve the replacement first, so a 402/503 keeps the old state;
                        # a lone fallback account is the same backend and is kept as is.
                        replacement = affinity_backend or self._next_backend(model_entry, exclude)
                        if replacement is not session.backend:
                            self._check_route(replacement, model_entry)
                            old = session
                            session = RelaySession(replacement, await self._new_thread_id(replacement))
                            self._conversations[key] = session
                            await old.aclose()
                            # The new account must write the prefix cache once.
                            journal.emit("account.migrate", "Диалог перенесён на другой аккаунт", "warn",
                                         conversation=journal.tag(key),
                                         from_account=account_tag(old.backend), to_account=account_tag(replacement),
                                         reason=(getattr(old.backend, "unavailable", None) or {}).get(
                                             "reason", "failover" if self._routable(old.backend, model_entry)
                                             else "no_route"))
                    if session is None:
                        backend = affinity_backend or self._next_backend(model_entry, exclude)
                        self._check_route(backend, model_entry)
                        session = RelaySession(backend, await self._new_thread_id(backend))
                        self._conversations[key] = session
                        journal.emit("session.create", "Новый диалог — новый тред Retool", "info",
                                     conversation=journal.tag(key), thread=journal.tag(session.thread_id),
                                     account=account_tag(backend), pool=len(self._conversations))
                    keep = True
                    self._check_route(session.backend, model_entry)
            else:
                backend = (affinity_backend if isolated and affinity_backend is not None
                           else self._next_backend(model_entry, exclude))
                self._check_route(backend, model_entry)
                session = RelaySession(backend, await self._new_thread_id(backend))
            session._client_key = client_key
            session._parent_key = parent_key
            if dependent is not None:
                dependent.session = session
            session.begin_request()
            lease = SessionLease(session)
            lease._cache_parents = parents
            lease._cache_dependent = dependent
            self._active[lease] = (session, key, lock, keep)
            return lease
        except BaseException:
            self._touch_cache_parents(parents, -1, dependent)
            if locked:
                lock.release()
            if referenced:
                self._conversation_refs[key] -= 1
                if not self._conversation_refs[key] and key not in self._conversations:
                    self._conversation_refs.pop(key, None)
                    self._conversation_locks.pop(key, None)
            self._slots.release()
            raise

    async def release(self, lease: SessionLease) -> None:
        """Stale or duplicate release/cancel cannot affect a subsequent lease."""
        record = self._active.pop(lease, None)
        if record is None:
            return
        lease.released = True
        session, key, lock, keep = record
        try:
            if not keep:
                if key and key in self._conversations:
                    parent = self._conversations[key]
                    if parent is not None and getattr(session, "_registered_tools", None):
                        # Transfer only tools the child ADDED over its inherited
                        # snapshot: a plain union would resurrect tools the
                        # parent's client has since removed or replaced.
                        inherited = getattr(session, "_inherited_tools", set())
                        added = {name: tool for name, tool in session._registered_tools.items()
                                 if name not in inherited}
                        if added:
                            parent._registered_tools.update(added)
            if not keep or session._needs_drain or session._cancelled:
                await session.aclose()
        finally:
            self._touch_cache_parents(getattr(lease, "_cache_parents", []), -1,
                                      getattr(lease, "_cache_dependent", None))
            if key is not None:
                self._conversation_refs[key] -= 1
                self._last_used[key] = time.monotonic()
            if lock is not None:
                lock.release()
            self._slots.release()
        await self._evict_idle()

    async def __aenter__(self):
        self._lease = await self.acquire()
        return self._lease

    async def __aexit__(self, *exc):
        await self.release(self._lease)
        return False
