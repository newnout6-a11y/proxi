"""Admin account pool: enable/disable, priority, manual balance and renewal, add and delete,
and how routing and the dispatcher honour those controls."""
import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

import admin
import apierr
import config
import journal
import proxy
from test_account_dispatcher import Account, dispatcher
from test_admin import admin_relay  # noqa: F401 - single-account fixture
from test_native_context_reuse import Backend, setup
from test_review_fixes_proxy import FakeManager

KEY = "pool-relay-key-123"
NEW = {"label": "третий", "base": "https://tenant.retool.com", "page_uuid": "11111111-2222-3333-4444-555555555555",
       "cookie": "accessToken=cookie-new-123456", "xsrf": "xsrf-new-123456"}
POOL_KEYS = {"account", "label", "enabled", "priority", "state", "routable", "reserve", "available",
             "monthly_allocation", "used_month", "reset_date", "stale", "session_used", "turns", "auth_failures",
             "last_renewed_at", "last_balance_at", "error", "pending"}
SECRETS = ("cookie-acc-a-123456", "cookie-acc-b-123456", "xsrf-acc-a-123456", "xsrf-acc-b-123456", "page-acc-a",
           "page-acc-b", "thread-acc-a", "thread-acc-b", "example.invalid", "other.invalid", KEY,
           "cookie-new-123456", "xsrf-new-123456", "11111111-2222")


def _account(name, base):
    return {"account_id": name, "base": base, "page_uuid": "page-" + name, "thread_id": "thread-" + name,
            "cookie": "accessToken=cookie-%s-123456; xsrfToken=xsrf-%s-123456" % (name, name),
            "xsrf": "xsrf-%s-123456" % name, "provider_resource_name": "resource-" + name}


def _saved(tmp_path, **overrides):
    first = _account("acc-a", "https://example.invalid")
    saved = {"base": first["base"], "page_uuid": first["page_uuid"], "cookie": first["cookie"],
             "xsrf": first["xsrf"], "account_id": "acc-a", "ua": "test-agent", "relay_api_key": KEY,
             "workspace_root": str(tmp_path), "client_metadata_home": str(tmp_path), "model": "claude-opus-5-5",
             "accounts": [first, _account("acc-b", "https://other.invalid")]}
    saved.update(overrides)
    return saved


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RETOOL_THREAD_ID",
                 "RELAY_SESSION_MODE", "RELAY_API_KEY", "RELAY_HOST", "RELAY_PORT", "RELAY_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def pool(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_saved(tmp_path)), encoding="utf-8")
    cfg = config.load(str(path))
    cfg["host"], cfg["port"] = "127.0.0.1", 0
    runtime = proxy.Runtime(cfg)
    managers = [FakeManager(100.0), FakeManager(60.0)]
    for backend, manager in zip(runtime.accounts.backends, managers):
        runtime.accounts.managers[backend] = manager
        backend.credits = manager
    runtime.credits = managers[0]
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(8))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]

    class Ctx:
        def call(self, route, payload=None, *, method="GET", guard=True):
            body = json.dumps(payload).encode() if payload is not None else None
            request = urllib.request.Request(base + route, data=body, method=method)
            request.add_header("authorization", "Bearer " + KEY)
            if body is not None:
                request.add_header("content-type", "application/json")
                if guard:
                    request.add_header("x-admin-request", "1")
            try:
                response = urllib.request.urlopen(request, timeout=20)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read().decode()
                return response.status, json.loads(raw or "{}")

        def saved(self):
            return json.loads(path.read_text(encoding="utf-8"))

    ctx = Ctx()
    ctx.runtime, ctx.path, ctx.managers = runtime, path, managers
    ctx.backends = runtime.accounts.backends
    ctx.tags = [journal.tag("acc-a"), journal.tag("acc-b")]
    yield ctx
    server.shutdown()
    server.server_close()
    runtime.loop.call_soon_threadsafe(runtime.loop.stop)


def accounts(pool):
    status, body = pool.call("/admin/api/accounts")
    assert status == 200
    return body


