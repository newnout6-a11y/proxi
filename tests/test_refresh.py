import asyncio
import io
import concurrent.futures
import json
import threading
import urllib.error

import auth
import config as config_module
import pytest
import upstream_refresh
from credits import CreditsManager
from session import Backend
from upstream_refresh import Refresher

CFG = {
    "base": "https://x.invalid", "page_uuid": "p", "ua": "ua", "xsrf": "old-xsrf",
    "cookie": "accessToken=old-token; xsrfToken=old-xsrf; keep=1",
    "cookie_values": ["old-token"], "upstream_timeout": 5,
}


def _401():
    return urllib.error.HTTPError("https://x.invalid", 401, "Unauthorized",
                                  {"content-type": "application/json"}, io.BytesIO(b"expired"))


def test_merge_cookie_updates_token_and_xsrf():
    cfg = dict(CFG)
    changed = auth.merge_set_cookie(cfg, ["accessToken=new-token; Path=/", "xsrfToken=new-xsrf; Path=/"])
    assert changed
    assert cfg["xsrf"] == "new-xsrf"
    assert "accessToken=new-token" in cfg["cookie"] and "keep=1" in cfg["cookie"]
    assert "new-token" in cfg["cookie_values"]


def test_merge_cookie_ignores_empty_deletions():
    cfg = dict(CFG)
    assert auth.merge_set_cookie(cfg, ["accessToken=; Path=/; Expires=Thu, 01 Jan 1970"]) is False
    assert "old-token" not in cfg["cookie"]


def test_backend_retries_once_after_successful_refresh():
    cfg = dict(CFG)
    calls = []

    def post(path, payload, timeout):
        calls.append(path)
        if len(calls) == 1:
            raise _401()
        return {"id": "thread-1"}

    refresher = Refresher(cfg, http_refresh=lambda: ["accessToken=fresh; Path=/"])
    backend = Backend(cfg, http_post=post, refresher=refresher)
    assert asyncio.run(backend.create_thread()) == "thread-1"
    assert "accessToken=fresh" in cfg["cookie"]
    assert len(calls) == 2


def test_backend_reports_auth_failure_when_refresh_fails():
    cfg = dict(CFG)
    refresher = Refresher(cfg, http_refresh=lambda: (_ for _ in ()).throw(_401()))
    backend = Backend(cfg, http_post=lambda p, pl, t: (_ for _ in ()).throw(_401()), refresher=refresher)
    try:
        asyncio.run(backend.create_thread())
    except Exception as exc:
        assert getattr(exc, "status", 0) == 502
        assert "session" in str(exc).lower() or "expired" in str(exc).lower()
    else:
        raise AssertionError("expected failure")


def test_refresher_rate_limits_calls():
    cfg = dict(CFG)
    counter = {"n": 0}

    def http():
        counter["n"] += 1
        return ["accessToken=" + str(counter["n"]) + "; Path=/"]

    refresher = Refresher(cfg, http_refresh=http)
    assert refresher.refresh()
    assert refresher.refresh() is False
    assert counter["n"] == 1


def _stored_config(tmp_path):
    cfg = dict(CFG)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    cfg.update(_config_path=str(path), _persist_auth=True,
               _disk_auth=(cfg["cookie"], cfg["xsrf"]))
    return cfg, path


def test_parallel_auth_failures_share_one_renewal():
    cfg = dict(CFG)
    barrier = threading.Barrier(8)
    renewals = []
    refresher = Refresher(cfg, http_refresh=lambda: renewals.append(1) or ["accessToken=fresh; Path=/"])

    def request():
        if "accessToken=old-token" in cfg["cookie"]:
            barrier.wait(timeout=5)
            raise _401()
        return "ok"

    with concurrent.futures.ThreadPoolExecutor(8) as executor:
        assert list(executor.map(lambda _: refresher.call(request), range(8))) == ["ok"] * 8
    assert len(renewals) == 1


def test_auth_replay_is_bounded_even_if_new_session_is_rejected():
    cfg = dict(CFG)
    calls = []
    refresher = Refresher(cfg, http_refresh=lambda: ["accessToken=fresh; Path=/"])

    def request():
        calls.append(1)
        raise _401()

    with pytest.raises(urllib.error.HTTPError):
        refresher.call(request)
    assert len(calls) == 2


def test_non_auth_failure_does_not_renew():
    refresher = Refresher(dict(CFG), http_refresh=lambda: pytest.fail("unexpected renewal"))
    error = urllib.error.HTTPError("https://x.invalid", 429, "limit", {}, None)
    with pytest.raises(urllib.error.HTTPError) as raised:
        refresher.call(lambda: (_ for _ in ()).throw(error))
    assert raised.value.code == 429


