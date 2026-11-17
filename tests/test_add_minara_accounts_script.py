"""scripts/add_minara_accounts.py: a dry run by default, one atomic write with --write, never a token on screen.

Tokens are synthetic JWTs and config files live in tmp_path; --check is answered by a fake GET (no api.minara.ai)."""
import json
import time
import types

import pytest

import accounts
import config
from scripts import add_minara_accounts as script
from test_minara_accounts import DAY, jwt

T0 = int(time.time())


def write_config(tmp_path, records, **root):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"relay_api_key": "script-test-key-123", "workspace_root": str(tmp_path),
                                "accounts": records, **root}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def tokens_file(tmp_path, lines, name="tokens.txt"):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def retool():
    return {"base": "https://tenant.retool.com", "page_uuid": "11111111-2222-3333-4444-555555555555",
            "cookie": "accessToken=cookie-value-123456", "xsrf": "xsrf-value-123456", "account_id": "retool-1"}


def minara(label, token):
    return {"label": label, "upstream": "minara", "bearer_token": token}


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the run asked Minara without --check")
    monkeypatch.setattr(accounts, "_minara_network_get", refuse)


def run(tokens, config_path, **options):
    lines = []
    code = script.run(tokens, config_path, out=lines.append, **options)
    return code, lines


def test_a_dry_run_reports_every_token_by_its_line_and_writes_nothing(tmp_path, no_network):
    stored = jwt("user-1", exp=T0 + 3 * DAY)
    config_path = write_config(tmp_path, [retool(), minara("Minara 1", stored), minara("Minara 4", jwt("user-4"))])
    new = [jwt("user-5", exp=T0 + 30 * DAY), jwt("user-6", exp=T0 + 29 * DAY)]
    tokens = tokens_file(tmp_path, ["# Minara accounts, 2026-11-09", "", "Minara 5 (alice):", new[0], stored,
                                    "  " + new[1] + "  ", jwt("user-1", exp=T0 + 30 * DAY), jwt("user-7", exp=T0 - 60),
                                    "eyJ-not-really a token", new[0]])
    before = config_path.read_bytes()
    code, lines = run(tokens, config_path)
    assert code == 0 and config_path.read_bytes() == before
    report = "\n".join(lines)
    assert "3 accounts (2 Minara); 7 tokens" in lines[0]
    assert lines[1].startswith("line 4 ") and "add        Minara 5" in lines[1] and "(30.0 days)" in lines[1]
    assert lines[2].startswith("line 5 ") and "Minara 1 already has this token" in lines[2]
    assert lines[3].startswith("line 6 ") and "Minara 6" in lines[3]
    # a newer token of Minara 1's user renews that account (its stored token expires in 3 days)
    assert lines[4].startswith("line 7 ") and "renew      Minara 1" in lines[4] and "the stored one" in lines[4]
    assert lines[5].startswith("line 8 ") and "expired" in lines[5]
    assert lines[6].startswith("line 9 ") and "invalid" in lines[6]
    assert lines[7].startswith("line 10") and "repeats the token on line 4" in lines[7]
    assert lines[-1] == "Dry run: 2 accounts would be added, 1 renewed. Run again with --write to save them."
    for token in [stored, *new]:
        assert token not in report


def test_write_adds_the_new_accounts_in_one_atomic_write(tmp_path, no_network):
    config_path = write_config(tmp_path, [retool(), minara("Minara 2", jwt("user-2"))], model="claude-opus-5-5")
    new = [jwt("user-3"), jwt("user-4")]
    code, lines = run(tokens_file(tmp_path, new + [jwt("user-2")]), config_path, write=True)
    assert code == 0 and lines[-1] == "Added 2 accounts to %s. Restart the relay to serve them." % config_path
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    added = saved["accounts"][2:]
    assert [(record["label"], record["bearer_token"]) for record in added] == [("Minara 3", new[0]), ("Minara 4", new[1])]
    assert all(len(record["account_id"]) == 32 and record["enabled"] is True for record in added)
    assert saved["accounts"][0] == retool() and saved["model"] == "claude-opus-5-5"
    assert not list(tmp_path.glob(".session-*.tmp"))
    cfg = config.load(str(config_path))
    assert [account.get("label") for account in cfg["accounts"]] == [None, "Minara 2", "Minara 3", "Minara 4"]
    # A second run finds nothing new and leaves the file as it is.
    before = config_path.read_bytes()
    code, lines = run(tokens_file(tmp_path, new), config_path, write=True)
    assert code == 0 and config_path.read_bytes() == before and lines[-1].startswith("Added 0 accounts")
    assert all(token not in "\n".join(lines) for token in new)


