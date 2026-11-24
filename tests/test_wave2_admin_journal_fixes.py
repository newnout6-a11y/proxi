"""Regression tests for Wave 2 Admin API, Secret Masking, Transaction Serialization, Journal & Account Report fixes."""
from __future__ import annotations

import builtins
import json
import socket
import threading
import time

import account_report
import admin
import drivers.http_pool as http_pool
import journal
from accounts import AccountDispatcher
from catalog import admin_ops, discovery, router
from drivers import balance_checkers
from test_account_dispatcher import Account, Clock
from test_admin import admin_relay  # noqa: F401 - single-account fixture
from test_admin_pool import NEW, clean_environment, pool  # noqa: F401 - pool fixture
from test_review_fixes_proxy import FakeManager
from test_stage5_admin_api import gateway_admin, mock_upstream  # noqa: F401 - gateway fixtures


# -- HIGH-10: Pending Retool credentials & proxy_url usernames ----------------

def test_pending_retool_add_failure_masks_all_credentials_and_proxy_userinfo(pool, monkeypatch):
    journal.JOURNAL.reset()
    cookie_tok = "RetoolPendingSessionCookie9988"
    xsrf_tok = "RetoolPendingXsrfSecret7766"
    page_uuid = "99999999-8888-7777-6666-555555555555"
    base = "https://secret-pending-tenant.retool.com"
    proxy_user = "ProxyPendingUser88"
    proxy_pass = "ProxyPendingPass88"
    proxy_url = f"socks5://{proxy_user}:{proxy_pass}@127.0.0.1:1080"

    def fail_adopt(record):
        raise RuntimeError(
            f"adopt failed for {record['cookie']} {record['xsrf']} {record['page_uuid']} "
            f"{record['base']} {record['proxy_url']}"
        )

    monkeypatch.setattr(pool.runtime.pool, "add_account", fail_adopt)
    payload = {
        "label": "pending-retool",
        "base": base,
        "page_uuid": page_uuid,
        "cookie": f"accessToken={cookie_tok}; xsrfToken={xsrf_tok}; theme=light",
        "xsrf": xsrf_tok,
        "proxy_url": proxy_url,
    }
    status, body = pool.call("/admin/api/accounts", payload, method="POST")
    assert status == 200
    assert body["restart_required"] is True
    assert body["account"]["pending"] == "added"

    leaked_checks = (cookie_tok, xsrf_tok, page_uuid, base, proxy_user, proxy_pass)
    resp_text = json.dumps(body, ensure_ascii=False)
    events_status, events_body = pool.call("/admin/api/events")
    assert events_status == 200
    events_text = json.dumps(events_body, ensure_ascii=False)
    buf_text = json.dumps(list(journal.JOURNAL._events), ensure_ascii=False)
    for secret in leaked_checks:
        assert secret not in resp_text, f"{secret} leaked in response"
        assert secret not in events_text, f"{secret} leaked in /admin/api/events"
        assert secret not in buf_text, f"{secret} leaked in journal buffer"

    # Short cookie values ("light") must not pollute _extra_secrets and mangle normal text.
    assert "light" not in (pool.runtime.cfg.get("_extra_secrets") or [])

    # Even a fresh Admin instance (like after restart before adoption) masks saved pending Retool credentials.
    fresh_admin = admin.Admin(pool.runtime)
    saved_tokens = fresh_admin._saved_tokens()
    for secret in leaked_checks:
        assert secret in saved_tokens or any(secret in tok for tok in saved_tokens)
        assert secret not in fresh_admin._safe(f"err: {secret}")