def test_refresh_does_not_block_async_turns():
    cfg = dict(CFG)
    heartbeat = threading.Event()

    def renew():
        assert heartbeat.wait(2), "refresh blocked the event loop"
        return ["accessToken=fresh; Path=/"]

    def post(*_):
        if "old-token" in cfg["cookie"]:
            raise _401()
        return {"id": "thread-1"}

    backend = Backend(cfg, http_post=post, refresher=Refresher(cfg, http_refresh=renew))

    async def scenario():
        async def tick():
            await asyncio.sleep(0.01)
            heartbeat.set()
        result, _ = await asyncio.gather(backend.create_thread(), tick())
        return result

    assert asyncio.run(scenario()) == "thread-1"


def test_rotation_survives_restart_and_preserves_other_settings(tmp_path, monkeypatch):
    cfg, path = _stored_config(tmp_path)
    saved = json.loads(path.read_text())
    saved["port"] = 9876
    path.write_text(json.dumps(saved), encoding="utf-8")
    refresher = Refresher(cfg, http_refresh=lambda: ["accessToken=fresh; Path=/", "xsrfToken=fresh-xsrf; Path=/"])
    assert refresher.refresh()
    monkeypatch.delenv("RETOOL_COOKIE", raising=False)
    monkeypatch.delenv("RETOOL_XSRF", raising=False)
    monkeypatch.setenv("RELAY_API_KEY", "test-key")
    loaded = config_module.load(path)
    assert "accessToken=fresh" in loaded["cookie"]
    assert loaded["xsrf"] == "fresh-xsrf"
    assert loaded["port"] == 9876
    assert not list(tmp_path.glob(".session-*.tmp"))


def test_failed_atomic_save_keeps_working_session_and_retries(tmp_path, monkeypatch):
    cfg, path = _stored_config(tmp_path)
    original = path.read_bytes()
    refresher = Refresher(cfg, http_refresh=lambda: ["accessToken=fresh; Path=/"])
    with monkeypatch.context() as patch:
        patch.setattr(config_module.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError()))
        assert refresher.refresh()
    assert path.read_bytes() == original
    assert "accessToken=fresh" in cfg["cookie"]
    assert refresher.persistence_error
    assert not list(tmp_path.glob(".session-*.tmp"))
    # The unchanged old disk credentials must not replace the fresh memory token.
    assert refresher.refresh(force=True)
    assert "accessToken=fresh" in json.loads(path.read_text())["cookie"]
    assert refresher.persistence_error is None


def test_new_local_login_is_adopted_without_restart(tmp_path):
    cfg, path = _stored_config(tmp_path)
    saved = json.loads(path.read_text())
    saved.update(cookie="accessToken=from-browser; xsrfToken=new", xsrf="new")
    path.write_text(json.dumps(saved), encoding="utf-8")
    refresher = Refresher(cfg, http_refresh=lambda: pytest.fail("already have a fresh local login"))

    def request():
        if "from-browser" not in cfg["cookie"]:
            raise _401()
        return cfg["xsrf"]

    assert refresher.call(request) == "new"


def test_new_login_bypasses_failed_renewal_cooldown(tmp_path):
    cfg, path = _stored_config(tmp_path)
    refresher = Refresher(cfg, http_refresh=lambda: (_ for _ in ()).throw(_401()))
    assert not refresher.refresh()
    saved = json.loads(path.read_text())
    saved.update(cookie="accessToken=new-login", xsrf="new")
    path.write_text(json.dumps(saved), encoding="utf-8")
    assert refresher.refresh()
    assert cfg["cookie"] == "accessToken=new-login"


def test_changed_origin_is_not_imported_or_overwritten(tmp_path):
    cfg, path = _stored_config(tmp_path)
    saved = json.loads(path.read_text())
    saved["base"] = "https://another.invalid"
    path.write_text(json.dumps(saved), encoding="utf-8")
    assert config_module.reload_auth(cfg) is False
    with pytest.raises(ValueError):
        config_module.persist_auth(cfg)
    assert json.loads(path.read_text())["base"] == "https://another.invalid"


def test_env_credentials_are_not_written_to_config(tmp_path, monkeypatch):
    _, path = _stored_config(tmp_path)
    original = path.read_bytes()
    monkeypatch.setenv("RETOOL_COOKIE", "accessToken=env-token")
    monkeypatch.setenv("RETOOL_XSRF", "env-xsrf")
    monkeypatch.setenv("RELAY_API_KEY", "test-key")
    cfg = config_module.load(path)
    assert Refresher(cfg, http_refresh=lambda: ["accessToken=rotated; Path=/"]).refresh()
    assert path.read_bytes() == original