# -- listing --------------------------------------------------------------------
def test_pool_lists_every_account_with_its_controls_and_no_secrets(pool):
    body = accounts(pool)
    assert [item["account"] for item in body["accounts"]] == pool.tags
    assert all(set(item) == POOL_KEYS for item in body["accounts"])
    first = body["accounts"][0]
    assert (first["label"], first["enabled"], first["priority"], first["pending"]) == ("", True, 0, None)
    assert (first["available"], first["state"], first["routable"]) == (100.0, "active", True)
    assert body["summary"] == {"total": 2, "enabled": 2, "routable": 2, "reserve": 0, "states": {"active": 2}}
    assert body["limits"] == {"min_account_balance": 20.0, "account_balance_interval": 300,
                              "account_refresh_parallel": 3}
    assert body["restart_required"] is False
    encoded = json.dumps(body)
    for secret in SECRETS:
        assert secret not in encoded, secret


# -- enable / disable / priority ---------------------------------------------------
def test_disabling_is_persisted_applied_live_and_routes_new_work_elsewhere(pool):
    status, body = pool.call("/admin/api/accounts/" + pool.tags[0], {"enabled": False}, method="PATCH")
    assert status == 200
    assert (body["account"]["enabled"], body["account"]["routable"]) == (False, False)
    assert pool.saved()["accounts"][0]["enabled"] is False
    assert pool.backends[0].cfg["enabled"] is False
    assert pool.runtime.cfg["accounts"][0]["enabled"] is False
    summary = accounts(pool)["summary"]
    assert (summary["enabled"], summary["routable"]) == (1, 1)
    assert {pool.runtime.pool._next_backend() for _ in range(4)} == {pool.backends[1]}
    overview = pool.call("/admin/api/overview")[1]
    assert [item["routable"] for item in overview["accounts"]] == [False, True]
    assert any(event["kind"] == "admin.account" for event in journal.JOURNAL.query())

    status, body = pool.call("/admin/api/accounts/" + pool.tags[1], {"enabled": False}, method="PATCH")
    assert status == 409 and "last enabled" in body["error"]["message"]
    assert "enabled" not in pool.saved()["accounts"][1] and pool.backends[1].cfg["enabled"] is True

    status, _ = pool.call("/admin/api/accounts/" + pool.tags[0], {"enabled": True}, method="PATCH")
    assert status == 200 and pool.backends[0].cfg["enabled"] is True


def test_priority_and_label_are_saved_and_lower_priority_wins(pool):
    chosen = {pool.runtime.pool._next_backend() for _ in range(4)}
    assert chosen == set(pool.backends)  # equal priority: round-robin across both
    status, body = pool.call("/admin/api/accounts/" + pool.tags[0], {"priority": 5, "label": "  основной  "},
                             method="PATCH")
    assert status == 200 and (body["account"]["priority"], body["account"]["label"]) == (5, "основной")
    assert {pool.runtime.pool._next_backend() for _ in range(4)} == {pool.backends[1]}
    record = pool.saved()["accounts"][0]
    assert (record["priority"], record["label"]) == (5, "основной")


@pytest.mark.parametrize("payload", [{}, {"color": "red"}, {"priority": 100}, {"priority": -1},
                                     {"priority": True}, {"priority": 1.5}, {"enabled": "no"},
                                     {"label": "x" * 41}, {"label": 5}, {"label": "a\nb"}, {"label": "a\x00b"}])
def test_invalid_patches_are_rejected_without_writing(pool, payload):
    before = pool.path.read_bytes()
    status, _ = pool.call("/admin/api/accounts/" + pool.tags[0], payload, method="PATCH")
    assert status == 400
    assert pool.path.read_bytes() == before


def test_routes_methods_unknown_accounts_and_the_csrf_guard(pool):
    tag = pool.tags[0]
    assert pool.call("/admin/api/accounts/deadbeef", {"enabled": False}, method="PATCH")[0] == 404
    assert pool.call("/admin/api/accounts/deadbeef/balance", {}, method="POST")[0] == 404
    assert pool.call("/admin/api/accounts/deadbeef", {}, method="DELETE")[0] == 404
    assert pool.call("/admin/api/accounts/" + tag)[0] == 405
    assert pool.call("/admin/api/accounts/" + tag + "/renew")[0] == 405
    assert pool.call("/admin/api/accounts/" + tag + "/balance", {}, method="DELETE")[0] == 405
    assert pool.call("/admin/api/accounts", {}, method="PUT")[0] == 405
    assert pool.call("/admin/api/accounts/" + tag, {"enabled": False}, method="PATCH", guard=False)[0] == 403
    assert pool.backends[0].cfg["enabled"] is True


