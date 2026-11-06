"""A step-wide budget shared by transport, prompt retries and account failover."""
import threading
import time

import apierr

MAX_ATTEMPTS = 5  # Initial POST plus at most four further POSTs per step, never five retries.


class AttemptBudget:
    """Counts POSTs of one client request.

    The five-POST limit applies to one *step*: the initial POST and the retries after
    failed attempts (empty answer, auth refresh, Assist fallback, failover, ...). A
    relay exec step that produced usable output starts a new step via new_step();
    exec steps are bounded only by max_exec_turns. total_cap is an optional hard cap
    on all POSTs of the request, set only when max_exec_turns bounds the steps.
    """

    def __init__(self, cancel_event=None, total_cap=None, clock=None):
        self.used = 0
        self.step_used = 0
        self.total_cap = total_cap
        self.last = {}
        self.last_trace = None
        self.empty_dump_id = None
        self.empty_dump_attempted = False
        self.cancel_event = cancel_event
        # The account that got the latest POST and how many POSTs in a row it got within this step.
        self.account = None
        self.account_streak = 0
        # When the latest POST was claimed, on `clock`: the account dispatcher's, so that it can tell
        # a limit reported by a request that left before an account's last return from a new one.
        self.sent_at = None
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def remaining(self):
        left = max(0, MAX_ATTEMPTS - self.step_used)
        if self.total_cap:
            left = min(left, max(0, self.total_cap - self.used))
        return left

    def new_step(self):
        """A successful local exec follow-up is a new step with a fresh retry counter."""
        with self._lock:
            self.step_used = 0
            self.account_streak = 0

    def _limit_error(self):
        limit = self.total_cap if self.total_cap and self.used >= self.total_cap else MAX_ATTEMPTS
        return self.error("upstream_attempt_limit", "upstream attempt limit reached (%d attempts)" % limit)

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

    def take(self, cancelled, account=None):
        """Claim the next POST of the step; `account` is the backend it goes to (None when unknown)."""
        if not self.remaining:
            raise self._limit_error()
        # The first POST of a step is not a retry and never waits. Neither does a POST to another
        # account than the previous one: the backoff spares the account that just failed, and that
        # attempt cost nothing, so a failover starts at once and its new account backs off from scratch.
        same = account is None or account is self.account
        streak = self.account_streak if same else 0
        delay = (min(0.5 * 2 ** max(0, streak - 1), 4.0)
                 if streak and self.last.get("outcome") != "content" else 0)
        self.wait(delay, cancelled)
        if cancelled() or (self.cancel_event and self.cancel_event.is_set()):
            raise InterruptedError("upstream retry cancelled")
        with self._lock:
            if not self.remaining:
                raise self._limit_error()
            self.used += 1
            self.step_used += 1
            self.account_streak = streak + 1
            self.sent_at = (self._clock or time.monotonic)()
            if account is not None:
                self.account = account
            return self.used

    def persist_last(self):
        """Write the last attempt's summary even when it was skipped as a content outcome."""
        trace = self.last_trace
        if trace is not None:
            trace.save()

    def attach(self, error):
        # An error that points at a diagnostic id must find its summary on disk,
        # even when that attempt (a content outcome) was not saved on its own.
        self.persist_last()
        if not getattr(error, "diagnostic_id", None):
            error.diagnostic_id = self.last.get("diagnostic_id")
        if not hasattr(error, "upstream_status"):
            error.upstream_status = self.last.get("http_status")
        error.upstream_request_id = self.last.get("upstream_request_id")
        error.upstream_attempts = self.used
        error.stage = "upstream"
        return error

    # The answer to a turn whose every attempt came back without text or a tool call. An empty or thinking-only end is how
    # the provider's classifier refuses a request (HTTP 200, no stop reason through Retool); probes of 2026-11-06 traced
    # such refusals to instructions in the client's system prompt (docs/thinking-only-replies-2026-11-05.md).
    EMPTY_TURN = ("upstream returned an empty turn: no text and no tool call. This can be a refusal by the provider's "
                  "classifier, for example of instructions that claim priority over the platform's rules or ask the model "
                  "to reveal its reasoning; check the system prompt and the project instructions the client sends")

    def error(self, code="upstream_empty_turn", message=EMPTY_TURN):
        return self.attach(apierr.ApiError(502, code, message))
