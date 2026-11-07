"""Regression tests for the account-related findings of the review of PR #18."""
import asyncio
import json
import os
import stat
import threading
import urllib.error
from types import SimpleNamespace

import pytest

import accounts as accounts_module
import apierr
import config
import models
import upstream_refresh
from accounts import AccountDispatcher
from test_account_dispatcher import Account, FakeManager, FakeRefresher, dispatcher
from test_admin import admin_relay  # noqa: F401 - fixture
from test_native_context_reuse import Backend, setup
from upstream_refresh import Refresher

REJECTED = "Retool rejected session renewal"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RELAY_SESSION_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RELAY_API_KEY", "synthetic-key")


def exhausted(available):
    return {"reason": "exhausted", "available": available, "threshold": 20.0}


# -- 18-1: min_account_balance is a failover reserve ---------------------------------
@pytest.mark.parametrize("mode", ["per_conversation", "shared"])
def test_18_1_lone_account_below_threshold_with_credits_is_served(tmp_path, mode):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False, session_mode=mode)
    pool.backends[0].unavailable = exhausted(2.0)

    async def scenario():
        lease = await pool.acquire("chat")
        assert lease.backend is pool.backends[0]
        await pool.release(lease)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["per_conversation", "shared"])
@pytest.mark.parametrize("balance", [0.0, -3.0])
def test_18_1_lone_account_without_credits_gets_402(tmp_path, mode, balance):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False, session_mode=mode)
    pool.backends[0].unavailable = exhausted(balance)

    async def scenario():
        with pytest.raises(apierr.ApiError) as caught:
            await pool.acquire("chat")
        assert pool._slots._value == 8
        return caught.value

    error = asyncio.run(scenario())
    assert error.status == 402 and error.code == "insufficient_credits"


def test_18_1_all_below_threshold_uses_the_richest_and_respects_failover_exclusion(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False)
    other = Backend(tmp_path, provider_resource_name="other-account")
    pool.backends.append(other)
    first = pool.backends[0]
    first.unavailable, other.unavailable = exhausted(3.0), exhausted(9.0)

    async def scenario():
        lease = await pool.acquire("new")
        assert lease.backend is other
        await pool.release(lease)
        lease = await pool.acquire("new-2", exclude=(other,))
        assert lease.backend is first
        await pool.release(lease)
        with pytest.raises(apierr.ApiError) as caught:
            await pool.acquire("new-3", exclude=(first, other))
        return caught.value

    assert asyncio.run(scenario()).status == 402


def test_18_1_unknown_balance_counts_as_usable(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False)
    pool.backends[0].unavailable = {"reason": "exhausted", "available": None, "threshold": 20.0}

    async def scenario():
        lease = await pool.acquire("chat")
        await pool.release(lease)

    asyncio.run(scenario())


# -- 18-2: request-path renewal returns the account at once ---------------------------
def _no_network():
    raise AssertionError("must not hit the network")


def test_18_2_adopted_cookie_marks_refresher_as_renewed():
    cfg = {"base": "https://x.invalid", "cookie": "accessToken=new", "xsrf": "x", "ua": "t",
           "upstream_timeout": 5}
    refresher = Refresher(cfg, http_refresh=_no_network)
    refresher.last_error, refresher.last_failure_kind = "old error", "auth"
    assert refresher.refresh(rejected_cookie="accessToken=old") is True
    assert refresher.last_success_at is not None
    assert refresher.last_error is None and refresher.last_failure_kind is None


