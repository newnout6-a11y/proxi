"""Account identity, migration and cross-process configuration transactions."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import time

import pytest

from apierr import ApiError
import config
from session import Backend, SessionPool
from upstream_refresh import Refresher


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RELAY_API_KEY", "synthetic-key")


def stored_pool(tmp_path, *, same_page=True, same_host=True, legacy=False):
    a = {"account_id": "account-a", "base": "https://a.invalid", "page_uuid": "page-a",
         "cookie": "accessToken=synthetic-A; xsrfToken=xsrf-A", "xsrf": "xsrf-A"}
    b = {"account_id": "account-b", "base": a["base"] if same_host else "https://b.invalid",
         "page_uuid": a["page_uuid"] if same_page else "page-b",
         "cookie": "accessToken=synthetic-B; xsrfToken=xsrf-B", "xsrf": "xsrf-B"}
    if legacy:
        a.pop("account_id")
        b.pop("account_id")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"accounts": [a, b], "ua": "test",
                               "workspace_root": str(tmp_path)}), encoding="utf-8")
    return SessionPool(Backend(config.load(path))), path


@pytest.mark.parametrize("same_page,same_host", [(True, True), (True, False), (False, True)])
def test_rotation_changes_only_the_exact_account(tmp_path, same_page, same_host):
    pool, path = stored_pool(tmp_path, same_page=same_page, same_host=same_host)
    before_a = dict(pool.backends[0].cfg["_account_ref"])
    cfg = pool.backends[1].cfg
    refresher = Refresher(cfg, http_refresh=lambda: [
        "accessToken=synthetic-B-new; Path=/", "xsrfToken=xsrf-B-new; Path=/"])
    assert refresher.refresh()
    assert refresher.last_success_at is not None
    assert not refresher.persistence_error
    assert pool.backends[0].cfg["_account_ref"] == before_a
    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert saved[0] == before_a
    assert "synthetic-B-new" in saved[1]["cookie"]


def test_migration_assigns_persistent_distinct_ids_to_shared_pages(tmp_path):
    pool, path = stored_pool(tmp_path, legacy=True)
    ids = [b.cfg["account_id"] for b in pool.backends]
    assert len(set(ids)) == 2
    assert [a["account_id"] for a in config.load(path)["accounts"]] == ids
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["accounts"].reverse()
    saved["accounts"][0].update(cookie="accessToken=B-browser-login", xsrf="B-browser-xsrf")
    path.write_text(json.dumps(saved), encoding="utf-8")
    assert config.reload_auth(pool.backends[1].cfg)
    assert pool.backends[1].cfg["cookie"] == "accessToken=B-browser-login"
    assert "synthetic-A" in pool.backends[0].cfg["cookie"]


@pytest.mark.parametrize("change", ["missing", "tenant", "duplicate"])
def test_account_changes_never_fall_back_to_root_or_sibling_auth(tmp_path, change):
    pool, path = stored_pool(tmp_path)
    cfg = pool.backends[1].cfg
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved.update(base=cfg["base"], cookie="wrong-root", xsrf="wrong-root")
    if change == "missing":
        saved["accounts"].pop()
    elif change == "tenant":
        saved["accounts"][1]["base"] = "https://other.invalid"
    else:
        saved["accounts"][0]["account_id"] = cfg["account_id"]
    path.write_text(json.dumps(saved), encoding="utf-8")
    before = path.read_bytes()
    if change == "duplicate":
        with pytest.raises(ValueError, match="ambiguous"):
            config.reload_auth(cfg)
    else:
        assert not config.reload_auth(cfg)
    with pytest.raises(ValueError):
        config.persist_auth(cfg)
    assert path.read_bytes() == before
    assert "synthetic-B" in cfg["cookie"]


def test_duplicate_ids_are_rejected_without_rewriting_config(tmp_path):
    _, path = stored_pool(tmp_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["accounts"][1]["account_id"] = saved["accounts"][0]["account_id"]
    path.write_text(json.dumps(saved), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ApiError, match="account_id"):
        config.load(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("compatibility", [False, True])
def test_configured_unsupported_effort_requires_explicit_compatibility(tmp_path, compatibility):
    _, path = stored_pool(tmp_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved.update(reasoning_effort="xhigh", allow_effort_downgrade=compatibility)
    path.write_text(json.dumps(saved), encoding="utf-8")
    if compatibility:
        assert config.load(path)["allow_effort_downgrade"] is True
    else:
        with pytest.raises(ApiError, match="reasoning effort"):
            config.load(path)


def test_string_false_cannot_enable_effort_compatibility(tmp_path):
    _, path = stored_pool(tmp_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["allow_effort_downgrade"] = "false"
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ApiError, match="boolean"):
        config.load(path)


def test_ambiguous_unmigrated_runtime_refuses_persistence(tmp_path):
    pool, _ = stored_pool(tmp_path)
    cfg = dict(pool.backends[1].cfg)
    cfg.pop("account_id")
    with pytest.raises(ValueError, match="ambiguous"):
        config.persist_auth(cfg)


def _parallel_save(cfg, start, results):
    original = config._read_file

    def slow_read(path):
        saved = original(path)
        time.sleep(0.05)
        return saved

    config._read_file = slow_read
    start.wait(10)
    try:
        config.persist_auth(cfg)
        results.put(None)
    except Exception as exc:
        results.put(type(exc).__name__)


def test_parallel_processes_preserve_both_rotations(tmp_path):
    pool, path = stored_pool(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    configs = []
    for index, backend in enumerate(pool.backends):
        cfg = {k: backend.cfg[k] for k in ("account_id", "base", "page_uuid", "cookie", "xsrf",
                                          "_config_path", "_persist_auth", "_disk_auth")}
        cfg.update(cookie=f"new-{index}", xsrf=f"new-xsrf-{index}")
        configs.append(cfg)
    processes = [ctx.Process(target=_parallel_save, args=(cfg, start, results)) for cfg in configs]
    try:
        for process in processes:
            process.start()
        start.set()
        assert [results.get(timeout=15) for _ in processes] == [None, None]
        for process in processes:
            process.join(5)
            assert process.exitcode == 0
        saved = json.loads(path.read_text(encoding="utf-8"))["accounts"]
        assert [a["cookie"] for a in saved] == ["new-0", "new-1"]
        assert [a["xsrf"] for a in saved] == ["new-xsrf-0", "new-xsrf-1"]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        results.close()


def test_parallel_threads_preserve_both_rotations(tmp_path, monkeypatch):
    pool, path = stored_pool(tmp_path)
    original = config._read_file

    def slow_read(path):
        saved = original(path)
        time.sleep(0.02)
        return saved

    monkeypatch.setattr(config, "_read_file", slow_read)
    configs = [dict(b.cfg, cookie=f"new-{i}", xsrf=f"xsrf-{i}") for i, b in enumerate(pool.backends)]
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(config.persist_auth, configs))
    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert [a["cookie"] for a in saved] == ["new-0", "new-1"]
    assert not list(tmp_path.glob(".session-*.tmp"))


def test_network_renewal_does_not_hold_the_configuration_lock(tmp_path):
    pool, path = stored_pool(tmp_path)

    def take_lock():
        with config.storage_lock(path):
            return True

    def refresh():
        with ThreadPoolExecutor(1) as executor:
            assert executor.submit(take_lock).result(timeout=2)
        return []

    assert Refresher(pool.backends[1].cfg, http_refresh=refresh).refresh()


def _parallel_refresh(cfg, start, results, requests):
    def network():
        with requests.get_lock():
            requests.value += 1
        time.sleep(0.05)
        return ["accessToken=coordinated-fresh; Path=/"]

    start.wait(10)
    refresher = Refresher(cfg, http_refresh=network)
    results.put((refresher.refresh(), cfg["cookie"], refresher.persistence_error))


def test_same_account_refresh_is_coordinated_between_processes(tmp_path):
    pool, _ = stored_pool(tmp_path)
    backend = pool.backends[1]
    cfg = {k: backend.cfg[k] for k in ("account_id", "base", "page_uuid", "cookie", "xsrf",
                                      "_config_path", "_persist_auth", "_disk_auth", "upstream_timeout")}
    cfg["cookie_values"] = []
    ctx = multiprocessing.get_context("spawn")
    start, results, requests = ctx.Event(), ctx.Queue(), ctx.Value("i", 0)
    processes = [ctx.Process(target=_parallel_refresh, args=(cfg, start, results, requests)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(5)
            assert process.exitcode == 0
        assert requests.value == 1
        assert all(ok and "coordinated-fresh" in cookie and error is None for ok, cookie, error in outcomes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        results.close()
