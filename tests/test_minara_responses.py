"""Offline regression tests for native Minara `/v1/responses` transport, detection, catalog, and billing safety."""
from __future__ import annotations

import asyncio
import json
import queue
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import apierr
import config
import minara
import minara_catalog
import minara_probe
import minara_refresh
import native_history

CFG = {"bearer_token": "tok"}


def _tool_spec(name="read_file", description="Read a file"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            "strict": True,
            "x-client-type": "custom",
            "x-format": {"type": "grammar"},
        },
    }


def _sse_bytes(events):
    chunks = []
    for ev in events:
        payload = ev if isinstance(ev, str) else json.dumps(ev, ensure_ascii=False)
        chunks.append(f"data: {payload}\n\n".encode("utf-8"))
    return chunks


class _MockResponse:
    def __init__(self, status_code, chunks=None, *, headers=None):
        self.status_code = status_code
        self.headers = {"content-type": "text/event-stream"} if headers is None and status_code == 200 else (headers or {})
        self._chunks = list(chunks or [])
        self.closed = False

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        self.closed = True


async def _collect(cfg, session, provider, prompt="hello", head="sys", tools=None, tool_choice="auto", max_tokens=256, chunks=None):
    out = []
    async for ev in minara.iter_events(cfg, session, provider, prompt, head, tools, tool_choice, max_tokens, chunks):
        out.append(ev)
    return out


# ----- 1. Endpoint selection & backward compatibility ---------------------------------------------------------------


def test_use_responses_preserves_backward_compatibility_and_precedence(tmp_path):
    # Legacy models without Responses metadata stay on chat/messages (even if backend is "openai-responses").
    assert not minara.use_responses({"model": "openai/gpt-6.1-sol", "backend": "openai-responses"})
    assert not minara.use_responses({"model": "anthropic/claude-opus-5.5"})

    # Explicit overrides win over everything.
    assert minara.use_responses({"model": "openai/gpt-5.5", "endpoint": "responses"})
    assert minara.use_responses({"model": "openai/gpt-5.5", "endpoint": "/v1/responses"})
    assert minara.use_responses({"model": "anthropic/claude-opus-5.5", "endpoint": "openai-response"})
    assert minara.use_responses({"model": "anthropic/claude-opus-5.5", "use_responses": True})

    # Explicit opt-out overrides catalog capabilities.
    assert not minara.use_responses({
        "model": "openai/gpt-6.1-sol",
        "supported_endpoint_types": ["openai-response"],
        "endpoint": "chat",
    })
    assert not minara.use_responses({
        "model": "openai/gpt-6.1-sol",
        "supported_endpoint_types": ["openai-response"],
        "use_responses": False,
    })

    # Catalog capabilities on bound_entry or _minara_models.
    sess = SimpleNamespace(_bound_entry={
        "id": "openai/gpt-6.1-sol",
        "upstream_model": "openai/gpt-6.1-sol",
        "supported_endpoint_types": ["openai", "openai-response"],
        "responses": True,
    })
    assert minara.use_responses({"model": "openai/gpt-6.1-sol"}, session=sess)

    cfg = {
        "_minara_models": {
            "minara/gpt-resp-only": {
                "id": "gpt-resp-only",
                "upstream_model": "openai/gpt-resp-only",
                "supported_endpoint_types": ["responses"],
            }
        }
    }
    assert minara.use_responses({"model": "openai/gpt-resp-only"}, cfg=cfg)

    # Claude routes keep chat/messages cache_control breakpoints when openai/anthropic are also advertised,
    # unless openai-response is the sole endpoint.
    assert not minara.use_responses({
        "model": "anthropic/claude-opus-5.5",
        "supported_endpoint_types": ["openai", "anthropic", "openai-response"],
        "responses": True,
    })
    assert minara.use_responses({
        "model": "anthropic/claude-opus-5.5",
        "supported_endpoint_types": ["openai-response"],
    })

    # config.load() accepts endpoint and use_responses on minara_models overrides.
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "relay_key": "k",
        "upstream": "minara",
        "accounts": [{
            "upstream": "minara",
            "bearer_token": "tok",
            "minara_models": [{"id": "openai/gpt-6.1-sol", "endpoint": "responses", "use_responses": True}],
        }],
    }), encoding="utf-8")
    loaded = config.load(str(cfg_file))
    assert loaded["_minara_models"]["minara/gpt-6.1-sol"]["endpoint"] == "responses"
    assert loaded["_minara_models"]["minara/gpt-6.1-sol"]["use_responses"] is True


