"""Renewing a session rewrites config.json only when the stored values really change."""
import json

import pytest

import config
import journal
from session import Backend, SessionPool
from test_account_isolation import stored_pool
from upstream_refresh import Refresher


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RELAY_API_KEY", "synthetic-key")


@pytest.fixture
def writes(monkeypatch):
    calls = []
    real = config._write_file

    def counted(path, saved):
        calls.append(path)
        return real(path, saved)

    monkeypatch.setattr(config, "_write_file", counted)
    return calls


def renewals(cfg, since):
    return [e["data"] for e in journal.JOURNAL.query(since=since) if e["kind"] == "auth.refresh"
            and e["data"].get("account") == journal.tag(cfg["account_id"])]


def test_persisting_the_values_already_on_disk_leaves_the_file_untouched(tmp_path, writes):
    pool, path = stored_pool(tmp_path)
    cfg = pool.backends[1].cfg
    writes.clear()
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    assert config.persist_auth(cfg) is False
    assert writes == [] and path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert cfg["_disk_auth"] == (cfg["cookie"], cfg["xsrf"])


def test_a_rotation_is_written_once_and_repeating_it_is_free(tmp_path, writes):
    pool, path = stored_pool(tmp_path)
    cfg = pool.backends[1].cfg
    writes.clear()
    cfg.update(cookie="accessToken=rotated; xsrfToken=rotated-xsrf", xsrf="rotated-xsrf")
    assert config.persist_auth(cfg) is True
    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert saved[1]["cookie"] == cfg["cookie"] and saved[1]["xsrf"] == "rotated-xsrf"
    assert "synthetic-A" in saved[0]["cookie"]
    assert config.persist_auth(cfg) is False
    assert len(writes) == 1


def test_a_lost_account_id_is_written_back_even_when_nothing_rotated(tmp_path, writes):
    pool, path = stored_pool(tmp_path, same_page=False)
    cfg = pool.backends[1].cfg
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["accounts"][1].pop("account_id")
    path.write_text(json.dumps(saved), encoding="utf-8")
    writes.clear()
    assert config.persist_auth(cfg) is True
    assert json.loads(path.read_text(encoding="utf-8"))["accounts"][1]["account_id"] == "account-b"
    assert config.persist_auth(cfg) is False
    assert len(writes) == 1


def test_credentials_from_the_environment_are_never_persisted(tmp_path, writes):
    pool, path = stored_pool(tmp_path)
    cfg = dict(pool.backends[1].cfg, _persist_auth=False, cookie="accessToken=env", xsrf="env")
    before = path.read_bytes()
    assert config.persist_auth(cfg) is False
    assert path.read_bytes() == before


@pytest.mark.parametrize("headers", [[], ["accessToken=synthetic-B; Path=/"],
                                     ["accessToken=synthetic-B; Path=/", "xsrfToken=xsrf-B; Path=/"]])
def test_renewals_that_rotate_nothing_never_rewrite_config_json(tmp_path, writes, headers):
    pool, path = stored_pool(tmp_path)
    cfg = pool.backends[1].cfg
    writes.clear()
    before, mark = path.read_bytes(), journal.JOURNAL.last_seq
    refresher = Refresher(cfg, http_refresh=lambda: list(headers))
    assert all(refresher.refresh(force=True) for _ in range(3))
    assert writes == [] and path.read_bytes() == before
    events = renewals(cfg, mark)
    assert len(events) == 3
    assert all(e["persisted"] is False and e["rotated"] is False and not e["persistence_error"] for e in events)


def test_a_rotation_is_saved_once_and_unchanged_renewals_after_it_are_not(tmp_path, writes):
    pool, path = stored_pool(tmp_path)
    cfg = pool.backends[1].cfg
    writes.clear()
    mark = journal.JOURNAL.last_seq
    answers = iter([["accessToken=B-new; Path=/", "xsrfToken=xsrf-B-new; Path=/"],
                    ["accessToken=B-new; Path=/"], []])
    refresher = Refresher(cfg, http_refresh=lambda: next(answers))
    assert all(refresher.refresh(force=True) for _ in range(3))
    assert len(writes) == 1
    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"][1]
    assert "B-new" in saved["cookie"] and saved["xsrf"] == "xsrf-B-new"
    events = renewals(cfg, mark)
    assert [(e["rotated"], e["persisted"]) for e in events] == [(True, True), (False, False), (False, False)]


