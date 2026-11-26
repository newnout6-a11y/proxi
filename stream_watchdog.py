"""Stall watchdog for normalized async upstream event streams.

Four independent silence budgets run on elapsed time since the last meaningful model event:
  - first_token: from stream start until the first meaningful event (default 300 s)
  - idle:        between two consecutive meaningful events (default 120 s)
  - tool_call:   while an announced tool call is not complete: its upstream may buffer the arguments (default 600 s,
                 renewed by every argument piece that arrives)
  - thinking:    after a real sign from the upstream that the model reasons in silence (default 900 s): Retool sends
                 nothing between the start of an Opus thinking block and its signature, 9-107 s in probes and over
                 300 s on a ~1M-character prompt at max effort (2026-11-18)

Setting a budget to 0 or None disables it. Non-content events (request_sent, usage_start, usage, credits,
thinking_heartbeat, the relay's own thinking_status timer, and duplicate empty tool-call announcements) never reset any
silence timer; a real thinking sign (thinking_start, an empty thinking delta, thinking_signature, or thinking_status from
the upstream) switches to the thinking budget, never to a shorter one. So does reasoning text (a non-empty thinking
delta) until the answer starts: a model may stream a reasoning summary and then reason in silence for minutes before it
writes; text and tool-call progress go back to the idle and tool budgets.
"""
from __future__ import annotations

import asyncio
import math

import apierr

DEFAULT_FIRST_TOKEN_TIMEOUT = 300.0
DEFAULT_IDLE_TIMEOUT = 120.0
DEFAULT_TOOL_TIMEOUT = 600.0
DEFAULT_THINKING_TIMEOUT = 900.0
CLOSE_GRACE = 2.0

FIRST_TOKEN = "first_token"
IDLE = "idle"
TOOL = "tool_call"
THINKING = "thinking"

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


def thinking_timeout(cfg) -> float | None:
    """Seconds allowed while the upstream said the model is thinking and sends nothing else; None when disabled."""
    return _seconds((cfg or {}).get("stream_thinking_timeout", DEFAULT_THINKING_TIMEOUT), DEFAULT_THINKING_TIMEOUT)


def max_silence_budget(cfg) -> float | None:
    """Largest active phase timeout (including tool and thinking), so socket read timeouts never fire before the watchdog."""
    first, idle = timeouts(cfg)
    budgets = [b for b in (first, idle, tool_timeout(cfg), thinking_timeout(cfg)) if b is not None]
    return max(budgets) if budgets else None


def is_stream_timeout(error) -> bool:
    return getattr(error, "stream_timeout", False) is True


def stream_timeout_error(phase: str, seconds: float) -> apierr.ApiError:
    if phase == FIRST_TOKEN:
        message = f"upstream produced no output within {seconds:g}s of the request (first-token timeout)"
    elif phase == TOOL:
        message = f"upstream stream went silent: no output for {seconds:g}s after announcing a tool call (tool timeout)"
    elif phase == THINKING:
        message = f"upstream stream went silent: no output for {seconds:g}s after the model began thinking (thinking timeout)"
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


def _outlasts(budget, other) -> bool:
    """True when `budget` is longer than `other`; a disabled budget (None) is the longest of all."""
    return other is not None and (budget is None or budget > other)


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


def is_thinking_signal(event) -> bool:
    """True for the upstream's own sign that the model reasons without output yet: the start of a thinking block, its
    (possibly empty) delta, its signature, or the transport's status for such a start. The relay's timer status
    (source "relay") and its heartbeats are no such sign."""
    if not isinstance(event, dict):
        return False
    kind = event.get("type")
    if kind in ("thinking_start", "thinking_signature", "thinking"):
        return True
    return kind == "thinking_status" and event.get("source") == "upstream"


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


async def _close_iterator(iterator, grace: float):
    close = getattr(iterator, "aclose", None)
    if close is not None:
        try:
            await _bounded(close(), grace)
        except Exception:
            pass


async def _shutdown(iterator, pending, grace: float):
    if pending is not None:
        if not pending.done():
            pending.cancel()
            await asyncio.wait({pending}, timeout=grace)
        if not pending.done():
            loop = asyncio.get_running_loop()

            def _finish(task):
                _consume(task)
                try:
                    closer = loop.create_task(_close_iterator(iterator, grace))
                    closer.add_done_callback(_consume)
                except RuntimeError:
                    pass

            pending.add_done_callback(_finish)
            return
        _consume(pending)
    await _close_iterator(iterator, grace)


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
    thinking_timeout: float | None = DEFAULT_THINKING_TIMEOUT,
):
    """Yield events of `source` unchanged; abort and raise upstream_timeout when a silence budget expires."""
    first = _seconds(first_timeout, None)
    idle = _seconds(idle_timeout, None)
    tool = _seconds(tool_timeout, None)
    thinking = _seconds(thinking_timeout, None)
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
                        key = _call_key(event)
                        if event.get("partial"):
                            # OpenAI Chat and Minara often attach initial arguments on the very first
                            # partial tool_call event; register it in `announced` so later pauses
                            # receive the TOOL budget instead of falling back to IDLE.
                            announced.add(key)
                            calls.setdefault(key, 0)
                        else:
                            # Only the complete call ends its budget: Retool without fine-grained streaming sends the first
                            # ~20 characters of the arguments and holds the rest for tens of seconds (the idle budget cut
                            # six Claude Code turns at a Write on 2026-11-18).
                            announced.discard(key)
                    if announced:
                        phase, limit = TOOL, tool
                    elif event.get("type") == "thinking" and _outlasts(thinking, idle):
                        # Reasoning text is no answer yet: the model may reason in silence again before it writes.
                        # gpt-6.2-sol at max streamed its reasoning summary for 59-64 s, then thought for over 120 s
                        # without a word, and the idle budget cut it with a 504 (2026-11-19).
                        phase, limit = THINKING, thinking
                    else:
                        phase, limit = IDLE, idle
                    deadline = None if limit is None else now + limit
                elif opens_tool_call(event, calls):
                    announced.add(_call_key(event))
                    phase, limit = TOOL, tool
                    deadline = None if limit is None else now + limit
                elif phase != TOOL and deadline is not None and is_thinking_signal(event) and (
                        thinking is None or now + thinking > deadline):
                    phase, limit = THINKING, thinking
                    deadline = None if thinking is None else now + thinking
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
    return watch(source, first, idle, on_timeout, close_grace=close_grace, tool_timeout=tool_timeout(cfg),
                 thinking_timeout=thinking_timeout(cfg))
