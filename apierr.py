"""Typed API errors with OpenAI/Anthropic-shaped envelopes."""
import math
import time
from email.utils import parsedate_to_datetime

# Retool answers 429 with this sentence once an organization has spent its hourly managed-key budget.
HOURLY_LIMIT = "hourly limit of managed api key usage"


class ApiError(Exception):
    """An error with a definite HTTP status and machine code."""

    def __init__(self, status: int, code: str, message: str, *, param=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param
        self.retry_after = retry_after

    def openai_envelope(self) -> dict:
        return {"error": {"message": self.message, "type": self.code, "code": self.code, "param": self.param}}

    def anthropic_envelope(self) -> dict:
        # Anthropic uses a flat type + message pair.
        return {"type": "error", "error": {"type": self.code, "message": self.message}}

    def responses_envelope(self) -> dict:
        # Responses SSE errors are flat events with an explicit type; without it a
        # strict SDK keeps waiting for response.completed forever.
        return {"type": "error", "code": self.code, "message": self.message, "param": self.param}


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
    """Seconds from a Retry-After header (delta-seconds or an HTTP date); None when absent or unusable."""
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
    return seconds if math.isfinite(seconds) and seconds > 0 else None


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


def service_unavailable(message: str) -> ApiError:
    return ApiError(503, "service_unavailable", message)


def conflict(message: str) -> ApiError:
    return ApiError(409, "conflict", message)


def internal(message: str) -> ApiError:
    return ApiError(500, "internal_error", message)