def test_18_2_external_sign_in_revives_account_immediately(tmp_path):
    data = {"base": "https://a.invalid", "page_uuid": "p", "cookie": "accessToken=x; xsrfToken=y", "xsrf": "y",
            "ua": "t", "workspace_root": str(tmp_path)}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    cfg = config.load(str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved.update(cookie="accessToken=NEW; xsrfToken=NEWX", xsrf="NEWX")
    path.write_text(json.dumps(saved), encoding="utf-8")
    refresher = Refresher(cfg, http_refresh=_no_network)
    refresher.last_error, refresher.last_failure_kind = "old error", "auth"
    backend = Account("a", refresher)
    backend.cfg = cfg
    value = AccountDispatcher([backend], {"min_account_balance": 20}, {})
    with value._lock:
        value._set_reason(backend, "auth_dead", {"renewed_at": None})
    assert backend.unavailable is not None
    assert refresher.refresh(force=True) is True
    assert cfg["cookie"] == "accessToken=NEW; xsrfToken=NEWX"
    assert value.revive_renewed() is True
    assert backend.unavailable is None


# -- 18-3: legacy record without account_id -------------------------------------------
def test_18_3_record_without_account_id_is_adopted_and_gets_its_id_back(tmp_path):
    data = {"base": "https://a.invalid", "page_uuid": "p", "cookie": "accessToken=x; xsrfToken=y", "xsrf": "y",
            "ua": "t", "workspace_root": str(tmp_path)}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    cfg = config.load(str(path))
    assert cfg["account_id"]
    path.write_text(json.dumps(dict(data, cookie="accessToken=NEW; xsrfToken=NEWX", xsrf="NEWX")), encoding="utf-8")
    assert config.reload_auth(cfg) is True
    assert cfg["cookie"] == "accessToken=NEW; xsrfToken=NEWX"
    config.persist_auth(dict(cfg, cookie="accessToken=R; xsrfToken=NEWX"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["cookie"] == "accessToken=R; xsrfToken=NEWX" and saved["account_id"] == cfg["account_id"]


def test_18_3_account_list_record_without_id_is_adopted(tmp_path):
    record = {"account_id": "A", "base": "https://a.invalid", "page_uuid": "p",
              "cookie": "accessToken=a; xsrfToken=a", "xsrf": "a"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"accounts": [record], "ua": "t", "workspace_root": str(tmp_path)}), encoding="utf-8")
    cfg = config.load(str(path))
    account = cfg["accounts"][0]
    account_cfg = dict(cfg, **account, _account_ref=account, _disk_auth=(account["cookie"], account["xsrf"]))
    rewritten = {k: v for k, v in record.items() if k != "account_id"}
    rewritten.update(cookie="accessToken=NEW; xsrfToken=n", xsrf="n")
    path.write_text(json.dumps({"accounts": [rewritten], "ua": "t", "workspace_root": str(tmp_path)}),
                    encoding="utf-8")
    assert config.reload_auth(account_cfg) is True
    config.persist_auth(dict(account_cfg, cookie="accessToken=R; xsrfToken=n"))
    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert saved[0]["account_id"] == "A" and saved[0]["cookie"] == "accessToken=R; xsrfToken=n"


def test_18_3_ambiguous_legacy_records_are_still_refused(tmp_path):
    record = {"account_id": "A", "base": "https://a.invalid", "page_uuid": "p",
              "cookie": "accessToken=a; xsrfToken=a", "xsrf": "a"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"accounts": [record], "ua": "t", "workspace_root": str(tmp_path)}), encoding="utf-8")
    cfg = config.load(str(path))
    account = cfg["accounts"][0]
    account_cfg = dict(cfg, **account, _account_ref=account, _disk_auth=(account["cookie"], account["xsrf"]))
    twin = {k: v for k, v in record.items() if k != "account_id"}
    other = dict(twin, cookie="accessToken=other; xsrfToken=o", xsrf="o")
    path.write_text(json.dumps({"accounts": [twin, other], "ua": "t", "workspace_root": str(tmp_path)}),
                    encoding="utf-8")
    before = path.read_bytes()
    assert not config.reload_auth(account_cfg)
    with pytest.raises(ValueError):
        config.persist_auth(account_cfg)
    assert path.read_bytes() == before


# -- 18-4: config.load on read-only / missing storage ---------------------------------
def _config_file(tmp_path):
    data = {"base": "https://a.invalid", "page_uuid": "p", "cookie": "accessToken=x; xsrfToken=y", "xsrf": "y",
            "ua": "t", "workspace_root": str(tmp_path)}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_18_4_unwritable_config_keeps_generated_ids_in_memory(tmp_path, monkeypatch, capsys):
    path = _config_file(tmp_path)

    def read_only(*_):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(config, "_write_file", read_only)
    before = path.read_bytes()
    cfg = config.load(str(path))
    assert cfg["account_id"]
    assert path.read_bytes() == before
    assert "account_id" in capsys.readouterr().err


def test_18_4_read_only_config_leaves_no_temporary_file(tmp_path, capsys):
    path = _config_file(tmp_path)
    before = path.read_bytes()
    os.chmod(path, stat.S_IREAD)
    try:
        cfg = config.load(str(path))
        assert cfg["account_id"]
        assert path.read_bytes() == before
        assert not list(tmp_path.glob(".session-*.tmp"))
        assert "ids are kept in memory" in capsys.readouterr().err
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def test_18_4_unwritable_lock_sidecar_does_not_break_load(tmp_path):
    path = _config_file(tmp_path)
    (tmp_path / "config.json.lock").mkdir()
    assert config.load(str(path))["account_id"]