def test_check_shows_credits_and_skips_a_token_minara_refuses(tmp_path, monkeypatch):
    config_path = write_config(tmp_path, [minara("Minara 1", jwt("user-1"))])
    good, dead, flaky = jwt("good"), jwt("dead"), jwt("flaky")
    asked = []

    def auth_me(url, **kwargs):
        token = kwargs["headers"]["Authorization"][len("Bearer "):]
        asked.append((url, token))
        if token == dead:
            return types.SimpleNamespace(status_code=401, json=lambda: {})
        if token == flaky:
            raise ConnectionError("down")
        return types.SimpleNamespace(status_code=200, json=lambda: {
            "creditBalanceBreakdown": {"plan": "300", "extra": "140"}})
    monkeypatch.setattr(accounts, "_minara_network_get", auth_me)
    tokens = tokens_file(tmp_path, [good, dead, flaky, jwt("user-1")])
    code, lines = run(tokens, config_path, probe=True)
    assert code == 0
    assert [url for url, _ in asked] == ["https://api.minara.ai/auth/me"] * 3  # never for a duplicate
    assert "credits 440.00 ($2.00)" in lines[1] and "Minara 2" in lines[1]
    assert lines[2].startswith("line 2 ") and "dead" in lines[2] and "401" in lines[2]
    assert "balance unknown: ConnectionError" in lines[3]
    assert lines[-1].startswith("Dry run: 2 accounts would be added")
    asked.clear()
    code, lines = run(tokens, config_path, write=True, probe=True)
    saved = json.loads(config_path.read_text(encoding="utf-8"))["accounts"]
    assert [record["bearer_token"] for record in saved[1:]] == [good, flaky]
    assert [record["label"] for record in saved[1:]] == ["Minara 2", "Minara 3"]
    assert [line.split()[1] for line in lines[1:-1]] == ["1", "2", "3", "4"]
    for token in (good, dead, flaky):
        assert token not in "\n".join(lines)


def test_main_never_prints_a_token_and_says_what_stops_it(tmp_path, capsys, no_network):
    token = jwt("user-1")
    config_path = write_config(tmp_path, [minara("Minara 1", jwt("other"))])
    tokens = tokens_file(tmp_path, ["Minara 2", token])
    assert script.main(["--tokens-file", str(tokens), "--config", str(config_path)]) == 0
    assert script.main(["--tokens-file", str(tokens), "--config", str(config_path), "--write"]) == 0
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err and "Added 1 account to" in captured.out
    assert json.loads(config_path.read_text(encoding="utf-8"))["accounts"][1]["bearer_token"] == token

    single = tmp_path / "single.json"
    single.write_text(json.dumps({"upstream": "minara", "bearer_token": jwt("root")}), encoding="utf-8")
    assert script.main(["--tokens-file", str(tokens), "--config", str(single), "--write"]) == 2
    assert "has no accounts list" in capsys.readouterr().err
    assert script.main(["--tokens-file", str(tmp_path / "missing.txt"), "--config", str(config_path)]) == 2
    assert "cannot read the tokens file" in capsys.readouterr().err
    empty = tokens_file(tmp_path, ["no tokens here"], name="empty.txt")
    assert script.main(["--tokens-file", str(empty), "--config", str(config_path)]) == 2
    assert "no token in" in capsys.readouterr().err
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert script.main(["--tokens-file", str(tokens), "--config", str(broken)]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_write_plans_again_on_what_the_file_holds_under_the_lock(tmp_path, monkeypatch, no_network):
    """The relay (or the admin form) may add an account between the dry read and the write."""
    config_path = write_config(tmp_path, [minara("Minara 1", jwt("user-1"))])
    tokens = tokens_file(tmp_path, [jwt("user-2"), jwt("user-3")])
    real_lock = config.storage_lock

    def lock_after_another_writer(path, *args, **kwargs):
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        saved["accounts"].append(minara("Minara 9", jwt("user-2")))
        config_path.write_text(json.dumps(saved), encoding="utf-8")
        return real_lock(path, *args, **kwargs)
    monkeypatch.setattr(config, "storage_lock", lock_after_another_writer)
    code, lines = run(tokens, config_path, write=True)
    saved = json.loads(config_path.read_text(encoding="utf-8"))["accounts"]
    assert [record["label"] for record in saved] == ["Minara 1", "Minara 9", "Minara 10"]
    assert "Minara 9 belongs to the same Minara user" in lines[1] or "Minara 9 already has this token" in lines[1]
    assert lines[-1].startswith("Added 1 account to")


def test_write_renews_an_account_with_a_newer_token_of_its_user(tmp_path, no_network):
    stored = jwt("user-1", exp=T0 + 2 * DAY)
    fresh = jwt("user-1", exp=T0 + 30 * DAY)
    config_path = write_config(tmp_path, [minara("Minara 1", stored)])
    code, lines = run(tokens_file(tmp_path, [fresh]), config_path, write=True)
    saved = json.loads(config_path.read_text(encoding="utf-8"))["accounts"]
    assert code == 0 and [record["label"] for record in saved] == ["Minara 1"] and saved[0]["bearer_token"] == fresh
    assert lines[-1].startswith("Added 0 accounts and renewed 1 to") and all(fresh not in line for line in lines)