# -- routing and dispatcher --------------------------------------------------------
def test_conversation_pinned_to_a_disabled_account_migrates(tmp_path):
    _, sessions, _ = setup(tmp_path, transport="direct", project_context=False)
    first, other = sessions.backends[0], Backend(tmp_path, provider_resource_name="other-account")
    sessions.backends.append(other)

    async def scenario():
        lease = await sessions.acquire("chat")
        assert lease.backend is first
        await sessions.release(lease)
        first.cfg["enabled"] = False
        lease = await sessions.acquire("chat")
        assert lease.backend is other
        await sessions.release(lease)

    asyncio.run(scenario())
    migrations = [e for e in journal.JOURNAL.query() if e["kind"] == "account.migrate"
                  and e.get("conversation") == journal.tag("chat")]
    assert migrations


@pytest.mark.parametrize("mode", ["per_conversation", "shared"])
def test_no_enabled_account_is_a_clear_503(tmp_path, mode):
    _, sessions, _ = setup(tmp_path, transport="direct", project_context=False, session_mode=mode)
    sessions.backends[0].cfg["enabled"] = False
    sessions.backends[0].unavailable = {"reason": "exhausted", "available": 5.0, "threshold": 20.0}

    async def scenario():
        with pytest.raises(apierr.ApiError) as caught:
            await sessions.acquire("chat")
        assert sessions._slots._value == 8
        return caught.value

    error = asyncio.run(scenario())
    assert error.status == 503 and "switched off" in error.message


def test_dispatcher_does_not_renew_or_poll_a_disabled_account():
    first, second = Account("a"), Account("b")
    first.cfg["enabled"] = False
    value, clock = dispatcher([first, second], {first: FakeManager(50.0), second: FakeManager(50.0)})
    clock.now += 120
    jobs = value.due()
    assert jobs and all(backend is second for backend, _ in jobs)


# -- manual balance and renewal ----------------------------------------------------
def test_balance_check_fetches_now_and_reports_when(pool, monkeypatch):
    manager = pool.managers[1]
    forced = []

    def get_balance(force_refresh=False):
        forced.append(force_refresh)
        manager.last_fetched_at = 1700000000.0
        return dict(manager.balance, available=55.5)

    monkeypatch.setattr(manager, "get_balance", get_balance)
    status, body = pool.call("/admin/api/accounts/%s/balance" % pool.tags[1], {}, method="POST")
    assert status == 200 and True in forced
    assert body["account"]["last_balance_at"] == 1700000000.0
    assert pool.runtime.accounts.state[pool.backends[1]]["available"] == 55.5


def test_renew_reports_success_and_a_redacted_failure_as_200(pool, monkeypatch):
    refresher = pool.backends[0].refresher

    def ok(force=False, **_):
        refresher.last_success_at = 1700000100.0
        return True, None, None

    monkeypatch.setattr(refresher, "refresh_detailed", ok)
    status, body = pool.call("/admin/api/accounts/%s/renew" % pool.tags[0], {}, method="POST")
    assert status == 200 and body["ok"] is True and body["error"] is None
    assert body["account"]["last_renewed_at"] == 1700000100.0

    def rejected(force=False, **_):
        refresher.last_error = "session renewal failed: HTTP 401 for accessToken=cookie-acc-a-123456"
        return False, "auth", refresher.last_error

    monkeypatch.setattr(refresher, "refresh_detailed", rejected)
    status, body = pool.call("/admin/api/accounts/%s/renew" % pool.tags[0], {}, method="POST")
    assert status == 200 and body["ok"] is False and "HTTP 401" in body["error"]
    assert "cookie-acc-a-123456" not in json.dumps(body)


# -- add / delete ------------------------------------------------------------------
def test_added_account_is_saved_with_an_id_and_waits_for_a_restart(pool):
    status, body = pool.call("/admin/api/accounts", NEW, method="POST")
    assert status == 200 and body["restart_required"] is True
    account = body["account"]
    assert (account["pending"], account["label"], account["enabled"], account["state"]) == \
        ("added", "третий", True, "pending")
    record = pool.saved()["accounts"][2]
    assert record["cookie"] == NEW["cookie"] and record["base"] == NEW["base"]
    assert len(record["account_id"]) == 32 and account["account"] == journal.tag(record["account_id"])
    listing = accounts(pool)
    assert [item["pending"] for item in listing["accounts"]] == [None, None, "added"]
    assert listing["summary"]["total"] == 3 and listing["restart_required"] is True
    for payload in (body, listing):
        for secret in SECRETS:
            assert secret not in json.dumps(payload), secret
    assert pool.call("/admin/api/accounts", NEW, method="POST")[0] == 409
    assert config.load(str(pool.path))["accounts"][2]["cookie"] == NEW["cookie"]


