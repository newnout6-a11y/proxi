"""Wave 2-3 regression tests: catalog/router locking, CN balances, presets, minara/retool refresh, and model parsing."""
from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import pathlib
import threading
import types
import urllib.error

import httpx
import pytest

from catalog import discovery, models_dev, router
from drivers import balance_checkers, presets
import forced_choice
import generation
import minara
import minara_catalog
import minara_probe
import models
import retool_catalog
import retool_refresh
from test_retool_refresh import ENTRY, Frontend, registry


@pytest.fixture(autouse=True)
def isolated_minara_and_models():
    measured = dict(minara_catalog.MEASURED)
    catalog = list(models.CATALOG)
    by_id = dict(models._BY_ID)
    aliases = dict(models.ALIASES)
    minara_routes = dict(models._MINARA_MODELS)
    switched = {k: set(v) for k, v in minara.SWITCHED.items()}
    listeners = list(minara._switch_listeners)
    try:
        yield
    finally:
        with minara_catalog._LOCK:
            minara_catalog.MEASURED.clear()
            minara_catalog.MEASURED.update(measured)
        with models._CATALOG_LOCK:
            models.CATALOG[:] = catalog
            models._BY_ID.clear()
            models._BY_ID.update(by_id)
            models.ALIASES.clear()
            models.ALIASES.update(aliases)
            models._MINARA_MODELS.clear()
            models._MINARA_MODELS.update(minara_routes)
        with minara._SWITCH_LOCK:
            minara.SWITCHED.clear()
            minara.SWITCHED.update(switched)
        minara._switch_listeners[:] = listeners


# --- 1. MEDIUM #3: Synchronized global catalog mutations -----------------------------------------------------------------
def test_minara_catalog_concurrent_overlay_and_reads(tmp_path, isolated_minara_and_models):
    overlay_file = tmp_path / minara_catalog.AUTO_NAME
    base_entry = dict(minara_catalog.SHIPPED["anthropic/claude-opus-5.5"])
    errors = []

    def writer(idx):
        try:
            for step in range(40):
                uid = f"openai/concurrent-test-{idx}-{step}"
                entry = dict(base_entry, id=f"minara/concurrent-test-{idx}-{step}", upstream_model=uid,
                             refreshed_at=f"2999-01-01T00:00:{step % 60:02d}+00:00")
                overlay_file.write_text(json.dumps({"version": 1, "models": {uid: entry}}), encoding="utf-8")
                minara_catalog.apply_overlay(overlay_file)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(80):
                minara_catalog.upstream_of("minara/claude-opus-5-5")
                minara_catalog.route("anthropic/claude-opus-5.5")
                minara_catalog.descriptors({})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(writer, i) for i in range(2)] + [pool.submit(reader) for _ in range(4)]
        for fut in futs:
            fut.result()
    assert errors == []


def test_models_concurrent_discovered_levels_and_catalog_reads(isolated_minara_and_models):
    errors = []
    cfg = {"_minara_models": {}, "_provider_models": {}, "_route_aliases": {}}

    def mutator(worker):
        try:
            for i in range(30):
                mid = f"discovered-concurrent-{worker}-{i}"
                models.add_discovered([{"id": mid, "provider": "anthropic", "reasoning_effort_levels": ["low", "high"]}])
                models.add_levels({mid: ["max"]})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(80):
                entries = models.catalog(cfg)
                assert any(e["id"] == "claude-opus-5-5" for e in entries)
                models.codex_catalog()
                models.ignores_output_cap("claude-opus-5-5")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(mutator, i) for i in range(2)] + [pool.submit(reader) for _ in range(4)]
        for fut in futs:
            fut.result()
    assert errors == []


