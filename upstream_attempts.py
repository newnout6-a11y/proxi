"""A request-wide budget shared by transport, prompt retries and account failover."""
import threading
import time

import apierr

MAX_ATTEMPTS = 5  # Initial POST plus at most four further POSTs, never five retries.


class AttemptBudget:
    def __init__(self, cancel_event=None):
        self.used = 0
        self.last = {}
        self.empty_dump_id = None
        self.empty_dump_attempted = False
        self.cancel_event = cancel_event
        self._lock = threading.Lock()

    @property
    def remaining(self):
        return max(0, MAX_ATTEMPTS - self.used)

    def wait(self, delay, cancelled):
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if cancelled() or (self.cancel_event and self.cancel_event.is_set()):
                raise InterruptedError("upstream retry cancelled")
            interval = min(0.05, max(0, deadline - time.monotonic()))
            if self.cancel_event:
                self.cancel_event.wait(interval)
            else:
                time.sleep(interval)

    def take(self, cancelled):
        if not self.remaining:
            raise self.error("upstream_attempt_limit", "upstream attempt limit reached (5 attempts)")
        # A successful local exec follow-up is a new step, not a failed retry.
        delay = min(0.5 * 2 ** max(0, self.used - 1), 4.0) if self.used and self.last.get("outcome") != "content" else 0
        self.wait(delay, cancelled)
        if cancelled() or (self.cancel_event and self.cancel_event.is_set()):
            raise InterruptedError("upstream retry cancelled")
        with self._lock:
            if not self.remaining:
                raise self.error("upstream_attempt_limit", "upstream attempt limit reached (5 attempts)")
            self.used += 1
            return self.used

    def attach(self, error):
        if not getattr(error, "diagnostic_id", None):
            error.diagnostic_id = self.last.get("diagnostic_id")
        if not hasattr(error, "upstream_status"):
            error.upstream_status = self.last.get("http_status")
        error.upstream_request_id = self.last.get("upstream_request_id")
        error.upstream_attempts = self.used
        error.stage = "upstream"
        return error

    def error(self, code="upstream_empty_turn", message="upstream returned an empty turn"):
        return self.attach(apierr.ApiError(502, code, message))