# ----- 2. Catalog, probe, refresh, and Claude default effort --------------------------------------------------------


def test_catalog_probe_and_refresh_recognize_openai_response():
    measured = {
        "openai/gpt-resp": {
            "id": "minara/gpt-resp",
            "upstream_model": "openai/gpt-resp",
            "supported_endpoint_types": ["openai-response"],
            "responses": True,
        }
    }
    entry = minara_catalog.route("openai/gpt-resp", {"endpoint": "responses", "use_responses": True}, measured=measured)
    assert entry["responses"] is True
    assert entry["supported_endpoint_types"] == ["openai-response"]
    assert entry["endpoint"] == "responses"
    assert entry["use_responses"] is True

    built = minara_catalog.build([{
        "upstream": "minara",
        "bearer_token": "tok",
        "minara_models": [{"id": "openai/gpt-resp", "endpoint": "responses", "use_responses": True}],
    }])["minara/gpt-resp"]
    assert built["responses"] is True and built["endpoint"] == "responses" and built["use_responses"] is True

    # Invalid OVERRIDABLE types fail cleanly.
    with pytest.raises(ValueError, match="use_responses"):
        minara_catalog.route("openai/gpt-resp", {"use_responses": "yes"})
    with pytest.raises(ValueError, match="endpoint"):
        minara_catalog.route("openai/gpt-resp", {"endpoint": "invalid-ep"})

    # Probe capability helpers + catalog generation.
    item_resp_only = {
        "id": "openai/gpt-6-resp-only",
        "supported_endpoint_types": ["openai-response"],
        "pricing": {"quota_type": 0, "input_price": 1.0, "output_price": 4.0},
    }
    item_claude = {
        "id": "anthropic/claude-opus-5.5",
        "supported_endpoint_types": ["openai", "openai-response"],
        "pricing": {"quota_type": 0, "input_price": 3.0, "output_price": 15.0},
    }
    assert minara_probe.supports_responses(item_resp_only)
    assert not minara_probe.supports_chat(item_resp_only)
    assert minara_refresh.chat_model(item_resp_only)

    sample = {
        "tools": {"status": "ok", "tools": True, "id": "resp_1", "served": None},
        "vision": {"status": "ok", "vision": True},
        "thinking": {"status": "ok", "thinking": "end", "signature": True, "reasoning_tokens": 40},
        "responses": {"status": "ok", "responses": True, "id": "resp_1"},
        "checked_at": "2026-11-24",
    }
    catalog = minara_probe.generate(
        {
            "openai/gpt-6-resp-only": sample,
            "anthropic/claude-opus-5.5": dict(sample, tools={"status": "ok", "tools": True, "id": "gen-1", "served": None}),
        },
        [item_resp_only, item_claude],
    )
    by_id = {m["upstream_model"]: m for m in catalog["models"]}
    assert by_id["openai/gpt-6-resp-only"]["id"] == "minara/gpt-6-resp-only"
    assert by_id["openai/gpt-6-resp-only"]["responses"] is True
    assert by_id["openai/gpt-6-resp-only"]["supported_endpoint_types"] == ["openai-response"]
    assert by_id["anthropic/claude-opus-5.5"]["responses"] is True
    assert by_id["anthropic/claude-opus-5.5"]["supported_endpoint_types"] == ["openai", "openai-response"]


def test_minara_claude_models_populate_sensible_default_reasoning_effort():
    routes = minara_catalog.build([{"upstream": "minara", "bearer_token": "tok"}])
    for name in ("minara/claude-sonnet-5-5", "minara/claude-opus-5-5", "minara/claude-fable-5-2", "minara/claude-haiku-5-5"):
        entry = routes[name]
        assert entry["default_reasoning_effort"] in entry["reasoning_effort_levels"]
    for entry in routes.values():
        if entry["provider"] == "anthropic" and entry["reasoning"] and entry["reasoning_effort_levels"]:
            assert entry["default_reasoning_effort"] in entry["reasoning_effort_levels"], entry["id"]
    # Explicit account-level reasoning_effort_levels overrides still stay passthrough (default_reasoning_effort=None).
    custom = minara_catalog.build([{
        "upstream": "minara",
        "bearer_token": "tok",
        "minara_models": [{"id": "anthropic/claude-sonnet-5.5", "reasoning_effort_levels": ["low", "high"]}],
    }])["minara/claude-sonnet-5-5"]
    assert custom["default_reasoning_effort"] is None