# --- 2. MEDIUM #6: Moonshot .cn & SiliconFlow .cn balance checks ---------------------------------------------------------
@pytest.mark.parametrize("preset_id, base_url, expected_endpoint, payload, expected_raw", [
    (
        "moonshot",
        "https://api.moonshot.cn/v1",
        "https://api.moonshot.cn/v1/users/me/balance",
        {"data": {"available_balance": 100.0}},
        100.0,
    ),
    (
        "siliconflow",
        "https://api.siliconflow.cn/v1",
        "https://api.siliconflow.cn/v1/user/info",
        {"data": {"totalBalance": "50.00"}},
        50.0,
    ),
])
def test_cn_presets_derive_cn_balance_endpoint_and_convert_cny(preset_id, base_url, expected_endpoint, payload, expected_raw):
    assert presets.detect_preset_from_url(base_url) == preset_id

    for passed_preset in (None, preset_id):
        conn = presets.identify_connection(base_url=base_url, preset=passed_preset)
        assert conn["preset"] == preset_id
        assert conn["balance_endpoint"] == expected_endpoint
        assert balance_checkers.own_balance_endpoint(conn["balance_endpoint"], conn["base_url"]) == expected_endpoint

    seen_hosts = []

    def handler(request):
        seen_hosts.append((request.url.host, request.url.path))
        return httpx.Response(200, json=payload)

    _meta, records, _secrets = presets.expand_provider_to_accounts(
        {"id": f"{preset_id}-cn", "preset": preset_id, "base_url": base_url, "api_key": "sk-cn-test-0123456789"},
        "provider",
    )
    cfg = dict(records[0], _http_transport=httpx.MockTransport(handler))
    account = balance_checkers.ProviderBalanceAccount(cfg)
    assert account.supports_balance is True
    snap = account.get_balance(force_refresh=True)
    assert snap["balance_supported"] is True
    assert snap["raw_currency"] == "CNY"
    assert snap["available"] == pytest.approx(round(expected_raw * balance_checkers.CNY_TO_USD_FALLBACK, 4))
    assert seen_hosts == [(httpx.URL(expected_endpoint).host, httpx.URL(expected_endpoint).path)]


def test_non_official_cn_host_with_moonshot_preset_does_not_receive_cn_balance_endpoint():
    conn = presets.identify_connection(preset="moonshot", base_url="https://evil.cn/v1")
    assert conn["balance_endpoint"] == "https://api.moonshot.ai/v1/users/me/balance"
    assert balance_checkers.own_balance_endpoint(conn["balance_endpoint"], conn["base_url"]) is None


# --- 3. MEDIUM #7 + #25: Per-provider/per-credential settings & authoritative api_key_env --------------------------------
def test_expand_provider_preserves_per_provider_and_per_credential_settings():
    provider = {
        "id": "customhub",
        "driver": "openai-chat",
        "base_url": "https://hub.example/v1",
        "http2": False,
        "verify_tls": False,
        "use_system_proxy": True,
        "user_agent": "ProviderAgent/1.0",
        "min_balance_usd": 4.5,
        "direct_cache": False,
        "connect_timeout": 7.5,
        "upstream_timeout": 45,
        "turn_timeout": 300,
        "recv_timeout": 90,
        "stream_first_token_timeout": 12,
        "stream_idle_timeout": 0,
        "stream_tool_timeout": 180,
        "stream_thinking_timeout": 240,
        "credentials": [
            {"id": "cred-inherit", "api_key": "sk-cred-one-123456"},
            {
                "id": "cred-override",
                "api_key": "sk-cred-two-123456",
                "http2": True,
                "verify_tls": True,
                "use_system_proxy": False,
                "user_agent": "CredAgent/2.0",
                "min_balance_usd": 0,
                "direct_cache": True,
                "upstream_timeout": 60,
                "stream_idle_timeout": 30,
            },
        ],
    }
    meta, records, _secrets = presets.expand_provider_to_accounts(provider, "provider")
    assert meta["http2"] is False and meta["min_balance_usd"] == 4.5 and meta["direct_cache"] is False

    first, second = records
    assert first["http2"] is False
    assert first["verify_tls"] is False
    assert first["use_system_proxy"] is True
    assert first["user_agent"] == "ProviderAgent/1.0"
    assert first["min_balance_usd"] == 4.5
    assert first["direct_cache"] is False
    assert first["connect_timeout"] == 7.5
    assert first["upstream_timeout"] == 45
    assert first["turn_timeout"] == 300
    assert first["recv_timeout"] == 90
    assert first["stream_first_token_timeout"] == 12
    assert first["stream_idle_timeout"] == 0
    assert first["stream_tool_timeout"] == 180
    assert first["stream_thinking_timeout"] == 240

    assert second["http2"] is True
    assert second["verify_tls"] is True
    assert second["use_system_proxy"] is False
    assert second["user_agent"] == "CredAgent/2.0"
    assert second["min_balance_usd"] == 0.0
    assert second["direct_cache"] is True
    assert second["upstream_timeout"] == 60
    assert second["stream_idle_timeout"] == 30
    assert second["connect_timeout"] == 7.5  # inherited when not overridden

    # Single-credential shorthand preserves settings too
    single_meta, single_recs, _ = presets.expand_provider_to_accounts(
        {
            "id": "single",
            "driver": "openai-chat",
            "base_url": "https://hub.example/v1",
            "api_key": "sk-single-123456",
            "http2": False,
            "direct_cache": False,
            "min_balance_usd": 1.25,
            "user_agent": "Single/1.0",
            "upstream_timeout": 25,
        },
        "provider",
    )
    assert single_meta["http2"] is False
    assert single_recs[0]["http2"] is False
    assert single_recs[0]["direct_cache"] is False
    assert single_recs[0]["min_balance_usd"] == 1.25
    assert single_recs[0]["user_agent"] == "Single/1.0"
    assert single_recs[0]["upstream_timeout"] == 25


