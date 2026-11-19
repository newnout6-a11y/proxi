"""Stage 3 tests: OpenAI Chat, OpenAI Responses, Anthropic Messages drivers, 4-point cache, native history & signatures."""
from __future__ import annotations

import http.client
import json
import threading

import httpx
import pytest

import config as config_module
import conversation_state
from drivers import cache_engine, http_pool
import proxy


def _sse_bytes(frames: list[tuple[str | None, dict | str]]) -> bytes:
    chunks: list[str] = []
    for ev_name, payload in frames:
        if ev_name:
            chunks.append(f"event: {ev_name}\n")
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        chunks.append(f"data: {data}\n\n")
    return "".join(chunks).encode("utf-8")


@pytest.mark.parametrize("preset,model,frames,expected", [
    ("openai", "gpt-5.4", [
        (None, {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]})], 502),
    ("openai", "gpt-5.4", [
        (None, {"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]})], 200),
    ("openai-responses", "gpt-5.4", [
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hello"}),
        (None, "[DONE]")], 502),
    ("openai-responses", "gpt-5.4", [
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hello"}),
        ("response.completed", {"type": "response.completed", "response": {}})], 200),
    ("anthropic", "claude-sonnet-4-6", [
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}})], 502),
    ("anthropic", "claude-sonnet-4-6", [
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        ("message_stop", {"type": "message_stop"})], 200),
], ids=["chat-eof", "chat-finish", "responses-done-only", "responses-completed", "messages-eof", "messages-stop"])
def test_provider_requires_a_terminal_stream_frame_before_committing_history(monkeypatch, tmp_path, preset, model, frames, expected):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    sent, saved = [], []

    def handler(request):
        sent.append(request.method)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=_sse_bytes(frames))

    monkeypatch.setattr(cache_engine, "record_canonical_reply", lambda *_a, **_k: saved.append(True))
    path = tmp_path / "config.json"
    key = "stage3-terminal-frame-test-key"
    path.write_text(json.dumps({
        "relay_api_key": key, "workspace_root": str(tmp_path), "conversation_state": False,
        "project_context": False, "exec_policy": "off", "providers": [
            {"id": "probe", "preset": preset, "api_key": "stage3-synthetic-upstream-key", "models": [model]}]
    }), encoding="utf-8")
    cfg = config_module.load(str(path))
    cfg["journal_path"] = str(tmp_path / "journal.jsonl")
    cfg["accounts"][0]["_http_transport"] = httpx.MockTransport(handler)
    rt = proxy.Runtime(cfg)
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(rt), threading.BoundedSemaphore(4))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps({
            "model": "probe/" + model, "messages": [{"role": "user", "content": "Say hello"}],
        }), {"Authorization": "Bearer " + key, "Content-Type": "application/json", "x-proxi-session-id": "test"})
        with conn.getresponse() as reply:
            assert reply.status == expected
            result = json.loads(reply.read())
        assert sent == ["POST"]
        if expected == 200:
            assert result["choices"][0]["message"]["content"] == "hello"
        else:
            assert not saved
            assert result["error"]["code"] == "upstream_error"
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        rt.close()