def test_probe_responses_only_model_uses_responses_endpoints():
    calls = []

    class FakeClient:
        def chat(self, *args, **kwargs):
            raise AssertionError("chat endpoint must not be called for responses-only model")

        def messages(self, *args, **kwargs):
            raise AssertionError("messages endpoint must not be called for responses-only model")

        def responses(self, body, stream=True, timeout=600):
            calls.append(body)
            tools = body.get("tools") or []
            if tools:
                events = [
                    {"type": "response.created", "response": {"id": "resp_p1"}},
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                                 "name": tools[0]["name"], "arguments": '{"city": "Paris"}'},
                    },
                    {"type": "response.completed", "response": {"id": "resp_p1", "status": "completed",
                                                               "usage": {"input_tokens": 50, "output_tokens": 10}}},
                ]
            else:
                events = [
                    {"type": "response.created", "response": {"id": "resp_p2"}},
                    {"type": "response.reasoning_summary_text.delta", "delta": "thinking step"},
                    {"type": "response.output_item.done", "output_index": 0,
                     "item": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking step"}],
                              "encrypted_content": "enc_probe"}},
                    {"type": "response.output_text.delta", "delta": "yellow top and green bottom"},
                    {"type": "response.completed", "response": {
                        "id": "resp_p2", "status": "completed",
                        "usage": {"input_tokens": 50, "output_tokens": 12,
                                  "output_tokens_details": {"reasoning_tokens": 12}},
                    }},
                ]
            return _MockResponse(200, _sse_bytes(events)), None

    report, study = minara_probe.measure_model(
        FakeClient(),
        {
            "id": "openai/gpt-resp-probe",
            "supported_endpoint_types": ["openai-response"],
            "pricing": {"quota_type": 0, "input_price": 1.0, "output_price": 2.0},
        },
        checks=["responses", "tools", "vision", "thinking"],
    )
    assert study is None
    assert len(calls) == 4
    assert report["responses"]["responses"] is True
    assert report["tools"]["tools"] is True
    assert report["vision"]["vision"] is True
    assert report["thinking"]["signature"] is True and report["thinking"]["reasoning_tokens"] == 12


def test_refresh_overlay_syncs_responses_metadata(tmp_path):
    overlay_path = tmp_path / "minara_models.auto.json"
    routes = {
        "minara/gpt-sync": {
            "id": "gpt-sync",
            "upstream_model": "openai/gpt-sync",
            "pricing": {"quota_type": 0, "input_price": 1.0, "output_price": 2.0},
        }
    }
    cfg = {
        "_minara_models": routes,
        "_minara_catalog_file": str(overlay_path),
        "upstream": "minara",
        "accounts": [],
    }
    measured_snapshot = {
        "openai/gpt-sync": {
            "id": "minara/gpt-sync",
            "upstream_model": "openai/gpt-sync",
            "provider": "openai",
            "status": "ok",
            "pricing": {"quota_type": 0, "input_price": 1.0, "output_price": 2.0},
        }
    }

    class FakeClient:
        def get(self, path):
            assert path == "/v1/models"
            payload = [{
                "id": "openai/gpt-sync",
                "pricing": {"quota_type": 0, "input_price": 1.0, "output_price": 2.0},
                "supported_endpoint_types": ["openai", "openai-response"],
            }]
            return SimpleNamespace(status_code=200, json=lambda: {"data": payload})

        def credits(self):
            return 100.0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(minara_catalog, "MEASURED", measured_snapshot)
        mp.setattr(minara_catalog, "SHIPPED", dict(measured_snapshot))
        refresher = minara_refresh.CatalogRefresher(cfg, client_factory=lambda tok: FakeClient())
        refresher._run_once_with_client(None, FakeClient())

    saved = json.loads(overlay_path.read_text(encoding="utf-8"))
    assert saved["models"]["openai/gpt-sync"]["responses"] is True
    assert saved["models"]["openai/gpt-sync"]["supported_endpoint_types"] == ["openai", "openai-response"]


# ----- 3. responses_body conversion, structured output, reasoning, and cache keys ---------------------------------


