"""Typed API errors with OpenAI/Anthropic-shaped envelopes."""


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


def upstream_auth_failed(detail: str = "") -> ApiError:
    return ApiError(502, "upstream_auth_error", f"Retool rejected the session (cookies/xsrf expired?){(': ' + detail) if detail else ''}")


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