def test_collect_extra_secrets_and_journal_configured_secrets_include_proxy_usernames(tmp_path):
    saved = {
        "proxy_url": "http://RootProxyUser1:RootProxyPass1@127.0.0.1:8080",
        "accounts": [
            {"account_id": "a1", "proxy_url": "socks5://AccProxyUser2:AccProxyPass2@127.0.0.1:1080"},
        ],
        "providers": [
            {
                "id": "p1",
                "proxy_url": "socks5h://ProvProxyUser3:ProvProxyPass3@127.0.0.1:1081",
                "credentials": [
                    {"api_key": "sk-cred-key-123456", "proxy_url": "http://CredProxyUser4:CredProxyPass4@127.0.0.1:1082"},
                ],
            }
        ],
    }
    extra = admin_ops.collect_extra_secrets(saved)
    for expected in (
        "RootProxyUser1", "RootProxyPass1",
        "AccProxyUser2", "AccProxyPass2",
        "ProvProxyUser3", "ProvProxyPass3",
        "CredProxyUser4", "CredProxyPass4",
    ):
        assert expected in extra, expected

    j = journal.Journal()
    j.configure(saved, str(tmp_path / "j.jsonl"), load=False)
    ev = j.emit(
        "probe",
        "RootProxyUser1 RootProxyPass1 AccProxyUser2 AccProxyPass2 ProvProxyUser3 ProvProxyPass3 CredProxyUser4 CredProxyPass4",
    )
    for expected in (
        "RootProxyUser1", "RootProxyPass1",
        "AccProxyUser2", "AccProxyPass2",
        "ProvProxyUser3", "ProvProxyPass3",
        "CredProxyUser4", "CredProxyPass4",
    ):
        assert expected not in ev["message"], expected


def test_patch_account_proxy_masks_username_and_password(pool):
    journal.JOURNAL.reset()
    tag = pool.tags[0]
    status, body = pool.call(
        "/admin/api/accounts/" + tag,
        {"proxy_url": "socks5h://PatchedProxyUser11:PatchedProxyPass11@127.0.0.1:9050"},
        method="PATCH",
    )
    assert status == 200
    assert "PatchedProxyUser11" not in json.dumps(body)
    assert "PatchedProxyPass11" not in json.dumps(body)
    assert "PatchedProxyUser11" not in pool.runtime.admin._safe("auth PatchedProxyUser11 failed")
    ev = journal.JOURNAL.emit("probe", "fail PatchedProxyUser11 PatchedProxyPass11")
    assert "PatchedProxyUser11" not in ev["message"] and "PatchedProxyPass11" not in ev["message"]


# -- HIGH-11: Longest-first regex replacement in Journal._clean() --------------

def test_journal_clean_longest_first_never_leaks_prefix_secret_tails(tmp_path):
    short_secret = "sk-prefix-1234"
    long_secret = "sk-prefix-1234-SENSITIVE-SUFFIX-9876"
    sub_secret = "inner-secret"
    outer_secret = "outer-inner-secret-tail"

    cfg = {
        "relay_api_key": short_secret,
        "api_key": sub_secret,
        "_extra_secrets": [short_secret, long_secret, sub_secret, outer_secret],
    }
    secrets = journal.configured_secret_values(cfg)
    assert secrets.index(long_secret) < secrets.index(short_secret)
    assert secrets.index(outer_secret) < secrets.index(sub_secret)

    j = journal.Journal()
    j.configure(cfg, str(tmp_path / "j.jsonl"), load=False)
    ev = j.emit("probe", f"saw {long_secret} and {outer_secret}")
    assert "SENSITIVE-SUFFIX-9876" not in ev["message"]
    assert "-tail" not in ev["message"]
    assert ev["message"] == "saw [redacted] and [redacted]"

    # Even if a plain unsorted list is passed directly to _clean, longest match still wins.
    cleaned = j._clean(f"raw {long_secret}", [short_secret, long_secret])
    assert cleaned == "raw [redacted]"


# -- HIGH-15: Serialization under admin.lock + _mask_tokens lock + _saved() retry --