def test_explicit_api_key_env_is_authoritative_over_inline_or_options_api_key(monkeypatch):
    monkeypatch.delenv("MISSING_PROV_KEY", raising=False)
    with pytest.raises(ValueError, match=r"env MISSING_PROV_KEY is empty"):
        presets.expand_provider_to_accounts(
            {
                "id": "or-env",
                "preset": "openrouter",
                "api_key_env": "MISSING_PROV_KEY",
                "api_key": "sk-or-v1-stale-inline-key",
                "options": {"apiKey": "sk-or-v1-stale-options-key"},
            },
            "provider",
        )

    monkeypatch.setenv("MISSING_PROV_KEY", "   ")
    with pytest.raises(ValueError, match=r"env MISSING_PROV_KEY is empty"):
        presets.expand_provider_to_accounts(
            {
                "id": "or-env",
                "preset": "openrouter",
                "api_key_env": "MISSING_PROV_KEY",
                "options": {"apiKey": "sk-or-v1-stale-options-key"},
            },
            "provider",
        )

    # Credential-level api_key_env must also never fall back to inline api_key
    monkeypatch.delenv("MISSING_CRED_KEY", raising=False)
    with pytest.raises(ValueError, match=r"env MISSING_CRED_KEY is empty"):
        presets.expand_provider_to_accounts(
            {
                "id": "multi-env",
                "preset": "openai",
                "credentials": [{"api_key_env": "MISSING_CRED_KEY", "api_key": "sk-stale-cred-key"}],
            },
            "provider",
        )

    # Valid env key is used even if stale options.apiKey has a different prefix
    monkeypatch.setenv("ACTIVE_OR_KEY", "sk-or-v1-active-env-secret-123456")
    meta, recs, secrets_found = presets.expand_provider_to_accounts(
        {
            "id": "or-active",
            "api_key_env": "ACTIVE_OR_KEY",
            "options": {"apiKey": "sk-ant-stale-wrong-prefix-123456"},
        },
        "provider",
    )
    assert meta["preset"] == "openrouter"
    assert recs[0]["api_key"] == "sk-or-v1-active-env-secret-123456"
    assert "sk-ant-stale-wrong-prefix-123456" not in secrets_found


# --- 4. MEDIUM #8: Resurrected empty `switched` dict in minara_refresh ---------------------------------------------------
def test_minara_refresh_persists_cleared_empty_switched_dict(tmp_path, monkeypatch, isolated_minara_and_models):
    from test_minara_refresh import FakeClient, listing_of, refresher, relay_cfg

    cfg = relay_cfg(tmp_path)
    items = listing_of()
    client = FakeClient(items)
    ref = refresher(cfg, client)

    assert minara.switch_to_messages("anthropic/claude-opus-5.5", "high", reason="test")
    saved_before = json.loads((tmp_path / minara_catalog.AUTO_NAME).read_text(encoding="utf-8"))
    assert saved_before["switched"] == {"anthropic/claude-opus-5.5": ["high"]}

    # Clear the last override while _run_once_with_client has already read `state` containing the old override
    original_relevel = ref._relevel

    def clear_during_run(*args, **kwargs):
        assert minara.switch_to_messages("anthropic/claude-opus-5.5", "high", False)
        return original_relevel(*args, **kwargs)

    monkeypatch.setattr(ref, "_relevel", clear_during_run)
    ref.run_once()
    saved_after = json.loads((tmp_path / minara_catalog.AUTO_NAME).read_text(encoding="utf-8"))
    assert saved_after["switched"] == {}