def test_idle_keepalive_rotates_and_saves_until_stopped(tmp_path, monkeypatch):
    cfg, path = _stored_config(tmp_path)
    cfg["session_refresh_interval"] = 0.02
    monkeypatch.setattr(upstream_refresh, "MIN_INTERVAL", 0.005)
    twice = threading.Event()
    calls = []

    def renew():
        calls.append(1)
        if len(calls) >= 2:
            twice.set()
        return ["accessToken=token-" + str(len(calls)) + "; Path=/"]

    refresher = Refresher(cfg, http_refresh=renew)
    refresher.start()
    thread = refresher._thread
    refresher.start()
    assert refresher._thread is thread
    try:
        assert twice.wait(3)
    finally:
        refresher.stop()
    assert not thread.is_alive()
    assert json.loads(path.read_text())["cookie"] == cfg["cookie"]
    assert refresher.status()["last_success_at"] is not None


def test_keepalive_can_be_disabled():
    cfg = dict(CFG, session_refresh_interval=0)
    refresher = Refresher(cfg, http_refresh=lambda: pytest.fail("keepalive disabled"))
    refresher.start()
    refresher.stop()
    assert not refresher.status()["keepalive"]


def test_keepalive_recovers_after_transient_failure(monkeypatch):
    cfg = dict(CFG, session_refresh_interval=900)
    monkeypatch.setattr(upstream_refresh, "MIN_INTERVAL", 0.01)
    recovered = threading.Event()
    calls = []

    def renew():
        calls.append(1)
        if len(calls) == 1:
            raise _401()
        recovered.set()
        return ["accessToken=fresh; Path=/"]

    refresher = Refresher(cfg, http_refresh=renew)
    refresher.start()
    try:
        assert recovered.wait(3)
    finally:
        refresher.stop()
    assert len(calls) == 2
    assert "accessToken=fresh" in cfg["cookie"]
    assert refresher.last_error is None


def test_authenticated_without_rotation_is_successful(monkeypatch):
    body = io.BytesIO(b'{"error":false,"message":"Authenticated, but token was not refreshed."}')
    body.headers = type("Headers", (), {"get_all": lambda *_: []})()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kw: body)
    refresher = Refresher(dict(CFG))
    assert refresher.refresh()
    assert refresher.last_error is None


def test_refresh_error_in_200_body_is_not_success(monkeypatch):
    body = io.BytesIO(b'{"error":true,"message":"not authenticated"}')
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kw: body)
    refresher = Refresher(dict(CFG))
    assert not refresher.refresh()
    assert refresher.last_error


def test_balance_recovers_using_the_shared_refresher(monkeypatch):
    cfg = dict(CFG)
    calls = []
    refresher = Refresher(cfg, http_refresh=lambda: ["accessToken=fresh; Path=/"])

    def open_request(request, **_):
        calls.append(request.get_header("Cookie"))
        if "old-token" in calls[-1]:
            raise _401()
        return io.BytesIO(b'{"org":{"balance":42,"monthlyAllocation":100}}')

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    manager = CreditsManager(cfg["base"], lambda: auth.upstream_headers(cfg), refresher)
    balance = manager.get_balance(force_refresh=True)
    assert balance["available"] == 42
    assert balance["stale"] is False
    assert len(calls) == 2


def test_recent_balance_stays_stale_after_failed_refresh(monkeypatch):
    manager = CreditsManager(CFG["base"], lambda: {})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kw:
                        io.BytesIO(b'{"org":{"balance":42}}'))
    assert not manager.get_balance()["stale"]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kw: (_ for _ in ()).throw(_401()))
    assert manager.get_balance(force_refresh=True)["stale"]
    assert manager.get_balance()["stale"]


def test_cookie_merge_accepts_missing_separator_spaces():
    cfg = dict(CFG, cookie="accessToken=old-token;xsrfToken=old-xsrf;keep=1")
    assert auth.merge_set_cookie(cfg, ["accessToken=fresh; Path=/"])
    assert "xsrfToken=old-xsrf" in cfg["cookie"]
    assert "keep=1" in cfg["cookie"]


def test_cookie_merge_repairs_mismatched_xsrf_header():
    cfg = dict(CFG, xsrf="mismatched-header")
    assert auth.merge_set_cookie(cfg, ["xsrfToken=old-xsrf; Path=/"])
    assert cfg["xsrf"] == "old-xsrf"