def test_live_mutations_hold_admin_lock_and_serialize_concurrent_ops(gateway_admin, mock_upstream, monkeypatch):
    adm = gateway_admin.runtime.admin
    lock_held = {}

    orig_replace = admin_ops._replace_provider_backends_live
    orig_build_aliases = router.build_route_aliases
    orig_reload_keys = gateway_admin.runtime.client_keys.reload

    def check_lock(name):
        # Acquire from a separate thread to verify admin.lock (RLock) is exclusively held by request thread.
        acquired = []
        t = threading.Thread(target=lambda: acquired.append(adm.lock.acquire(blocking=False)))
        t.start()
        t.join(5)
        if acquired and acquired[0]:
            # Must not happen; release cleanly before failing.
            raise AssertionError(f"{name} ran without holding admin.lock")
        lock_held[name] = True

    def wrapped_replace(admin_obj, pid, records):
        check_lock("replace_provider_backends_live")
        return orig_replace(admin_obj, pid, records)

    def wrapped_aliases(cfg, mod):
        check_lock("build_route_aliases")
        return orig_build_aliases(cfg, mod)

    def wrapped_reload(entries):
        check_lock("client_keys.reload")
        return orig_reload_keys(entries)

    monkeypatch.setattr(admin_ops, "_replace_provider_backends_live", wrapped_replace)
    monkeypatch.setattr(router, "build_route_aliases", wrapped_aliases)
    monkeypatch.setattr(gateway_admin.runtime.client_keys, "reload", wrapped_reload)

    st, _ = gateway_admin.admin_call(
        "/admin/api/providers",
        {
            "id": "p1",
            "driver": "openai-chat",
            "base_url": mock_upstream,
            "api_key": "sk-live-secret-provider-key-9999",
            "discover_now": False,
            "auto_discover": False,
        },
        method="POST",
    )
    assert st == 200 and lock_held.get("replace_provider_backends_live") is True

    st, _ = gateway_admin.admin_call(
        "/admin/api/routes",
        {"id": "r1", "targets": ["p1/gpt-4o"]},
        method="POST",
    )
    assert st == 200 and lock_held.get("build_route_aliases") is True

    st, ck = gateway_admin.admin_call("/admin/api/client-keys", {"label": "k1"}, method="POST")
    assert st == 200 and lock_held.get("client_keys.reload") is True

    # Concurrent create/delete provider race must never leave deleted provider resurrected in live state.
    monkeypatch.setattr(admin_ops, "_replace_provider_backends_live", orig_replace)
    for round_idx in range(6):
        pid = f"race-{round_idx}"
        create_payload = {
            "id": pid,
            "driver": "openai-chat",
            "base_url": mock_upstream,
            "api_key": "sk-live-secret-provider-key-9999",
            "discover_now": False,
            "auto_discover": False,
        }
        barrier = threading.Barrier(2)
        results = {}

        def do_create():
            barrier.wait()
            results["create"] = gateway_admin.admin_call("/admin/api/providers", create_payload, method="POST")[0]

        def do_delete():
            barrier.wait()
            results["delete"] = gateway_admin.admin_call(f"/admin/api/providers/{pid}", method="DELETE")[0]

        t1 = threading.Thread(target=do_create)
        t2 = threading.Thread(target=do_delete)
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)
        saved_ids = {p["id"] for p in json.loads(gateway_admin.cfg_path.read_text(encoding="utf-8"))["providers"]}
        meta_ids = set((gateway_admin.runtime.cfg.get("_providers_meta") or {}).keys())
        live_ids = {b.cfg.get("provider_id") for b in gateway_admin.runtime.pool.backends if b.cfg.get("provider_id")}
        assert saved_ids == meta_ids == live_ids

    # Clean up key
    gateway_admin.admin_call(f"/admin/api/client-keys/{ck['client_key']['id']}", method="DELETE")