@pytest.mark.parametrize("change", [{"base": "http://tenant.retool.com"}, {"base": "https://tenant.retool.com/app"},
                                    {"base": "https://user:pw@tenant.retool.com"}, {"page_uuid": "not-a-uuid"},
                                    {"cookie": "a=b\r\nx-evil: 1"}, {"xsrf": ""}, {"label": "x" * 41},
                                    {"extra": "field"}, {"cookie": None}, {"base": "https://tenant.retool.com:abc"},
                                    {"base": "https://ten ant.retool.com"}, {"cookie": "accessToken=тест"},
                                    {"provider_resource_name": "res\tname"}])
def test_invalid_new_accounts_are_rejected(pool, change):
    before = pool.path.read_bytes()
    payload = {key: value for key, value in {**NEW, **change}.items() if value is not None}
    status, _ = pool.call("/admin/api/accounts", payload, method="POST")
    assert status == 400
    assert pool.path.read_bytes() == before


def test_adding_to_a_single_account_config_converts_it_and_keeps_the_session(admin_relay):
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    saved["account_id"] = "root-account"
    admin_relay.path.write_text(json.dumps(saved), encoding="utf-8")
    status, body, _ = admin_relay.request("/admin/api/accounts", NEW, method="POST",
                                          headers={"x-admin-request": "1", "origin": admin_relay.url})
    assert status == 200 and body["account"]["pending"] == "added"
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    first, second = saved["accounts"]
    assert first["account_id"] == "root-account"
    assert (first["cookie"], first["xsrf"], first["page_uuid"]) == \
        ("accessToken=private-cookie-value", "private-xsrf-token", "private-page")
    assert first["provider_resource_name"] == "private-resource" and first["thread_id"] == "private-thread"
    assert "cookie" not in saved and "xsrf" not in saved and second["cookie"] == NEW["cookie"]
    # The running account still finds its own record for cookie rotation.
    assert config._account_record(saved, admin_relay.runtime.backend.cfg) is first
    _, listing, _ = admin_relay.request("/admin/api/accounts")
    assert [item["pending"] for item in listing["accounts"]] == [None, "added"]
    reloaded = config.load(str(admin_relay.path))
    assert reloaded["cookie"] == "accessToken=private-cookie-value" and len(reloaded["accounts"]) == 2


def test_single_account_cannot_be_disabled_or_deleted(admin_relay):
    headers = {"x-admin-request": "1", "origin": admin_relay.url}
    _, listing, _ = admin_relay.request("/admin/api/accounts")
    tag = listing["accounts"][0]["account"]
    status, _, _ = admin_relay.request("/admin/api/accounts/" + tag, {"enabled": False}, method="PATCH",
                                       headers=headers)
    assert status == 409
    status, _, _ = admin_relay.request("/admin/api/accounts/" + tag, {}, method="DELETE", headers=headers)
    assert status == 409


def test_deleted_account_leaves_routing_now_and_config_for_good(pool):
    status, body = pool.call("/admin/api/accounts/" + pool.tags[1], {}, method="DELETE")
    assert status == 200 and body == {"ok": True, "pending": "removed", "restart_required": True}
    assert [record["account_id"] for record in pool.saved()["accounts"]] == ["acc-a"]
    assert pool.backends[1].cfg["enabled"] is False
    listing = accounts(pool)
    assert [item["pending"] for item in listing["accounts"]] == [None, "removed"]
    assert listing["summary"]["total"] == 1 and listing["restart_required"] is True
    assert pool.call("/admin/api/accounts/" + pool.tags[1], {"priority": 3}, method="PATCH")[0] == 409
    assert pool.call("/admin/api/accounts/" + pool.tags[0], {}, method="DELETE")[0] == 409


