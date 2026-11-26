"""R2 universal protocol & preset regression tests."""
from __future__ import annotations

import asyncio
import json
import types
import uuid

import httpx
import pytest

import adapters
import apierr
from catalog import discovery
from drivers import (
    anthropic_messages,
    cache_engine,
    openai_chat,
    openai_responses,
    presets,
    sse_utils,
)
import validate


def _dummy_session(
    model: str = "gpt-5",
    *,
    reasoning: bool = True,
    conv_key: str | None = "conv-1",
    cred_scope: str | None = None,
    provider_options: dict | None = None,
    bound_extra: dict | None = None,
):
    bound = {"upstream_model": model, "reasoning": reasoning}
    if bound_extra:
        bound.update(bound_extra)
    return types.SimpleNamespace(
        _cancelled=False,
        _bound_entry=bound,
        _native_conversation_key=conv_key,
        _credential_scope=cred_scope,
        _provider_options=provider_options or {},
        _provider_request_sent=None,
        _active_direct_abort=None,
        _pending_history_commit=None,
        backend=types.SimpleNamespace(),
    )


def _make_cfg(**overrides) -> dict:
    cfg = {
        "upstream": "custom",
        "provider_id": "test-prov",
        "provider_name": "TestProvider",
        "preset": "openai",
        "base_url": "https://api.example.com/v1",
        "base": "https://api.example.com/v1",
        "api_key": "sk-test-secret",
        "bearer_token": "tok-test-secret",
        "direct_cache": True,
        "cache_policy": "auto",
    }
    cfg.update(overrides)
    return cfg


def _sse_bytes(frames: list[tuple[str | None, dict | str]]) -> bytes:
    parts: list[str] = []
    for ev_name, payload in frames:
        if ev_name:
            parts.append(f"event: {ev_name}\n")
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        parts.append(f"data: {data}\n\n")
    return "".join(parts).encode("utf-8")


def _collect(async_gen):
    async def _run():
        return [ev async for ev in async_gen]

    return asyncio.run(_run())