def test_account_adopt_and_drop_hold_admin_lock_and_saved_retries_permission_error(pool, monkeypatch):
    adm = pool.runtime.admin
    held = {}
    orig_adopt = adm._adopt
    orig_drop = adm._drop

    def assert_locked(name):
        acquired = []
        t = threading.Thread(target=lambda: acquired.append(adm.lock.acquire(blocking=False)))
        t.start()
        t.join(5)
        assert not (acquired and acquired[0]), f"{name} ran without holding admin.lock"
        held[name] = True

    monkeypatch.setattr(adm, "_adopt", lambda rec: assert_locked("_adopt") or orig_adopt(rec))
    monkeypatch.setattr(adm, "_drop", lambda backend: assert_locked("_drop") or orig_drop(backend))

    st, body = pool.call("/admin/api/accounts", NEW, method="POST")
    assert st == 200 and held.get("_adopt") is True
    st, _ = pool.call("/admin/api/accounts/" + body["account"]["account"], {}, method="DELETE")
    assert st == 200 and held.get("_drop") is True

    # _mask_tokens concurrent stress
    errs = []

    def add_toks(prefix):
        try:
            for i in range(40):
                adm._mask_tokens([f"{prefix}-secret-token-{i:04d}"])
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=add_toks, args=(f"t{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errs
    for n in range(4):
        assert f"t{n}-secret-token-0039" in adm.cfg["_extra_secrets"]

    # _saved() retries transient Windows PermissionError
    real_open = builtins.open
    attempts = {"n": 0}
    target_path = str(pool.path)

    def flaky_open(file, *args, **kwargs):
        if str(file) == target_path and attempts["n"] < 2:
            attempts["n"] += 1
            raise PermissionError(13, "transient sharing violation")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)
    loaded = adm._saved()
    assert isinstance(loaded, dict) and attempts["n"] == 2


# -- MEDIUM #29: Single-account promotion & account_id cleanup ----------------

def test_single_account_promotion_clears_root_id_and_decouples_backend_cfg(admin_relay, monkeypatch):
    monkeypatch.setattr(admin_relay.runtime, "account_manager", lambda backend: FakeManager(80.0), raising=False)
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    saved["account_id"] = "legacy-root-id"
    admin_relay.path.write_text(json.dumps(saved), encoding="utf-8")

    headers = {"x-admin-request": "1", "origin": admin_relay.url}
    status, added, _ = admin_relay.request("/admin/api/accounts", NEW, method="POST", headers=headers)
    assert status == 200

    saved_after = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    assert "account_id" not in saved_after
    assert saved_after["accounts"][0]["account_id"] == "legacy-root-id"

    _, listing, _ = admin_relay.request("/admin/api/accounts")
    first_tag = listing["accounts"][0]["account"]
    backend0 = admin_relay.runtime.pool.backends[0]
    assert backend0.cfg is admin_relay.runtime.cfg  # initially shared before patch

    closed_scopes = []
    monkeypatch.setattr(http_pool, "close_account_clients", lambda scope: closed_scopes.append(scope))
    # Simulate an account whose backend.cfg lacked account_id before decoupling, falling back safely.
    admin_relay.runtime.cfg.pop("account_id", None)

    status, patched, _ = admin_relay.request(
        f"/admin/api/accounts/{first_tag}",
        {
            "label": "first-decoupled",
            "priority": 7,
            "proxy_url": "socks5://Acc1User99:Acc1Pass99@127.0.0.1:1080",
            "impersonate": "chrome150",
        },
        method="PATCH",
        headers=headers,
    )
    assert status == 200
    assert patched["account"]["label"] == "first-decoupled"
    assert backend0.cfg is not admin_relay.runtime.cfg
    assert backend0.refresher.cfg is backend0.cfg
    assert backend0.cfg["label"] == "first-decoupled"
    assert backend0.cfg["priority"] == 7
    assert backend0.cfg["proxy_url"] == "socks5://Acc1User99:Acc1Pass99@127.0.0.1:1080"
    assert "label" not in admin_relay.runtime.cfg
    assert "priority" not in admin_relay.runtime.cfg
    assert admin_relay.runtime.cfg.get("proxy_url") == ""
    assert admin_relay.runtime.cfg.get("impersonate") == "chrome152"
    assert closed_scopes and closed_scopes[-1] == "legacy-root-id"


# -- MEDIUM #24: Client key Minara limits & strict enabled bool ---------------