# --- 5. MEDIUM #9: Uncaught client.credits() & unclosed SSE error response in minara_probe ------------------------------
def test_measure_model_and_refresh_survive_credits_failure(tmp_path, monkeypatch, isolated_minara_and_models):
    from test_minara_refresh import FakeClient, listing_of, refresher, relay_cfg

    class FailingCreditsClient:
        def __init__(self):
            self.calls = 0

        def credits(self):
            self.calls += 1
            raise RuntimeError("/auth/me HTTP 502")

        def wait(self, _seconds):
            return False

    monkeypatch.setattr(minara_probe, "probe_tools", lambda c, m: {"status": "ok", "tools": True, "id": "gen-1", "served": m})
    monkeypatch.setattr(minara_probe, "probe_vision", lambda c, m: {"status": "ok", "vision": True})
    monkeypatch.setattr(minara_probe, "probe_thinking", lambda c, m: {"status": "ok", "thinking": "end", "reasoning_tokens": 20})
    monkeypatch.setattr(minara_probe, "probe_cache", lambda c, m: {"status": "ok", "caches": True})

    fc = FailingCreditsClient()
    item = {"id": "anthropic/claude-test-credits", "supported_endpoint_types": ["openai"],
            "pricing": {"quota_type": 0, "prompt": 3, "completion": 15, "cache_read": 0.3, "cache_write": 3.75}}
    assert minara_probe.study_effort(fc, item["id"], samples=1, budget=50.0) is None
    record, study = minara_probe.measure_model(fc, item, study_budget=50.0)
    assert study is None
    assert record["tools"]["status"] == "ok" and record["vision"]["status"] == "ok"

    # Second failure mode: credits() fails on the post-sample check (line 1183)
    class SecondCallFailsClient(FailingCreditsClient):
        def credits(self):
            self.calls += 1
            if self.calls == 1:
                return 500.0
            raise RuntimeError("/auth/me HTTP 503")

    monkeypatch.setattr(minara_probe, "study_row", lambda c, m, l: {"status": 200, "reasoning_tokens": 100})
    assert minara_probe.study_effort(SecondCallFailsClient(), item["id"], samples=1, budget=50.0) is None

    # And CatalogRefresher.run_once publishes the model with inherited effort rather than discarding measurements
    cfg = relay_cfg(tmp_path)
    list_client = FakeClient(listing_of([item]))
    list_client.credits = lambda: (_ for _ in ()).throw(RuntimeError("auth/me down"))
    summary = refresher(cfg, list_client).run_once()
    assert summary["added"] == ["anthropic/claude-test-credits"]


def test_messages_summary_closes_response_on_sse_error():
    closed = []

    class FakeResp:
        status_code = 200

        def iter_content(self):
            yield b'data: {"type": "message_start", "message": {"id": "msg_1", "usage": {}}}\n\n'
            yield b'data: {"type": "error", "error": {"message": "upstream failure"}}\n\n'
            yield b'data: {"type": "message_stop"}\n\n'

        def close(self):
            closed.append(True)

    fake_client = types.SimpleNamespace(messages=lambda body: (FakeResp(), None))
    out = minara_probe._messages_summary(fake_client, {"model": "anthropic/claude-opus-5.5"})
    assert out["status"] == "stream_error"
    assert closed == [True]


def test_probe_client_credits_validates_status_and_closes_response():
    closed = []

    class BadStatusResp:
        status_code = 502

        def json(self):
            return {"error": "bad gateway"}

        def close(self):
            closed.append(True)

    c = minara_probe.Client("tok", requests=types.SimpleNamespace(get=lambda *a, **k: BadStatusResp()))
    with pytest.raises(RuntimeError, match="HTTP 502"):
        c.credits()
    assert closed == [True]


# --- 6. MEDIUM #10: Balance checker locked cache reads, failure coalescing, async MockTransport -------------------------
def test_balance_checker_coalesces_concurrent_failures():
    fetch_calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow_failing_fetch(_cfg):
        fetch_calls.append(1)
        entered.set()
        assert release.wait(timeout=5.0)
        raise RuntimeError("upstream 503")

    cfg = {"preset": "openrouter", "base_url": "https://openrouter.ai/api/v1",
           "balance_endpoint": "https://openrouter.ai/api/v1/key", "api_key": "sk-or-v1-test-123456"}
    account = balance_checkers.ProviderBalanceAccount(cfg, fetch=slow_failing_fetch)

    results = []

    def worker():
        results.append(account.get_balance())

    t1 = threading.Thread(target=worker)
    t1.start()
    assert entered.wait(timeout=5.0)
    t2 = threading.Thread(target=worker)
    t2.start()
    # Give t2 a moment to block on _fetch_lock before releasing t1
    threading.Event().wait(0.05)
    release.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert len(fetch_calls) == 1
    assert len(results) == 2
    assert all(r["stale"] is True and "RuntimeError" in (r.get("error") or "") for r in results)


def test_balance_checker_async_mock_transport_inside_running_event_loop():
    async def async_handler(_request):
        return httpx.Response(200, json={"data": {"usage": 2.0, "limit": 20.0, "limit_remaining": 18.0}})

    cfg = {
        "preset": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "balance_endpoint": "https://openrouter.ai/api/v1/key",
        "api_key": "sk-or-v1-test-123456",
        "_http_transport": httpx.MockTransport(async_handler),
    }
    account = balance_checkers.ProviderBalanceAccount(cfg)

    async def scenario():
        return account.get_balance(force_refresh=True)

    snap = asyncio.run(scenario())
    assert snap["available"] == 18.0
    assert snap["balance_supported"] is True