def test_a_failed_save_is_retried_by_the_next_renewal_even_without_rotation(tmp_path, writes, monkeypatch):
    pool, path = stored_pool(tmp_path)
    cfg = pool.backends[1].cfg
    writes.clear()
    refresher = Refresher(cfg, http_refresh=lambda: ["accessToken=B-new; Path=/"])
    with monkeypatch.context() as patch:
        patch.setattr(config.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError()))
        assert refresher.refresh(force=True)
    assert refresher.persistence_error and "B-new" not in path.read_text(encoding="utf-8")
    mark = journal.JOURNAL.last_seq
    assert refresher.refresh(force=True)  # the same answer again: nothing rotated, but the disk is behind
    assert refresher.persistence_error is None
    assert "B-new" in path.read_text(encoding="utf-8")
    assert [(e["rotated"], e["persisted"]) for e in renewals(cfg, mark)] == [(False, True)]


def test_five_accounts_renewing_all_day_write_only_their_rotations(tmp_path, writes):
    accounts = [{"account_id": "acc-%d" % i, "base": "https://t%d.invalid" % i, "page_uuid": "page-%d" % i,
                 "cookie": "accessToken=tok-%d; xsrfToken=x-%d" % (i, i), "xsrf": "x-%d" % i} for i in range(5)]
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"accounts": accounts, "ua": "test", "workspace_root": str(tmp_path)}),
                    encoding="utf-8")
    pool = SessionPool(Backend(config.load(path)))
    writes.clear()
    for index, backend in enumerate(pool.backends):
        answers = iter([["accessToken=new-%d; Path=/" % index]] + [[]] * 3)
        refresher = Refresher(backend.cfg, http_refresh=lambda answers=answers: next(answers))
        assert all(refresher.refresh(force=True) for _ in range(4))
    assert len(writes) == 5, "20 renewals, but only the 5 rotations may touch the file"
    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert [item["cookie"] for item in saved] == ["accessToken=new-%d; xsrfToken=x-%d" % (i, i) for i in range(5)]


# -- a sign-in adopted from the file settles an earlier failure to save ----------------------------------------
def sign_in_again(path, account_id, cookie, xsrf):
    saved = json.loads(path.read_text(encoding="utf-8"))
    for record in saved["accounts"]:
        if record["account_id"] == account_id:
            record["cookie"], record["xsrf"] = cookie, xsrf
    path.write_text(json.dumps(saved), encoding="utf-8")


def test_adopting_a_newer_sign_in_from_the_file_clears_the_save_error_it_replaces(tmp_path):
    pool, path = stored_pool(tmp_path)
    cfg = pool.backends[1].cfg

    def renew_while_the_operator_signs_in():
        sign_in_again(path, cfg["account_id"], "accessToken=B-ext; xsrfToken=xsrf-ext", "xsrf-ext")
        return ["accessToken=B-new; Path=/"]

    refresher = Refresher(cfg, http_refresh=renew_while_the_operator_signs_in)
    assert refresher.refresh(force=True)
    assert refresher.persistence_error and "B-ext" in path.read_text(encoding="utf-8")  # the save was refused
    assert refresher.status()["persistence_error"]
    refresher._http_refresh = lambda: []
    assert refresher.refresh(force=True)  # adopts the new sign-in: file and memory agree again
    assert "B-ext" in cfg["cookie"]
    assert refresher.persistence_error is None and refresher.status()["persistence_error"] is None


def test_credentials_another_request_renewed_in_memory_keep_a_genuine_save_error(tmp_path):
    pool, path = stored_pool(tmp_path)
    cfg = pool.backends[1].cfg
    refresher = Refresher(cfg, http_refresh=lambda: [])
    refresher.persistence_error = "cannot save renewed session: PermissionError"
    rejected = cfg["cookie"]
    cfg.update(cookie="accessToken=B-renewed-elsewhere; xsrfToken=x", xsrf="x")
    assert refresher.refresh(rejected_cookie=rejected)  # adopted from memory, never read from the file
    assert refresher.persistence_error == "cannot save renewed session: PermissionError"  # the file is still behind


def test_a_renewal_after_the_adoption_reports_the_file_as_current(tmp_path):
    pool, path = stored_pool(tmp_path)
    cfg = pool.backends[1].cfg
    refresher = Refresher(cfg, http_refresh=lambda: [])
    refresher.persistence_error = "cannot save renewed session: ValueError"
    sign_in_again(path, cfg["account_id"], "accessToken=B-ext; xsrfToken=xsrf-ext", "xsrf-ext")
    mark = journal.JOURNAL.last_seq
    assert refresher.refresh(force=True) and refresher.persistence_error is None
    assert refresher.refresh(force=True) and refresher.persistence_error is None
    assert "B-ext" in path.read_text(encoding="utf-8")
    # Nothing was written by either: the adopted sign-in came from the file.
    assert all(e["persisted"] is False and not e["persistence_error"] for e in renewals(cfg, mark))
