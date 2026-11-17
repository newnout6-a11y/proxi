"""Optional quotas and explicit runtime bounds must not add healthy-request waits."""
import json
import threading
import urllib.error

import pytest

import apierr
import config
import proxy


def test_disabled_local_quota_never_exhausts():
    limiter = proxy.RateLimiter(0, 1)
    assert all(limiter.take() == 0 for _ in range(1000))


def test_turning_the_limit_off_while_a_request_takes_a_token_is_no_division_by_zero():
    """The rate was read before the lock the admin's settings save changes it under: a limit turned off in between was a
    ZeroDivisionError, a 500 for the request that was taking a token."""
    limiter = proxy.RateLimiter(1, 1)
    limiter.tokens = 0.0

    class SavedFirst:  # the settings save takes the lock first and turns the limit off, as a thread switch lets it
        def __init__(self):
            self.inner = threading.Lock()

        def __enter__(self):
            self.inner.acquire()
            limiter.rate = 0

        def __exit__(self, *exc):
            self.inner.release()

    limiter.lock = SavedFirst()
    assert limiter.take() == 0.0


def test_opt_in_quota_returns_retry_time_without_sleeping(monkeypatch):
    monkeypatch.setattr(proxy.time, "monotonic", lambda: 100.0)
    limiter = proxy.RateLimiter(2, 1)
    assert limiter.take() == 0
    assert limiter.take() == 0.5


@pytest.mark.parametrize("wait,sent", [(0.5, "1"), (0.05, "1"), (1.2, "2"), (3.0, "3")])
def test_the_relays_own_429_sends_whole_seconds_in_retry_after(relay, monkeypatch, wait, sent):
    monkeypatch.setattr(proxy.RateLimiter, "take", lambda self: wait)
    try:
        relay.request("/v1/chat/completions", {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]})
    except urllib.error.HTTPError as exc:
        assert exc.code == 429 and exc.headers["retry-after"] == sent
        assert json.loads(exc.read())["error"]["type"] == "rate_limit_error"
    else:
        raise AssertionError("the request was not limited")


def load_config(tmp_path, monkeypatch, **settings):
    path = tmp_path / "config.json"
    cfg = {"base": "https://example.invalid", "page_uuid": "page", "ua": "ua",
           "cookie": "accessToken=fake-token", "xsrf": "fake-xsrf", **settings}
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")
    monkeypatch.delenv("RETOOL_COOKIE", raising=False)
    monkeypatch.delenv("RETOOL_XSRF", raising=False)
    return config.load(path)


def test_zero_caps_and_auto_reasoning_load_without_imposing_local_caps(tmp_path, monkeypatch):
    cfg = load_config(tmp_path, monkeypatch, max_exec_turns=0, max_prompt_bytes=0,
                      command_output_limit=0, rate_limit_rps=0, reasoning_effort="auto")
    assert cfg["max_exec_turns"] == cfg["max_prompt_bytes"] == cfg["command_output_limit"] == 0
    assert proxy.RateLimiter(cfg["rate_limit_rps"], cfg["rate_limit_burst"]).take() == 0


@pytest.mark.parametrize("field", ["max_exec_turns", "max_prompt_bytes", "command_output_limit"])
def test_negative_caps_are_rejected_instead_of_hiding_an_accidental_limit(tmp_path, monkeypatch, field):
    with pytest.raises(apierr.ApiError):
        load_config(tmp_path, monkeypatch, **{field: -1})


@pytest.mark.parametrize("field", ["turn_timeout", "command_timeout", "upstream_timeout", "max_workers"])
def test_invalid_resource_bounds_are_rejected(tmp_path, monkeypatch, field):
    with pytest.raises(apierr.ApiError):
        load_config(tmp_path, monkeypatch, **{field: 0})


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_non_finite_quota_values_are_rejected(tmp_path, monkeypatch, value):
    with pytest.raises(apierr.ApiError):
        load_config(tmp_path, monkeypatch, rate_limit_rps=value)