# --- 7. MEDIUM #26: Async discovery deletion race & .tmp cleanup / Windows replace retry --------------------------------
def test_async_discovery_does_not_resurrect_deleted_provider(tmp_path):
    cfg_path = tmp_path / "config.json"
    overlay_path = discovery.overlay_path(cfg_path)
    meta_a = {"id": "prov-a", "name": "Prov A", "driver": "openai-chat", "base_url": "https://a.example/v1",
              "auth_mode": "bearer", "cache_policy": "auto", "auto_discover": True, "models": []}
    meta_b = {"id": "prov-b", "name": "Prov B", "driver": "openai-chat", "base_url": "https://b.example/v1",
              "auth_mode": "bearer", "cache_policy": "auto", "auto_discover": True, "models": []}
    acc_a = {"account_id": "a1", "provider_id": "prov-a", "driver": "openai-chat", "base_url": "https://a.example/v1",
             "auth_mode": "bearer", "api_key": "sk-a-123456", "enabled": True, "_provider_meta": meta_a}
    acc_b = {"account_id": "b1", "provider_id": "prov-b", "driver": "openai-chat", "base_url": "https://b.example/v1",
             "auth_mode": "bearer", "api_key": "sk-b-123456", "enabled": True, "_provider_meta": meta_b}
    backend_a = types.SimpleNamespace(cfg=acc_a)
    backend_b = types.SimpleNamespace(cfg=acc_b)
    runtime = types.SimpleNamespace(pool=types.SimpleNamespace(backends=[backend_a, backend_b]))

    entered_a = asyncio.Event()
    release_a = asyncio.Event()

    async def handler(request):
        if request.url.host == "a.example":
            entered_a.set()
            await release_a.wait()
            return httpx.Response(200, json={"data": [{"id": "model-a"}]})
        return httpx.Response(200, json={"data": [{"id": "model-b"}]})

    transport = httpx.MockTransport(handler)
    acc_a["_http_transport"] = transport
    acc_b["_http_transport"] = transport

    cfg = {
        "_config_path": str(cfg_path),
        "models_dev_sync": False,
        "providers": [{"id": "prov-a"}, {"id": "prov-b"}],
        "_providers_meta": {"prov-a": meta_a, "prov-b": meta_b},
        "accounts": [acc_a, acc_b],
        "routes": [],
        "_discovered_provider_models": {"prov-a": [{"id": "old-a"}], "prov-b": [{"id": "old-b"}]},
        "_provider_models": {},
        "_route_aliases": {},
    }
    discovery.save_overlay(overlay_path, cfg["_discovered_provider_models"])
    refresher = discovery.UniversalCatalogRefresher(cfg, runtime=runtime)

    async def scenario():
        try:
            task = asyncio.create_task(refresher.refresh_async())
            await entered_a.wait()
            # Simulate delete_provider("prov-a") occurring mid-flight during await
            cfg["providers"] = [{"id": "prov-b"}]
            cfg["_providers_meta"].pop("prov-a", None)
            runtime.pool.backends.remove(backend_a)
            cfg["_discovered_provider_models"].pop("prov-a", None)
            discovery.save_overlay(overlay_path, {"prov-b": [{"id": "old-b"}]})
            release_a.set()
            res = await task
            assert "prov-a" not in cfg["_discovered_provider_models"]
            assert "prov-a/model-a" not in cfg["_provider_models"]
            assert "prov-a" not in discovery.load_overlay(overlay_path)
            assert "prov-a" not in res["providers"]
            assert [m["id"] for m in cfg["_discovered_provider_models"]["prov-b"]] == ["model-b"]
            assert "prov-b/model-b" in cfg["_provider_models"]
        finally:
            from drivers import http_pool
            await http_pool.close_loop_clients()

    asyncio.run(scenario())


