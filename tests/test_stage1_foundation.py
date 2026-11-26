"""Stage 1 foundation tests: driver registry, presets, HTTP pool, config providers[], empty bootstrap & stream watchdog."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import apierr
import config as config_module
import drivers
from drivers import http_pool, presets
import proxy
import session as session_module
import stream_watchdog


def test_presets_normalize_endpoint_urls_and_infer_key_prefixes():
    assert presets.normalize_base_url("https://openrouter.ai/api/v1/chat/completions") == "https://openrouter.ai/api/v1"
    assert presets.normalize_base_url("https://api.openai.com/v1/responses/") == "https://api.openai.com/v1"
    assert presets.normalize_base_url("https://api.anthropic.com/v1/messages") == "https://api.anthropic.com/v1"
    assert presets.normalize_base_url("http://127.0.0.1:11434/v1/models") == "http://127.0.0.1:11434/v1"

    assert presets.identify_connection(api_key="sk-or-v1-secret12345")["preset"] == "openrouter"
    assert presets.identify_connection(api_key="sk-ant-api03-secret12345")["preset"] == "anthropic"
    assert presets.identify_connection(api_key="AIzaSyTestKey123456789")["preset"] == "gemini"
    assert presets.identify_connection(api_key="gsk_test_groq_key_12345")["preset"] == "groq"
    assert presets.identify_connection(api_key="xai-test-key-12345678")["preset"] == "xai"
    assert presets.identify_connection(base_url="http://localhost:11434/v1")["preset"] == "ollama"


def test_ssrf_and_http_policy_allows_local_inference_and_blocks_metadata_or_public_http():
    assert http_pool.validate_endpoint_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert http_pool.validate_endpoint_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"
    assert http_pool.validate_endpoint_url("http://192.168.1.25:1234/v1") == "http://192.168.1.25:1234/v1"
    assert http_pool.validate_endpoint_url("http://host.docker.internal:11434/v1") == "http://host.docker.internal:11434/v1"
    assert http_pool.validate_endpoint_url("https://api.openai.com/v1") == "https://api.openai.com/v1"

    with pytest.raises(apierr.ApiError, match="metadata|link-local|restricted"):
        http_pool.validate_endpoint_url("http://169.254.169.254/latest/meta-data/")

    with pytest.raises(apierr.ApiError, match="metadata|link-local|restricted"):
        http_pool.validate_endpoint_url("http://[fd00:ec2::254]/latest")

    with pytest.raises(apierr.ApiError, match="http://"):
        http_pool.validate_endpoint_url("http://api.openai.com/v1")


def test_opencode_style_provider_and_fill_first_expansion(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_OR_KEY_1", "sk-or-v1-alpha-secret123")
    monkeypatch.setenv("TEST_OR_KEY_2", "sk-or-v1-beta-secret123")

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "openrouter",
                        "preset": "openrouter",
                        "routing_strategy": "fill-first",
                        "priority": 5,
                        "options": {
                            "baseURL": "https://openrouter.ai/api/v1/chat/completions",
                            "headers": {"HTTP-Referer": "https://example.local"},
                        },
                        "credentials": [
                            {"api_key_env": "TEST_OR_KEY_1", "label": "OR #1"},
                            {"api_key_env": "TEST_OR_KEY_2", "label": "OR #2"},
                        ],
                    },
                    {
                        "id": "local-ollama",
                        "preset": "ollama",
                        "base_url": "http://127.0.0.1:11434/v1",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    cfg = config_module.load(str(cfg_file))
    assert not cfg.get("_empty_pool")
    assert len(cfg["accounts"]) == 3

    first, second, third = cfg["accounts"]
    assert first["upstream"] == "openai-chat"
    assert first["base"] == "https://openrouter.ai/api/v1"
    assert first["priority"] == 5
    assert second["priority"] == 6  # fill-first steps priority monotonically
    assert first["headers"]["HTTP-Referer"] == "https://example.local"
    assert third["auth_mode"] == "none"
    assert third["api_key"] == ""

    # Secrets from providers[] are registered for redaction
    assert "sk-or-v1-alpha-secret123" in cfg["cookie_values"]
    assert "sk-or-v1-beta-secret123" in cfg["_extra_secrets"]


def test_empty_pool_bootstrap_creates_zero_phantom_accounts(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(apierr.ApiError, match="config not found"):
        config_module.load(str(missing))
    assert not (tmp_path / "missing.json.lock").exists()

    boot_path = tmp_path / "bootstrap_config.json"
    cfg = config_module.load(str(boot_path), bootstrap=True)
    assert boot_path.exists()
    assert cfg.get("_empty_pool") is True
    assert cfg["accounts"] == []

    cfg["journal_path"] = str(tmp_path / "journal.jsonl")
    cfg["conversation_state"] = False
    rt = proxy.Runtime(cfg)
    try:
        assert rt.pool.backends == []
        assert rt.accounts.backends == []
    finally:
        rt.close()


def test_http_pool_reuses_client_and_never_follows_redirects():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.path.endswith("/redirect"):
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:9999/leaked"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def scenario():
        cfg = {
            "base": "https://api.openai.com/v1",
            "_http_transport": transport,
            "upstream_timeout": 15,
        }
        try:
            c1 = http_pool.get_async_client(cfg)
            c2 = http_pool.get_async_client(cfg)
            assert c1 is c2
            assert c1.follow_redirects is False

            resp = await c1.get("https://api.openai.com/v1/redirect", headers={"Authorization": "Bearer secret"})
            assert resp.status_code == 302
            assert len(seen) == 1
        finally:
            await http_pool.close_loop_clients()

    asyncio.run(scenario())


def test_http_pool_does_not_implicitly_share_cookies_between_credentials():
    seen = []

    def reply(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"set-cookie": "session=private; Path=/"})

    async def scenario():
        client = http_pool.get_async_client({"_http_transport": httpx.MockTransport(reply)})
        try:
            base = "https://provider.example/v1"
            await client.get(base, headers={"Authorization": "Bearer first", "Cookie": "manual=1"})
            await client.get(base, headers={"Authorization": "Bearer second"})
            assert seen == ["manual=1", None]
        finally:
            await http_pool.close_loop_clients()

    asyncio.run(scenario())


def test_http_pool_keeps_system_proxy_opt_in_out_of_direct_connections():
    async def scenario():
        try:
            opted_in = http_pool.get_async_client({"use_system_proxy": True})
            direct = http_pool.get_async_client({"use_system_proxy": False})
            assert direct is not opted_in
            assert not direct.trust_env and opted_in.trust_env
            assert http_pool.get_async_client({}) is direct
        finally:
            await http_pool.close_loop_clients()

    asyncio.run(scenario())


def test_stream_watchdog_first_token_idle_and_tool_call_budgets():
    aborted = []

    async def slow_first_token():
        await asyncio.sleep(0.25)
        yield {"type": "text", "delta": "late"}

    async def run_first_token():
        cfg = {
            "stream_first_token_timeout": 0.05,
            "stream_idle_timeout": 1.0,
            "stream_tool_timeout": 2.0,
        }
        guarded = stream_watchdog.guard(slow_first_token(), cfg, on_timeout=lambda: aborted.append("first"))
        with pytest.raises(apierr.ApiError) as exc_info:
            async for _ in guarded:
                pass
        assert exc_info.value.status == 504
        assert "first-token" in exc_info.value.message
        assert aborted == ["first"]

    asyncio.run(run_first_token())

    async def slow_mid_stream():
        yield {"type": "text", "delta": "hello "}
        await asyncio.sleep(0.25)
        yield {"type": "text", "delta": "world"}

    async def run_idle():
        aborted.clear()
        cfg = {
            "stream_first_token_timeout": 1.0,
            "stream_idle_timeout": 0.05,
            "stream_tool_timeout": 2.0,
        }
        chunks = []
        guarded = stream_watchdog.guard(slow_mid_stream(), cfg, on_timeout=lambda: aborted.append("idle"))
        with pytest.raises(apierr.ApiError) as exc_info:
            async for ev in guarded:
                chunks.append(ev)
        assert len(chunks) == 1
        assert exc_info.value.status == 504
        assert "idle timeout" in exc_info.value.message
        assert aborted == ["idle"]

    asyncio.run(run_idle())

    async def long_tool_call_stream():
        # Upstream announces a buffered tool call (partial=True, empty arguments) and stays silent while generating JSON
        yield {"type": "tool_call", "id": "call_1", "name": "write_file", "partial": True, "arguments": ""}
        await asyncio.sleep(0.12)  # exceeds idle (0.04s) but within stream_tool_timeout (0.5s)!
        yield {"type": "tool_call", "id": "call_1", "name": "write_file", "arguments": "{\"ok\":true}"}
        yield {"type": "end", "reason": "tool_calls"}

    async def run_tool_call():
        cfg = {
            "stream_first_token_timeout": 0.5,
            "stream_idle_timeout": 0.04,
            "stream_tool_timeout": 0.5,
        }
        collected = [ev async for ev in stream_watchdog.guard(long_tool_call_stream(), cfg)]
        assert collected[-1] == {"type": "end", "reason": "tool_calls"}

    asyncio.run(run_tool_call())


def test_driver_registry_resolves_retool_and_minara_and_dispatches_session():
    retool_drv = drivers.get_driver({"upstream": "retool"})
    minara_drv = drivers.get_driver({"upstream": "minara"})
    assert retool_drv.driver_id == "retool"
    assert retool_drv.capabilities.uses_retool_tool_schema is True
    assert minara_drv.driver_id == "minara"
    assert minara_drv.capabilities.uses_retool_tool_schema is False

    class DummyCustomDriver(drivers.UpstreamDriver):
        driver_id = "dummy-test"
        capabilities = drivers.DriverCapabilities(driver_id="dummy-test")

        async def stream_turn(self, cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens, chunks=None):
            yield {"type": "text", "delta": f"echo:{prompt}"}
            yield {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 2}}
            yield {"type": "end", "reason": "stop"}

    drivers.register_driver(DummyCustomDriver())
    try:
        cfg = {
            "upstream": "dummy-test",
            "transport": "direct",
            "turn_timeout": 30,
            "stream_first_token_timeout": 5,
            "stream_idle_timeout": 5,
            "stream_tool_timeout": 5,
        }
        backend = session_module.Backend(cfg)
        assert backend.refresher is None
        sess = session_module.RelaySession(backend, "direct-test")
        sess._direct = {"head": "sys", "tools": [], "tool_choice": None, "max_tokens": 64, "chunks": None}

        async def run_turn():
            return [ev async for ev in sess.stream("ping", {"model": "test"})]

        events = asyncio.run(run_turn())
        assert events[0] == {"type": "text", "delta": "echo:ping"}
        assert events[-1] == {"type": "end", "reason": "stop"}
    finally:
        drivers.DRIVERS.pop("dummy-test", None)