def test_anthropic_driver_signed_thinking_replay_4point_cache_pdf_and_forced_choice(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    captured_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_bodies.append(body)
        turn_num = len(captured_bodies)
        if turn_num == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=_sse_bytes([
                    ("message_start", {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 120, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me inspect the PDF."}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_anth_cryptographic_001"}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_01", "name": "lookup", "input": {}}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"q\":\"test\"}"}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 45}}),
                    ("message_stop", {"type": "message_stop"}),
                ]),
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes([
                ("message_start", {"type": "message_start", "message": {"id": "msg_2", "usage": {"input_tokens": 25, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1120}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ANTHROPIC_DONE"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 10}}),
                ("message_stop", {"type": "message_stop"}),
            ]),
        )

    transport = httpx.MockTransport(handler)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({
            "relay_api_key": "stage3-anth-key",
            "conversation_state": True,
            "conversation_state_dir": str(tmp_path / "conversations"),
            "project_context": False,
            "exec_policy": "off",
            "providers": [
                {
                    "id": "anthropic",
                    "preset": "anthropic",
                    "api_key": "sk-ant-api03-test123456",
                    "models": ["claude-sonnet-4-6"],
                }
            ],
        }),
        encoding="utf-8",
    )
    cfg = config_module.load(str(cfg_path))
    cfg["journal_path"] = str(tmp_path / "journal.jsonl")
    cfg["accounts"][0]["_http_transport"] = transport

    rt = proxy.Runtime(cfg)
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(rt), threading.BoundedSemaphore(4))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        tools = [{"name": "lookup", "description": "Search", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}]
        # Turn 1: thinking + forced tool_choice ("any") + PDF document block
        conn.request(
            "POST",
            "/v1/messages",
            json.dumps({
                "model": "anthropic/claude-sonnet-4-6",
                "max_tokens": 4096,
                "system": "x-anthropic-billing-header: cc_version=2.1; cch=abc123;\nYou are a careful assistant.",
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high"},
                "tools": tools,
                "tool_choice": {"type": "any"},
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "document", "title": "spec.pdf", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQK"}},
                            {"type": "text", "text": "Inspect spec.pdf"},
                        ],
                    }
                ],
            }),
            {"x-api-key": "stage3-anth-key", "Content-Type": "application/json", "x-proxi-session-id": "sess-anth-1"},
        )
        r1 = conn.getresponse()
        b1 = json.loads(r1.read().decode("utf-8"))
        assert r1.status == 200, b1
        assert any(blk["type"] == "tool_use" and blk["name"] == "lookup" for blk in b1["content"])

        sent1 = captured_bodies[0]
        # Billing header stripped so cch= never fragments system prefix cache!
        assert sent1["system"][0]["text"] == "You are a careful assistant."
        assert "cache_control" in sent1["system"][-1]
        assert "cache_control" in sent1["tools"][-1]
        # Forced tool_choice ("any") automatically converted to "auto" + Gate instruction when thinking is active!
        assert sent1["tool_choice"] == {"type": "auto"}
        # Native PDF block preserved!
        assert sent1["messages"][0]["content"][0]["type"] == "document"

        # Turn 2: Client sends OpenAI Chat format WITHOUT signature — relay must replay signed thinking from native_history!
        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({
                "model": "anthropic/claude-sonnet-4-6",
                "reasoning_effort": "high",
                "tools": [{"type": "function", "function": {"name": "lookup", "description": "Search", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}],
                "messages": [
                    {"role": "system", "content": "You are a careful assistant."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "file", "file": {"filename": "spec.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQK"}},
                            {"type": "text", "text": "Inspect spec.pdf"},
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "toolu_01", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"test\"}"}}],
                    },
                    {"role": "tool", "tool_call_id": "toolu_01", "content": "Found 42"},
                ],
            }),
            {"Authorization": "Bearer stage3-anth-key", "Content-Type": "application/json", "x-proxi-session-id": "sess-anth-1"},
        )
        r2 = conn.getresponse()
        b2 = json.loads(r2.read().decode("utf-8"))
        assert r2.status == 200, b2
        assert b2["choices"][0]["message"]["content"] == "ANTHROPIC_DONE"

        sent2 = captured_bodies[1]
        assistant_turn = next(m for m in sent2["messages"] if m["role"] == "assistant")
        assert assistant_turn["content"][0] == {
            "type": "thinking",
            "thinking": "Let me inspect the PDF.",
            "signature": "sig_anth_cryptographic_001",
        }
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        rt.close()

    # Verify disk ConversationStore persisted the state cleanly without format/STAMP errors!
    store = conversation_state.open_store(cfg)
    try:
        loaded = store.load("header:sess-anth-1")
        assert loaded is not None
        assert len(loaded.state.branches) >= 1
    finally:
        store.close()


