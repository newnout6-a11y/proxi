"""What a restart must not change: the id of each account, and the startup events of the prompts layer in the journal."""
import json
import os
import shutil
import stat
from pathlib import Path

import config
import journal
import proxy

ROOT = Path(__file__).resolve().parents[1]
BASE = {"ua": "ua", "provider_resource_name": "__managed__"}


def clean_env(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")
    for name in ("RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)


def write(path, saved):
    path.write_text(json.dumps(saved), encoding="utf-8")


def test_an_account_id_that_cannot_be_saved_is_the_same_at_every_start(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    path = tmp_path / "config.json"
    write(path, {**BASE, "base": "https://tenant.invalid", "page_uuid": "p0", "workspace_root": str(tmp_path)})
    # the session comes from the environment, so config.json is never written (README's way to keep secrets out of it)
    monkeypatch.setenv("RETOOL_COOKIE", "accessToken=fake-token")
    monkeypatch.setenv("RETOOL_XSRF", "fake-xsrf")
    first, second = config.load(str(path)), config.load(str(path))
    assert first["account_id"] == second["account_id"]
    assert "account_id" not in json.loads(path.read_text(encoding="utf-8"))


def test_a_read_only_pool_keeps_its_ids_across_starts_and_a_writable_start_saves_the_same(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    path = tmp_path / "config.json"
    accounts = [{"base": "https://a.invalid", "page_uuid": "p1", "cookie": "accessToken=one-token", "xsrf": "x1"},
                {"base": "https://b.invalid", "page_uuid": "p2", "cookie": "accessToken=two-token", "xsrf": "x2"},
                {"base": "https://b.invalid", "page_uuid": "p2", "cookie": "accessToken=three-token", "xsrf": "x3",
                 "account_id": "kept-as-it-is"}]
    write(path, {**BASE, "workspace_root": str(tmp_path), "accounts": accounts})
    os.chmod(path, stat.S_IREAD)
    try:
        ids = [[item.get("account_id") for item in config.load(str(path))["accounts"]] for _ in range(2)]
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    first = [item["account_id"] for item in json.loads(path.read_text(encoding="utf-8"))["accounts"] if "account_id" in item]
    assert first == ["kept-as-it-is"]  # nothing was saved while the file was read-only
    saved = config.load(str(path))
    stored = [item["account_id"] for item in json.loads(path.read_text(encoding="utf-8"))["accounts"]]
    assert stored == [item["account_id"] for item in saved["accounts"]]
    # the ids in memory of the read-only starts are the ones a writable start saves; a saved id is never replaced
    loaded = [[item["account_id"] for item in config.load(str(path))["accounts"]]]
    assert len(set(stored)) == 3 and stored[2] == "kept-as-it-is" and loaded[0] == stored
    assert all(found is None or found == stored[index] for found_ids in ids for index, found in enumerate(found_ids))


def test_the_startup_events_of_the_prompts_layer_reach_the_journal_file(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    shutil.copyfile(ROOT / "prompts.example.json", tmp_path / "prompts.json")
    cfg = dict(config.DEFAULTS)
    cfg.update({"base": "https://example.invalid", "page_uuid": "page-1", "thread_id": "thread-1", "ua": "test-agent",
                "cookie": "accessToken=abc123456789xyz; xsrfToken=q", "xsrf": "q", "relay_api_key": "k",
                "workspace_root": str(tmp_path), "host": "127.0.0.1", "port": 0, "cookie_values": ["abc123456789xyz"],
                "prompts_file": str(tmp_path / "prompts.json"), "journal_path": str(tmp_path / "journal.jsonl"),
                "conversation_state_dir": str(tmp_path / "conversations"), "command_log": str(tmp_path / "commands.log")})
    journal.JOURNAL.reset()
    runtime = proxy.Runtime(cfg)
    try:
        kinds = [json.loads(line)["kind"] for line in (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
        assert "prompt.reload" in kinds or "prompt.config_error" in kinds
    finally:
        runtime.close()
        journal.JOURNAL.configure(None, None, load=False)


def env_base_config(tmp_path, monkeypatch, file_base):
    clean_env(monkeypatch)
    monkeypatch.setenv("RETOOL_BASE", "https://tenant.invalid")
    path = tmp_path / "config.json"
    saved = {**BASE, "page_uuid": "p0", "cookie": "accessToken=old-token", "xsrf": "old-xsrf",
             "workspace_root": str(tmp_path)}
    if file_base is not None:
        saved["base"] = file_base
    write(path, saved)
    return config.load(str(path)), path


def test_with_retool_base_in_the_environment_a_rotated_cookie_is_saved(tmp_path, monkeypatch):
    for file_base in ("https://<your-subdomain>.retool.com", None):
        cfg, path = env_base_config(tmp_path, monkeypatch, file_base)
        assert cfg["base"] == "https://tenant.invalid"
        cfg.update(cookie="accessToken=rotated-token", xsrf="new-xsrf")
        assert config.persist_auth(cfg) is True
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["cookie"] == "accessToken=rotated-token" and stored.get("base") == file_base


def test_with_retool_base_in_the_environment_a_new_sign_in_is_adopted(tmp_path, monkeypatch):
    cfg, path = env_base_config(tmp_path, monkeypatch, "https://<your-subdomain>.retool.com")
    stored = json.loads(path.read_text(encoding="utf-8"))
    write(path, {**stored, "cookie": "accessToken=signed-in-again", "xsrf": "fresh-xsrf"})
    assert config.reload_auth(cfg) is True and cfg["cookie"] == "accessToken=signed-in-again"


def test_with_retool_base_a_file_edited_to_another_tenant_is_still_refused(tmp_path, monkeypatch):
    cfg, path = env_base_config(tmp_path, monkeypatch, "https://first.invalid")
    stored = json.loads(path.read_text(encoding="utf-8"))
    write(path, {**stored, "base": "https://another.invalid", "cookie": "accessToken=foreign"})
    assert config.reload_auth(cfg) is False
    cfg.update(cookie="accessToken=rotated-token")
    try:
        config.persist_auth(cfg)
    except ValueError:
        pass
    else:
        raise AssertionError("a session was written into another tenant's record")
    assert json.loads(path.read_text(encoding="utf-8"))["cookie"] == "accessToken=foreign"
