"""Review fixes for PR #18: renewal resilience, title cache, keepwarm reporting, bounded errors."""
import asyncio
import io
import json
import threading
import time
from types import SimpleNamespace
import urllib.error

import pytest

import apierr
import client_context
import console_log
import credits
import direct
import journal
import upstream_refresh
from upstream_refresh import Refresher

SESSION = "ac247765-c122-4cca-ba05-ef006343ee93"


def _refresh_cfg(tmp_path, **extra):
    cfg = {"base": "https://example.invalid", "page_uuid": "p1", "ua": "t",
           "cookie": "accessToken=old; xsrfToken=old-xsrf", "xsrf": "old-xsrf",
           "cookie_values": ["old"], "relay_api_key": "k", "upstream_timeout": 10,
           "_config_path": str(tmp_path / "config.json"), "_persist_auth": False}
    cfg.update(extra)
    return cfg


def test_ambiguous_in_memory_accounts_do_not_break_renewal(tmp_path):
    twin = {"base": "https://example.invalid", "page_uuid": "p1", "cookie": "c", "xsrf": "x"}
    cfg = _refresh_cfg(tmp_path, accounts=[dict(twin), dict(twin)])
    refresher = Refresher(cfg, http_refresh=lambda: ["accessToken=fresh; Path=/"])
    assert refresher.refresh() is True
    assert "fresh" in cfg["cookie"]


def test_keepalive_survives_an_unexpected_renewal_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream_refresh, "MIN_INTERVAL", 0.01)
    refresher = Refresher(_refresh_cfg(tmp_path, session_refresh_interval=60))
    calls, recovered = [], threading.Event()

    def flaky(force=False, **_kw):
        calls.append(force)
        if len(calls) == 1:
            raise RuntimeError("synthetic")
        recovered.set()
        return True

    monkeypatch.setattr(refresher, "refresh", flaky)
    refresher.start()
    try:
        assert recovered.wait(3), "keepalive thread died after the first exception"
    finally:
        refresher.stop()


def test_missing_local_title_is_cached_instead_of_rescanned(tmp_path, monkeypatch):
    client_context._CACHE.clear()
    try:
        scans = []
        monkeypatch.setattr(client_context, "_desktop_title", lambda *_a: {})
        monkeypatch.setattr(client_context, "_claude_title", lambda *_a: scans.append(1) or {})
        for _ in range(3):
            assert client_context._local_title("claude-code", SESSION, tmp_path) == {}
        assert len(scans) == 1
    finally:
        client_context._CACHE.clear()


def test_keepwarm_reporting_failure_keeps_result_and_debit(monkeypatch):
    manager = credits.CreditsManager("https://upstream.invalid", lambda: {})
    manager._balance = {"available": 100.0, "monthly_allocation": 500.0}
    manager._cached_at = time.monotonic()
    backend = SimpleNamespace(cfg={"base": "https://upstream.invalid"}, headers=lambda: {})
    target = SimpleNamespace(backend=backend, thread_id="t", _credits_manager=manager,
                             _cancelled=False, _request_tag="owner")
    stream = io.BytesIO(b"\n".join(json.dumps(e).encode() for e in (
        {"event": "metadata", "usage": {"creditsUsed": 0.1}}, {"event": "end_of_stream"})) + b"\n")
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_kw: stream)

    def broken(_line):
        raise RuntimeError("console unavailable")

    monkeypatch.setattr(console_log, "write", broken)
    assert direct._ping(target, {"model": "claude-opus-5-5"}) == 0.1
    assert manager.session_used == 0.1


def test_long_upstream_rejection_is_kept_on_disk_but_bounded_elsewhere(tmp_path, monkeypatch):
    detail = json.dumps({"message": "x" * 12000}).encode()
    cfg = {"upstream_error_dir": str(tmp_path), "turn_timeout": 2}
    body = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}
    session = SimpleNamespace(backend=SimpleNamespace(headers=lambda: {}, cfg={"base": "https://example.invalid"}),
                              _cancelled=False, _request_tag="req-long")

    def reject(*_a, **_kw):
        raise urllib.error.HTTPError("https://example.invalid", 400, "bad", {}, io.BytesIO(detail))

    monkeypatch.setattr(direct.urllib.request, "urlopen", reject)

    async def run():
        with pytest.raises(apierr.ApiError) as caught:
            async for _ in direct._stream_body(cfg, session, body):
                pass
        return caught.value

    error = asyncio.run(run())
    assert len(error.message) <= 300
    saved = (tmp_path / (error.diagnostic_id + "-error.txt")).read_text(encoding="utf-8")
    assert len(saved) >= 12000
    event = next(e for e in reversed(journal.JOURNAL.query()) if e["kind"] == "upstream.rejected")
    assert len(event["message"]) < 1200


def test_local_validation_error_prints_no_balance_line(relay, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(relay.credits, "balance_snapshot", lambda: {"available": 100.0}, raising=False)
    before = journal.JOURNAL.last_seq
    status, body = relay.json_any("/v1/messages", {"model": "no-such-model", "max_tokens": 8,
                                                   "messages": [{"role": "user", "content": "hi"}]})
    assert status >= 400 and not relay.fake.seen
    request_id = body["request_id"]
    deadline = time.monotonic() + 2
    while not any(e["kind"] == "http.request" and e.get("request_id") == request_id
                  for e in journal.JOURNAL.query(since=before)):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert "[balance]" not in capsys.readouterr().out