def test_deleting_the_account_the_root_mirrors_removes_its_session_everywhere(pool):
    status, _ = pool.call("/admin/api/accounts/" + pool.tags[0], {}, method="DELETE")
    assert status == 200
    saved = pool.saved()
    encoded = json.dumps(saved)
    assert "cookie-acc-a-123456" not in encoded and "xsrf-acc-a-123456" not in encoded
    assert saved["ua"] == "test-agent"
    reloaded = config.load(str(pool.path))
    assert reloaded["page_uuid"] == "page-acc-b" and reloaded["base"] == "https://other.invalid"
    assert [record["account_id"] for record in reloaded["accounts"]] == ["acc-b"]


# -- configuration and settings ----------------------------------------------------
def test_duplicate_detection_ignores_case(pool):
    assert pool.call("/admin/api/accounts", NEW, method="POST")[0] == 200
    upper = dict(NEW, page_uuid=NEW["page_uuid"].upper(), base="https://Tenant.Retool.com")
    assert pool.call("/admin/api/accounts", upper, method="POST")[0] == 409


@pytest.mark.parametrize("break_it", ["env", "no-cookie-in-file"])
def test_adding_is_refused_when_the_running_session_is_not_in_the_file(admin_relay, break_it):
    if break_it == "env":
        admin_relay.cfg["_persist_auth"] = False  # RETOOL_COOKIE / RETOOL_XSRF
    else:
        saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
        del saved["cookie"], saved["xsrf"]
        admin_relay.path.write_text(json.dumps(saved), encoding="utf-8")
    before = admin_relay.path.read_bytes()
    status, body, _ = admin_relay.request("/admin/api/accounts", NEW, method="POST",
                                          headers={"x-admin-request": "1", "origin": admin_relay.url})
    assert status == 409 and "RETOOL_COOKIE" in body["error"]["message"]
    assert admin_relay.path.read_bytes() == before


def test_delete_scrubs_a_rotated_root_session_and_hands_root_resources_to_same_tenant_accounts(pool):
    saved = pool.saved()
    saved["accounts"][0]["cookie"] = "accessToken=rotated-cookie-123456"  # rotation writes accounts[i] only
    saved["provider_resource_name"] = "root-resource"
    saved["thread_id"] = "root-thread"
    third = {key: value for key, value in _account("acc-c", "https://example.invalid").items()
             if key != "provider_resource_name"}
    saved["accounts"].append(third)
    pool.path.write_text(json.dumps(saved), encoding="utf-8")
    assert pool.call("/admin/api/accounts/" + pool.tags[0], {}, method="DELETE")[0] == 200
    saved = pool.saved()
    for key in ("cookie", "xsrf", "base", "page_uuid", "account_id", "thread_id"):
        assert key not in saved, key
    # The root now mirrors the first remaining account's resources, not the removed one's
    assert saved["provider_resource_name"] == "resource-acc-b"
    encoded = json.dumps(saved)
    assert "cookie-acc-a-123456" not in encoded and "rotated-cookie-123456" not in encoded
    by_id = {record["account_id"]: record for record in saved["accounts"]}
    assert by_id["acc-c"]["provider_resource_name"] == "root-resource"
    assert by_id["acc-b"]["provider_resource_name"] == "resource-acc-b"
    reloaded = config.load(str(pool.path))
    assert reloaded["page_uuid"] == "page-acc-b" and reloaded["cookie"] == by_id["acc-b"]["cookie"]


def test_shared_mode_cannot_be_chosen_once_the_root_thread_is_gone(pool):
    assert pool.call("/admin/api/accounts/" + pool.tags[0], {}, method="DELETE")[0] == 200
    pool.runtime.cfg["thread_id"] = "thread-still-in-memory"
    status, body = pool.call("/admin/api/settings", {"settings": {"session_mode": "shared"}}, method="PATCH")
    assert status == 400 and "thread_id" in body["error"]["message"]
    assert config.load(str(pool.path))["session_mode"] == "per_conversation"


@pytest.mark.parametrize("base,stored", [("https://Tenant.Retool.com:", "https://tenant.retool.com"),
                                         ("https://tenant.retool.com:443/", "https://tenant.retool.com"),
                                         ("https://tenant.retool.com:8443", "https://tenant.retool.com:8443")])
def test_new_account_base_is_rebuilt_from_its_parts(pool, base, stored):
    assert pool.call("/admin/api/accounts", dict(NEW, base=base), method="POST")[0] == 200
    assert pool.saved()["accounts"][-1]["base"] == stored


