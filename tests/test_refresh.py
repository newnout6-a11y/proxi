import asyncio
import io
import urllib.error

import auth
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
