"""Regression tests for Wave 2 security, redaction, persistence, cost guard, and credits fixes."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import auth
import client_context
import compaction_tools
import config
import cost_guard
import credits
import journal
import redact
from session import Backend, SessionPool
from upstream_refresh import Refresher


class _Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


# 1. HIGH-7: Guard.warm_prefix cold-cache bypass
def test_warm_prefix_does_not_fall_back_to_session_prefix_on_expiry_or_mismatch():
    clock = _Clock()
    guard = cost_guard.Guard(clock=clock)
    guard.spent(1.2, conversation="conv-1", account="acc-1", model="minara/gpt-6-astra", prompt_tokens=250_000)

    # Same account/model inside WARM_OWN is warm.
    assert guard.warm_prefix(
        "conv-1", model="minara/gpt-6-astra", account="acc-1", shared=False, session_prefix=250_000
    ) == 250_000

    # Non-Claude failover to another account must be cold (0), even with non-zero session_prefix.
    assert guard.warm_prefix(
        "conv-1", model="minara/gpt-6-astra", account="acc-2", shared=False, session_prefix=250_000
    ) == 0

    # Model switch must be cold (0), even with non-zero session_prefix.
    assert guard.warm_prefix(
        "conv-1", model="minara/claude-opus-5-5", account="acc-1", shared=True, session_prefix=250_000
    ) == 0

    # TTL expiry for non-Claude (WARM_OWN) must be cold (0) even at exact boundary.
    clock.now += cost_guard.WARM_OWN
    assert guard.warm_prefix(
        "conv-1", model="minara/gpt-6-astra", account="acc-1", shared=False, session_prefix=250_000
    ) == 0

    # Shared Claude cache: warm before WARM_SHARED, cold at/after WARM_SHARED even with session_prefix.
    guard.spent(0.5, conversation="conv-2", account="acc-1", model="minara/claude-opus-5-5", prompt_tokens=180_000)
    assert guard.warm_prefix(
        "conv-2", model="minara/claude-opus-5-5", account="acc-2", shared=True, session_prefix=180_000
    ) == 180_000
    clock.now += cost_guard.WARM_SHARED
    assert guard.warm_prefix(
        "conv-2", model="minara/claude-opus-5-5", account="acc-2", shared=True, session_prefix=180_000
    ) == 0

    # Unseen conversation (after restart) still honors restored session_prefix.
    assert guard.warm_prefix(
        "restored-conv", model="minara/gpt-6-astra", account="acc-1", shared=False, session_prefix=90_000
    ) == 90_000


# 2. HIGH-8: Guard._save concurrency, unique temp files, Windows os.replace retry & cleanup
def test_cost_guard_save_is_atomic_concurrent_and_retries_replace(tmp_path, monkeypatch):
    path = str(tmp_path / cost_guard.STATE_NAME)
    guard = cost_guard.Guard(path, clock=_Clock())

    # Concurrent spent() calls must serialize cleanly, preserve sum, and leave no temp files.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                guard.spent, 1.0, key_id="agent-k", conversation=f"c-{i}", account="a", model="m", prompt_tokens=10
            )
            for i in range(40)
        ]
        for fut in futures:
            fut.result()

    assert guard.key_spent("agent-k") == 40.0
    saved = json.loads((tmp_path / cost_guard.STATE_NAME).read_text(encoding="utf-8"))
    assert saved["keys"]["agent-k"]["credits"] == 40.0
    assert [p.name for p in tmp_path.iterdir()] == [cost_guard.STATE_NAME]

    # Transient PermissionError on Windows os.replace retries and succeeds.
    real_replace = cost_guard.os.replace
    attempts = []

    def flaky_replace(src, dst):
        attempts.append(src)
        if len(attempts) < 3:
            raise PermissionError("scanner hold")
        return real_replace(src, dst)

    monkeypatch.setattr(cost_guard.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cost_guard.os, "replace", flaky_replace)
    guard.spent(5.0, key_id="agent-k")
    assert len(attempts) == 3
    assert len(set(attempts)) == 1
    assert json.loads((tmp_path / cost_guard.STATE_NAME).read_text(encoding="utf-8"))["keys"]["agent-k"]["credits"] == 45.0
    assert [p.name for p in tmp_path.iterdir()] == [cost_guard.STATE_NAME]

    # Permanent PermissionError unlinks temp file without crashing.
    monkeypatch.setattr(cost_guard.os, "replace", lambda _s, _d: (_ for _ in ()).throw(PermissionError("locked")))
    guard.spent(2.0, key_id="agent-k")
    assert guard.key_spent("agent-k") == 47.0
    assert [p.name for p in tmp_path.iterdir()] == [cost_guard.STATE_NAME]


# 3. HIGH-14: config._write_file retries Windows PermissionError on os.replace and cleans up temp
def test_config_write_file_retries_permission_error_and_cleans_tmp(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"a": 1}\n', encoding="utf-8")

    real_replace = config.os.replace
    calls = []

    def flaky_replace(src, dst):
        calls.append(src)
        if len(calls) < 3:
            raise PermissionError("antivirus lock")
        return real_replace(src, dst)

    monkeypatch.setattr(config.time, "sleep", lambda _s: None)
    monkeypatch.setattr(config.os, "replace", flaky_replace)
    config._write_file(str(path), {"a": 2})
    assert len(calls) == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
    assert not list(tmp_path.glob(".session-*.tmp"))

    # Exhausting retries raises PermissionError and still removes temp file.
    monkeypatch.setattr(config.os, "replace", lambda _s, _d: (_ for _ in ()).throw(PermissionError("still locked")))
    with pytest.raises(PermissionError):
        config._write_file(str(path), {"a": 3})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
    assert not list(tmp_path.glob(".session-*.tmp"))


# 4. HIGH-12 + MEDIUM-HIGH-13: secret redaction gaps & stale root cfg["cookie_values"]
def test_config_extracts_custom_relay_key_proxy_user_pass_and_unspaced_cookies(tmp_path, monkeypatch):
    for name in ("RELAY_API_KEY", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.json"
    custom_key = "custom-master-secret-0123456789"
    cfg_data = {
        "relay_api_key": custom_key,
        "proxy_url": "socks5h://RootProxyUser123:RootProxyPass123@127.0.0.1:1080",
        "workspace_root": str(tmp_path),
        "accounts": [
            {
                "account_id": "acc-1",
                "base": "https://one.invalid",
                "page_uuid": "page-1",
                "ua": "ua",
                "cookie": "accessToken=unspaced-tok-12345;sessionSig=unspaced-sig-67890;lastSubdomain=one.retool.com",
                "xsrf": "xsrf-1",
                "proxy_url": "http://AccProxyUser987:AccProxyPass987@127.0.0.1:8080",
            },
            {
                "account_id": "acc-2",
                "base": "https://two.invalid",
                "page_uuid": "page-2",
                "ua": "ua",
                "cookie": "accessToken=acc2-tok-abcdef012345;xsrfToken=acc2-xsrf-12345",
                "xsrf": "acc2-xsrf-12345",
                "proxy_url": "http://UserOnlyProxySecret123@127.0.0.1:8081",
            },
        ],
    }
    path.write_text(json.dumps(cfg_data), encoding="utf-8")
    cfg = config.load(str(path))

    for secret in (
        custom_key,
        "RootProxyUser123",
        "RootProxyPass123",
        "AccProxyUser987",
        "AccProxyPass987",
        "UserOnlyProxySecret123",
        "unspaced-tok-12345",
        "unspaced-sig-67890",
        "acc2-tok-abcdef012345",
    ):
        assert secret in cfg["cookie_values"], f"missing from cookie_values: {secret}"
        assert secret not in redact.redact("leak: " + secret, cookie_values=cfg["cookie_values"])
    for extra in (
        custom_key,
        "RootProxyUser123",
        "RootProxyPass123",
        "AccProxyUser987",
        "AccProxyPass987",
        "UserOnlyProxySecret123",
    ):
        assert extra in cfg["_extra_secrets"], f"missing from _extra_secrets: {extra}"
    assert "one.retool.com" not in cfg["cookie_values"]


def test_cookie_rotation_and_reload_update_root_cookie_values_without_dropping_siblings(tmp_path, monkeypatch):
    for name in ("RELAY_API_KEY", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.json"
    custom_key = "custom-relay-master-987654321"
    cfg_data = {
        "relay_api_key": custom_key,
        "workspace_root": str(tmp_path),
        "ua": "ua",
        "accounts": [
            {
                "account_id": "acc-1",
                "base": "https://a.invalid",
                "page_uuid": "p1",
                "cookie": "accessToken=acc1-initial-token; lastSubdomain=a.retool.com",
                "xsrf": "xsrf-1",
            },
            {
                "account_id": "acc-2",
                "base": "https://b.invalid",
                "page_uuid": "p2",
                "cookie": "accessToken=acc2-initial-token; lastSubdomain=b.retool.com",
                "xsrf": "xsrf-2",
            },
        ],
    }
    path.write_text(json.dumps(cfg_data), encoding="utf-8")
    root_cfg = config.load(str(path))
    pool = SessionPool(Backend(root_cfg))

    # Rotate account 0 via Refresher -> auth.merge_set_cookie
    refresher = Refresher(pool.backends[0].cfg, http_refresh=lambda: ["accessToken=acc1-rotated-token-xyz; Path=/"])
    assert refresher.refresh(force=True)
    assert "acc1-rotated-token-xyz" in root_cfg["cookie_values"]
    assert "acc2-initial-token" in root_cfg["cookie_values"]
    assert custom_key in root_cfg["cookie_values"]
    assert "a.retool.com" not in root_cfg["cookie_values"]
    assert "acc1-rotated-token-xyz" not in redact.redact("echo acc1-rotated-token-xyz", cookie_values=root_cfg["cookie_values"])
    assert "acc2-initial-token" not in redact.redact("echo acc2-initial-token", cookie_values=root_cfg["cookie_values"])

    # Simulate external sign-in on disk for account 1 -> config.reload_auth (even if backend has a separate list)
    pool.backends[1].cfg["cookie_values"] = ["isolated-list-item"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["accounts"][1]["cookie"] = "accessToken=acc2-reloaded-token-xyz; lastSubdomain=b.retool.com"
    saved["accounts"][1]["xsrf"] = "xsrf-2-new"
    path.write_text(json.dumps(saved), encoding="utf-8")

    assert config.reload_auth(pool.backends[1].cfg) is True
    assert "acc2-reloaded-token-xyz" in root_cfg["cookie_values"]
    assert "acc2-reloaded-token-xyz" in pool.backends[1].cfg["cookie_values"]
    assert "acc1-rotated-token-xyz" in root_cfg["cookie_values"]
    assert custom_key in root_cfg["cookie_values"]

    # Journal configured with root_cfg immediately masks rotated/reloaded cookies.
    j = journal.Journal()
    j.configure(root_cfg, None, load=False)
    ev = j.emit("test.redact", "saw acc1-rotated-token-xyz and acc2-reloaded-token-xyz", "info")
    assert "acc1-rotated-token-xyz" not in ev["message"]
    assert "acc2-reloaded-token-xyz" not in ev["message"]


# 5. MEDIUM #22: cleanup_unused_renewal_locks & _STORAGE_LOCKS refcounted lifecycle
def test_cleanup_unused_renewal_locks_prunes_orphans_and_retains_active(tmp_path, monkeypatch):
    for name in ("RELAY_API_KEY", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "relay_api_key": "test-relay-key-123456789012345",
        "workspace_root": str(tmp_path),
        "ua": "ua",
        "accounts": [
            {"account_id": "keep-1", "base": "https://a.invalid", "page_uuid": "p1",
             "cookie": "accessToken=tok-keep", "xsrf": "x1"},
            {"account_id": "drop-1", "base": "https://b.invalid", "page_uuid": "p2",
             "cookie": "accessToken=tok-drop", "xsrf": "x2"},
        ],
    }), encoding="utf-8")

    cfg = config.load(str(path))
    acc_keep = dict(cfg["accounts"][0], _config_path=str(path), _persist_auth=True)
    acc_drop = dict(cfg["accounts"][1], _config_path=str(path), _persist_auth=True)

    # Create both renewal lock sidecars via renewal_lock.
    with config.renewal_lock(acc_keep):
        pass
    with config.renewal_lock(acc_drop):
        pass

    # _STORAGE_LOCKS should not leak idle entries after exiting storage_lock.
    keep_key = os.path.normcase(os.path.realpath(str(path)) + ".auth-" + config._renewal_lock_tag(acc_keep))
    drop_key = os.path.normcase(os.path.realpath(str(path)) + ".auth-" + config._renewal_lock_tag(acc_drop))
    assert keep_key not in config._STORAGE_LOCKS
    assert drop_key not in config._STORAGE_LOCKS

    keep_lock = tmp_path / f"config.json.auth-{config._renewal_lock_tag(acc_keep)}.lock"
    drop_lock = tmp_path / f"config.json.auth-{config._renewal_lock_tag(acc_drop)}.lock"
    bogus_lock = tmp_path / ("config.json.auth-" + "f" * 24 + ".lock")
    bogus_lock.write_bytes(b"\0")
    unrelated = tmp_path / "config.json.auth-nothex.lock"
    unrelated.write_bytes(b"\0")
    assert keep_lock.exists() and drop_lock.exists()

    # Remove drop-1 from config, keep an active lock check in _STORAGE_LOCKS for a held orphan, and reload.
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["accounts"] = [saved["accounts"][0]]
    path.write_text(json.dumps(saved), encoding="utf-8")

    # Insert an orphan lock entry into _STORAGE_LOCKS and verify cleanup prunes it if unheld, skips if held.
    fake_orphan_tag = "a" * 24
    fake_orphan_key = os.path.normcase(os.path.realpath(str(path))) + ".auth-" + fake_orphan_tag
    fake_orphan_lock = tmp_path / f"config.json.auth-{fake_orphan_tag}.lock"
    fake_orphan_lock.write_bytes(b"\0")

    busy_tag = "b" * 24
    busy_lock_path = os.path.realpath(str(path)) + ".auth-" + busy_tag
    busy_file = tmp_path / f"config.json.auth-{busy_tag}.lock"
    held_ready = threading.Event()
    release_busy = threading.Event()

    def hold_busy():
        with config.storage_lock(busy_lock_path):
            held_ready.set()
            release_busy.wait(5)

    t = threading.Thread(target=hold_busy)
    t.start()
    try:
        assert held_ready.wait(5)
        config._STORAGE_LOCKS[fake_orphan_key] = threading.RLock()
        config.load(str(path))
        # Orphaned unused locks are removed, active account lock & busy lock & unrelated file stay intact.
        assert not drop_lock.exists()
        assert not bogus_lock.exists()
        assert not fake_orphan_lock.exists()
        assert fake_orphan_key not in config._STORAGE_LOCKS
        assert keep_lock.exists()
        assert busy_file.exists()
        assert unrelated.exists()
    finally:
        release_busy.set()
        t.join(5)

    # Once released, cleanup removes the previously-busy orphan lock too.
    assert config.cleanup_unused_renewal_locks(cfg) >= 1
    assert not busy_file.exists()


# 6. MEDIUM #24 part: ClientKeyManager.reload prunes deleted key usage/RPM/counted
def test_client_key_manager_reload_prunes_deleted_key_state():
    key_v1 = {
        "id": "team-key",
        "label": "Team V1",
        "key": "sk-proxi-team-v1-0123456789abcdef",
        "enabled": True,
        "allowed_models": None,
        "rpm_limit": 1,
        "max_budget_usd": 1.0,
    }
    key_keep = {
        "id": "keep-key",
        "label": "Keep",
        "key": "sk-proxi-keep-key-0123456789abcdef",
        "enabled": True,
        "allowed_models": None,
        "rpm_limit": 10,
        "max_budget_usd": 5.0,
    }
    mgr = auth.ClientKeyManager({"client_keys": [key_v1, key_keep]})
    profile_v1 = dict(key_v1, master=False)
    profile_keep = dict(key_keep, master=False)

    tok = client_context.bind({"request_id": "req-old"})
    try:
        mgr.enforce_request(profile_v1, "claude-opus-5-5")
        mgr.enforce_request(profile_keep, "claude-opus-5-5")
    finally:
        client_context.reset(tok)
    mgr.record_usage("team-key", input_tokens=100, output_tokens=50, cost_usd=1.0)
    mgr.record_usage("keep-key", input_tokens=10, output_tokens=5, cost_usd=0.2)

    # team-key is now exhausted (budget & RPM)
    with pytest.raises(Exception) as exc:
        mgr.enforce_request(profile_v1, "claude-opus-5-5")
    assert getattr(exc.value, "status", None) == 402

    # Delete team-key via reload([key_keep]), then recreate with the same id "team-key"
    mgr.reload([key_keep])
    assert "team-key" not in mgr._usage
    assert "team-key" not in mgr._rpm_stamps
    assert "team-key" not in mgr._counted
    # Retained key keeps its usage
    assert mgr.list_public()[0]["spent_usd"] == pytest.approx(0.2)

    key_v2 = {**key_v1, "label": "Team V2", "key": "sk-proxi-team-v2-0123456789abcdef"}
    mgr.reload([key_keep, key_v2])
    profile_v2 = dict(key_v2, master=False)
    stats = {item["id"]: item for item in mgr.list_public()}
    assert stats["team-key"]["requests"] == 0
    assert stats["team-key"]["spent_usd"] == 0.0
    assert stats["keep-key"]["spent_usd"] == pytest.approx(0.2)

    # Recreated key with the same id and even the same request_id must not inherit old budget/RPM/counted state!
    tok2 = client_context.bind({"request_id": "req-old"})
    try:
        mgr.enforce_request(profile_v2, "claude-opus-5-5")
    finally:
        client_context.reset(tok2)
    stats_after = {item["id"]: item for item in mgr.list_public()}
    assert stats_after["team-key"]["requests"] == 1


# 7. MEDIUM #11: CreditsManager failed fetch does not refresh _cached_at
def test_credits_manager_failed_fetch_does_not_refresh_cached_at(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(credits.time, "monotonic", lambda: now[0])
    calls = []

    def fake_open(*_args, **_kwargs):
        calls.append(now[0])
        if len(calls) == 1:
            return io.BytesIO(b'{"org":{"balance":88.5,"monthlyAllocation":100}}')
        if len(calls) == 2:
            raise urllib.error.HTTPError("https://tenant.invalid", 502, "Bad Gateway", {}, io.BytesIO(b""))
        return io.BytesIO(b'{"org":{"balance":77.0,"monthlyAllocation":100}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    mgr = credits.CreditsManager("https://tenant.invalid", lambda: {})

    first = mgr.get_balance()
    assert first["available"] == 88.5 and first["stale"] is False
    cached_at_after_success = mgr._cached_at

    # 900 seconds pass (> max_age=600). Fetch fails.
    now[0] += 900.0
    failed = mgr.get_balance()
    assert failed["available"] == 88.5 and failed["stale"] is True
    assert mgr._cached_at == cached_at_after_success

    # recent_balance(600) must NOT treat the 900s-old balance as recent!
    assert mgr.recent_balance(600.0) is None
    assert mgr.balance_snapshot()["stale"] is True

    # Inside CACHE_TTL failure backoff, get_balance() serves cached stale without hammering upstream.
    now[0] += credits.CACHE_TTL - 1.0
    backoff = mgr.get_balance()
    assert backoff["stale"] is True and len(calls) == 2

    # Once failure backoff expires, get_balance() retries and recovers.
    now[0] += 2.0
    recovered = mgr.get_balance()
    assert recovered["available"] == 77.0 and recovered["stale"] is False and len(calls) == 3
    assert mgr.recent_balance(600.0)["available"] == 77.0

    # If a recent success (100s < max_age=600s) is followed by a failed force_refresh and CACHE_TTL passes,
    # recent_balance(600) stops suppressing retries so recovery can occur.
    now[0] += 100.0
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError()))
    assert mgr.get_balance(force_refresh=True)["stale"] is True
    assert mgr.recent_balance(600.0)["stale"] is True  # inside CACHE_TTL backoff
    now[0] += credits.CACHE_TTL + 1.0
    assert mgr.recent_balance(600.0) is None


# 8. LOW: compaction_tools.ChatTools._save keeps _dirty on failure, retries replace, unlinks .tmp
def test_chat_tools_save_preserves_dirty_retries_replace_and_cleans_tmp(tmp_path, monkeypatch):
    path = tmp_path / compaction_tools.FILE_NAME
    now = [2000.0]
    tools = [{"type": "function", "name": "exec", "parameters": {"type": "object"}}]
    ct = compaction_tools.ChatTools(clock=lambda: now[0], path=str(path))
    real_replace = compaction_tools.os.replace

    # Permanent PermissionError keeps _dirty=True, does not advance _saved_at, and leaves no .tmp file.
    monkeypatch.setattr(compaction_tools.time, "sleep", lambda _s: None)
    monkeypatch.setattr(compaction_tools.os, "replace", lambda _s, _d: (_ for _ in ()).throw(PermissionError("locked")))
    ct.remember("chat-1", "gpt-6.1-sol", tools)
    assert ct._dirty is True
    assert ct._saved_at == 0.0
    assert [p.name for p in tmp_path.iterdir()] == []

    # Now transient PermissionError recovers on retry; a repeat remember() with same tools saves because _saved_at wasn't bumped!
    attempts = []

    def flaky_replace(src, dst):
        attempts.append(src)
        if len(attempts) < 3:
            raise PermissionError("transient lock")
        return real_replace(src, dst)

    monkeypatch.setattr(compaction_tools.os, "replace", flaky_replace)
    now[0] += 1.0  # < SAVE_EVERY, yet _saved_at is still 0.0 so due=True!
    ct.remember("chat-1", "gpt-6.1-sol", tools)
    assert len(attempts) == 3
    assert ct._dirty is False
    assert ct._saved_at == now[0]
    assert [p.name for p in tmp_path.iterdir()] == [compaction_tools.FILE_NAME]
    assert compaction_tools.ChatTools(clock=lambda: now[0], path=str(path)).recall("chat-1", "gpt-6.1-sol") == tools