def test_shared_mode_keeps_its_account(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_saved(tmp_path, session_mode="shared", thread_id="thread-root")), encoding="utf-8")
    runtime = proxy.Runtime(config.load(str(path)))
    for backend in runtime.accounts.backends:
        runtime.accounts.managers[backend] = backend.credits = FakeManager(50.0)
    runtime.credits = runtime.accounts.managers[runtime.accounts.backends[0]]
    try:
        primary = journal.tag("acc-a")
        for call in (lambda: runtime.admin._patch_account(primary, {"enabled": False}),
                     lambda: runtime.admin._delete_account(primary)):
            with pytest.raises(apierr.ApiError) as caught:
                call()
            assert caught.value.status == 409
        assert runtime.pool.backend.cfg["enabled"] is True and len(json.loads(path.read_text())["accounts"]) == 2
    finally:
        runtime.loop.call_soon_threadsafe(runtime.loop.stop)


def test_disabled_accounts_stay_off_the_network(pool, monkeypatch):
    assert pool.call("/admin/api/accounts/" + pool.tags[1], {"enabled": False}, method="PATCH")[0] == 200
    fetched = []
    monkeypatch.setattr(pool.managers[1], "get_balance", lambda force_refresh=False: fetched.append(force_refresh))
    for route in ("/admin/api/accounts", "/admin/api/overview"):
        assert pool.call(route)[0] == 200
    status, health = pool.call("/health")
    assert status == 200 and fetched == []
    assert accounts(pool)["accounts"][1]["available"] == 60.0  # the cached figure, not a fetch


def test_no_route_on_enabled_accounts_is_named_as_such(tmp_path):
    import models
    _, sessions, _ = setup(tmp_path, transport="direct", project_context=False)
    sessions.backends[0].cfg["enabled"] = False
    sessions.backends.append(Backend(tmp_path, provider_resource_name=""))
    entry = next(item for item in models.catalog() if item["provider"] == "anthropic")
    with pytest.raises(apierr.ApiError) as caught:
        sessions._next_backend(entry)
    assert caught.value.status == 503 and "no enabled account has a route" in caught.value.message


@pytest.mark.parametrize("field,value", [("priority", 100), ("priority", "1"), ("enabled", "yes"),
                                         ("label", "x" * 41)])
def test_config_rejects_invalid_pool_fields(tmp_path, field, value):
    saved = _saved(tmp_path)
    saved["accounts"][1][field] = value
    path = tmp_path / "config.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(apierr.ApiError) as caught:
        config.load(str(path))
    assert field in caught.value.message


def test_root_pool_fields_do_not_leak_into_accounts(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_saved(tmp_path, enabled=False, priority=9)), encoding="utf-8")
    runtime = proxy.Runtime(config.load(str(path)))
    try:
        assert [(b.cfg["enabled"], b.cfg["priority"]) for b in runtime.pool.backends] == [(True, 0), (True, 0)]
    finally:
        runtime.loop.call_soon_threadsafe(runtime.loop.stop)


def test_every_setting_has_a_group_a_real_description_and_units(admin_relay):
    _, body, _ = admin_relay.request("/admin/api/settings")
    schema = body["schema"]
    for key in ("min_account_balance", "account_balance_interval", "account_refresh_parallel",
                "upstream_empty_retries", "upstream_error_max_dumps", "upstream_attempt_summaries_max",
                "cache_keepwarm_seconds", "cache_keepwarm_idle"):
        assert key in schema, key
    for key, field in schema.items():
        assert field["group"] in admin.SETTING_GROUPS, key
        assert len(field["description"]) > 25 and "перезапуск" not in field["description"], key
    assert schema["min_account_balance"]["unit"] == "кредитов" and schema["turn_timeout"]["unit"] == "с"
    assert set(body["settings"]) == set(schema)


def test_reserve_threshold_and_balance_interval_apply_without_a_restart(admin_relay):
    status, body, _ = admin_relay.patch({"min_account_balance": 50, "account_balance_interval": 120,
                                         "account_refresh_parallel": 5})
    assert status == 200
    assert set(body["applied"]) == {"min_account_balance", "account_balance_interval"}
    assert body["pending_restart"] == ["account_refresh_parallel"]
    dispatcher_ = admin_relay.runtime.accounts
    assert dispatcher_.threshold == 50.0 and dispatcher_.balance_interval == 120.0
