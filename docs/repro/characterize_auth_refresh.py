"""Characterize auth identity collisions. Synthetic credentials only.

Passing assertions demonstrate current defects; repository files are unchanged.
"""
import json
import threading

import config

import pytest

from upstream_refresh import Refresher


def setup_accounts(tmp_path, *, same_page=False, same_host=False):
    a = {"base":"https://tenant-a.invalid", "page_uuid":"page-a",
         "cookie":"accessToken=synthetic-A; xsrfToken=xsrf-A", "xsrf":"xsrf-A"}
    b = {"base":a["base"] if same_host else "https://tenant-b.invalid",
         "page_uuid":a["page_uuid"] if same_page else "page-b",
         "cookie":"accessToken=synthetic-B; xsrfToken=xsrf-B", "xsrf":"xsrf-B"}
    accounts = [a,b]
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"accounts":accounts}))
    cfg = dict(b, ua="audit-fixture", upstream_timeout=5, cookie_values=[],
        accounts=accounts, _account_ref=b, _config_path=str(path),
        _persist_auth=True, _disk_auth=(b["cookie"], b["xsrf"]))
    return accounts, path, cfg


def test_control_distinct_identity_keeps_other_account(tmp_path):
    accounts, path, cfg = setup_accounts(tmp_path)
    original_a = dict(accounts[0])
    refresher = Refresher(cfg, http_refresh=lambda:[
        "accessToken=synthetic-B-new; Path=/", "xsrfToken=xsrf-B-new; Path=/"])
    assert refresher.refresh()
    assert accounts[0] == original_a
    saved = json.loads(path.read_text())["accounts"]
    assert saved[0] == original_a
    assert "synthetic-B-new" in saved[1]["cookie"]
    print("EVIDENCE distinct pages: B rotates, A stays unchanged")


def test_same_host_and_page_adopts_other_account_without_network(tmp_path):
    accounts, path, cfg = setup_accounts(tmp_path, same_page=True, same_host=True)
    calls = []
    refresher = Refresher(cfg, http_refresh=lambda:calls.append("network"))
    assert refresher.refresh()
    assert cfg["cookie"] == accounts[0]["cookie"]
    assert cfg["xsrf"] == accounts[0]["xsrf"]
    assert not calls
    assert refresher.last_success_at is None
    print("EVIDENCE SAME HOST/PAGE: B adopts A credentials during reload; HTTP calls=0")


def test_same_page_different_host_overwrites_wrong_memory_record(tmp_path):
    accounts, path, cfg = setup_accounts(tmp_path, same_page=True)
    before = path.read_text()
    refresher = Refresher(cfg, http_refresh=lambda:[
        "accessToken=synthetic-B-new; Path=/", "xsrfToken=xsrf-B-new; Path=/"])
    assert refresher.refresh()
    assert accounts[0]["cookie"] == cfg["cookie"]
    assert accounts[0]["xsrf"] == cfg["xsrf"]
    assert accounts[1]["cookie"] == cfg["cookie"]
    assert refresher.persistence_error == "cannot save renewed session: ValueError"
    assert path.read_text() == before
    print("EVIDENCE DIFFERENT HOSTS/SAME PAGE: B overwrites shared record A; persistence fails")


def evidence(name, **fields):
    print("EVIDENCE " + name + " " + json.dumps(fields, ensure_ascii=False))


def test_evidence_multiaccount_persistence_loses_parallel_rotation(tmp_path,monkeypatch):
    path=tmp_path/'config.json'
    a={'base':'https://a.invalid','page_uuid':'page-a','cookie':'old-a','xsrf':'old-xa'}
    b={'base':'https://b.invalid','page_uuid':'page-b','cookie':'old-b','xsrf':'old-xb'}
    path.write_text(json.dumps({'accounts':[a,b]}))
    ca=dict(a,cookie='new-a',xsrf='new-xa',_config_path=str(path),_persist_auth=True,_disk_auth=('old-a','old-xa'))
    cb=dict(b,cookie='new-b',xsrf='new-xb',_config_path=str(path),_persist_auth=True,_disk_auth=('old-b','old-xb'))
    barrier=threading.Barrier(2)
    original=config._read_file
    def read(path):
        data=original(path)
        barrier.wait(3)
        return data
    monkeypatch.setattr(config,'_read_file',read)
    errors=[]
    def save(cfg):
        try:config.persist_auth(cfg)
        except Exception as exc:errors.append(str(exc))
    ts=[threading.Thread(target=save,args=(c,)) for c in (ca,cb)]
    for t in ts:t.start()
    for t in ts:t.join(4)
    assert not errors
    saved=json.loads(path.read_text())['accounts']
    rotated=sum(c['cookie'].startswith('new-') for c in saved)
    evidence('multiaccount_rotation',successful_saves=2,persisted_rotations=rotated,
             disk_values=[c['cookie'] for c in saved],disk_checkpoints=[ca['_disk_auth'][0],cb['_disk_auth'][0]])
    assert rotated==1
