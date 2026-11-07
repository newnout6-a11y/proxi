"""Terminal-only presentation; redirected logs stay readable without ANSI."""
import math
import os
import re
import sys
import threading

import journal

# Terminal theme colors, by meaning; no fixed background or truecolor dependency.
PALETTE = {"read": "36", "write": "33", "alert": "1;91", "success": "32",
           "request": "34", "model": "92", "balance": "32", "muted": "90"}
LARGE_WRITE = 50_000
LARGE_INPUT = 50_000
WRITE_JUMP = 8_192
_LOCK = threading.Lock()
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_FIELDS = re.compile(r"\b(in|out|read|write|credits|balance|limit|spent)=([^\s]+)")


def number(value, *, signed=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and (signed or value >= 0) else None
    except (ValueError, TypeError, OverflowError):
        return None


def amount(value, *, tokens=False, signed=False):
    value = number(value, signed=signed)
    if value is None:
        return "?"
    return format(int(value), ",") if tokens else format(value, ",.4f")


def color_enabled(stream=None):
    stream = sys.stdout if stream is None else stream
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("FORCE_COLOR") not in (None, "", "0"):
        return True
    enabled = os.environ.get("TERM") != "dumb" and bool(getattr(stream, "isatty", lambda: False)())
    if enabled and os.name == "nt":
        # Enable ANSI processing only on this process's attached console handle.
        try:
            import ctypes
            import msvcrt
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
            mode = ctypes.c_uint()
            kernel = ctypes.windll.kernel32
            enabled = bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and
                           kernel.SetConsoleMode(handle, mode.value | 0x0004))
        except (OSError, ValueError, AttributeError):
            enabled = False
    return enabled


def _paint(text, role, enabled):
    return "\x1b[%sm%s\x1b[0m" % (PALETTE[role], text) if enabled else text


def render(line, *, color=None):
    # Scrub before inserting escape sequences, including raw model/effort fields.
    line = _ANSI.sub("", str(journal.scrub(str(line)))).replace("\x1b", "")
    enabled = color_enabled() if color is None else color
    if not enabled:
        return line
    if line.startswith("[err]") or " EXC " in line:
        return _paint(line, "alert", True)
    write_alert = "[WRITE SPIKE" in line or "[WRITE HIGH" in line
    input_alert = "[INPUT HIGH]" in line

    def field(match):
        key = match[1]
        role = {"read": "read", "write": "alert" if write_alert else "write",
                "in": "alert" if input_alert else "muted",
                "credits": "alert" if input_alert else "model",
                "balance": "balance", "spent": "model"}.get(key, "muted")
        if match[2] == "?":
            role = "muted"
        elif key == "balance" and match[2].startswith("-"):
            role = "alert"
        return _paint(match[0], role, True)

    line = _FIELDS.sub(field, line)
    line = re.sub(r"\[(?:WRITE (?:SPIKE|HIGH)|INPUT HIGH)[^\]]*\]",
                  lambda m: _paint(m[0], "alert", True), line)
    line = re.sub(r"\b(?:MODEL|EFFORT):\s*\S+", lambda m: _paint(m[0], "model", True), line)
    line = re.sub(r"^\[([^\]]+)\]", lambda m: _paint(m[0], "request", True), line)
    match = re.match(r"(relay \S+ \S+ \S+ )(\d+)(.*)", line)
    if match:
        status = int(match[2])
        role = "alert" if status >= 400 or status == 0 else "success"
        line = match[1] + _paint(match[2], role, True) + match[3]
    return line


def write(line):
    """Best effort, as the journal is: a console that cannot be written (the supervisor that read the relay's stdout was
    killed without /T, and the pipe is gone) used to raise OSError out of request handling, so every completion and every
    error ended with no response at all, and the journal lost the event too."""
    with _LOCK:
        try:
            print(render(line), flush=True)
        except (OSError, ValueError):
            pass


class CacheMonitor:
    """Compare writes within one session/model scope, never across other chats."""
    def __init__(self):
        self.scope = None
        self.request = None
        self.previous_write = None
        self.last_write = None

    def line(self, request_id, usage, *, scope=None, sample_id=None):
        if scope != self.scope:
            self.scope = scope
            self.request = self.previous_write = self.last_write = None
        sample_id = request_id if sample_id is None else sample_id
        if sample_id != self.request:
            self.previous_write = self.last_write
            self.request = sample_id
        read = number(usage.get("promptTokensReadFromCache"))
        written = number(usage.get("promptTokensWrittenToCache"))
        marker = ""
        if written is not None:
            previous = self.previous_write
            if previous is not None and written - previous >= WRITE_JUMP and written >= 4 * max(previous, 1):
                marker = " [WRITE SPIKE +%s]" % amount(written - previous, tokens=True)
            elif written >= LARGE_WRITE:
                marker = " [WRITE HIGH]"
            self.last_write = written
        uncached = number(usage.get("inputTokens"))
        if uncached is not None and uncached >= LARGE_INPUT:
            marker += " [INPUT HIGH]"
        output = number(usage.get("outputTokens"))
        output_note = " out=%s" % amount(output, tokens=True) if output is not None else ""
        return "[%s] [cache] in=%s read=%s write=%s credits=%s%s%s" % (
            request_id, amount(usage.get("inputTokens"), tokens=True), amount(read, tokens=True),
            amount(written, tokens=True), amount(usage.get("creditsUsed")), output_note, marker)


def balance_line(request_id, snapshot, spent=None, account=None):
    state = ("UNAVAILABLE" if number(snapshot.get("available"), signed=True) is None else
             "UNREPORTED USAGE" if snapshot.get("unreported_usage") else
             "STALE" if snapshot.get("stale") else "LOCAL ESTIMATE" if snapshot.get("estimated") else "CACHED")
    return "[%s] [balance]%s balance=%s limit=%s spent=%s [%s]" % (
        request_id, " account=%s" % account if account else "", amount(snapshot.get("available"), signed=True), amount(snapshot.get("monthly_allocation")), amount(spent), state)