def _capture_chat_request(cfg: dict, sess, model_provider: dict, *, max_tokens: int | None = None):
    captured: list[tuple[dict, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append((dict(req.headers), json.loads(req.content.decode("utf-8"))))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    run_cfg = dict(cfg, _http_transport=httpx.MockTransport(handler))
    driver = openai_chat.OpenAIChatDriver()
    events = _collect(driver.stream_turn(run_cfg, sess, model_provider, "hello", None, None, None, max_tokens))
    assert any(ev.get("type") == "end" and ev.get("reason") == "stop" for ev in events)
    assert len(captured) == 1
    return captured[0]


# ---------------------------------------------------------------------------
# 1. Presets: Z.ai, Google Vertex, and all 18 built-in presets
# ---------------------------------------------------------------------------


def test_all_18_presets_have_valid_protocol_auth_host_and_expand():
    assert len(presets.PRESETS) == 18
    assert "zai" in presets.PRESETS
    assert "vertex" in presets.PRESETS

    for pid, spec in presets.PRESETS.items():
        assert spec["name"]
        assert spec["driver"] in presets.UNIVERSAL_DRIVERS
        assert spec["auth_mode"] in presets.AUTH_MODES
        assert spec["cache_policy"] in presets.CACHE_POLICIES

        conn = presets.identify_connection(preset=pid)
        assert conn["preset"] == pid
        assert conn["driver"] == spec["driver"]
        assert conn["auth_mode"] == spec["auth_mode"]
        assert conn["base_url"] == presets.normalize_base_url(spec["base_url"])

        prov = {"id": f"p-{pid}", "preset": pid}
        if spec["auth_mode"] != "none":
            prov["api_key"] = f"key-{pid}-0123456789"
        meta, records, _secrets = presets.expand_provider_to_accounts(prov, "provider")
        assert meta["driver"] == spec["driver"]
        assert meta["auth_mode"] == spec["auth_mode"]
        hdrs = discovery.build_auth_headers(records[0])
        if spec["auth_mode"] == "bearer":
            assert hdrs.get("Authorization") == f"Bearer key-{pid}-0123456789"
        elif spec["auth_mode"] == "x-api-key":
            assert hdrs.get("x-api-key") == f"key-{pid}-0123456789"
            assert "anthropic-version" in hdrs
        elif spec["auth_mode"] == "none":
            assert "Authorization" not in hdrs and "x-api-key" not in hdrs


def test_zai_and_vertex_url_detection_and_normalization():
    assert presets.detect_preset_from_url("https://api.z.ai/api/paas/v4") == "zai"
    assert presets.detect_preset_from_url("https://open.bigmodel.cn/api/paas/v4") == "zai"
    assert presets.normalize_base_url("https://api.z.ai") == "https://api.z.ai/api/paas/v4"
    assert presets.normalize_base_url("https://open.bigmodel.cn/") == "https://open.bigmodel.cn/api/paas/v4"
    assert presets.normalize_base_url("https://api.z.ai/api/paas/v4/chat/completions") == "https://api.z.ai/api/paas/v4"

    zai_conn = presets.identify_connection(base_url="https://open.bigmodel.cn/api/paas/v4")
    assert zai_conn["preset"] == "zai"
    assert zai_conn["driver"] == "openai-chat"
    assert zai_conn["auth_mode"] == "bearer"

    assert presets.detect_preset_from_url("https://aiplatform.googleapis.com/v1beta1/projects/p/locations/global/endpoints/openapi") == "vertex"
    assert presets.detect_preset_from_url("https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/endpoints/openapi") == "vertex"

    vertex_conn = presets.identify_connection(
        base_url="https://europe-west4-aiplatform.googleapis.com/v1/projects/my-proj/locations/europe-west4/endpoints/openapi"
    )
    assert vertex_conn["preset"] == "vertex"
    assert vertex_conn["driver"] == "openai-chat"
    assert vertex_conn["auth_mode"] == "bearer"

    # Vertex must never receive OpenAI's prompt_cache_key even when model starts with google/
    vertex_body = {"messages": [{"role": "user", "content": "hi"}]}
    cache_engine.apply_openai_cache_hints(
        vertex_body, _make_cfg(preset="vertex"), _dummy_session("google/gemini-2.5-pro"), "google/gemini-2.5-pro"
    )
    assert "prompt_cache_key" not in vertex_body


# ---------------------------------------------------------------------------
# 2. OpenAI Chat: OpenRouter reasoning, Z.ai/Moonshot thinking, local/model guards, max_completion_tokens
# ---------------------------------------------------------------------------


def test_openrouter_sends_structured_reasoning_and_strips_anthropic_sampling():
    sess = _dummy_session(
        "anthropic/claude-sonnet-4.6",
        reasoning=True,
        provider_options={"temperature": 0.4, "top_p": 0.9},
    )
    _hdrs, body = _capture_chat_request(
        _make_cfg(preset="openrouter", base_url="https://openrouter.ai/api/v1", direct_cache=False),
        sess,
        {"model": "anthropic/claude-sonnet-4.6", "effort": "high"},
        max_tokens=2048,
    )
    assert body["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in body
    assert body["max_tokens"] == 2048
    assert "temperature" not in body and "top_p" not in body

    # Non-reasoning model on OpenRouter does NOT get `reasoning`
    non_r_sess = _dummy_session("meta-llama/llama-3.3-70b", reasoning=False)
    _, non_r_body = _capture_chat_request(
        _make_cfg(preset="openrouter", base_url="https://openrouter.ai/api/v1", direct_cache=False),
        non_r_sess,
        {"model": "meta-llama/llama-3.3-70b", "effort": "high"},
    )
    assert "reasoning" not in non_r_body and "reasoning_effort" not in non_r_body

    # effort="none" or thinking disabled does NOT send active reasoning
    none_sess = _dummy_session("anthropic/claude-sonnet-4.6", reasoning=True)
    _, none_body = _capture_chat_request(
        _make_cfg(preset="openrouter", base_url="https://openrouter.ai/api/v1", direct_cache=False),
        none_sess,
        {"model": "anthropic/claude-sonnet-4.6", "effort": "none"},
    )
    assert "reasoning" not in none_body and "reasoning_effort" not in none_body

    # OpenAI reasoning model via OpenRouter uses max_completion_tokens and strips temperature
    oa_or_sess = _dummy_session(
        "openai/gpt-5.4",
        reasoning=True,
        provider_options={"temperature": 0.5, "top_p": 0.8},
        bound_extra={"provider": "openai"},
    )
    _, oa_or_body = _capture_chat_request(
        _make_cfg(preset="openrouter", base_url="https://openrouter.ai/api/v1", direct_cache=False),
        oa_or_sess,
        {"model": "openai/gpt-5.4", "effort": "medium"},
        max_tokens=1024,
    )
    assert oa_or_body["reasoning"] == {"effort": "medium"}
    assert oa_or_body["max_completion_tokens"] == 1024
    assert "max_tokens" not in oa_or_body
    assert "temperature" not in oa_or_body and "top_p" not in oa_or_body


def test_zai_and_moonshot_thinking_param_and_active_tool_loop_replay():
    # Z.ai enabled via effort, disabled via effort="none" or thinking.type="disabled", omitted when unconfigured
    _, zai_on = _capture_chat_request(
        _make_cfg(preset="zai", base_url="https://api.z.ai/api/paas/v4", direct_cache=False),
        _dummy_session("glm-5.3", reasoning=True),
        {"model": "glm-5.3", "effort": "high"},
    )
    assert zai_on["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in zai_on

    _, zai_off = _capture_chat_request(
        _make_cfg(preset="custom", base_url="https://open.bigmodel.cn/api/paas/v4", direct_cache=False),
        _dummy_session("glm-5.3", reasoning=True),
        {"model": "glm-5.3", "effort": "none"},
    )
    assert zai_off["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in zai_off

    _, zai_plain = _capture_chat_request(
        _make_cfg(preset="zai", base_url="https://api.z.ai/api/paas/v4", direct_cache=False),
        _dummy_session("glm-5.3", reasoning=True),
        {"model": "glm-5.3"},
    )
    assert "thinking" not in zai_plain and "reasoning_effort" not in zai_plain

    # Moonshot/Kimi K2.5 enabled strips non-1 temperature and uses thinking={"type": "enabled"}
    _, kimi_on = _capture_chat_request(
        _make_cfg(preset="moonshot", base_url="https://api.moonshot.ai/v1", direct_cache=False),
        _dummy_session("kimi-k2.5", reasoning=True, provider_options={"temperature": 0.6}),
        {"model": "kimi-k2.5", "effort": "medium"},
    )
    assert kimi_on["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in kimi_on
    assert "temperature" not in kimi_on

    # Active tool-call loop replays reasoning_content for zai
    canonical = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "glm thought", "signature": "unsigned:reasoning_content"},
                {"type": "tool_call", "toolCallId": "c1", "toolName": "calc", "toolInput": {"x": 1}},
            ],
        },
        {"role": "tool", "content": {"toolCallId": "c1", "toolResult": "2"}},
    ]
    msgs = openai_chat._canonical_to_chat_messages(canonical, preset="zai")
    assert msgs[1]["reasoning_content"] == "glm thought"


def test_ollama_lmstudio_nonreasoning_guards_and_max_completion_tokens():
    for local_preset, base in (
        ("ollama", "http://127.0.0.1:11434/v1"),
        ("lmstudio", "http://127.0.0.1:1234/v1"),
        ("", "http://127.0.0.1:11434/v1"),
    ):
        _, body = _capture_chat_request(
            _make_cfg(preset=local_preset, base_url=base, base=base, direct_cache=False, allow_insecure_http=True),
            _dummy_session("qwen3:32b", reasoning=True, provider_options={"temperature": 0.4}),
            {"model": "qwen3:32b", "effort": "high"},
            max_tokens=512,
        )
        assert "stream_options" not in body
        assert "reasoning_effort" not in body
        assert body["max_tokens"] == 512
        assert body["temperature"] == 0.4

    # Non-reasoning OpenAI model (e.g. gpt-4.1) gets NO reasoning_effort, uses max_tokens, preserves temperature
    _, gpt41_body = _capture_chat_request(
        _make_cfg(preset="openai", base_url="https://api.openai.com/v1", direct_cache=False),
        _dummy_session("gpt-4.1", reasoning=False, provider_options={"temperature": 0.2, "top_p": 0.8}),
        {"model": "gpt-4.1", "effort": "high"},
        max_tokens=400,
    )
    assert "reasoning_effort" not in gpt41_body
    assert gpt41_body["max_tokens"] == 400
    assert "max_completion_tokens" not in gpt41_body
    assert gpt41_body["temperature"] == 0.2 and gpt41_body["top_p"] == 0.8

    # Reasoning OpenAI model (gpt-5.4) gets reasoning_effort, max_completion_tokens, strips temperature/top_p
    _, gpt5_body = _capture_chat_request(
        _make_cfg(preset="openai", base_url="https://api.openai.com/v1", direct_cache=False),
        _dummy_session("gpt-5.4", reasoning=True, provider_options={"temperature": 0.2, "top_p": 0.8}),
        {"model": "gpt-5.4", "effort": "high"},
        max_tokens=400,
    )
    assert gpt5_body["reasoning_effort"] == "high"
    assert gpt5_body["max_completion_tokens"] == 400
    assert "max_tokens" not in gpt5_body
    assert "temperature" not in gpt5_body and "top_p" not in gpt5_body

    # Explicit max_completion_tokens capability on another preset (e.g. groq/custom)
    _, cap_body = _capture_chat_request(
        _make_cfg(preset="groq", base_url="https://api.groq.com/openai/v1", direct_cache=False),
        _dummy_session("openai/gpt-oss-120b", reasoning=True, bound_extra={"max_completion_tokens": True}),
        {"model": "openai/gpt-oss-120b", "effort": "low"},
        max_tokens=300,
    )
    assert cap_body["reasoning_effort"] == "low"
    assert cap_body["max_completion_tokens"] == 300
    assert "max_tokens" not in cap_body

    # Explicit operator body_overrides can still override Ollama guards
    _, override_body = _capture_chat_request(
        _make_cfg(
            preset="ollama",
            base_url="http://127.0.0.1:11434/v1",
            direct_cache=False,
            allow_insecure_http=True,
            body_overrides={"stream_options": {"include_usage": True}, "reasoning_effort": "low"},
        ),
        _dummy_session("qwen3:32b", reasoning=True),
        {"model": "qwen3:32b", "effort": "high"},
    )
    assert override_body["stream_options"] == {"include_usage": True}
    assert override_body["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# 3. OpenAI Responses: raw `response.reasoning_text.delta`
# ---------------------------------------------------------------------------


def test_openai_responses_handles_raw_reasoning_text_delta():
    sse = _sse_bytes([
        ("response.reasoning_text.delta", {"type": "response.reasoning_text.delta", "delta": "Raw step 1. "}),
        ("response.reasoning_summary_text.delta", {"type": "response.reasoning_summary_text.delta", "delta": "Summary."}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Final answer"}),
        ("response.completed", {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 4}}}),
    ])
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)
    )
    driver = openai_responses.OpenAIResponsesDriver()
    sess = _dummy_session("gpt-5")
    cfg = _make_cfg(preset="openai-responses", direct_cache=False, _http_transport=transport)
    events = _collect(driver.stream_turn(cfg, sess, {"model": "gpt-5"}, "hi", None, None, None, None))

    thinking_deltas = [ev["delta"] for ev in events if ev.get("type") == "thinking"]
    assert thinking_deltas == ["Raw step 1. ", "Summary."]
    assert any(ev.get("type") == "thinking_start" for ev in events)
    assert any(ev.get("type") == "text" and ev.get("delta") == "Final answer" for ev in events)

    # Verify adapter compatibility (ResponsesAdapter consumes events cleanly without KeyError)
    parsed = validate.parse_responses({"model": "claude-opus-5-5", "input": "hi"}, {"strict_models": True, "model": "claude-opus-5-5"})
    adapter = adapters.ResponsesAdapter(parsed)
    for ev in events:
        adapter.feed(ev)
    assert adapter.thinking == "Raw step 1. Summary."