def test_18_4_missing_config_is_a_clean_error_without_a_stray_lock(tmp_path):
    with pytest.raises(apierr.ApiError, match="config not found"):
        config.load(str(tmp_path / "nope.json"))
    assert not list(tmp_path.glob("*.lock"))


def test_18_4_missing_directory_is_a_clean_error(tmp_path):
    with pytest.raises(apierr.ApiError, match="config not found"):
        config.load(str(tmp_path / "nodir" / "config.json"))


def test_18_4_non_finite_values_are_service_unavailable_not_valueerror(tmp_path):
    path = _config_file(tmp_path)
    before = path.read_bytes()
    with pytest.raises(apierr.ApiError) as caught:
        config._write_file(str(path), {"cookie": float("nan")})
    assert caught.value.status == 503
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".session-*.tmp"))


# -- 18-5: affinity account without a route for the model -----------------------------
def _route_pool(tmp_path):
    first, pool, _ = setup(tmp_path, transport="direct", project_context=False)
    second = Backend(tmp_path, provider_resource_name="b-anthropic", openai_provider_resource_name="b-openai")
    pool.backends.append(second)
    by_provider = {}
    for entry in models.catalog():
        by_provider.setdefault(entry["provider"], entry)
    return first, second, pool, by_provider["anthropic"], by_provider["openai"]


def test_18_5_existing_conversation_migrates_to_the_account_with_a_route(tmp_path):
    first, second, pool, anthropic_entry, openai_entry = _route_pool(tmp_path)

    async def scenario():
        lease = await pool.acquire("chat", model_entry=anthropic_entry)
        assert lease.backend is first
        await pool.release(lease)
        lease = await pool.acquire("chat", model_entry=openai_entry)
        assert lease.backend is second
        await pool.release(lease)

    asyncio.run(scenario())


def test_18_5_isolated_job_ignores_affinity_without_a_route(tmp_path):
    first, second, pool, anthropic_entry, openai_entry = _route_pool(tmp_path)

    async def scenario():
        lease = await pool.acquire("chat", model_entry=anthropic_entry)
        await pool.release(lease)
        lease = await pool.acquire("chat", isolated=True, model_entry=openai_entry)
        assert lease.backend is second
        await pool.release(lease)

    asyncio.run(scenario())


# -- 18-7: only network failures pause every account ----------------------------------
@pytest.mark.parametrize("kind", ["upstream", "local"])
def test_18_7_one_failing_account_does_not_pause_the_others(kind):
    bad, good = Account("bad", FakeRefresher([(False, kind)] * 10)), Account("good")
    value, clock = dispatcher([bad, good])
    delays = []
    for _ in range(3):
        value.refresh(bad)
        delays.append(value.state[bad]["next_refresh"] - clock.now)
    assert value.pause_until == float("-inf")
    assert delays[0] < delays[1] < delays[2]
    clock.now += 5
    assert value.due() == [(good, "refresh")]


def test_18_7_network_failure_still_pauses_everyone():
    bad, good = Account("bad", FakeRefresher([(False, "network")])), Account("good")
    value, clock = dispatcher([bad, good])
    value.refresh(bad)
    clock.now += 5
    assert value.pause_until > clock.now and value.due() == []


def test_18_7_renewal_rejected_by_retool_is_an_auth_failure():
    cfg = {"base": "https://x.invalid", "cookie": "a=b", "xsrf": "x", "upstream_timeout": 5}

    def rejected():
        raise ValueError(REJECTED)

    refresher = Refresher(cfg, http_refresh=rejected)
    assert refresher.refresh(force=True) is False
    assert refresher.last_failure_kind == "auth"


def test_18_7_success_false_body_is_an_auth_failure(monkeypatch):
    class Response:
        headers = SimpleNamespace(get_all=lambda name: [])

        def read(self):
            return b'{"success": false}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(upstream_refresh.urllib.request, "urlopen", lambda *a, **k: Response())
    cfg = {"base": "https://x.invalid", "cookie": "a=b", "xsrf": "x", "ua": "t", "upstream_timeout": 5}
    refresher = Refresher(cfg)
    assert refresher.refresh(force=True) is False
    assert refresher.last_failure_kind == "auth"