def test_responses_body_full_conversion_and_cache_key_parity():
    session = SimpleNamespace(
        _native_conversation_key="c-1",
        _provider_options={"parallel_tool_calls": False, "service_tier": "priority"},
        _structured_output={"kind": "json_schema", "name": "Answer", "schema": {"type": "object"}, "strict": True},
        _native_client_messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect image and pdf"},
                    {"type": "image", "imageData": "AAA", "mimeType": "image/jpeg"},
                    {"type": "document", "filename": "spec.pdf", "media_type": "application/pdf", "data": "PDFDATA"},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking file."},
                    {"type": "tool_call", "toolCallId": "call_1", "toolName": "read_file", "toolInput": {"path": "a.txt"}},
                ],
            },
            {
                "role": "tool",
                "content": {
                    "toolCallId": "call_1",
                    "toolResult": [
                        {"type": "text", "text": "diagram loaded"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/chart.png", "detail": "high"}},
                    ],
                },
            },
        ],
    )
    head = "x-anthropic-billing-header: cc_version=2; cch=secret;\nFollow workspace rules."
    tools = [_tool_spec()]
    provider = {"model": "openai/gpt-6.1-sol", "reasoning": True, "reasoningEffort": "high", "verbosity": "low"}

    body = minara.responses_body({}, provider, head, "ignored", tools, "required", 4096, session=session, instruction="Now answer.")

    assert body["model"] == "openai/gpt-6.1-sol"
    assert body["instructions"] == "Follow workspace rules."
    assert body["max_output_tokens"] == 4096
    assert body["store"] is False
    assert body["stream"] is True
    assert body["parallel_tool_calls"] is False
    assert body["service_tier"] == "priority"
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["tool_choice"] == "required"
    assert body["tools"] == [{
        "type": "function",
        "name": "read_file",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "strict": True,
    }]
    assert body["text"] == {
        "verbosity": "low",
        "format": {"type": "json_schema", "name": "Answer", "schema": {"type": "object"}, "strict": True},
    }

    # Verify input structure including visual tool output follow-up and relay instruction.
    types = [(it.get("type"), it.get("role")) for it in body["input"]]
    assert types == [
        ("message", "user"),
        ("message", "assistant"),
        ("function_call", None),
        ("function_call_output", None),
        ("message", "user"),
        ("message", "user"),
    ]
    user_parts = body["input"][0]["content"]
    assert user_parts[0] == {"type": "input_text", "text": "inspect image and pdf"}
    assert user_parts[1] == {"type": "input_image", "image_url": "data:image/jpeg;base64,AAA"}
    assert user_parts[2] == {"type": "input_file", "filename": "spec.pdf", "file_data": "data:application/pdf;base64,PDFDATA"}
    assert body["input"][3] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "diagram loaded\nImage result follows as visual input.",
    }
    assert body["input"][4]["content"][1] == {
        "type": "input_image",
        "image_url": "https://example.com/chart.png",
        "detail": "high",
    }
    assert body["input"][5]["content"] == [{"type": "input_text", "text": "Now answer."}]

    # prompt_cache_key must match build_body identically across effort changes, and isolate different chats/models/heads.
    chat_equiv = minara.build_body({}, provider, head, "ignored", tools, "auto", 4096, session=session)
    assert body["prompt_cache_key"] == chat_equiv["prompt_cache_key"]
    assert "c-1" not in body["prompt_cache_key"]

    diff_effort = minara.responses_body({}, dict(provider, reasoningEffort="low"), head, "ignored", tools, "auto", 4096, session=session)
    assert diff_effort["prompt_cache_key"] == body["prompt_cache_key"]

    diff_conv = minara.responses_body({}, provider, head, "ignored", tools, "auto", 4096,
                                       session=SimpleNamespace(_native_conversation_key="c-2", _native_client_messages=[]))
    assert diff_conv["prompt_cache_key"] != body["prompt_cache_key"]
    assert "prompt_cache_key" not in minara.responses_body({"direct_cache": False}, provider, head, "hi", tools, "auto", 100, session=session)

    # Non-reasoning model omits reasoning & include.
    non_r = minara.responses_body({}, {"model": "openai/gpt-4o", "reasoning": False}, "", "hi", None, None, 100)
    assert "reasoning" not in non_r and "include" not in non_r


# ----- 4. ResponsesParser SSE parsing & R2 strict completion ------------------------------------------------------


