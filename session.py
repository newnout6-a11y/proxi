"""Upstream Retool transport: thread creation, WebSocket turns, retries, cancellation."""
import asyncio
import json
import os
import time
import urllib.error
import urllib.request
import uuid

import websockets

import apierr
from auth import upstream_headers
from upstream_refresh import Refresher

MAX_FRAME_DEFAULT = 16 * 1024 * 1024


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
                    None, lambda: self._http_post(path, payload, timeout))
            except urllib.error.HTTPError as exc:
                body = exc.read()[:300].decode("utf-8", "replace") if exc.fp else ""
                if exc.code in (401, 403):
                    detail = body or (self.refresher.last_error or "")
                    last = apierr.upstream_auth_failed(detail)
                    if attempt < attempts - 1 and self.refresher.refresh():
                        continue
                    raise last
                if exc.code == 429:
                    retry_after = exc.headers.get("retry-after") if exc.headers else None
                    last = apierr.too_many_requests("Retool rate limit", float(retry_after or 2))
                    await asyncio.sleep(min(float(retry_after or 2), 5) * (attempt + 1))
                    continue
                if 500 <= exc.code < 600:
                    last = apierr.upstream_error(f"Retool HTTP {exc.code}{(': ' + body) if body else ''}")
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                raise apierr.upstream_error(f"Retool HTTP {exc.code}{(': ' + body) if body else ''}")
            except TimeoutError:
                last = apierr.upstream_timeout(f"Retool did not answer within {timeout}s")
                await asyncio.sleep(0.4 * (attempt + 1))
            except urllib.error.URLError as exc:
                last = apierr.upstream_error(f"Retool unreachable: {exc.reason}")
                await asyncio.sleep(0.4 * (attempt + 1))
        raise last if last is not None else apierr.upstream_error("upstream call failed")

    # -- API ----------------------------------------------------------------
    async def get_messages(self, thread_id):
        """Fetch persisted messages (used to explain silent turns)."""
        path = f"/api/pages/{self.cfg['page_uuid']}/sandpack/threads/{thread_id}/messages"

        def call():
            req = urllib.request.Request(self.cfg["base"] + path, headers=upstream_headers(self.cfg), method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))

        return await asyncio.get_running_loop().run_in_executor(None, call)

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