# -- 18-12: scheduler claims are rolled back ------------------------------------------
def test_18_12_failed_submit_releases_the_claim(monkeypatch):
    submitted = threading.Event()

    class Broken:
        def __init__(self, *a, **k):
            pass

        def submit(self, *a, **k):
            submitted.set()
            raise RuntimeError("cannot schedule new futures after shutdown")

        def shutdown(self, *a, **k):
            pass

    monkeypatch.setattr(accounts_module, "DaemonIOExecutor", Broken)  # the dispatcher's workers (daemon threads)
    accounts = [Account("a"), Account("b")]
    value, _ = dispatcher(accounts)
    value.start()
    assert submitted.wait(5)
    value.stop()
    assert value._busy == 0
    assert all(not value.state[a]["running"] for a in accounts)


def test_18_12_restart_after_stop_resets_leaked_claims():
    account = Account("a")
    value, _ = dispatcher([account], session_refresh_interval=0)
    value._busy = 2
    value.state[account]["running"].add("refresh")
    value.start()
    try:
        assert value._busy == 0 and not value.state[account]["running"]
    finally:
        value.stop()


# -- 18-13: capped exponents ----------------------------------------------------------
def test_18_13_backoff_exponents_are_capped():
    auth = Account("auth", FakeRefresher([(False, "auth")] * 3))
    value, clock = dispatcher([auth])
    value.state[auth]["auth_failures"] = 5000
    value.refresh(auth)
    assert value.state[auth]["next_refresh"] - clock.now <= 600.0 * 1.1

    net = Account("net", FakeRefresher([(False, "network")]))
    value, clock = dispatcher([net])
    value._network_failures = 5000
    value.refresh(net)
    assert value.pause_until - clock.now == accounts_module.NETWORK_PAUSE_MAX

    soft = Account("soft", FakeRefresher([(False, "upstream")]))
    value, clock = dispatcher([soft])
    value.state[soft]["soft_failures"] = 5000
    value.refresh(soft)
    assert value.state[soft]["next_refresh"] - clock.now <= accounts_module.NETWORK_PAUSE_MAX * 1.1


# -- 18-18: a concurrent forced check waits for the in-flight one ---------------------
def test_18_18_concurrent_forced_check_shares_the_in_flight_result():
    entered, release = threading.Event(), threading.Event()

    class SlowManager(FakeManager):
        def get_balance(self, force_refresh=False):
            entered.set()
            release.wait(5)
            return {"available": 5.0}

    account = Account("a")
    value, _ = dispatcher([account], {account: SlowManager(5.0)})
    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", value.check_balance_now(account)))
    second = threading.Thread(target=lambda: results.setdefault("second", value.check_balance_now(account)))
    first.start()
    assert entered.wait(5)
    second.start()
    second.join(0.3)
    release.set()
    first.join(5)
    second.join(5)
    assert results == {"first": True, "second": True}


# -- 18-19: kind and error come from one locked snapshot -------------------------------
def test_18_19_dispatcher_uses_the_consistent_refresh_result():
    class Racy:
        keepalive_owner = None
        last_failure_kind = "network"  # stale value another thread left behind
        last_error = "stale"
        last_success_at = None

        def refresh(self, force=False, **_kw):
            return False

        def refresh_detailed(self, force=False, **_kw):
            return False, "auth", "session renewal failed: HTTP 401"

    account = Account("a", Racy())
    value, _ = dispatcher([account])
    value.refresh(account)
    assert value.pause_until == float("-inf")
    assert value.state[account]["auth_failures"] == 1


def test_18_19_refresher_returns_ok_kind_and_error_together():
    cfg = {"base": "https://x.invalid", "cookie": "a=b", "xsrf": "x", "upstream_timeout": 5}

    def unauthorized():
        raise urllib.error.HTTPError("https://x.invalid", 401, "no", {}, None)

    refresher = Refresher(cfg, http_refresh=unauthorized)
    assert refresher.refresh_detailed(force=True) == (False, "auth", "session renewal failed: HTTP 401")
    refresher = Refresher(cfg, http_refresh=lambda: [])
    assert refresher.refresh_detailed(force=True) == (True, None, None)
    assert refresher.refresh(force=True) is True


# -- 18-20: admin settings busy lock -> 503 --------------------------------------------
def test_18_20_busy_configuration_storage_is_service_unavailable(admin_relay, monkeypatch):  # noqa: F811
    def busy(*_a, **_k):
        raise TimeoutError("configuration storage is busy")

    monkeypatch.setattr(config, "storage_lock", busy)
    before = admin_relay.path.read_bytes()
    status, body, _ = admin_relay.patch({"show_thinking": False})
    assert status == 503 and "error" in body
    assert admin_relay.path.read_bytes() == before