def test_save_overlay_and_models_dev_retry_replace_and_unlink_tmp(tmp_path, monkeypatch):
    target = tmp_path / discovery.AUTO_NAME
    real_replace = discovery.os.replace
    attempts = []

    def flaky_replace(src, dst):
        attempts.append(pathlib.Path(src).name)
        if len(attempts) < 3:
            raise PermissionError(13, "locked")
        return real_replace(src, dst)

    monkeypatch.setattr(discovery.os, "replace", flaky_replace)
    monkeypatch.setattr(discovery.time, "sleep", lambda _s: None)
    assert discovery.save_overlay(target, {"p": [{"id": "m"}]}) is True
    assert len(attempts) == 3
    assert [p.name for p in tmp_path.glob("*.tmp")] == []

    # Permanent OSError cleans up temp file instead of leaking
    monkeypatch.setattr(discovery.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("disk full")))
    assert discovery.save_overlay(target, {"p": [{"id": "m2"}]}) is False
    assert [p.name for p in tmp_path.glob("*.tmp")] == []

    # models_dev.refresh_models_dev_async retries PermissionError & unlinks temp file on failure
    cfg_path = tmp_path / "config.json"

    def md_handler(_request):
        return httpx.Response(200, json={"openai": {"models": {"gpt-5": {"id": "gpt-5", "name": "GPT 5"}}}})

    md_cfg = {
        "_config_path": str(cfg_path),
        "models_dev_sync": True,
        "models_dev_url": "https://models.dev/api.json",
        "_http_transport": httpx.MockTransport(md_handler),
    }

    md_attempts = []

    def flaky_md_replace(src, dst):
        md_attempts.append(src)
        if len(md_attempts) < 2:
            raise PermissionError(13, "busy")
        return real_replace(src, dst)

    monkeypatch.setattr(models_dev.os, "replace", flaky_md_replace)

    async def run_md():
        from drivers import http_pool
        try:
            count = await models_dev.refresh_models_dev_async(md_cfg)
            assert count == 1 and len(md_attempts) == 2
            assert (tmp_path / models_dev.CACHE_NAME).exists()
            assert [p.name for p in tmp_path.glob("*.tmp")] == []

            monkeypatch.setattr(models_dev.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("perm")))
            await models_dev.refresh_models_dev_async(md_cfg)
            assert [p.name for p in tmp_path.glob("*.tmp")] == []
        finally:
            await http_pool.close_loop_clients()

    asyncio.run(run_md())


# --- 8. MEDIUM #27 + LOWs: codex context_window, route alias _is_claude_route, colon parsing, router lock --------------
def test_codex_catalog_never_emits_null_context_window():
    entries = [
        {"id": "local/qwen", "label": "Local Qwen", "provider": "qwen", "context": None, "reasoning": False},
        {"id": "local/zero", "label": "Local Zero", "provider": "qwen", "context": 0, "reasoning": False},
        {"id": "local/sized", "label": "Local Sized", "provider": "qwen", "context": 64000, "reasoning": False},
    ]
    codex_models = models.codex_catalog(entries)["models"]
    assert [m["context_window"] for m in codex_models] == [128000, 128000, 64000]


def test_is_claude_route_recognizes_retool_claude_aliases():
    cfg = {
        "_provider_models": {
            "openrouter/anthropic/claude-sonnet-4.6": {
                "id": "openrouter/anthropic/claude-sonnet-4.6",
                "upstream": "openai-chat",
                "provider_id": "openrouter",
                "provider": "anthropic",
            }
        },
        "_minara_models": {
            "minara/claude-opus-5-5": {
                "id": "minara/claude-opus-5-5",
                "upstream": "minara",
                "upstream_model": "anthropic/claude-opus-5.5",
                "provider": "anthropic",
                "forced_tool_choice": False,
            }
        },
        "routes": [
            {"id": "smart-opus", "targets": ["claude-opus-5-5", "minara/claude-opus-5-5"]},
            {"id": "or-claude", "targets": ["openrouter/anthropic/claude-sonnet-4.6"]},
            {"id": "smart-gpt", "targets": ["gpt-6-sol"]},
        ],
    }
    cfg["_route_aliases"] = aliases = router.build_route_aliases(cfg, models)

    assert models.refuses_forced_tool_choice("smart-opus", entry=aliases["smart-opus"]) is True
    assert models.ignores_output_cap("smart-opus", entry=aliases["smart-opus"]) is True
    assert models.refuses_forced_tool_choice("smart-opus", cfg=cfg) is True
    assert models.ignores_output_cap("smart-opus", cfg=cfg) is True

    assert models.refuses_forced_tool_choice("or-claude", entry=aliases["or-claude"]) is False
    assert models.ignores_output_cap("or-claude", cfg=cfg) is False
    assert models.refuses_forced_tool_choice("smart-gpt", entry=aliases["smart-gpt"]) is False
    assert models.ignores_output_cap("smart-gpt", cfg=cfg) is False

    # 1-arg backward compatibility
    assert models.refuses_forced_tool_choice("claude-opus-5-5") is True
    assert models.ignores_output_cap("claude-opus-5-5") is True
    assert models.refuses_forced_tool_choice("gpt-6-sol") is False
    assert forced_choice.requirement("claude-opus-5-5", [{"name": "t"}], "required") is not None
    assert generation.output_cap(models.resolve("claude-opus-5-5")[0], 500) == 500