def test_client_key_crud_preserves_minara_limits_and_rejects_non_bool_enabled(gateway_admin):
    # "false" string must be rejected with 400, not coerced to True!
    for bad_enabled in ("false", "true", 0, 1, None):
        st, body = gateway_admin.admin_call(
            "/admin/api/client-keys",
            {"label": "bad-bool", "enabled": bad_enabled},
            method="POST",
        )
        assert st == 400, (bad_enabled, body)

    st, created = gateway_admin.admin_call(
        "/admin/api/client-keys",
        {
            "label": "Agent Key",
            "enabled": False,
            "rpm_limit": 20,
            "max_budget_usd": 2.5,
            "max_request_minara": 100.5,
            "max_day_minara": 500,
            "allow_cost_confirm": True,
        },
        method="POST",
    )
    assert st == 200, created
    ck = created["client_key"]
    kid = ck["id"]
    assert ck["enabled"] is False
    assert ck["max_request_minara"] == 100.5
    assert ck["max_day_minara"] == 500.0
    assert ck["allow_cost_confirm"] is True

    # Invalid Minara limits or allow_cost_confirm rejected with 400
    assert gateway_admin.admin_call(
        "/admin/api/client-keys",
        {"label": "bad-minara", "max_request_minara": -5},
        method="POST",
    )[0] == 400
    assert gateway_admin.admin_call(
        "/admin/api/client-keys",
        {"label": "bad-confirm", "allow_cost_confirm": "true"},
        method="POST",
    )[0] == 400

    # PATCH updates Minara limits and allow_cost_confirm
    st, updated = gateway_admin.admin_call(
        f"/admin/api/client-keys/{kid}",
        {"enabled": True, "max_request_minara": 50, "max_day_minara": None, "allow_cost_confirm": False},
        method="PATCH",
    )
    assert st == 200, updated
    assert updated["client_key"]["enabled"] is True
    assert updated["client_key"]["max_request_minara"] == 50.0
    assert updated["client_key"]["max_day_minara"] is None
    assert updated["client_key"]["allow_cost_confirm"] is False


# -- MEDIUM #25 + #32 server side: _merge_provider_patch ----------------------