def test_openai_chat_deepseek_reasoning_loop_and_gemini_thought_signature(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append(body)
        n = len(captured)
        if n == 1:
            # Turn 1: emits reasoning_content + tool_call with Gemini extra_content.google.thought_signature
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=_sse_bytes([
                    (None, {"choices": [{"index": 0, "delta": {"reasoning_content": "Step 1 reasoning..."}}]}),
                    (None, {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_ds_1", "type": "function", "function": {"name": "calc", "arguments": "{\"x\":7}"}, "extra_content": {"google": {"thought_signature": "gem_sig_xyz"}}}]}}]}),
                    (None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 50, "completion_tokens": 20}}),
                    (None, "[DONE]"),
                ]),
            )
        if n == 2:
            # Turn 2: completes tool loop with final text
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=_sse_bytes([
                    (None, {"choices": [{"index": 0, "delta": {"reasoning_content": "Step 2 reasoning..."}}]}),
                    (None, {"choices": [{"index": 0, "delta": {"content": "Result is 49"}}]}),
                    (None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 80, "completion_tokens": 15, "prompt_tokens_details": {"cached_tokens": 50}}}),
                    (None, "[DONE]"),
                ]),
            )
        # Turn 3: new user question — older Turn 1 & Turn 2 reasoning_content MUST be stripped!
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes([
                (None, {"choices": [{"index": 0, "delta": {"content": "Followup OK"}}]}),
                (None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 5}}),
                (None, "[DONE]"),
            ]),
        )

    transport = httpx.MockTransport(handler)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({
            "relay_api_key": "stage3-ds-key",
            "conversation_state": False,
            "project_context": False,
            "exec_policy": "off",
            "providers": [
                {
                    "id": "deepseek",
                    "preset": "deepseek",
                    "api_key": "sk-deepseek-test123456",
                    "body_overrides": {"__remove__": ["service_tier"], "top_logprobs": None},
                    "models": ["deepseek-reasoner"],
                }
            ],
        }),
        encoding="utf-8",
    )
    cfg = config_module.load(str(cfg_path))
    cfg["journal_path"] = str(tmp_path / "journal.jsonl")
    cfg["accounts"][0]["_http_transport"] = transport

    rt = proxy.Runtime(cfg)
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(rt), threading.BoundedSemaphore(4))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    tools = [{"type": "function", "function": {"name": "calc", "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}}} ]
    try:
        m1 = [{"role": "user", "content": "Square 7"}]
        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({"model": "deepseek/deepseek-reasoner", "messages": m1, "tools": tools, "service_tier": "auto"}),
            {"Authorization": "Bearer stage3-ds-key", "Content-Type": "application/json", "x-proxi-session-id": "sess-ds-1"},
        )
        resp1 = json.loads(conn.getresponse().read().decode("utf-8"))
        assert resp1["choices"][0]["message"]["tool_calls"][0]["id"] == "call_ds_1"
        assert "service_tier" not in captured[0]  # removed by body_overrides.__remove__!

        # Turn 2: inside active tool-call loop -> reasoning_content AND Gemini extra_content MUST be replayed!
        m2 = [
            *m1,
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_ds_1", "type": "function", "function": {"name": "calc", "arguments": "{\"x\":7}"}}]},
            {"role": "tool", "tool_call_id": "call_ds_1", "content": "49"},
        ]
        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({"model": "deepseek/deepseek-reasoner", "messages": m2, "tools": tools}),
            {"Authorization": "Bearer stage3-ds-key", "Content-Type": "application/json", "x-proxi-session-id": "sess-ds-1"},
        )
        resp2 = json.loads(conn.getresponse().read().decode("utf-8"))
        assert resp2["choices"][0]["message"]["content"] == "Result is 49"
        sent2_asst = next(m for m in captured[1]["messages"] if m["role"] == "assistant")
        assert sent2_asst["reasoning_content"] == "Step 1 reasoning..."
        assert sent2_asst["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "gem_sig_xyz"}}

        # Turn 3: new plain user turn -> older Turn 1 & Turn 2 reasoning_content MUST be stripped!
        m3 = [*m2, {"role": "assistant", "content": "Result is 49"}, {"role": "user", "content": "Thanks!"}]
        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({"model": "deepseek/deepseek-reasoner", "messages": m3, "tools": tools}),
            {"Authorization": "Bearer stage3-ds-key", "Content-Type": "application/json", "x-proxi-session-id": "sess-ds-1"},
        )
        resp3 = json.loads(conn.getresponse().read().decode("utf-8"))
        assert resp3["choices"][0]["message"]["content"] == "Followup OK"
        for m in captured[2]["messages"]:
            if m["role"] == "assistant":
                assert "reasoning_content" not in m
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        rt.close()


