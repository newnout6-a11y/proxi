"""After each catalog look the relay writes its models into OpenCode's settings file by itself (client_sync): a model it
published, or effort levels it took from a provider, reach OpenCode without anyone running the sync script."""
import json
import subprocess

import client_sync
import journal
import minara_refresh
import retool_refresh

RELAY = {"base": "https://example.invalid", "provider_resource_name": "anthropic-resource",
         "openai_provider_resource_name": "openai-resource", "retool_fast_provider_resource_name": "fast-resource"}


def opencode_file(tmp_path):
    path = tmp_path / "opencode" / "opencode.jsonc"
    path.parent.mkdir()
    path.write_text(json.dumps({"provider": {"retool-relay": {
        "options": {"baseURL": "http://localhost:8787/v1", "apiKey": "relay-private"}, "models": {}}}}, indent=2) + "\n",
        encoding="utf-8")
    return path


def relay_files(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(RELAY), encoding="utf-8")
    return config, opencode_file(tmp_path)


def test_a_look_writes_the_relay_models_into_opencode_and_a_current_list_is_left_alone(tmp_path):
    config, settings = relay_files(tmp_path)
    sync = client_sync.OpenCodeSync({"_config_path": str(config), "opencode_settings": str(settings)})
    before = journal.JOURNAL.last_seq
    summary = sync("minara")
    assert summary["written"] is True and summary["changed"] is True
    written = json.loads(settings.read_text(encoding="utf-8"))["provider"]["retool-relay"]
    assert "claude-opus-5-5" in written["models"] and written["options"]["apiKey"] == "relay-private"
    backups = list((settings.parent / client_sync.BACKUPS).iterdir())
    assert len(backups) == 1 and json.loads(backups[0].read_text(encoding="utf-8"))["provider"]["retool-relay"]["models"] == {}
    said = [event for event in journal.JOURNAL.query(since=before, limit=50) if event["kind"] == "client.opencode_synced"]
    assert len(said) == 1 and said[0]["data"]["reason"] == "minara" and "relay-private" not in json.dumps(said)
    # The next look finds the list current: nothing written, no second backup, nothing said.
    again = sync("retool")
    assert again["written"] is False and again["changed"] is False
    assert len(list((settings.parent / client_sync.BACKUPS).iterdir())) == 1
    assert [event for event in journal.JOURNAL.query(since=before, limit=50)
            if event["kind"] == "client.opencode_synced"] == said


def test_nothing_runs_without_an_opencode_file_or_when_switched_off(tmp_path):
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        raise AssertionError("not expected to run")
    config, settings = relay_files(tmp_path)
    # conftest points OpenCode's own folder at an empty one: no file there, nothing to update.
    assert client_sync.OpenCodeSync({"_config_path": str(config)}, runner=runner)("minara") is None
    assert client_sync.OpenCodeSync({"_config_path": str(config), "opencode_settings": str(settings),
                                     "opencode_sync": False}, runner=runner)("minara") is None
    assert client_sync.OpenCodeSync({"_config_path": str(config), "opencode_settings": str(tmp_path / "missing.json")},
                                    runner=runner)("minara") is None
    assert calls == []


def test_a_failed_sync_is_a_warning_and_the_file_stays(tmp_path):
    config, settings = relay_files(tmp_path)
    original = settings.read_bytes()
    seen = {}

    def runner(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 1, "", "error: Expected an existing retool-relay provider\n")
    sync = client_sync.OpenCodeSync({"_config_path": str(config), "opencode_settings": str(settings)}, runner=runner)
    before = journal.JOURNAL.last_seq
    summary = sync("retool")
    assert summary == {"error": "error: Expected an existing retool-relay provider"} and settings.read_bytes() == original
    command = seen["command"]
    assert command[command.index("--settings") + 1] == str(settings) and "--write" in command
    assert command[command.index("--backup-dir") + 1] == str(settings.parent / client_sync.BACKUPS)
    warned = [event for event in journal.JOURNAL.query(since=before, limit=50)
              if event["kind"] == "client.opencode_sync_failed"]
    assert len(warned) == 1 and warned[0]["level"] == "warn"


def test_every_look_of_both_catalogs_ends_with_the_client_lists(tmp_path):
    called = []
    minara = minara_refresh.CatalogRefresher({"_minara_catalog_file": str(tmp_path / "minara.auto.json")}, backends=[])
    retool = retool_refresh.CatalogRefresher({"_retool_catalog_file": str(tmp_path / "retool.auto.json")}, backends=[])
    minara.after_look = retool.after_look = called.append
    minara.look()  # no Minara account: the look says so, the client lists are still brought up to date
    retool.look()
    assert called == ["minara", "retool"]