# ---------------------------------------------------------------------------
# 4. Anthropic Messages: multi-block thinking signatures
# ---------------------------------------------------------------------------


def test_anthropic_messages_emits_all_multi_block_thinking_signatures_without_duplicate():
    sse = _sse_bytes([
        ("message_start", {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 10}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "First plan"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_block_0"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"q\":\"x\"}"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("content_block_start", {"type": "content_block_start", "index": 2, "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 2, "delta": {"type": "thinking_delta", "thinking": "Second plan"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 2, "delta": {"type": "signature_delta", "signature": "sig_block_2"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 2}),
        # Duplicate stop for block 2 must NOT emit signature twice!
        ("content_block_stop", {"type": "content_block_stop", "index": 2}),
        ("content_block_start", {"type": "content_block_start", "index": 3, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 3, "delta": {"type": "text_delta", "text": "Done"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 3}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ])
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)
    )
    driver = anthropic_messages.AnthropicMessagesDriver()
    sess = _dummy_session("claude-sonnet-4-6")
    cfg = _make_cfg(preset="anthropic", base_url="https://api.anthropic.com/v1", direct_cache=False, _http_transport=transport)
    events = _collect(driver.stream_turn(cfg, sess, {"model": "claude-sonnet-4-6"}, "hi", None, None, None, 1024))

    sigs = [ev["signature"] for ev in events if ev.get("type") == "thinking_signature"]
    assert sigs == ["sig_block_0", "sig_block_2"]


# ---------------------------------------------------------------------------
# 5. xAI `x-grok-conv-id` header vs DeepSeek automatic
# ---------------------------------------------------------------------------


def test_xai_emits_scoped_grok_conv_id_header_and_no_prompt_cache_key():
    seen: list[tuple[dict, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(({k.lower(): v for k, v in req.headers.items()}, json.loads(req.content.decode("utf-8"))))
        if req.url.path.endswith("/responses"):
            sse = _sse_bytes([
                ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "ok"}),
                ("response.completed", {"type": "response.completed", "response": {}}),
            ])
        else:
            sse = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    transport = httpx.MockTransport(handler)
    chat_driver = openai_chat.OpenAIChatDriver()
    resp_driver = openai_responses.OpenAIResponsesDriver()

    cfg_a = _make_cfg(preset="xai", base_url="https://api.x.ai/v1", account_id="xai-acc-1", api_key="xai-secret-1", _http_transport=transport)
    cfg_b = _make_cfg(preset="xai", base_url="https://api.x.ai/v1", account_id="xai-acc-2", api_key="xai-secret-2", _http_transport=transport)

    s_a1 = _dummy_session("grok-4", conv_key="raw-secret-conv-1", cred_scope="client-key-A")
    s_a1_again = _dummy_session("grok-4", conv_key="raw-secret-conv-1", cred_scope="client-key-A")
    s_a2 = _dummy_session("grok-4", conv_key="raw-secret-conv-2", cred_scope="client-key-A")
    s_b1 = _dummy_session("grok-4", conv_key="raw-secret-conv-1", cred_scope="client-key-B")
    s_no_conv = _dummy_session("grok-4", conv_key=None, cred_scope="client-key-A")

    _collect(chat_driver.stream_turn(cfg_a, s_a1, {"model": "grok-4"}, "hi", None, None, None, None))
    _collect(chat_driver.stream_turn(cfg_a, s_a1_again, {"model": "grok-4"}, "hi", None, None, None, None))
    _collect(chat_driver.stream_turn(cfg_a, s_a2, {"model": "grok-4"}, "hi", None, None, None, None))
    _collect(chat_driver.stream_turn(cfg_a, s_b1, {"model": "grok-4"}, "hi", None, None, None, None))
    _collect(chat_driver.stream_turn(cfg_b, s_a1, {"model": "grok-4"}, "hi", None, None, None, None))
    _collect(chat_driver.stream_turn(cfg_a, s_no_conv, {"model": "grok-4"}, "hi", None, None, None, None))
    _collect(resp_driver.stream_turn(cfg_a, s_a1, {"model": "grok-4"}, "hi", None, None, None, None))

    conv_ids = [hdrs.get("x-grok-conv-id") for hdrs, _body in seen]
    # All active turns have valid UUID x-grok-conv-id and zero prompt_cache_key in body
    for idx in (0, 1, 2, 3, 4, 6):
        assert conv_ids[idx]
        uuid.UUID(conv_ids[idx])
        assert "raw-secret-conv" not in conv_ids[idx]
        assert "xai-secret" not in conv_ids[idx]
        assert "prompt_cache_key" not in seen[idx][1]

    # Same conv + same client + same upstream cred -> identical across turns & drivers
    assert conv_ids[0] == conv_ids[1] == conv_ids[6]
    # Different conversation, different client credential, or different upstream credential -> strictly distinct!
    assert conv_ids[0] != conv_ids[2]
    assert conv_ids[0] != conv_ids[3]
    assert conv_ids[0] != conv_ids[4]
    # Missing conversation key -> no header
    assert conv_ids[5] is None

    # Explicit custom header is preserved and DeepSeek gets neither header nor prompt_cache_key
    hdrs_custom, _body = _capture_chat_request(
        _make_cfg(preset="xai", base_url="https://api.x.ai/v1", headers={"X-Grok-Conv-Id": "manual-id"}),
        s_a1,
        {"model": "grok-4"},
    )
    assert hdrs_custom.get("x-grok-conv-id") == "manual-id"

    hdrs_ds, body_ds = _capture_chat_request(
        _make_cfg(preset="deepseek", base_url="https://api.deepseek.com"),
        s_a1,
        {"model": "deepseek-reasoner"},
    )
    assert "x-grok-conv-id" not in hdrs_ds
    assert "prompt_cache_key" not in body_ds

    # Custom/empty preset pointing at api.x.ai must also emit x-grok-conv-id and NEVER prompt_cache_key
    hdrs_xai_url, body_xai_url = _capture_chat_request(
        _make_cfg(preset="custom", base_url="https://api.x.ai/v1", account_id="xai-acc-url"),
        s_a1,
        {"model": "gpt-5"},
    )
    assert hdrs_xai_url.get("x-grok-conv-id")
    assert "prompt_cache_key" not in body_xai_url


# ---------------------------------------------------------------------------
# 6. SSE EOF handling for unterminated / truncated vs valid trailing frames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chunks, expected",
    [
        # Valid complete JSON / [DONE] without trailing newline (trailing-newline-optional mocks)
        ([b'data: {"ok": true}'], [(None, '{"ok": true}')]),
        ([b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\ndata: [DONE]'], [
            (None, '{"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}'),
            (None, "[DONE]"),
        ]),
        ([b'event: message_stop\ndata: {"type":"message_stop"}'], [("message_stop", '{"type":"message_stop"}')]),
        # Trailing newline present, blank line omitted
        ([b"data: hello\n"], [(None, "hello")]),
        # Trailing SSE comment without newline
        ([b'data: [DONE]\n\n: keepalive'], [(None, "[DONE]")]),
    ],
)
def test_sse_eof_accepts_valid_trailing_newline_optional_frames(chunks, expected):
    async def _bytes():
        for c in chunks:
            yield c

    got = _collect(sse_utils.aiter_sse_events(_bytes()))
    assert got == expected


@pytest.mark.parametrize(
    "chunks",
    [
        [b'data: {"choices":[{"delta":{"content":"hel'],  # truncated mid-JSON without newline
        [b'data: {"choices":[{"delta":{"content":"hel\n'],  # truncated mid-JSON with single newline
        [b"data: [DO"],  # truncated [DONE] without newline
        [b"data: "],  # empty unterminated data: at EOF
        [b"data: partial_unterminated"],  # non-JSON unterminated data: without newline
        [b'data: {"a": 1}\n\ndat'],  # truncated field name at EOF
        [b"data: \xff\xfe"],  # invalid UTF-8 at EOF
    ],
)
def test_sse_eof_rejects_truncated_or_unterminated_frames(chunks):
    async def _bytes():
        for c in chunks:
            yield c

    with pytest.raises(apierr.ApiError) as ei:
        _collect(sse_utils.aiter_sse_events(_bytes()))
    assert ei.value.code == "upstream_error"


def test_drivers_accept_trailing_newline_optional_complete_sse_and_reject_truncated_eof():
    # Valid trailing-newline-optional Chat & Messages mocks succeed end-to-end
    chat_sse = b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\ndata: [DONE]'
    _, chat_body = _capture_chat_request(
        _make_cfg(preset="openai", direct_cache=False),
        _dummy_session("gpt-5"),
        {"model": "gpt-5"},
    )
    assert chat_body["model"] == "gpt-5"

    chat_drv = openai_chat.OpenAIChatDriver()
    chat_events = _collect(
        chat_drv.stream_turn(
            _make_cfg(
                preset="openai",
                direct_cache=False,
                _http_transport=httpx.MockTransport(
                    lambda _r: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chat_sse)
                ),
            ),
            _dummy_session("gpt-5"),
            {"model": "gpt-5"},
            "hi",
            None,
            None,
            None,
            None,
        )
    )
    assert any(ev.get("type") == "text" and ev.get("delta") == "hi" for ev in chat_events)
    assert chat_events[-1] == {"type": "end", "reason": "stop"}

    # Truncated mid-JSON data: line at EOF raises upstream_error
    trunc_sse = b'data: {"choices":[{"delta":{"content":"hel'
    with pytest.raises(apierr.ApiError) as ei:
        _collect(
            chat_drv.stream_turn(
                _make_cfg(
                    preset="openai",
                    direct_cache=False,
                    _http_transport=httpx.MockTransport(
                        lambda _r: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=trunc_sse)
                    ),
                ),
                _dummy_session("gpt-5"),
                {"model": "gpt-5"},
                "hi",
                None,
                None,
                None,
                None,
            )
        )
    assert ei.value.code == "upstream_error"
