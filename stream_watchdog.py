"""Stall watchdog for normalized async upstream event streams.

Three independent silence budgets run on elapsed time since the last meaningful model event:
  - first_token: from stream start until the first meaningful event (default 300 s)
  - idle:        between two consecutive meaningful events (default 120 s)
  - tool_call:   while waiting for arguments of an announced tool call that the upstream buffers whole (default 600 s)

Setting a budget to 0 or None disables it. Non-content events (request_sent, usage_start, usage, credits,
thinking_start, thinking_heartbeat, thinking_status, thinking_signature, and duplicate empty tool-call announcements)
never reset any silence timer.
"""
from __future__ import annotations

import asyncio
import math

import apierr

DEFAULT_FIRST_TOKEN_TIMEOUT = 300.0
DEFAULT_IDLE_TIMEOUT = 120.0
DEFAULT_TOOL_TIMEOUT = 600.0
CLOSE_GRACE = 2.0

FIRST_TOKEN = "first_token"
IDLE = "idle"
TOOL = "tool_call"

_END = object()


def _seconds(value, default):
    """Positive seconds as a float; None when disabled (0, inf); `default` when missing or invalid."""
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or number < 0:
        return default
    return None if number == 0 or math.isinf(number) else number


def timeouts(cfg) -> tuple[float | None, float | None]:
    """(first_token_timeout, idle_timeout) from config; 0 disables."""
    cfg = cfg or {}
    first = _seconds(cfg.get("stream_first_token_timeout", DEFAULT_FIRST_TOKEN_TIMEOUT), DEFAULT_FIRST_TOKEN_TIMEOUT)
    idle = _seconds(cfg.get("stream_idle_timeout", DEFAULT_IDLE_TIMEOUT), DEFAULT_IDLE_TIMEOUT)
    return first, idle


def tool_timeout(cfg) -> float | None:
    """Seconds allowed while an upstream buffers non-streaming tool arguments; None when disabled."""
    return _seconds((cfg or {}).get("stream_tool_timeout", DEFAULT_TOOL_TIMEOUT), DEFAULT_TOOL_TIMEOUT)


def max_silence_budget(cfg) -> float | None:
    """Largest active phase timeout (including tool_timeout), so socket read timeouts never fire before the watchdog."""
    first, idle = timeouts(cfg)
    tool = tool_timeout(cfg)
    budgets = [b for b in (first, idle, tool) if b is not None]
    return max(budgets) if budgets else None


def is_stream_timeout(error) -> bool:
    return getattr(error, "stream_timeout", False) is True


def stream_timeout_error(phase: str, seconds: float) -> apierr.ApiError:
    if phase == FIRST_TOKEN:
        message = f"upstream produced no output within {seconds:g}s of the request (first-token timeout)"
    elif phase == TOOL:
        message = f"upstream stream went silent: no output for {seconds:g}s after announcing a tool call (tool timeout)"
    else:
        message = f"upstream stream went silent: no output for {seconds:g}s after output started (idle timeout)"
    error = apierr.upstream_timeout(message)
    error.stage = "upstream"
    error.phase = phase
    error.timeout = seconds
    error.stream_timeout = True
    return error


def _call_key(event: dict):
    return event.get("id") or event.get("index") or event.get("name")


def opens_tool_call(event, calls: dict) -> bool:
    """True for the first empty announcement of a tool call; tracked in `calls` to prevent reset floods."""
    if not isinstance(event, dict) or event.get("type") != "tool_call" or not event.get("partial"):
        return False
    key = _call_key(event)
    if key in calls:
        return False
    calls[key] = 0
    return True


def _nonempty(value) -> bool:
    return isinstance(value, str) and value != ""


def is_progress(event, calls: dict | None = None) -> bool:
    """True when `event` carries actual model progress."""
    if not isinstance(event, dict):
        return False
    kind = event.get("type")
    if kind in ("text", "thinking"):
        return _nonempty(event.get("delta"))
    if kind == "final_text":
        return _nonempty(event.get("text"))
    if kind == "hitl":
        return True
    if kind == "tool_call":
        if not event.get("partial"):
            return True
        arguments = event.get("arguments")
        size = len(arguments) if isinstance(arguments, (str, bytes)) else (1 if arguments else 0)
        key = _call_key(event)
        grew = size > (calls.get(key, 0) if calls is not None else 0)
        if calls is not None and grew:
            calls[key] = size
        return grew or _nonempty(event.get("delta"))
    return False


async def _advance(iterator):
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _END


def _consume(task: asyncio.Future):
    if not task.cancelled():
        task.exception()


async def _bounded(awaitable, grace: float) -> bool:
    task = asyncio.ensure_future(awaitable)
    task.add_done_callback(_consume)
    done, _ = await asyncio.wait({task}, timeout=grace)
    if not done:
        task.cancel()
        await asyncio.wait({task}, timeout=grace)
    return task.done()


async def _shutdown(iterator, pending, grace: float):
    if pending is not None:
        if not pending.done():
            pending.cancel()
            await asyncio.wait({pending}, timeout=grace)
        if not pending.done():
            pending.add_done_callback(_consume)
            return
        _consume(pending)
    close = getattr(iterator, "aclose", None)
    if close is not None:
        try:
            await _bounded(close(), grace)
        except Exception:
            pass


def _expire(phase: str, limit: float, on_timeout):
    if callable(on_timeout):
        try:
            on_timeout()
        except Exception:
            pass
    raise stream_timeout_error(phase, limit)


async def watch(
    source,
    first_timeout: float | None = DEFAULT_FIRST_TOKEN_TIMEOUT,
    idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT,
    on_timeout=None,
    *,
    close_grace: float = CLOSE_GRACE,
    tool_timeout: float | None = DEFAULT_TOOL_TIMEOUT,
):
    """Yield events of `source` unchanged; abort and raise upstream_timeout when a silence budget expires."""
    first = _seconds(first_timeout, None)
    idle = _seconds(idle_timeout, None)
    tool = _seconds(tool_timeout, None)
    iterator = source.__aiter__()
    loop = asyncio.get_running_loop()
    calls: dict = {}
    announced: set = set()
    pending = None
    phase, limit = FIRST_TOKEN, first
    deadline = None if first is None else loop.time() + first
    try:
        while True:
            pending = asyncio.ensure_future(_advance(iterator))
            wait = None if deadline is None else max(0.0, deadline - loop.time())
            done, _ = await asyncio.wait({pending}, timeout=wait)
            if done:
                event = pending.result()
                pending = None
                if event is _END:
                    return
                now = loop.time()
                if is_progress(event, calls):
                    if event.get("type") == "tool_call":
                        announced.discard(_call_key(event))
                    phase, limit = (TOOL, tool) if announced else (IDLE, idle)
                    deadline = None if limit is None else now + limit
                elif opens_tool_call(event, calls):
                    announced.add(_call_key(event))
                    phase, limit = TOOL, tool
                    deadline = None if limit is None else now + limit
                elif deadline is not None and now >= deadline:
                    _expire(phase, limit, on_timeout)
                yield event
            else:
                _expire(phase, limit, on_timeout)
    finally:
        await _shutdown(iterator, pending, close_grace)


def guard(source, cfg, on_timeout=None, *, close_grace: float = CLOSE_GRACE):
    """Wrap `source` with the silence budgets configured in `cfg`."""
    first, idle = timeouts(cfg)
    return watch(source, first, idle, on_timeout, close_grace=close_grace, tool_timeout=tool_timeout(cfg))
