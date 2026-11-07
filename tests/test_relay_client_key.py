"""The manual clients (scripts/relay_client.py, scripts/live_smoke.py) find the key of the relay they talk to.

They read RELAY_API_KEY or the relay_key.txt beside auth.py, nothing else. A relay whose key is relay_api_key in its
config.json, or one started with --config <folder>\\config.json (its key then beside that config), got 'missing relay
API key' or an older relay_key.txt key, answered 401. They now look where config._ensure_relay_key does: RELAY_API_KEY,
the config's relay_api_key, relay_key.txt beside the config, for the config --config names; and never generate a key.
"""
import json
import sys
from pathlib import Path

import pytest

import auth
import config
from scripts import live_smoke, relay_client

PLACEHOLDER = json.loads((Path(__file__).resolve().parents[1] / "config.example.json")
                         .read_text(encoding="utf-8"))["relay_api_key"]


def relay_config(folder, **settings):
    """A config the relay loads, and the key the relay then uses (config.load, which may generate relay_key.txt)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "config.json"
    saved = {"base": "https://tenant.invalid", "page_uuid": "p0", "ua": "ua", "cookie": "accessToken=fake-token",
             "xsrf": "fake-xsrf", "workspace_root": str(folder), "provider_resource_name": "__managed__", **settings}
    path.write_text(json.dumps(saved), encoding="utf-8")
    return path, config.load(str(path))["relay_api_key"]


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """The checkout the clients run from: its auth.py, and a relay_key.txt an older relay left beside it."""
    folder = tmp_path / "checkout"
    folder.mkdir()
    (folder / "relay_key.txt").write_text("relay-stale-key-of-the-checkout\n", encoding="utf-8")
    monkeypatch.setattr(auth, "__file__", str(folder / "auth.py"))
    for name in ("RELAY_API_KEY", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    return folder


def test_the_key_written_in_the_checkouts_config_wins_over_an_older_key_file(checkout):
    _, key = relay_config(checkout, relay_api_key="relay-key-from-the-config")
    assert key == "relay-key-from-the-config"
    auth.verify(auth.client_headers(), None, key)


@pytest.mark.parametrize("settings", [{}, {"relay_api_key": "relay-key-from-the-config"},
                                      {"relay_api_key": PLACEHOLDER}],
                         ids=["generated beside the config", "written in the config", "placeholder of the example"])
def test_a_relay_started_with_another_config_is_reached_with_its_own_key(checkout, tmp_path, settings):
    path, key = relay_config(tmp_path / "elsewhere", **settings)
    assert key != "relay-stale-key-of-the-checkout" and key != PLACEHOLDER
    auth.verify(auth.client_headers(config_path=str(path)), None, key)


def test_relay_api_key_in_the_environment_wins_as_it_does_for_the_relay(checkout, tmp_path, monkeypatch):
    path, _ = relay_config(tmp_path / "elsewhere", relay_api_key="relay-key-from-the-config")
    monkeypatch.setenv("RELAY_API_KEY", "relay-key-from-the-environment")
    assert config.load(str(path))["relay_api_key"] == "relay-key-from-the-environment"
    auth.verify(auth.client_headers(config_path=str(path)), None, "relay-key-from-the-environment")


@pytest.mark.parametrize("saved", [{"relay_api_key": ""}, {"relay_api_key": PLACEHOLDER}, None],
                         ids=["empty key in the config", "placeholder of the example", "no config"])
def test_the_client_never_generates_a_key(checkout, tmp_path, saved):
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    path = folder / "config.json"
    if saved is not None:
        path.write_text(json.dumps(saved), encoding="utf-8")
    (checkout / "relay_key.txt").unlink()
    for config_path in (str(path), None):
        with pytest.raises(ValueError, match="missing relay API key.*--config.*--key"):
            auth.client_headers(config_path=config_path)
    assert sorted(item.name for item in folder.iterdir()) == ([] if saved is None else ["config.json"])
    assert sorted(item.name for item in checkout.iterdir()) == []


def elsewhere(tmp_path, key):
    """The config of a relay started with --config from another folder."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    (folder / "config.json").write_text(json.dumps({"relay_api_key": key}), encoding="utf-8")
    return str(folder / "config.json")


def test_relay_client_reaches_a_relay_through_the_config_it_was_started_with(checkout, tmp_path, relay, monkeypatch,
                                                                             capsys):
    path = elsewhere(tmp_path, relay.key)
    monkeypatch.setattr(sys, "argv", ["relay_client.py", "--models", "--url", relay.url, "--config", path])
    relay_client.main()
    assert "Models:" in capsys.readouterr().out


def test_live_smoke_reaches_a_relay_through_the_config_it_was_started_with(checkout, tmp_path, relay, monkeypatch,
                                                                           capsys):
    path = elsewhere(tmp_path, relay.key)
    for name in ("BASE", "KEY", "CONFIG"):  # main() sets them; restored after the test
        monkeypatch.setattr(live_smoke, name, getattr(live_smoke, name))
    skipped = []
    for name in ("test_models", "test_balance", "test_non_streaming", "test_local_exec_and_context"):
        monkeypatch.setattr(live_smoke, name, lambda name=name: skipped.append(name))
    relay.fake.script = [{"type": "text", "delta": "README relay"}, {"type": "end", "reason": "stop"}]
    live_smoke.main(["--url", relay.url, "--config", path])
    output = capsys.readouterr().out
    assert "PASS: streaming OpenAI" in output and "ALL INTEGRATION TESTS PASSED" in output and len(skipped) == 4
