"""Tests for stream-safety hardening: SSRF guard, ping billing, bounded queues."""
import json
from types import SimpleNamespace

import pytest

import direct
from validate import _assert_public_http_url


def test_ssrf_guard_rejects_loopback_and_link_local():
    for url in ("http://127.0.0.1/x.png", "http://localhost/x.png",
                "http://169.254.169.254/latest/meta-data", "http://[::1]/x.png"):
        with pytest.raises(Exception):
            _assert_public_http_url(url)


def test_ssrf_guard_rejects_non_http_schemes():
    with pytest.raises(Exception):
        _assert_public_http_url("ftp://example.com/x.png")
    with pytest.raises(Exception):
        _assert_public_http_url("file:///etc/passwd")


class _FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


def test_ping_extracts_actual_credits(monkeypatch):
    events = [
        json.dumps({"event": "content_block_start", "type": "text"}),
        json.dumps({"event": "metadata", "usage": {"creditsUsed": 0.0123,
                                                   "promptTokensReadFromCache": 5256}}),
        json.dumps({"event": "end_of_stream"}),
    ]
    monkeypatch.setattr(direct.urllib.request, "urlopen",
                        lambda request, timeout: _FakeResponse([line.encode() + b"\n" for line in events]))
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"base": "http://upstream"}, headers=lambda: {}))
    assert direct._ping(session, {"model": "unknown-model"}) == 0.0123


def test_ping_returns_none_without_usage_event(monkeypatch):
    events = [json.dumps({"event": "content_block_start", "type": "text"}),
              json.dumps({"event": "end_of_stream"})]
    monkeypatch.setattr(direct.urllib.request, "urlopen",
                        lambda request, timeout: _FakeResponse([line.encode() + b"\n" for line in events]))
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"base": "http://upstream"}, headers=lambda: {}))
    assert direct._ping(session, {"model": "unknown-model"}) is None