def test_openai_responses_driver_stateless_encrypted_content_replay(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append(body)
        if len(captured) == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=_sse_bytes([
                    ("response.reasoning_summary_text.delta", {"type": "response.reasoning_summary_text.delta", "delta": "Checking weather..."}),
                    ("response.output_item.done", {"type": "response.output_item.done", "item": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Checking weather..."}], "encrypted_content": "enc_zdr_state_blob_999"}}),
                    ("response.output_item.added", {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_r1", "name": "weather", "arguments": ""}}),
                    ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{\"city\":\"Paris\"}"}),
                    ("response.output_item.done", {"type": "response.output_item.done", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_r1", "name": "weather", "arguments": "{\"city\":\"Paris\"}"}}),
                    ("response.completed", {"type": "response.completed", "response": {"usage": {"input_tokens": 100, "output_tokens": 30, "input_tokens_details": {"cached_tokens": 0}}}}),
                ]),
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes([
                ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Sunny 22C in Paris"}),
                ("response.completed", {"type": "response.completed", "response": {"usage": {"input_tokens": 140, "output_tokens": 12, "input_tokens_details": {"cached_tokens": 100}}}}),
            ]),
        )

    transport = httpx.MockTransport(handler)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({
            "relay_api_key": "stage3-resp-key",
            "conversation_state": False,
            "project_context": False,
            "exec_policy": "off",
            "providers": [
                {
                    "id": "openai-resp",
                    "preset": "openai-responses",
                    "api_key": "sk-proj-responses123456",
                    "models": ["gpt-5.4"],
                }
            ],
        }),
        encoding="utf-8",
    )
    cfg = config_module.load(str(cfg_path))
    cfg["journal_path"] = str(tmp_path / "journal.jsonl")
    cfg["accounts"][0]["_http_transport"] = transport

    rt = proxy.Runtime(cfg)
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(rt), threading.BoundedSemaphore(4))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    tools = [{"type": "function", "name": "weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}]
    try:
        conn.request(
            "POST",
            "/v1/responses",
            json.dumps({"model": "openai-resp/gpt-5.4", "input": "Weather in Paris?", "tools": tools}),
            {"Authorization": "Bearer stage3-resp-key", "Content-Type": "application/json", "x-proxi-session-id": "sess-resp-1"},
        )
        b1 = json.loads(conn.getresponse().read().decode("utf-8"))
        assert captured[0]["store"] is False
        assert "reasoning.encrypted_content" in captured[0]["include"]
        assert any(item.get("type") == "function_call" and item.get("call_id") == "call_r1" for item in b1["output"])

        # Turn 2: client echoes function_call + function_call_output WITHOUT encrypted_content; relay replays encrypted_content!
        conn.request(
            "POST",
            "/v1/responses",
            json.dumps({
                "model": "openai-resp/gpt-5.4",
                "tools": tools,
                "input": [
                    {"type": "message", "role": "user", "content": "Weather in Paris?"},
                    {"type": "function_call", "call_id": "call_r1", "name": "weather", "arguments": "{\"city\":\"Paris\"}"},
                    {"type": "function_call_output", "call_id": "call_r1", "output": "Sunny 22C"},
                ],
            }),
            {"Authorization": "Bearer stage3-resp-key", "Content-Type": "application/json", "x-proxi-session-id": "sess-resp-1"},
        )
        b2 = json.loads(conn.getresponse().read().decode("utf-8"))
        assert any("Sunny 22C in Paris" in json.dumps(item) for item in b2["output"])
        assert any(
            item.get("type") == "reasoning" and item.get("encrypted_content") == "enc_zdr_state_blob_999"
            for item in captured[1]["input"]
        )
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        rt.close()
        import asyncio
        asyncio.run(http_pool.close_loop_clients())


def test_cross_provider_failover_strips_incompatible_signatures(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    or_bodies: list[dict] = []
    anth_calls = 0

    def anth_handler(request: httpx.Request) -> httpx.Response:
        nonlocal anth_calls
        anth_calls += 1
        if anth_calls == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=_sse_bytes([
                    ("message_start", {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 20}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Secret Anthropic thinking"}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_must_not_leak_to_openrouter"}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": "Hello from Anthropic"}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
                    ("message_stop", {"type": "message_stop"}),
                ]),
            )
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"error": {"message": "Rate limited"}})

    def or_handler(request: httpx.Request) -> httpx.Response:
        or_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes([
                (None, {"choices": [{"index": 0, "delta": {"content": "Hello from OpenRouter fallback"}}]}),
                (None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 40, "completion_tokens": 6, "cost": 0.0005}}),
                (None, "[DONE]"),
            ]),
        )

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({
            "relay_api_key": "stage3-ha-key",
            "conversation_state": False,
            "project_context": False,
            "exec_policy": "off",
            "upstream_rate_limit_retries": 0,
            "providers": [
                {"id": "anth", "preset": "anthropic", "api_key": "sk-ant-test12345678", "models": ["claude-sonnet-4-6"]},
                {"id": "or", "preset": "openrouter", "api_key": "sk-or-v1-test12345678", "models": ["anthropic/claude-sonnet-4.6"]},
            ],
            "routes": [
                {"id": "claude-prod", "targets": ["anth/claude-sonnet-4-6", "or/anthropic/claude-sonnet-4.6"]}
            ],
        }),
        encoding="utf-8",
    )
    cfg = config_module.load(str(cfg_path))
    cfg["journal_path"] = str(tmp_path / "journal.jsonl")
    cfg["accounts"][0]["_http_transport"] = httpx.MockTransport(anth_handler)
    cfg["accounts"][1]["_http_transport"] = httpx.MockTransport(or_handler)

    rt = proxy.Runtime(cfg)
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(rt), threading.BoundedSemaphore(4))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        # Turn 1 hits Anthropic and stores signed thinking in native_history
        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({"model": "claude-prod", "messages": [{"role": "user", "content": "Hi"}]}),
            {"Authorization": "Bearer stage3-ha-key", "Content-Type": "application/json", "x-proxi-session-id": "sess-ha-1"},
        )
        r1 = json.loads(conn.getresponse().read().decode("utf-8"))
        assert r1["choices"][0]["message"]["content"] == "Hello from Anthropic"

        # Turn 2: Anthropic 429s -> fails over to OpenRouter; Anthropic signature MUST NOT leak into OpenRouter payload!
        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({
                "model": "claude-prod",
                "messages": [
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "Hello from Anthropic"},
                    {"role": "user", "content": "Continue"},
                ],
            }),
            {"Authorization": "Bearer stage3-ha-key", "Content-Type": "application/json", "x-proxi-session-id": "sess-ha-1"},
        )
        r2 = json.loads(conn.getresponse().read().decode("utf-8"))
        assert r2["choices"][0]["message"]["content"] == "Hello from OpenRouter fallback"
        assert len(or_bodies) == 1
        serialized_or = json.dumps(or_bodies[0])
        assert "sig_must_not_leak_to_openrouter" not in serialized_or
        assert "prompt_cache_key" in or_bodies[0]
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        rt.close()