def test_colon_model_id_case_insensitive_effort_and_unknown_colon_rsplit():
    cfg = {
        "_provider_models": {
            "ollama/qwen3:32b": {"id": "ollama/qwen3:32b", "provider": "qwen", "upstream": "openai-chat", "provider_id": "ollama"},
            "openrouter/deepseek/deepseek-r1:free": {
                "id": "openrouter/deepseek/deepseek-r1:free", "provider": "deepseek", "upstream": "openai-chat", "provider_id": "openrouter"
            },
        },
        "_minara_models": {
            "minara/test:high": {"id": "minara/test:high", "upstream": "minara", "upstream_model": "openai/test:high", "provider": "openai"}
        },
        "_route_aliases": {},
    }
    # Case-insensitive #HIGH on colon-ID models (with and without @profile)
    assert tuple(models.parse_model_name("ollama/qwen3:32b#HIGH", cfg)) == ("ollama/qwen3:32b", None, "high")
    assert tuple(models.parse_model_name("ollama/qwen3:32b@mine#HIGH", cfg)) == ("ollama/qwen3:32b", "mine", "high")
    assert tuple(models.parse_model_name("ollama/qwen3:32b@mine:HIGH", cfg)) == ("ollama/qwen3:32b", "mine", "high")
    assert tuple(models.parse_model_name("ollama/qwen3:32b:HIGH", cfg)) == ("ollama/qwen3:32b", None, "high")
    assert tuple(models.parse_model_name("minara/openai/test:high#HIGH", cfg)) == ("minara/openai/test:high", None, "high")
    assert tuple(models.parse_model_name("openrouter/deepseek/deepseek-r1:free@fast#LOW", cfg)) == (
        "openrouter/deepseek/deepseek-r1:free", "fast", "low"
    )

    # Unknown provider-qualified colon models split on the last ':'
    assert tuple(models.parse_model_name("ollama/qwen3:32b:high")) == ("ollama/qwen3:32b", None, "high")
    assert tuple(models.parse_model_name("openrouter/deepseek/deepseek-r1:free:HIGH")) == (
        "openrouter/deepseek/deepseek-r1:free", None, "high"
    )
    assert tuple(models.parse_model_name("ollama/qwen3:32b#HIGH")) == ("ollama/qwen3:32b", None, "high")


def test_router_build_and_refresh_route_aliases_are_serialized():
    cfg = {
        "_minara_models": {"minara/claude-opus-5-5": {"id": "minara/claude-opus-5-5", "upstream": "minara", "provider": "anthropic"}},
        "_provider_models": {},
        "routes": [{"id": "alias-one", "targets": ["minara/claude-opus-5-5"]}],
    }
    cfg["_route_aliases"] = router.build_route_aliases(cfg, models)
    errors = []

    def builder(idx):
        try:
            for step in range(40):
                cfg["_minara_models"] = {
                    "minara/claude-opus-5-5": {"id": "minara/claude-opus-5-5", "upstream": "minara",
                                               "provider": "anthropic", "tag": f"{idx}-{step}"}
                }
                router.build_route_aliases(cfg, models)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def refresher():
        try:
            for _ in range(80):
                aliases = router.refresh_route_aliases(cfg, models)
                assert isinstance(aliases, dict)
                assert isinstance(cfg.get("_route_sources"), list)
                assert isinstance(cfg.get("_route_warnings"), list)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(builder, i) for i in range(2)] + [pool.submit(refresher) for _ in range(4)]
        for fut in futs:
            fut.result()
    assert errors == []


# --- 9. Operational fix: retool_refresh chunk OSError, account rotation, and refresher in probe -------------------------
def test_retool_refresh_registry_survives_chunk_and_account_oserror(tmp_path):
    doc = registry()
    frontend = Frontend(doc)
    frontend.pages[retool_refresh.EDGE + "other-1.js"] = OSError("transient chunk timeout")

    def flaky_fetch(url, limit):
        val = frontend.pages[url]
        if isinstance(val, BaseException):
            raise val
        return val

    acc1 = types.SimpleNamespace(
        cfg={"upstream": "retool", "base": "https://t1.retool.invalid", "enabled": True,
             "provider_resource_name": "__managed__", "openai_provider_resource_name": "res"},
        unavailable=None,
        credits=None,
    )
    acc2 = types.SimpleNamespace(
        cfg={"upstream": "retool", "base": "https://t2.retool.invalid", "enabled": True,
             "provider_resource_name": "__managed__", "openai_provider_resource_name": "res"},
        unavailable=None,
        credits=None,
    )
    frontend.pages["https://t1.retool.invalid/"] = OSError("proxy down")
    frontend.pages["https://t2.retool.invalid/"] = f'<script src="https://retool-edge.com/assets_vjs/{ENTRY}"></script>'

    # Even with t1 failing login page AND other-1.js failing with OSError, t2 + index-Reg456.js succeed!
    ref = retool_refresh.CatalogRefresher(
        {"_retool_catalog_file": str(tmp_path / retool_catalog.AUTO_NAME)},
        backends=[acc1, acc2],
        fetcher=flaky_fetch,
        prober=lambda backend, entry, effort=None: {"ok": True, "accepted": True, "http": 200, "credits": 0.01, "thinking": 0},
    )
    summary = ref.run_once()
    assert summary.get("error") is None
    assert "claude-fable-5-2" in summary["added"]