def test_provider_patch_clears_options_apikey_preserves_cred_proxy_and_resets_preset(gateway_admin, mock_upstream, monkeypatch):
    monkeypatch.setenv("WAVE2_PROV_KEY", "sk-live-secret-provider-key-9999")
    seen_discovery_cfgs = []

    async def fake_discover(cfg, *, timeout=None):
        seen_discovery_cfgs.append(dict(cfg))
        return [{"id": "stub-model"}]

    monkeypatch.setattr(discovery, "discover_provider_models", fake_discover)
    monkeypatch.setattr(balance_checkers.ProviderBalanceAccount, "_http_get_balance", lambda self: {})

    # 1. OpenCode-style provider with options.apiKey switched to api_key_env
    st, _ = gateway_admin.admin_call(
        "/admin/api/providers",
        {
            "id": "opencode-prov",
            "driver": "openai-chat",
            "base_url": mock_upstream,
            "options": {"apiKey": "sk-stale-inline-opencode-key", "headers": {"X-Keep": "1"}},
            "discover_now": False,
            "auto_discover": False,
        },
        method="POST",
    )
    assert st == 200
    st, patched = gateway_admin.admin_call(
        "/admin/api/providers/opencode-prov",
        {"api_key_env": "WAVE2_PROV_KEY"},
        method="PATCH",
    )
    assert st == 200, patched
    saved = json.loads(gateway_admin.cfg_path.read_text(encoding="utf-8"))
    prov_disk = next(p for p in saved["providers"] if p["id"] == "opencode-prov")
    assert prov_disk["api_key_env"] == "WAVE2_PROV_KEY"
    assert "apiKey" not in (prov_disk.get("options") or {})
    assert prov_disk.get("options", {}).get("headers") == {"X-Keep": "1"}

    # 2. Multi-credential provider preserves saved proxy_url & api_key when masked placeholders are sent back
    st, created_multi = gateway_admin.admin_call(
        "/admin/api/providers",
        {
            "id": "multi-cred",
            "driver": "openai-chat",
            "auth_mode": "bearer",
            "base_url": mock_upstream,
            "credentials": [
                {"label": "C1", "api_key": "sk-cred-one-secret-0001", "proxy_url": "socks5h://u1:Cred1ProxyPass@127.0.0.1:1080"},
                {"label": "C2", "api_key": "sk-cred-two-secret-0002", "proxy_url": "socks5h://u2:Cred2ProxyPass@127.0.0.1:1081"},
            ],
            "discover_now": False,
            "auto_discover": False,
        },
        method="POST",
    )
    assert st == 200
    masked_creds = created_multi["provider"]["credentials"]
    st, patched_multi = gateway_admin.admin_call(
        "/admin/api/providers/multi-cred",
        {
            "credentials": [
                {"index": 1, "label": "C1-edited", "proxy_url": masked_creds[0]["proxy_url"]},
                {"index": 2, "label": "C2-edited", "api_key": masked_creds[1]["api_key_preview"], "proxy_url": masked_creds[1]["proxy_url"]},
            ]
        },
        method="PATCH",
    )
    assert st == 200, patched_multi
    saved_multi = next(p for p in json.loads(gateway_admin.cfg_path.read_text(encoding="utf-8"))["providers"] if p["id"] == "multi-cred")
    assert saved_multi["credentials"][0]["api_key"] == "sk-cred-one-secret-0001"
    assert saved_multi["credentials"][0]["proxy_url"] == "socks5h://u1:Cred1ProxyPass@127.0.0.1:1080"
    assert saved_multi["credentials"][1]["api_key"] == "sk-cred-two-secret-0002"
    assert saved_multi["credentials"][1]["proxy_url"] == "socks5h://u2:Cred2ProxyPass@127.0.0.1:1081"

    # 3. Preset provider moved to non-preset base_url (with or without explicit preset: "custom") resets preset to "custom"
    st, _ = gateway_admin.admin_call(
        "/admin/api/providers",
        {
            "id": "or-edit",
            "preset": "openrouter",
            "api_key": "sk-or-v1-secret-openrouter-key-1234",
            "discover_now": False,
            "auto_discover": False,
        },
        method="POST",
    )
    assert st == 200
    # Move base_url without explicit preset -> must NOT retain "openrouter" or openrouter balance_endpoint!
    st, moved = gateway_admin.admin_call(
        "/admin/api/providers/or-edit",
        {"base_url": mock_upstream},
        method="PATCH",
    )
    assert st == 200, moved
    assert moved["provider"]["preset"] == "custom"
    assert moved["provider"]["balance_supported"] is False
    meta = gateway_admin.runtime.cfg["_providers_meta"]["or-edit"]
    assert meta["preset"] == "custom" and meta["balance_endpoint"] is None

    # Explicit preset: "custom" or "" even when base_url matches preset host resets preset to "custom"
    st, restored = gateway_admin.admin_call(
        "/admin/api/providers/or-edit",
        {"preset": "openrouter", "base_url": "https://openrouter.ai/api/v1"},
        method="PATCH",
    )
    assert st == 200 and restored["provider"]["preset"] == "openrouter"
    st, forced_custom = gateway_admin.admin_call(
        "/admin/api/providers/or-edit",
        {"preset": "custom"},
        method="PATCH",
    )
    assert st == 200 and forced_custom["provider"]["preset"] == "custom"
    assert forced_custom["provider"]["base_url"] == "https://openrouter.ai/api/v1"
    assert forced_custom["provider"]["balance_supported"] is False
    meta = gateway_admin.runtime.cfg["_providers_meta"]["or-edit"]
    assert meta["preset"] == "custom" and meta["balance_endpoint"] is None

    st, restored2 = gateway_admin.admin_call(
        "/admin/api/providers/or-edit",
        {"preset": "openrouter"},
        method="PATCH",
    )
    assert st == 200 and restored2["provider"]["preset"] == "openrouter"
    st, empty_preset = gateway_admin.admin_call(
        "/admin/api/providers/or-edit",
        {"preset": ""},
        method="PATCH",
    )
    assert st == 200 and empty_preset["provider"]["preset"] == "custom"
    assert empty_preset["provider"]["base_url"] == "https://openrouter.ai/api/v1"
    meta = gateway_admin.runtime.cfg["_providers_meta"]["or-edit"]
    assert meta["preset"] == "custom" and meta["balance_endpoint"] is None

    # Dry-run test_provider_connection on multi-cred with masked proxy_url & on preset moved off host
    seen_discovery_cfgs.clear()
    st, probe_multi = gateway_admin.admin_call(
        "/admin/api/providers/test",
        {
            "provider_id": "multi-cred",
            "credentials": [{"index": 1, "proxy_url": masked_creds[0]["proxy_url"]}],
        },
        method="POST",
    )
    assert st == 200 and probe_multi["ok"] is True
    assert "Cred1ProxyPass" not in json.dumps(probe_multi)
    assert seen_discovery_cfgs[0]["proxy_url"] == "socks5h://u1:Cred1ProxyPass@127.0.0.1:1080"
    st, probe_custom = gateway_admin.admin_call(
        "/admin/api/providers/test",
        {"provider_id": "or-edit", "base_url": mock_upstream},
        method="POST",
    )
    assert st == 200 and probe_custom["ok"] is True and probe_custom["preset"] == "custom"