class RelaySession:
    """One WebSocket turn stream bound to a single Retool thread."""

    def __init__(self, backend: Backend, thread_id):
        self.backend = backend
        self.cfg = backend.cfg
        self.thread_id = thread_id
        self.ws = None
        self._cancelled = False
        self._needs_drain = False
        self._drain_task = None
        self.sandbox_id = None

    def cancel(self):
        self._cancelled = True
        self._needs_drain = True
        # Keep reading (and discarding) the aborted turn so its frames cannot
        # bleed into the next one; the next turn awaits this drain first.
        if self.ws is not None and self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain_until_done())

    async def _drain_until_done(self):
        deadline = time.time() + 45
        try:
            while self.ws is not None and time.time() < deadline:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=5)
                await self._maybe_pong(raw)
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "agent_loop_done":
                    break
        except Exception:
            pass
        finally:
            self._drain_task = None

    async def open(self):
        if self.ws is not None:
            return
        try:
            started = await self.backend.start_run(self.thread_id)
            self.ws = await self.backend.connect(started.url)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in (401, 403) or "401" in str(exc):
                if not self.backend.refresher.refresh():
                    raise apierr.upstream_auth_failed(self.backend.refresher.last_error or str(exc))
                started = await self.backend.start_run(self.thread_id)
                self.ws = await self.backend.connect(started.url)
            else:
                raise
        self.sandbox_id = started.sandbox_id
        await self._drain()

    async def _drain(self):
        while True:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=0.4)
            except asyncio.TimeoutError:
                return
            except Exception:
                return
            await self._maybe_pong(raw)

    async def _maybe_pong(self, raw):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        if event.get("type") == "ping":
            try:
                await self.ws.send('{"type":"pong"}')
            except Exception:
                pass

    async def aclose(self):
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    async def stream(self, prompt, model_provider):
        """Yield normalised events. Raises ApiError on failure."""
        # A session outlives one turn in shared mode: clear the flag and any
        # frames left over from a cancelled turn before starting a new one.
        if self._drain_task is not None:
            try:
                await asyncio.shield(self._drain_task)
            except Exception:
                pass
        self._cancelled = False
        if self.ws is not None and self._needs_drain:
            await self._drain()
            self._needs_drain = False
        try:
            await self.open()
        except Exception as exc:
            raise exc if isinstance(exc, apierr.ApiError) else apierr.upstream_error(str(exc))
        try:
            await self.ws.send(json.dumps({
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
            }, ensure_ascii=True))
        except Exception as exc:
            raise apierr.upstream_error(f"websocket send failed: {exc}")

        deadline = time.time() + self.cfg["turn_timeout"]
        last_text = ""
        full_text = ""
        block_id = None
        thinking_text = ""
        thinking_id = None
        last_thinking = ""
        restart_hint = False
        reconnects = 0
        while True:
            if self._cancelled:
                if self.ws is not None and self._drain_task is None:
                    self._drain_task = asyncio.create_task(self._drain_until_done())
                yield {"type": "end", "reason": "cancelled"}
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                yield {"type": "end", "reason": "timeout"}
                return
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=min(remaining, self.cfg["recv_timeout"]))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if reconnects >= 1:
                    raise apierr.upstream_error(f"websocket closed: {exc}")
                reconnects += 1
                await self.aclose()
                try:
                    await self.open()
                except Exception as inner:
                    raise apierr.upstream_error(f"websocket reconnect failed: {inner}")
                continue

            await self._maybe_pong(raw)
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
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
                    yield {"type": "text", "delta": delta}
            elif kind == "thinking":
                content = event.get("content") or ""
                if event.get("messageId") != thinking_id:
                    if thinking_text.strip():
                        thinking_text += "\n"
                    thinking_id = event.get("messageId")
                    last_thinking = ""
                if content.startswith(last_thinking):
                    thinking_text += content[len(last_thinking):]
                elif len(content) >= len(last_thinking):
                    thinking_text += content[len(last_thinking):]
                else:
                    thinking_text += content
                last_thinking = content
            elif kind == "clear_streaming":
                # UI signal (usually before a tool call): the next frame may restart
                # the same text, so we only mark that the buffer may be replaced.
                restart_hint = True
            elif kind == "run_credits_persisted":
                value = event.get("creditsUsed")
                if isinstance(value, (int, float)):
                    yield {"type": "credits", "value": float(value)}
            elif kind == "hitl":
                yield {"type": "hitl", "question": _hitl_text(event)}
            elif kind == "agent_loop_done":
                for trail in await self._trailing():
                    yield trail
                final = full_text
                if not final.strip() and thinking_text.strip():
                    # The model answered only in its thinking channel: surface it.
                    final = thinking_text.strip()
                    yield {"type": "text", "delta": final}
                yield {"type": "final_text", "text": final}
                yield {"type": "end", "reason": "stop"}
                return
            elif kind == "error":
                raise apierr.upstream_error(str(event)[:300])
            elif os.environ.get("DEBUG_EVENTS"):
                print("dbg-unknown:", json.dumps(event, ensure_ascii=False)[:400], flush=True)

    async def _trailing(self):
        """Collect a trailing credits frame, but never block the turn for long."""
        end = time.time() + 0.8
        out = []
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=0.25)
            except asyncio.TimeoutError:
                break
            await self._maybe_pong(raw)
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
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


class SessionPool:
    """Provides a RelaySession per request (isolated) or one shared session."""

    def __init__(self, backend: Backend):
        self.backend = backend
        self.cfg = backend.cfg
        self._shared = None
        self._lock = asyncio.Lock()
        self._slots = asyncio.BoundedSemaphore(max(1, self.cfg["max_workers"]))
        self._active = set()
        self._starts = 0
        self._started_at = time.time()

    @property
    def mode(self):
        return self.cfg["session_mode"]

    async def acquire(self) -> RelaySession:
        """Take a session slot. Caller must always release() it (once)."""
        await self._slots.acquire()
        if self.mode == "shared":
            locked = False
            try:
                await self._lock.acquire()
                locked = True
                if self._shared is None:
                    self._shared = RelaySession(self.backend, self.cfg["thread_id"])
                self._active.add(self._shared)
                return self._shared
            except BaseException:
                if locked:
                    self._lock.release()
                self._slots.release()
                raise
        try:
            thread_id = await self.backend.create_thread()
        except BaseException:
            # CancelledError is a BaseException: the slot must still come back.
            self._slots.release()
            raise
        session = RelaySession(self.backend, thread_id)
        self._active.add(session)
        return session

    async def release(self, session: RelaySession) -> None:
        """Release a session exactly once; extra releases are ignored."""
        if self.mode != "shared" and session not in self._active:
            return
        self._active.discard(session)
        try:
            if self.mode != "shared":
                await session.aclose()
        finally:
            if self.mode == "shared":
                try:
                    self._lock.release()
                except RuntimeError:
                    pass
            try:
                self._slots.release()
            except ValueError:
                pass

    async def __aenter__(self):
        self._lease = await self.acquire()
        return self._lease

    async def __aexit__(self, *exc):
        await self.release(self._lease)
        return False


