"""Stage 2 tests: models.dev enrichment, /models auto-discovery, route aliases/fallbacks & OpenCode sync."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from catalog import discovery, models_dev, router
import config as config_module
import drivers
from drivers import http_pool
import models as model_catalog
import proxy
from scripts.probe_client_settings import routable_entries
from scripts.sync_opencode_catalog import update_document
import validate


def test_models_dev_enrichment_fuzzy_matching_and_usd_cost():
    meta = models_dev.lookup_metadata("anthropic/claude-sonnet-4.6")
    assert meta["family"] == "anthropic"
    assert meta["context"] == 1000000
    assert meta["max_output"] == 128000
    assert meta["vision"] is True
    assert meta["pdf"] is True
    assert meta["pricing"]["input"] == 3.0
    assert meta["pricing"]["output"] == 15.0
    assert meta["pricing"]["cache_read"] == 0.3

    free_meta = models_dev.lookup_metadata("deepseek/deepseek-r1:free")
    assert free_meta["pricing"] == {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}

    # Unknown local model gets permissive defaults (vision=True, tools=True) so requests are never blocked with 422
    local_meta = models_dev.lookup_metadata("qwen3-custom-local:32b", provider_hint="ollama")
    assert local_meta["vision"] is True
    assert local_meta["tools"] is True
    assert local_meta["pricing"] is None

    # Exact USD cost calculation: 10,000 total input (8,000 cache_read + 1,000 cache_write + 1,000 uncached) + 2,000 output
    cost = models_dev.calculate_cost_usd(
        meta,
        {
            "input_tokens": 10000,
            "cache_read_input_tokens": 8000,
            "cache_creation_input_tokens": 1000,
            "output_tokens": 2000,
        },
    )
    # (1000*3.0 + 8000*0.3 + 1000*3.75 + 2000*15.0) / 1_000_000 = (3000 + 2400 + 3750 + 30000) / 1e6 = 0.03915
    assert cost == pytest.approx(0.03915)


def test_live_models_discovery_openrouter_pricing_filters_and_anthropic_pagination():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "anthropic/claude-sonnet-4.6",
                            "name": "Anthropic: Claude Sonnet 4.6",
                            "context_length": 1000000,
                            "top_provider": {"max_completion_tokens": 128000},
                            "architecture": {"input_modalities": ["text", "image", "file"], "output_modalities": ["text"]},
                            "supported_parameters": ["tools", "reasoning"],
                            "pricing": {
                                "prompt": "0.000003",
                                "completion": "0.000015",
                                "input_cache_read": "0.0000003",
                                "input_cache_write": "0.00000375",
                            },
                        },
                        {
                            "id": "openai/text-embedding-3-large",
                            "name": "Embedding",
                        },
                        {
                            "id": "deepseek/deepseek-chat",
                            "name": "DeepSeek V3",
                            "context_length": 128000,
                            "pricing": {"prompt": "0.00000028", "completion": "0.00000042"},
                        },
                    ]
                },
            )
        if request.url.host == "api.anthropic.com":
            after = request.url.params.get("after_id")
            if not after:
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "claude-opus-4-6", "display_name": "Claude Opus 4.6"}],
                        "has_more": True,
                        "last_id": "claude-opus-4-6",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-haiku-4-5-20251001", "display_name": "Claude Haiku 4.5"}],
                    "has_more": False,
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    async def scenario():
        try:
            or_models = await discovery.discover_provider_models(
                {
                    "driver": "openai-chat",
                    "base_url": "https://openrouter.ai/api/v1",
                    "auth_mode": "bearer",
                    "api_key": "sk-or-v1-test123456",
                    "model_blacklist": ["deepseek/*"],
                    "_http_transport": transport,
                }
            )
            # Embedding filtered automatically; deepseek filtered by blacklist!
            assert [m["id"] for m in or_models] == ["anthropic/claude-sonnet-4.6"]
            assert or_models[0]["pricing"] == {
                "input": 3.0,
                "output": 15.0,
                "cache_read": 0.3,
                "cache_write": 3.75,
            }

            anth_models = await discovery.discover_provider_models(
                {
                    "driver": "anthropic-messages",
                    "base_url": "https://api.anthropic.com/v1",
                    "auth_mode": "x-api-key",
                    "api_key": "sk-ant-test123456",
                    "_http_transport": transport,
                }
            )
            assert [m["id"] for m in anth_models] == ["claude-opus-4-6", "claude-haiku-4-5-20251001"]
        finally:
            await http_pool.close_loop_clients()

    asyncio.run(scenario())


def test_universal_catalog_virtual_profiles_provider_options_and_opencode_sync(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "openrouter",
                        "preset": "openrouter",
                        "api_key": "sk-or-v1-secret123456",
                        "models": [
                            "anthropic/claude-sonnet-4.6",
                            {"id": "openai/gpt-5.4", "label": "GPT-5.4 via OR"},
                        ],
                    }
                ],
                "routes": [
                    {
                        "id": "sonnet-ha",
                        "label": "Sonnet High Availability",
                        "targets": ["openrouter/anthropic/claude-sonnet-4.6"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cfg = config_module.load(str(cfg_path))
    # Only universal models & route aliases are routable (Retool models stay unpublished!)
    published = [e["id"] for e in model_catalog.catalog(cfg) if model_catalog.catalog_routable(e, cfg)]
    assert published == [
        "openrouter/anthropic/claude-sonnet-4.6",
        "openrouter/openai/gpt-5.4",
        "sonnet-ha",
    ]

    # Virtual @profile + #effort parsing works seamlessly on universal provider IDs!
    parsed_name = model_catalog.parse_model_name("openrouter/anthropic/claude-sonnet-4.6@fast#high", cfg)
    assert parsed_name.base == "openrouter/anthropic/claude-sonnet-4.6"
    assert parsed_name.profile == "fast"
    assert parsed_name.effort == "high"

    # Sampling controls & parallel_tool_calls=False are accepted (not rejected or ignored) on universal drivers
    req = validate.parse_chat(
        {
            "model": "openrouter/openai/gpt-5.4",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
            "top_p": 0.9,
            "parallel_tool_calls": False,
            "tools": [{"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}],
        },
        cfg,
    )
    assert req.provider_options["temperature"] == 0.2
    assert req.provider_options["parallel_tool_calls"] is False
    assert "temperature" not in req.declared_ignored

    # OpenCode catalog sync discovers the exact published universal models & route aliases!
    sync_entries = routable_entries(str(cfg_path))
    assert [e["id"] for e in sync_entries] == published
    doc = {"provider": {"retool-relay": {"models": {}}}}
    updated = update_document(doc, sync_entries, default_model="sonnet-ha")
    assert set(updated["provider"]["retool-relay"]["models"].keys()) == set(published)


def test_route_alias_cross_provider_failover_end_to_end(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    calls = []

    class PrimaryDriver(drivers.UpstreamDriver):
        driver_id = "test-primary"
        capabilities = drivers.DriverCapabilities(driver_id="test-primary")

        async def stream_turn(self, cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens, chunks=None):
            calls.append(("primary", model_provider.get("model")))
            import apierr
            raise apierr.account_rate_limited("primary provider 429 rate limited", retry_after=10)
            yield  # pragma: no cover

    class BackupDriver(drivers.UpstreamDriver):
        driver_id = "test-backup"
        capabilities = drivers.DriverCapabilities(driver_id="test-backup")

        async def stream_turn(self, cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens, chunks=None):
            calls.append(("backup", model_provider.get("model"), tools))
            yield {"type": "text", "delta": "BACKUP_ANSWER_OK"}
            yield {"type": "usage", "usage": {"input_tokens": 10, "output_tokens": 4, "cost_usd": 0.0002}}
            yield {"type": "end", "reason": "stop"}

    drivers.register_driver(PrimaryDriver())
    drivers.register_driver(BackupDriver())
    try:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "relay_api_key": "test-stage2-secret-key",
                    "conversation_state": False,
                    "project_context": False,
                    "exec_policy": "off",
                    "upstream_rate_limit_retries": 0,
                    "providers": [
                        {
                            "id": "primary",
                            "preset": "anthropic",
                            "api_key": "sk-ant-primary-123456",
                            "models": ["claude-sonnet-4-6"],
                        },
                        {
                            "id": "backup",
                            "preset": "openrouter",
                            "api_key": "sk-or-v1-backup-123456",
                            "models": ["anthropic/claude-sonnet-4.6"],
                        },
                    ],
                    "routes": [
                        {
                            "id": "claude-ha",
                            "targets": ["primary/claude-sonnet-4-6", "backup/anthropic/claude-sonnet-4.6"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        cfg = config_module.load(str(cfg_path))
        cfg["journal_path"] = str(tmp_path / "journal.jsonl")
        # Swap test drivers onto the two loaded provider accounts
        cfg["accounts"][0]["upstream"] = cfg["accounts"][0]["driver"] = "test-primary"
        cfg["accounts"][1]["upstream"] = cfg["accounts"][1]["driver"] = "test-backup"
        cfg["_provider_models"]["primary/claude-sonnet-4-6"]["upstream"] = "test-primary"
        cfg["_provider_models"]["backup/anthropic/claude-sonnet-4.6"]["upstream"] = "test-backup"
        cfg["_route_aliases"] = router.build_route_aliases(cfg, model_catalog)

        rt = proxy.Runtime(cfg)
        import http.client
        import threading

        server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(rt), threading.BoundedSemaphore(4))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            conn.request(
                "POST",
                "/v1/chat/completions",
                json.dumps(
                    {
                        "model": "claude-ha",
                        "messages": [{"role": "user", "content": "Hello failover"}],
                        "tools": [{"type": "function", "function": {"name": "read_doc", "parameters": {"type": "object"}}}],
                    }
                ),
                {"Authorization": "Bearer test-stage2-secret-key", "Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200, body
            assert body["choices"][0]["message"]["content"] == "BACKUP_ANSWER_OK"
            # Verify primary was tried first with its upstream_model, then failed over to backup with backup's upstream_model
            # AND standard OpenAI tool schema (not Retool's converted schema)!
            assert calls[0] == ("primary", "claude-sonnet-4-6")
            assert calls[1][0] == "backup"
            assert calls[1][1] == "anthropic/claude-sonnet-4.6"
            assert calls[1][2][0]["type"] == "function"
            assert calls[1][2][0]["function"]["name"] == "read_doc"
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            rt.close()
    finally:
        drivers.DRIVERS.pop("test-primary", None)
        drivers.DRIVERS.pop("test-backup", None)
