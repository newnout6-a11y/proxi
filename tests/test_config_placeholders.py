"""A config copied from config.example.json must not run with a value anyone can read in the repository."""
import json

import config


def load_with(tmp_path, monkeypatch, **settings):
    path = tmp_path / "config.json"
    saved = {"base": "https://tenant.invalid", "page_uuid": "p0", "ua": "ua", "cookie": "accessToken=fake-token",
             "xsrf": "fake-xsrf", "workspace_root": str(tmp_path), "provider_resource_name": "__managed__", **settings}
    path.write_text(json.dumps(saved), encoding="utf-8")
    for name in ("RELAY_API_KEY", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    return config.load(str(path))


def test_the_example_relay_key_placeholder_is_not_the_relay_key(tmp_path, monkeypatch):
    example = json.loads((__import__("pathlib").Path(__file__).resolve().parents[1] / "config.example.json")
                         .read_text(encoding="utf-8"))["relay_api_key"]
    cfg = load_with(tmp_path, monkeypatch, relay_api_key=example)
    assert cfg["relay_api_key"] != example and cfg["relay_api_key"].startswith("relay-")
    assert (tmp_path / "relay_key.txt").read_text(encoding="utf-8").strip() == cfg["relay_api_key"]
    assert any("placeholder" in text for text in cfg["config_warnings"])


def test_a_real_relay_key_in_the_config_is_kept(tmp_path, monkeypatch):
    cfg = load_with(tmp_path, monkeypatch, relay_api_key="my-own-relay-key-123")
    assert cfg["relay_api_key"] == "my-own-relay-key-123"
    assert not any("placeholder" in text for text in cfg["config_warnings"])