def test_responses_parser_streaming_text_reasoning_and_tool_calls():
    pricing = {"quota_type": 0, "input_price": 2.0, "output_price": 8.0, "cache_read_price": 0.5}
    parser = minara.ResponsesParser(pricing=pricing)

    chunks = [
        {"type": "response.created", "response": {"id": "resp_abc123", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"id": "rs_1", "type": "reasoning"}},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "Step 1. "},
        {"type": "response.reasoning_text.delta", "output_index": 0, "delta": "Deep check."},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "rs_1",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Step 1. "}],
                "encrypted_content": "gAAA_signed_blob",
            },
        },
        {"type": "response.output_item.added", "output_index": 1, "item": {"id": "msg_1", "type": "message"}},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "Reading "},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "file."},
        {
            "type": "response.output_item.added",
            "output_index": 2,
            "item": {"id": "fc_1", "type": "function_call", "call_id": "call_read_1", "name": "read_file", "arguments": ""},
        },
        {"type": "response.function_call_arguments.delta", "output_index": 2, "item_id": "fc_1", "delta": '{"path":'},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "item_id": "fc_1", "delta": '"a.py"}'},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_abc123",
                "status": "completed",
                "usage": {
                    "input_tokens": 1000,
                    "input_tokens_details": {"cached_tokens": 400},
                    "output_tokens": 200,
                    "output_tokens_details": {"reasoning_tokens": 50},
                },
            },
        },
    ]

    events = []
    for chunk in chunks:
        events.extend(parser.feed(json.dumps(chunk)))

    # Trailing [DONE] proxy marker after completion is tolerated as a no-op.
    assert parser.feed("[DONE]") == []
    assert parser.done is True
    assert [e["delta"] for e in events if e["type"] == "thinking"] == ["Step 1. ", "Deep check."]
    assert [e["signature"] for e in events if e["type"] == "thinking_signature"] == ["gAAA_signed_blob"]
    assert [e["delta"] for e in events if e["type"] == "text"] == ["Reading ", "file."]
    final_calls = [e for e in events if e["type"] == "tool_call" and not e.get("partial")]
    assert final_calls == [{"type": "tool_call", "id": "call_read_1", "name": "read_file", "arguments": '{"path":"a.py"}'}]
    usage_evs = [e["usage"] for e in events if e["type"] == "usage"]
    assert len(usage_evs) == 1
    assert usage_evs[0]["input_tokens"] == 600
    assert usage_evs[0]["cache_read"] == 400
    assert usage_evs[0]["output_tokens"] == 200
    assert usage_evs[0]["reasoning_tokens"] == 50
    assert usage_evs[0]["channel_path"] == "responses"
    assert usage_evs[0]["channel"] == "openai-responses"
    assert events[-1] == {"type": "end", "reason": "tool_calls"}

    blocks = parser.reply_blocks()
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["responses_reasoning"]["encrypted_content"] == "gAAA_signed_blob"


