"""Typed API errors with OpenAI/Anthropic-shaped envelopes."""
import math
import time
from email.utils import parsedate_to_datetime

# Retool answers 429 with this sentence once an organization has spent its hourly managed-key budget.
HOURLY_LIMIT = "hourly limit of managed api key usage"

# Anthropic documents a closed set of error types (platform.claude.com/docs/en/api/errors, checked
# 2026-11-05). A relay code outside it is translated, or the client meets a type its error handling does
# not know. ApiError.code stays the relay's own: logs, the journal and the OpenAI dialects keep it.
ANTHROPIC_ERROR_TYPES = frozenset({
    "invalid_request_error", "authentication_error", "billing_error", "permission_error", "not_found_error",
    "conflict_error", "request_too_large", "rate_limit_error", "api_error", "timeout_error", "overloaded_error"})
_ANTHROPIC_TYPES = {
    "invalid_request_error": "invalid_request_error", "unprocessable_entity": "invalid_request_error",
    "unsupported_media_type": "invalid_request_error", "method_not_allowed": "invalid_request_error",
    "unauthorized": "authentication_error", "forbidden": "permission_error", "not_found_error": "not_found_error",
    "conflict": "conflict_error", "payload_too_large": "request_too_large", "prompt_too_large": "request_too_large",
    "rate_limit_error": "rate_limit_error", "insufficient_credits": "billing_error",
    "service_unavailable": "overloaded_error", "upstream_timeout": "timeout_error",
    # The relay's own upstream, not the client's key: a 502 the client can only retry or report.
    "upstream_error": "api_error", "upstream_auth_error": "api_error", "upstream_invalid_output": "api_error",
    "upstream_empty_turn": "api_error", "upstream_attempt_limit": "api_error",
    "upstream_tool_choice_ignored": "api_error", "internal_error": "api_error"}
_ANTHROPIC_STATUS_TYPES = {401: "authentication_error", 402: "billing_error", 403: "permission_error",
                           404: "not_found_error", 409: "conflict_error", 413: "request_too_large",
                           429: "rate_limit_error", 503: "overloaded_error", 504: "timeout_error",
                           529: "overloaded_error"}


def anthropic_type(code: str, status: int) -> str:
    """The documented Anthropic error type of a relay error; a code the table lacks follows its status."""
    if code in _ANTHROPIC_TYPES:
        return _ANTHROPIC_TYPES[code]
    return _ANTHROPIC_STATUS_TYPES.get(status) or ("api_error" if status >= 500 else "invalid_request_error")


# A Responses client reads error.code of `response.failed` to decide whether to ask again. codex-rs
# (rust-v0.164.0: codex-api/src/sse/responses_error.rs, protocol/src/error.rs, core/src/responses_retry.rs)
# stops at once for invalid_prompt and prints the message, stops for insufficient_quota and prints its own
# text, retries rate_limit_exceeded after the delay it reads from the message ("try again in 20s") and
# every other code up to stream_max_retries (5) times. ai-sdk (400 for "invalid", 429 and no retry for
# insufficient_quota, 5xx otherwise) and the openai SDK (server_error, rate_limit_exceeded, invalid_prompt are
# its documented codes) sort these codes the same way. A code outside the table follows its status.
RESPONSES_ERROR_CODES = {
    # Final: the request or the reply is what failed, so asking again only pays for the same failure.
    "invalid_request_error": "invalid_prompt", "unprocessable_entity": "invalid_prompt",
    "unsupported_media_type": "invalid_prompt", "method_not_allowed": "invalid_prompt",
    "unauthorized": "invalid_prompt", "forbidden": "invalid_prompt", "not_found_error": "invalid_prompt",
    "conflict": "invalid_prompt", "payload_too_large": "invalid_prompt", "prompt_too_large": "invalid_prompt",
    # The relay has already spent its own attempts on these.
    "upstream_invalid_output": "invalid_prompt", "upstream_empty_turn": "invalid_prompt",
    "upstream_attempt_limit": "invalid_prompt", "upstream_tool_choice_ignored": "invalid_prompt",
    "insufficient_credits": "insufficient_quota",
    # Temporary: the same request may well succeed later.
    "rate_limit_error": "rate_limit_exceeded",
    "upstream_error": "server_error", "upstream_auth_error": "server_error", "upstream_timeout": "server_error",
    "service_unavailable": "server_error", "internal_error": "server_error"}
_RESPONSES_STATUS_CODES = {402: "insufficient_quota", 429: "rate_limit_exceeded"}
# Codex sleeps the whole hinted delay before each of its retries and has no upper bound of its own.
RETRY_HINT_MAX = 60


def responses_code(code: str, status: int, headers=None) -> str:
    """The Responses error code of a relay error: the table, then a veto of retries, then the status."""
    if code in RESPONSES_ERROR_CODES:
        return RESPONSES_ERROR_CODES[code]
    if any(str(key).lower() == "x-should-retry" and str(value).lower() == "false"
           for key, value in (headers or {}).items()):
        return "invalid_prompt"
    return _RESPONSES_STATUS_CODES.get(status) or ("server_error" if status >= 500 else "invalid_prompt")


def retry_hint(seconds) -> str:
    """" Please try again in 1.5s." in the form codex-rs parses, only for a wait short enough to sit out."""
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        return ""
    wait = math.ceil(round(seconds * 10, 6)) / 10  # never earlier than the relay will accept the request
    return " Please try again in %gs." % wait if wait <= RETRY_HINT_MAX else ""


