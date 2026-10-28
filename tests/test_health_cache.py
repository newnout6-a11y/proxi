"""Unauthenticated health probes must not amplify upstream/auth requests."""
import concurrent.futures
import io
import json
import threading
import urllib.error
import urllib.request

import pytest

import credits
from credits import CACHE_TTL, CreditsManager


def response(balance=42):
    return io.BytesIO(json.dumps({"org": {"balance": balance, "monthlyAllocation": 100}}).encode())


def upstream_error():
    return urllib.error.HTTPError("https://upstream.invalid/api/credits/balance", 401,
                                  "Unauthorized", {}, io.BytesIO(b"expired"))


def test_health_burst_reuses_balance_without_requiring_a_key(relay, monkeypatch):
    original = urllib.request.urlopen
    calls = []

    def open_request(request, **kwargs):
        if request.full_url.startswith("https://upstream.invalid"):
            calls.append(1)
            return response()
        return original(request, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    manager = CreditsManager("https://upstream.invalid", lambda: {})
    monkeypatch.setattr(relay.credits, "get_balance", manager.get_balance)
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: relay.json_any("/health", key=None), range(16)))
    assert all(status == 200 and body["ok"] for status, body in results)
    assert len(calls) == 1


def test_cold_parallel_cache_misses_share_one_fetch(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def open_request(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    manager = CreditsManager("https://upstream.invalid", lambda: {})
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        pending = [pool.submit(manager.get_balance) for _ in range(8)]
        try:
            assert entered.wait(3)
        finally:
            release.set()
        assert [future.result()["available"] for future in pending] == [42] * 8
    assert len(calls) == 1


def test_failed_auth_health_burst_is_cached_as_unhealthy(relay, monkeypatch):
    fetches = []

    class Refresher:
        def call(self, fetch):
            fetches.append(1)
            raise upstream_error()

    manager = CreditsManager("https://upstream.invalid", lambda: {}, Refresher())
    monkeypatch.setattr(relay.credits, "get_balance", manager.get_balance)
    for _ in range(8):
        status, body = relay.json_any("/health", key=None)
        assert status == 503 and body["ok"] is False
        assert "401" in body["error"]
    assert len(fetches) == 1


def test_failure_expiry_retries_and_recovers(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(credits.time, "monotonic", lambda: now[0])
    calls = []

    def open_request(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise upstream_error()
        return response(55)

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    manager = CreditsManager("https://upstream.invalid", lambda: {})
    assert manager.get_balance()["available"] is None
    now[0] += CACHE_TTL - 1
    assert manager.get_balance()["available"] is None
    assert len(calls) == 1
    now[0] += 2
    recovered = manager.get_balance()
    assert recovered["available"] == 55 and recovered["stale"] is False
    assert manager.last_error is None and len(calls) == 2


def test_authenticated_balance_can_explicitly_refresh_inside_cache_ttl(monkeypatch):
    calls = []

    def open_request(*args, **kwargs):
        calls.append(1)
        return response(len(calls))

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    manager = CreditsManager("https://upstream.invalid", lambda: {})
    assert manager.get_balance()["available"] == 1
    assert manager.get_balance()["available"] == 1
    assert manager.get_balance(force_refresh=True)["available"] == 2
    assert manager.get_balance()["available"] == 2
    assert len(calls) == 2


def test_recent_cached_health_stays_stale_after_refresh_failure(monkeypatch):
    calls = []

    def open_request(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise upstream_error()
        return response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    manager = CreditsManager("https://upstream.invalid", lambda: {})
    assert manager.get_balance()["stale"] is False
    assert manager.get_balance(force_refresh=True)["stale"] is True
    assert manager.get_balance()["stale"] is True
    assert len(calls) == 2


@pytest.mark.parametrize("payload", [None, [], {}, {"org": []}, {"org": {}},
    {"org": {"balance": "invalid"}}, {"org": {"balance": True}},
    {"org": {"balance": "NaN"}}, {"org": {"balance": "Infinity"}},
    {"org": {"balance": 42, "monthlyAllocation": "invalid"}},
])
def test_malformed_balance_health_failure_is_cached(relay, monkeypatch, payload):
    calls = []

    def open_request(*args, **kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps(payload).encode())

    manager = CreditsManager("https://upstream.invalid", lambda: {})
    monkeypatch.setattr(relay.credits, "get_balance", manager.get_balance)
    original = urllib.request.urlopen

    def route_request(request, **kwargs):
        if request.full_url.startswith("https://upstream.invalid"):
            return open_request(request, **kwargs)
        return original(request, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", route_request)
    for _ in range(2):
        status, body = relay.json_any("/health", key=None)
        assert status == 503 and body["ok"] is False and body["error"]
    assert len(calls) == 1


def test_empty_timeout_error_does_not_make_cached_health_healthy(relay, monkeypatch):
    calls = []
    original = urllib.request.urlopen

    def open_request(request, **kwargs):
        if not request.full_url.startswith("https://upstream.invalid"):
            return original(request, **kwargs)
        calls.append(1)
        if len(calls) > 1:
            raise TimeoutError()
        return response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    manager = CreditsManager("https://upstream.invalid", lambda: {})
    monkeypatch.setattr(relay.credits, "get_balance", manager.get_balance)
    assert manager.get_balance()["stale"] is False
    failed = manager.get_balance(force_refresh=True)
    assert failed["stale"] is True and failed["stale_error"] == "TimeoutError"
    status, body = relay.json_any("/health", key=None)
    assert status == 503 and body["ok"] is False and body["error"] == "TimeoutError"
    assert len(calls) == 2
