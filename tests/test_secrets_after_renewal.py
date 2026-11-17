"""What the relay masks survives a session renewal and a new sign-in: the web search key stays, a host name stays shown."""
import json

import config
from upstream_refresh import Refresher

KEY = "exa-test-key-0123456789"
HOST = "acme.retool.com"


def loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")
    monkeypatch.setenv("EXA_API_KEY", KEY)
    for name in ("RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "base": "https://acme.retool.com", "page_uuid": "p0", "ua": "ua", "workspace_root": str(tmp_path),
        "cookie": "accessToken=old-token-value; lastSubdomain=" + HOST + "; xsrfToken=old-xsrf",
        "xsrf": "old-xsrf", "provider_resource_name": "__managed__", "web_search_provider": "exa"}), encoding="utf-8")
    return config.load(str(path)), path


def masks(cfg):
    return KEY in cfg["cookie_values"] and HOST not in cfg["cookie_values"]


def test_a_renewal_keeps_the_search_key_masked_and_the_host_shown(tmp_path, monkeypatch):
    cfg, _ = loaded(tmp_path, monkeypatch)
    assert masks(cfg)
    refresher = Refresher(cfg, http_refresh=lambda: ["accessToken=rotated-token-value; Path=/"])
    assert refresher.refresh(force=True)
    assert "rotated-token-value" in cfg["cookie_values"] and masks(cfg)


def test_a_new_sign_in_read_from_disk_keeps_them_too(tmp_path, monkeypatch):
    cfg, path = loaded(tmp_path, monkeypatch)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored.update(cookie="accessToken=signed-in-again; lastSubdomain=" + HOST + "; xsrfToken=new-xsrf", xsrf="new-xsrf")
    path.write_text(json.dumps(stored), encoding="utf-8")
    assert config.reload_auth(cfg) is True
    assert "signed-in-again" in cfg["cookie_values"] and masks(cfg)