# -- MEDIUM #30: Journal.query() / Journal.turns() on malformed JSONL ---------

def test_journal_query_and_turns_survive_malformed_restored_jsonl(tmp_path, admin_relay):
    path = tmp_path / "journal.jsonl"
    lines = [
        json.dumps({"seq": True, "kind": "turn.begin", "turn_id": "bad-bool"}),
        json.dumps({"seq": 1, "message": "partial dict without kind level category ts"}),
        json.dumps({"seq": 2, "kind": "turn.begin", "turn_id": "t-partial", "data": "not-a-dict"}),
        json.dumps({"seq": 3, "kind": "http.request", "request_id": "req-1", "data": ["not-a-dict"]}),
        json.dumps({"seq": 4, "kind": "turn.end", "turn_id": {"unhashable": 1}, "data": {"reason": "stop"}}),
        json.dumps({"seq": 5, "kind": "turn.end", "turn_id": "t-partial", "ts": 123.4, "level": "error"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    j = journal.Journal()
    j.configure({}, str(path), load=True)

    all_events = j.query()
    assert [e["seq"] for e in all_events] == [1, 2, 3, 4, 5]
    assert [e["seq"] for e in j.query(order="asc")] == [1, 2, 3, 4, 5]
    assert j.query(levels={"error"}) == [all_events[-1]]
    assert j.query(levels={"error"}, order="asc") == [all_events[-1]]
    assert j.query(categories={"http"}) == []
    assert j.query(categories={"http"}, order="asc") == []

    traces = j.turns()
    assert len(traces) == 1
    assert traces[0]["turn_id"] == "t-partial"
    assert traces[0]["ended"] == 123.4
    assert traces[0]["errors"] == 1

    # Also verify HTTP endpoints /admin/api/events and /admin/api/turns return 200 on malformed entries
    with admin_relay.runtime.journal._cond:
        admin_relay.runtime.journal._events.clear()
        admin_relay.runtime.journal._events.extend(all_events)
        admin_relay.runtime.journal._seq = 5
    st_ev, ev_body, _ = admin_relay.request("/admin/api/events?level=error&category=turn")
    assert st_ev == 200 and ev_body["data"] == []
    st_tu, tu_body, _ = admin_relay.request("/admin/api/turns")
    assert st_tu == 200 and tu_body["data"][0]["turn_id"] == "t-partial"


# -- MEDIUM #31 server side: Journal SSE reset race ---------------------------

def test_journal_sse_stream_delivers_reset_even_when_since_equals_1(admin_relay, monkeypatch):
    journal.JOURNAL.reset()
    journal.emit("probe", "first-before-clear", "info")  # seq == 1
    monkeypatch.setattr(admin, "STREAM_MAX_SECONDS", 5)

    port = int(admin_relay.url.rsplit(":", 1)[1])
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.settimeout(3.0)
    try:
        sock.sendall(
            b"GET /admin/api/events/stream?since=0 HTTP/1.1\r\nHost: x\r\n"
            b"Authorization: Bearer private-relay-key\r\n\r\n"
        )
        data = b""
        while b"first-before-clear" not in data:
            chunk = sock.recv(4096)
            assert chunk
            data += chunk

        # Now server stream has since == 1. Clear resets _seq -> 0 and emits admin.journal_cleared (seq == 1)!
        st, body, _ = admin_relay.request(
            "/admin/api/events/clear",
            {},
            method="POST",
            headers={"x-admin-request": "1", "origin": admin_relay.url},
        )
        assert st == 200 and body["reset_epoch"] >= 1

        deadline = time.monotonic() + 3.0
        while (b"event: reset" not in data or b"admin.journal_cleared" not in data) and time.monotonic() < deadline:
            chunk = sock.recv(4096)
            assert chunk
            data += chunk
        decoded = data.decode("utf-8")
        assert "event: reset" in decoded
        assert "admin.journal_cleared" in decoded
        assert decoded.index("event: reset") < decoded.index("admin.journal_cleared")
    finally:
        sock.close()


# -- LOWs: account_report snapshot lock & _loggable surrogate handling --------

def test_account_report_states_never_zip_mismatched_after_concurrent_remove():
    a, b, c = Account("a"), Account("b"), Account("c")
    disp = AccountDispatcher(
        [a, b, c],
        {"session_refresh_interval": 0},
        {a: FakeManager(10.0), b: FakeManager(20.0), c: FakeManager(30.0)},
        clock=Clock(),
    )
    disp.state[a]["auth_failures"] = 0
    disp.state[b]["auth_failures"] = 9
    disp.state[c]["auth_failures"] = 5
    rt = type("Rt", (), {"accounts": disp, "backend": b, "credits": disp.managers[b], "cfg": {}})()

    states = account_report.account_states(rt)
    assert states[a]["auth_failures"] == 0
    assert states[b]["auth_failures"] == 9
    assert states[c]["auth_failures"] == 5

    # Concurrent remove/add stress must never crash or mismatch backend -> auth_failures
    stop = threading.Event()
    errors = []

    def mutator():
        toggle = False
        while not stop.is_set():
            if toggle:
                disp.add_account(a, FakeManager(10.0))
            else:
                disp.remove_account(a)
            toggle = not toggle

    def reader():
        while not stop.is_set():
            try:
                snap = account_report.account_states(rt)
                if a in snap:
                    assert snap[a]["auth_failures"] == 0
                assert snap[b]["auth_failures"] == 9
                assert snap[c]["auth_failures"] == 5
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    t1 = threading.Thread(target=mutator)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    time.sleep(0.15)
    stop.set()
    t1.join(5)
    t2.join(5)
    assert not errors


def test_clearing_surrogate_relay_system_prompt_does_not_500_in_loggable(admin_relay):
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    saved["relay_system_prompt"] = "bad surrogate \ud800"
    admin_relay.path.write_text(json.dumps(saved), encoding="utf-8")

    journal.JOURNAL.reset()
    status, body, _ = admin_relay.patch({"relay_system_prompt": ""})
    assert status == 200, body
    assert body["settings"]["relay_system_prompt"] == ""
    assert json.loads(admin_relay.path.read_text(encoding="utf-8"))["relay_system_prompt"] == ""
    events = [e for e in journal.JOURNAL.query() if e["kind"] == "admin.settings"]
    assert events and events[-1]["data"]["changed"]["relay_system_prompt"]["from"]["length"] == len("bad surrogate \ud800")