def test_retool_refresh_rotates_accounts_and_avoids_7day_backoff_on_401(tmp_path, isolated_minara_and_models):
    from test_retool_refresh import Account

    doc = registry()
    doc["anthropic"]["models"]["claude-opus-5-5"]["capabilities"]["reasoningEffortLevels"] = ["low", "medium", "high", "xhigh", "max"]
    frontend = Frontend(doc)
    bad_acc = Account(account_id="bad-cookie", base="https://tenant.retool.com")
    good_acc = Account(account_id="good-cookie", base="https://tenant.retool.com")

    asked = []

    def prober(backend, entry, effort=None):
        asked.append((backend.cfg["account_id"], entry["id"], effort))
        if backend is bad_acc:
            return {"ok": False, "accepted": False, "http": 401, "error": "HTTP 401", "credits": None, "thinking": 0, "seconds": 0.1}
        return {"ok": True, "accepted": True, "http": 200, "error": None, "credits": 0.01, "thinking": 1, "seconds": 0.2}

    path = tmp_path / retool_catalog.AUTO_NAME
    ref = retool_refresh.CatalogRefresher(
        {"_retool_catalog_file": str(path)},
        backends=[bad_acc, good_acc],
        fetcher=frontend,
        prober=prober,
    )
    summary = ref.run_once()
    assert sorted(summary["added"]) == ["claude-fable-5-2", "gpt-7-mini"]
    assert summary["leveled"] == [["claude-opus-5-5", "xhigh"]]
    # Once bad_acc failed with 401 on the first probe, later probes skipped it directly to good_acc
    assert asked[0][0] == "bad-cookie"
    assert [call[0] for call in asked[1:]] == ["good-cookie", "good-cookie", "good-cookie"]
    assert len(good_acc.credits.turns) == 3

    # When every account returns 401, models must NOT be pushed into 7-day backoff
    all_bad = Account(account_id="all-bad", base="https://tenant.retool.com")
    path_bad = tmp_path / "bad.auto.json"
    ref_bad = retool_refresh.CatalogRefresher(
        {"_retool_catalog_file": str(path_bad)},
        backends=[all_bad],
        fetcher=frontend,
        prober=lambda backend, entry, effort=None: {"ok": False, "accepted": False, "http": 401, "error": "unauthorized",
                                                     "credits": None, "thinking": 0, "seconds": 0.1},
    )
    sum_bad = ref_bad.run_once()
    assert sum_bad["added"] == [] and len(sum_bad["failed"]) == 2
    saved_bad = retool_catalog.read(path_bad)
    for meas in saved_bad["measurements"].values():
        assert meas.get("failures", 0) == 0
        assert "retry_after" not in meas


def test_retool_probe_uses_backend_refresher_on_401(monkeypatch):
    seen_cookies = []
    cfg = {
        "upstream": "retool",
        "base": "https://tenant.retool.invalid",
        "cookie": "accessToken=stale-cookie",
        "xsrf": "stale-xsrf",
        "provider_resource_name": "__managed__",
    }

    class FakeRefresher:
        def __init__(self):
            self.refreshed = 0

        def call(self, fn):
            try:
                return fn()
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    exc.close()
                    self.refreshed += 1
                    cfg["cookie"] = "accessToken=fresh-cookie"
                    return fn()
                raise

    class Stream:
        def __init__(self, lines):
            self.lines = [json.dumps(line).encode() + b"\n" for line in lines]
            self.status = 200

        def __iter__(self):
            return iter(self.lines)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        seen_cookies.append(req.headers.get("Cookie"))
        if req.headers.get("Cookie") != "accessToken=fresh-cookie":
            raise urllib.error.HTTPError(req.full_url, 401, "unauth", {}, io.BytesIO(b"expired"))
        text = json.dumps(json.loads(req.data))
        marker = text[text.index("READY-"):text.index("READY-") + 14]
        return Stream([
            {"event": "content_block_delta", "type": "text", "partialText": marker},
            {"event": "end_of_stream"},
        ])

    monkeypatch.setattr(retool_refresh.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(retool_refresh.auth, "upstream_headers", lambda c: {"Cookie": c["cookie"]})
    backend = types.SimpleNamespace(cfg=cfg, refresher=FakeRefresher())
    entry = {"id": "claude-opus-5-5", "provider": "anthropic", "reasoning_effort_levels": ["low", "high"]}
    res = retool_refresh.probe(backend, entry)
    assert res["ok"] is True
    assert backend.refresher.refreshed == 1
    assert seen_cookies == ["accessToken=stale-cookie", "accessToken=fresh-cookie"]