class ApiError(Exception):
    """An error with a definite HTTP status and machine code."""

    def __init__(self, status: int, code: str, message: str, *, param=None, retry_after=None, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param
        self.retry_after = retry_after
        self.headers = dict(headers or {})

    def openai_envelope(self) -> dict:
        return {"error": {"message": self.message, "type": self.code, "code": self.code, "param": self.param}}

    def anthropic_envelope(self) -> dict:
        # Anthropic uses a flat type + message pair. The relay's own code used to show as the type of every
        # code the translation did not know; it stays visible as relay_code wherever the type replaces it.
        kind = anthropic_type(self.code, self.status)
        error = {"type": kind, "message": self.message}
        if self.code != kind:
            error["relay_code"] = self.code
        return {"type": "error", "error": error}

    def responses_envelope(self) -> dict:
        # Responses SSE errors are flat events with an explicit type; without it a
        # strict SDK keeps waiting for response.completed forever.
        return {"type": "error", "code": self.code, "message": self.message, "param": self.param}

    def responses_error(self) -> dict:
        """The `error` of a `response.failed` event: the code a Responses client classifies by, and the message."""
        code = responses_code(self.code, self.status, self.headers)
        return {"code": code,
                "message": self.message + (retry_hint(self.retry_after) if code == "rate_limit_exceeded" else "")}


def bad_request(message: str, param=None) -> ApiError:
    return ApiError(400, "invalid_request_error", message, param=param)


def unprocessable(message: str, param=None) -> ApiError:
    return ApiError(422, "unprocessable_entity", message, param=param)


def unauthorized(message: str = "missing or invalid relay API key") -> ApiError:
    return ApiError(401, "unauthorized", message)


def not_found(message: str = "not found") -> ApiError:
    return ApiError(404, "not_found_error", message)


def method_not_allowed(message: str = "method not allowed") -> ApiError:
    return ApiError(405, "method_not_allowed", message)


def payload_too_large(limit: int) -> ApiError:
    return ApiError(413, "payload_too_large", f"request body exceeds {limit} bytes")


def too_many_requests(message: str = "relay is busy", retry_after: float = 1.0) -> ApiError:
    return ApiError(429, "rate_limit_error", message, retry_after=retry_after)


def is_hourly_limit(text) -> bool:
    """Retool's per-organization managed-key quota, as opposed to a short-lived 429."""
    return HOURLY_LIMIT in " ".join(str(text or "").lower().split())


def account_rate_limited(message: str, retry_after=None) -> ApiError:
    """429 that says this account is out of its hourly quota: another account may still serve the turn."""
    error = too_many_requests(message, retry_after)
    error.account_quota = True
    return error


def is_account_quota(error) -> bool:
    return getattr(error, "account_quota", False) is True


def retry_after_seconds(value):
    """Whole seconds from a Retry-After header (delta-seconds or an HTTP date), rounded up; None when absent or unusable."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(text).timestamp() - time.time()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return math.ceil(seconds) if math.isfinite(seconds) and seconds > 0 else None


def retry_after_header(seconds):
    """The Retry-After value of a response: RFC 9110 delta-seconds are whole, so round up; None for no header."""
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        return None
    return str(math.ceil(seconds))


def wait_text(seconds: float) -> str:
    seconds = max(1, math.ceil(seconds))
    return "%d мин" % math.ceil(seconds / 60) if seconds >= 60 else "%d с" % seconds


def accounts_rate_limited(wait: float, limited: int = 0, total: int = 0) -> ApiError:
    """Every account able to serve the request is parked after Retool's hourly limit."""
    seconds = max(1, math.ceil(wait))
    if limited >= total:
        message = ("Нет доступных аккаунтов: все аккаунты упёрлись в часовой лимит Retool. "
                   "Ближайший вернётся через %s." % wait_text(seconds))
    else:
        message = ("Нет доступных аккаунтов: %d из %d упёрлись в часовой лимит Retool, остальные исключены "
                   "из пула (баланс или вход). Ближайший из упёршихся вернётся через %s."
                   % (limited, total, wait_text(seconds)))
    return ApiError(429, "rate_limit_error", message, retry_after=seconds)


def upstream_auth_failed(detail: str = "") -> ApiError:
    return ApiError(502, "upstream_auth_error", f"Retool rejected the session (cookies/xsrf expired?){(': ' + detail) if detail else ''}")


def insufficient_credits(threshold, balances=()) -> ApiError:
    """Every account able to serve the model is below the configured reserve."""
    known = ", ".join("%s: %s" % (account, "?" if value is None else round(value, 2))
                      for account, value in balances)
    return ApiError(402, "insufficient_credits",
                    "Баланс Retool закончился: на всех аккаунтах меньше %s кредитов%s. "
                    "Пополните баланс или добавьте аккаунт в config.json."
                    % (round(threshold, 2), " (%s)" % known if known else ""))


def upstream_error(message: str) -> ApiError:
    return ApiError(502, "upstream_error", message)


def upstream_timeout(message: str = "upstream turn timed out") -> ApiError:
    return ApiError(504, "upstream_timeout", message)


def invalid_structured_output(message: str) -> ApiError:
    """A paid reply that breaks the JSON contract; retrying blindly would pay again."""
    return ApiError(502, "upstream_invalid_output", message, headers={"x-should-retry": "false"})


def service_unavailable(message: str) -> ApiError:
    return ApiError(503, "service_unavailable", message)


def conflict(message: str) -> ApiError:
    return ApiError(409, "conflict", message)


def internal(message: str) -> ApiError:
    return ApiError(500, "internal_error", message)
