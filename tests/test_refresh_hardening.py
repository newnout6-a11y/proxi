"""Refresh/auth hardening: tolerant body parsing, deletion sync, journal level, caps."""
import threading

import auth
import upstream_refresh
from auth import merge_set_cookie
from upstream_refresh import Refresher


def _cfg(tmp_path):
    return {"base": "https://example.invalid", "page_uuid": "p1", "ua": "t",
            "cookie": "accessToken=old; xsrfToken=old-xsrf; keep=1", "xsrf": "old-xsrf",
            "cookie_values": ["old"], "relay_api_key": "k", "upstream_timeout": 10,
            "_config_path": str(tmp_path / "config.json"), "_persist_auth": False}


class _Resp:
    def __init__(self, body=b"", cookies=()):
        self._body = body
        self._cookies = list(cookies)

    def read(self):
        return self._body

    @property
    def headers(self):
        class H:
            def __init__(self, cookies):
                self._cookies = cookies

            def get_all(self, name):
                return self._cookies or None
        return H(self._cookies)


class _Resp:
    def __init__(self, body=b"", cookies=()):
        self._body = body
        self._cookies = list(cookies)

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def headers(self):
        return _Headers(self._cookies)


class _Headers:
    def __init__(self, cookies):
        self._cookies = cookies

    def get_all(self, name):
        return self._cookies or None


def test_refresh_tolerates_empty_body(monkeypatch, tmp_path):
    """PROTOCOL.md: empty body is a successful renewal — its Set-Cookie headers
    must survive (previously a strict json.loads discarded them and failed the
    whole refresh)."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(upstream_refresh.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(
                            b"", cookies=["accessToken=fresh; Path=/", "xsrfToken=fresh-xsrf; Path=/"]))
    refresher = Refresher(cfg)
    assert refresher.refresh(rejected_cookie="accessToken=old; xsrfToken=old-xsrf; keep=1") is True
    assert "accessToken=fresh" in cfg["cookie"]
    assert cfg["xsrf"] == "fresh-xsrf"


def test_refresh_tolerates_non_json_body(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(upstream_refresh.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(b"ok", cookies=["accessToken=fresh; Path=/"]))
    refresher = Refresher(cfg)
    assert refresher.refresh(rejected_cookie="accessToken=old; xsrfToken=old-xsrf; keep=1") is True
    assert "accessToken=fresh" in cfg["cookie"]


def test_deletion_cookie_clears_stale_xsrf():
    cfg = {"cookie": "accessToken=a; xsrfToken=stale", "xsrf": "stale", "cookie_values": []}
    merge_set_cookie(cfg, ["xsrfToken="])
    assert cfg["xsrf"] == "", "a deleted xsrf cookie must not keep a stale header"


def test_journal_marks_persist_failure_as_warn(monkeypatch, tmp_path):
    import journal
    cfg = _cfg(tmp_path)
    refresher = Refresher(cfg, http_refresh=lambda: ["accessToken=fresh; Path=/"])
    monkeypatch.setattr(upstream_refresh, "persist_auth",
                        lambda _cfg: (_ for _ in ()).throw(OSError("disk full")))
    ok = refresher.refresh(rejected_cookie="accessToken=old; xsrfToken=old-xsrf; keep=1")
    assert ok is True and refresher.persistence_error
    events = [e for e in journal.JOURNAL.query()
              if e["kind"] == "auth.refresh" and e["data"].get("persistence_error")]
    assert events, "persist failure must surface in the journal"
    assert any(e["level"] == "warn" for e in events), "persist failure must not be logged as success"


def test_auth_failure_table_is_bounded():
    auth._FAILURES.clear()
    try:
        for i in range(5000):
            auth.register_auth_failure(f"10.0.0.{i % 256}:{i}")
        assert len(auth._FAILURES) <= 4096, "flood of fresh IPs must be hard-capped"
    finally:
        auth._FAILURES.clear()


def test_refresh_events_carry_account_tag(monkeypatch, tmp_path):
    """With multiple accounts each auth.refresh event must say which tenant
    renewed/failed — otherwise a 401 on one account is indistinguishable from
    the other's."""
    import journal
    journal.JOURNAL.reset()
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(upstream_refresh.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(b"", cookies=["accessToken=fresh; Path=/"]))
    refresher = Refresher(cfg)
    assert refresher.refresh(rejected_cookie="accessToken=old; xsrfToken=old-xsrf; keep=1") is True
    events = [e for e in journal.JOURNAL.query() if e["kind"] == "auth.refresh"]
    assert events, "refresh must be journaled"
    for e in events:
        assert e["data"].get("account"), "auth.refresh must carry the account tag"
        assert "aiprovladimir634.retool.com" in e["data"].get("tenant", ""), \
            "tenant must be visible for multi-account diagnosis"