def test_responses_parser_incomplete_reasons_and_truncation_rejection():
    p_len = minara.ResponsesParser()
    events = p_len.feed(json.dumps({
        "type": "response.incomplete",
        "response": {"id": "resp_inc", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    }))
    assert events[-1] == {"type": "end", "reason": "length"}

    p_filter = minara.ResponsesParser()
    events_f = p_filter.feed(json.dumps({
        "type": "response.incomplete",
        "response": {"id": "resp_flt", "status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
    }))
    assert events_f[-1] == {"type": "end", "reason": "content_filter"}

    # Bare [DONE] without response.completed/incomplete MUST fail with unbilled=False.
    p_trunc = minara.ResponsesParser()
    p_trunc.feed(json.dumps({"type": "response.output_text.delta", "delta": "partial"}))
    with pytest.raises(apierr.ApiError) as err:
        p_trunc.feed("[DONE]")
    assert err.value.unbilled is False


@pytest.mark.parametrize("prefix", ["", '{"path":'])
def test_responses_parser_uses_terminal_arguments_for_announced_calls(prefix):
    parser = minara.ResponsesParser()
    call = {"type": "function_call", "id": "fc_one", "call_id": "call_one", "name": "read", "arguments": ""}
    before_terminal = parser.feed(json.dumps({"type": "response.output_item.added", "output_index": 0, "item": call}))
    if prefix:
        before_terminal += parser.feed(json.dumps({"type": "response.function_call_arguments.delta", "item_id": "fc_one",
                                                   "delta": prefix}))
    events = parser.feed(json.dumps({"type": "response.completed", "response": {"output": [
        {"type": "function_call", "id": "fc_one", "name": "read", "arguments": '{"path":"note.txt"}', "status": "completed"},
    ]}}))

    assert events == [{"type": "tool_call", "id": "call_one", "name": "read", "arguments": '{"path":"note.txt"}'},
                      {"type": "end", "reason": "tool_calls"}]
    assert parser.reply_blocks() == [{"type": "tool_call", "toolCallId": "call_one", "toolName": "read",
                                      "toolInput": {"path": "note.txt"}}]
    assert before_terminal == []


@pytest.mark.parametrize(("reason", "expected"), [("max_output_tokens", "length"), ("content_filter", "content_filter")])
def test_responses_parser_releases_only_completed_calls_on_an_incomplete_response(reason, expected):
    parser = minara.ResponsesParser()
    calls = [{"type": "function_call", "id": f"fc_{i}", "call_id": f"call_{i}", "name": "read", "arguments": "{}"}
             for i in range(3)]
    events = []
    for i, call in enumerate(calls):
        events.extend(parser.feed(json.dumps({"type": "response.output_item.added", "output_index": i, "item": call})))
    events.extend(parser.feed(json.dumps({"type": "response.output_item.done", "output_index": 0,
                                         "item": {**calls[0], "status": "completed"}})))
    events.extend(parser.feed(json.dumps({"type": "response.incomplete", "response": {
        "status": "incomplete", "incomplete_details": {"reason": reason}, "output": [
            calls[0], {**calls[1], "status": "incomplete"},
        ],
    }})))

    assert events == [{"type": "tool_call", "id": "call_0", "name": "read", "arguments": "{}"},
                      {"type": "end", "reason": expected}]
    assert [block["toolCallId"] for block in parser.reply_blocks()] == ["call_0"]


@pytest.mark.asyncio
async def test_truncated_sse_stream_without_completed_fails_unbilled_false():
    resp = _MockResponse(200, _sse_bytes([
        {"type": "response.created", "response": {"id": "resp_cut"}},
        {"type": "response.output_text.delta", "delta": "cut mid-sentence"},
    ]))
    sess = SimpleNamespace(backend=SimpleNamespace(_minara_post=lambda *a, **kw: resp), _cancelled=False)
    with pytest.raises(apierr.ApiError, match="before DONE") as raised:
        await _collect(CFG, sess, {"model": "openai/gpt-resp", "endpoint": "responses"})
    assert raised.value.unbilled is False


# ----- 5. Pre-send vs post-send billing safety & SSE errors ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_error_and_transport_error_unbilled_classification():
    # Post-output SSE error is ambiguous/billed -> unbilled=False.
    resp_post = _MockResponse(200, _sse_bytes([
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_text.delta", "delta": "hi"},
        {"type": "response.failed", "response": {"status": "failed", "error": {"code": "rate_limit_error", "message": "429 late"}}},
    ]))
    sess = SimpleNamespace(backend=SimpleNamespace(_minara_post=lambda *a, **kw: resp_post), _cancelled=False)
    with pytest.raises(apierr.ApiError) as err_post:
        await _collect(CFG, sess, {"model": "openai/gpt-resp", "endpoint": "responses"})
    assert err_post.value.unbilled is False

    # Ambiguous generic server failure in response.failed before output still has unbilled=False.
    resp_srv = _MockResponse(200, _sse_bytes([
        {"type": "response.created", "response": {"id": "resp_2"}},
        {"type": "response.failed", "response": {"status": "failed", "error": {"code": "server_error", "message": "boom"}}},
    ]))
    sess2 = SimpleNamespace(backend=SimpleNamespace(_minara_post=lambda *a, **kw: resp_srv), _cancelled=False)
    with pytest.raises(apierr.ApiError) as err_srv:
        await _collect(CFG, sess2, {"model": "openai/gpt-resp", "endpoint": "responses"})
    assert err_srv.value.unbilled is False

    # Pre-output 429 rate-limit SSE error is unbilled=True.
    resp_rl = _MockResponse(200, _sse_bytes([
        {"type": "error", "error": {"code": 429, "type": "rate_limit_error", "message": "Rate limit exceeded"}},
    ]))
    sess3 = SimpleNamespace(backend=SimpleNamespace(_minara_post=lambda *a, **kw: resp_rl), _cancelled=False)
    with pytest.raises(apierr.ApiError) as err_rl:
        await _collect(CFG, sess3, {"model": "openai/gpt-resp", "endpoint": "responses"})
    assert err_rl.value.unbilled is True

    # Non-SSE 200 response preserves unbilled=False.
    resp_html = _MockResponse(200, [b"<html>oops</html>"], headers={"content-type": "text/html"})
    sess4 = SimpleNamespace(backend=SimpleNamespace(_minara_post=lambda *a, **kw: resp_html), _cancelled=False)
    with pytest.raises(apierr.ApiError, match="SSE") as err_html:
        await _collect(CFG, sess4, {"model": "openai/gpt-resp", "endpoint": "responses"})
    assert err_html.value.unbilled is False

    # Pre-send curl error vs post-send curl timeout.
    from curl_cffi.requests.exceptions import ConnectTimeout, Timeout as CurlTimeout

    sess_conn = SimpleNamespace(
        backend=SimpleNamespace(_minara_post=lambda *a, **kw: (_ for _ in ()).throw(ConnectTimeout("secret-url"))),
        _cancelled=False,
    )
    with pytest.raises(apierr.ApiError) as err_conn:
        await _collect(CFG, sess_conn, {"model": "openai/gpt-resp", "endpoint": "responses"})
    assert err_conn.value.unbilled is True

    class _StreamTimeoutResp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def iter_content(self, chunk_size=None):
            raise CurlTimeout("read timed out")
            yield b""

        def close(self):
            pass

    sess_read = SimpleNamespace(backend=SimpleNamespace(_minara_post=lambda *a, **kw: _StreamTimeoutResp()), _cancelled=False)
    with pytest.raises(apierr.ApiError) as err_read:
        await _collect(CFG, sess_read, {"model": "openai/gpt-resp", "endpoint": "responses"})
    assert err_read.value.unbilled is False


# ----- 6. Channel fallback & native_history round-trip ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_fallback_on_unsupported_responses_and_no_fallback_on_billed_errors():
    seen = []

    def post_fallback(url, **kwargs):
        seen.append((url, kwargs["json"]))
        if url.endswith("/responses"):
            return _MockResponse(404, [b'{"error":{"message":"Responses API is not supported on this channel"}}'],
                                 headers={"content-type": "application/json"})
        return _MockResponse(200, _sse_bytes([
            {"id": "gen-fallback", "choices": [{"index": 0, "delta": {"content": "OK via chat"}, "finish_reason": "stop"}]},
            {"id": "gen-fallback", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            "[DONE]",
        ]))

    sess = SimpleNamespace(backend=SimpleNamespace(_minara_post=post_fallback), _cancelled=False)
    events = await _collect(CFG, sess, {"model": "openai/gpt-resp", "supported_endpoint_types": ["openai", "openai-response"]})
    assert [url for url, _ in seen] == ["https://api.minara.ai/v1/responses", "https://api.minara.ai/v1/chat/completions"]
    assert "".join(e["delta"] for e in events if e["type"] == "text") == "OK via chat"
    assert events[-1] == {"type": "end", "reason": "stop"}

    # Generic 400 validation error or 402 credit error must NOT trigger chat fallback.
    seen_no_fb = []

    def post_bad_schema(url, **kwargs):
        seen_no_fb.append(url)
        return _MockResponse(400, [b'{"error":{"message":"max_output_tokens must be positive"}}'],
                             headers={"content-type": "application/json"})

    sess_bad = SimpleNamespace(backend=SimpleNamespace(_minara_post=post_bad_schema), _cancelled=False)
    with pytest.raises(apierr.ApiError) as err_bad:
        await _collect(CFG, sess_bad, {"model": "openai/gpt-resp", "endpoint": "responses"})
    assert err_bad.value.unbilled is False
    assert seen_no_fb == ["https://api.minara.ai/v1/responses"]

    # Explicit 405 Method Not Allowed without fallback enabled marks unbilled=True.
    sess_405 = SimpleNamespace(
        backend=SimpleNamespace(_minara_post=lambda url, **kw: _MockResponse(405, [b"Method Not Allowed"], headers={})),
        _cancelled=False,
    )
    with pytest.raises(apierr.ApiError) as err_405:
        await _collect({"bearer_token": "tok", "minara_responses_fallback": False}, sess_405,
                       {"model": "openai/gpt-resp", "endpoint": "responses"})
    assert err_405.value.unbilled is True


@pytest.mark.asyncio
async def test_multi_turn_native_history_preserves_encrypted_reasoning():
    sent_bodies = []

    def post(url, **kwargs):
        sent_bodies.append(kwargs["json"])
        if len(sent_bodies) == 1:
            return _MockResponse(200, _sse_bytes([
                {"type": "response.created", "response": {"id": "resp_t1"}},
                {"type": "response.output_item.added", "output_index": 0, "item": {"id": "rs_1", "type": "reasoning"}},
                {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "Need to read a.txt"},
                {"type": "response.output_item.done", "output_index": 0,
                 "item": {"id": "rs_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "Need to read a.txt"}],
                          "encrypted_content": "enc_turn1_secret"}},
                {"type": "response.output_item.added", "output_index": 1,
                 "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path":"a.txt"}'}},
                {"type": "response.completed", "response": {"id": "resp_t1", "status": "completed",
                                                           "usage": {"input_tokens": 50, "output_tokens": 20}}},
            ]))
        return _MockResponse(200, _sse_bytes([
            {"type": "response.created", "response": {"id": "resp_t2"}},
            {"type": "response.output_text.delta", "output_index": 0, "delta": "All done."},
            {"type": "response.completed", "response": {"id": "resp_t2", "status": "completed",
                                                       "usage": {"input_tokens": 90, "output_tokens": 5}}},
        ]))

    tools = [_tool_spec()]
    sess = SimpleNamespace(
        backend=SimpleNamespace(_minara_post=post),
        _cancelled=False,
        _native_conversation_key="conv-multi",
        _native_history=native_history.History(),
        _native_client_messages=[{"role": "user", "content": [{"type": "text", "text": "read a.txt"}]}],
    )
    provider = {"model": "openai/gpt-6.1-sol", "endpoint": "responses", "reasoning": True, "reasoningEffort": "high"}
    await _collect(CFG, sess, provider, tools=tools, tool_choice="auto")

    # Turn 2 echoes assistant text+tool_call WITHOUT reasoning, plus tool result; Minara restores encrypted_content!
    sess._native_client_messages = [
        {"role": "user", "content": [{"type": "text", "text": "read a.txt"}]},
        {"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "call_1", "toolName": "read_file", "toolInput": {"path": "a.txt"}}]},
        {"role": "tool", "content": {"toolCallId": "call_1", "toolResult": "contents"}},
    ]
    await _collect(CFG, sess, provider, tools=tools, tool_choice="auto")

    assert len(sent_bodies) == 2
    reasoning_items = [it for it in sent_bodies[1]["input"] if it.get("type") == "reasoning"]
    assert reasoning_items == [{
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "Need to read a.txt"}],
        "encrypted_content": "enc_turn1_secret",
    }]


# ----- 7. <1s silent abort over loopback TCP / queue-backed curl_cffi response --------------------------------------


@pytest.mark.asyncio
async def test_silent_responses_stream_aborts_under_one_second():
    # Real loopback server sends 200 SSE headers then blocks silently waiting to be closed.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    closed_by_client = threading.Event()

    def serve():
        conn, _ = server.accept()
        try:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            conn.settimeout(0.05)
            while not closed_by_client.is_set():
                try:
                    data = conn.recv(1024)
                    if not data:
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break
        finally:
            closed_by_client.set()
            conn.close()
            server.close()

    threading.Thread(target=serve, daemon=True).start()

    class _LoopbackQueueResp:
        def __init__(self, sock):
            self.sock = sock
            self.status_code = 200
            self.headers = {"content-type": "text/event-stream"}
            self.queue = queue.Queue()
            self.quit_now = threading.Event()
            self._reader = threading.Thread(target=self._pump, daemon=True)
            self._reader.start()

        def _pump(self):
            buf = b""
            while not self.quit_now.is_set():
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if b"\r\n\r\n" in buf:
                    _, rest = buf.split(b"\r\n\r\n", 1)
                    if rest:
                        self.queue.put(rest)
                    break
            while not self.quit_now.is_set():
                time.sleep(0.05)

        def close(self):
            self.quit_now.set()
            closed_by_client.set()
            try:
                self.sock.close()
            except OSError:
                pass

    def fake_post(url, **kwargs):
        s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        s.sendall(b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        resp = _LoopbackQueueResp(s)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not closed_by_client.is_set():
            if not resp._reader.is_alive():
                break
            time.sleep(0.01)
            break
        return resp

    sess = SimpleNamespace(backend=SimpleNamespace(_minara_post=fake_post), _cancelled=False)
    stream = minara.iter_events(CFG, sess, {"model": "openai/gpt-resp", "endpoint": "responses"},
                                "hi", "sys", None, None, 100)
    first = await anext(stream)
    assert first["type"] == "usage_start"

    for _ in range(50):
        if getattr(sess, "_active_direct_abort", None) is not None:
            break
        await asyncio.sleep(0.01)

    t0 = time.monotonic()
    await stream.aclose()
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"Abort took {elapsed:.3f}s (>= 1.0s)"
    assert sess._active_direct_abort is None
