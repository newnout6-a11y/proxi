"""Universal Anthropic `/v1/messages` streaming driver with signed thinking, 4-point cache_control, and PDF/vision."""
from __future__ import annotations

import copy
import json
import re
import urllib.parse

import httpx

import apierr
from catalog import discovery, models_dev
import client_context
import conversation_state
from drivers import cache_engine, http_pool, sse_utils
from drivers.base import DriverCapabilities, UpstreamDriver, register_driver
import models as model_catalog
import native_history

DEFAULT_MAX_TOKENS = 32000
SAFE_ANTHROPIC_BETAS = frozenset({
    "extended-cache-ttl-2025-04-11",
    "fine-grained-tool-streaming-2025-05-14",
    "interleaved-thinking-2025-05-14",
    "context-1m-2025-08-07",
    "effort-2025-11-24",
})

_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}

_ADAPTIVE_CLAUDE_RE = re.compile(r"claude-(?:fable-|opus-(?:4-[678]|5)|sonnet-(?:4-6|5)|haiku-5)", re.I)


def _supports_adaptive_thinking(model_id: str) -> bool:
    slug = models_dev.canonical_slug(model_id)
    return bool(_ADAPTIVE_CLAUDE_RE.search(slug))


def _anthropic_content_blocks(blocks) -> list[dict]:
    if blocks is None:
        return []
    if isinstance(blocks, str):
        return [{"type": "text", "text": blocks}] if blocks else []
    out: list[dict] = []
    for part in blocks if isinstance(blocks, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and part.get("text"):
            out.append({"type": "text", "text": part["text"]})
        elif kind == "image":
            data = str(part.get("imageData") or "")
            mime = str(part.get("mimeType") or "image/png")
            if data.startswith(("https://", "http://")):
                out.append({"type": "image", "source": {"type": "url", "url": data}})
            elif data.startswith("data:"):
                m = re.match(r"data:([\w.+/-]+);base64,(.*)", data, re.DOTALL)
                if m:
                    out.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
            elif data:
                out.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
        elif kind == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            m = re.match(r"data:([\w.+/-]+);base64,(.*)", url, re.DOTALL)
            if m:
                out.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
            elif url.startswith(("https://", "http://")):
                out.append({"type": "image", "source": {"type": "url", "url": url}})
        elif kind == "document":
            b64 = part.get("data") or ""
            mime = part.get("media_type") or "application/pdf"
            if b64:
                doc: dict = {"type": "document", "source": {"type": "base64", "media_type": mime, "data": b64}}
                if part.get("filename"):
                    doc["title"] = str(part["filename"])[:120]
                out.append(doc)
    return out


def _is_real_anthropic_signature(sig: str | None) -> bool:
    if not isinstance(sig, str) or not sig:
        return False
    return not sig.startswith(("unsigned:", "openrouter:"))


def _canonical_to_anthropic_turns(canonical: list[dict], *, thinking_enabled: bool = True) -> list[dict]:
    """Convert canonical history (with signed `thinking` & `redacted_thinking`) into alternating Anthropic turns."""
    turns: list[dict] = []

    def add_turn(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if turns and turns[-1]["role"] == role:
            merged = turns[-1]["content"] + blocks
            if role == "user":
                # Anthropic requires `tool_result` blocks to precede any `text`/`image`/`document` blocks in a `user` turn
                turns[-1]["content"] = (
                    [b for b in merged if b.get("type") == "tool_result"]
                    + [b for b in merged if b.get("type") != "tool_result"]
                )
            else:
                turns[-1]["content"] = merged
        else:
            turns.append({"role": role, "content": list(blocks)})

    for msg in canonical:
        role = msg.get("role", "user")
        val = msg.get("content")
        if role == "tool" and isinstance(val, dict):
            call_id = val.get("toolCallId")
            res = val.get("toolResult", "")
            if isinstance(res, str):
                t_content: str | list = res
            else:
                parts = _anthropic_content_blocks(res)
                if parts and all(p.get("type") == "text" for p in parts):
                    t_content = "\n".join(p["text"] for p in parts)
                else:
                    t_content = parts or ""
            tr: dict = {"type": "tool_result", "tool_use_id": call_id, "content": t_content}
            if msg.get("isError"):
                tr["is_error"] = True
            add_turn("user", [tr])
            continue

        if role == "assistant":
            raw_blocks = val if isinstance(val, list) else ([{"type": "text", "text": val}] if isinstance(val, str) and val else [])
            a_blocks: list[dict] = []
            for b in raw_blocks:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "thinking" and thinking_enabled and _is_real_anthropic_signature(b.get("signature")):
                    a_blocks.append({
                        "type": "thinking",
                        "thinking": b.get("thinking", ""),
                        "signature": b["signature"],
                    })
                elif btype == "redacted_thinking" and thinking_enabled and isinstance(b.get("data"), str) and b["data"]:
                    a_blocks.append({"type": "redacted_thinking", "data": b["data"]})
                elif btype == "text" and b.get("text"):
                    a_blocks.append({"type": "text", "text": b["text"]})
                elif btype == "tool_call":
                    a_blocks.append({
                        "type": "tool_use",
                        "id": b["toolCallId"],
                        "name": b["toolName"],
                        "input": copy.deepcopy(b.get("toolInput") or {}),
                    })
            add_turn("assistant", a_blocks)
            continue

        if role in ("system", "developer"):
            txt_parts = _anthropic_content_blocks(val)
            txt = "\n".join(p["text"] for p in txt_parts if p.get("type") == "text")
            if txt.strip():
                add_turn("user", [{"type": "text", "text": native_history.reminder(txt)}])
            continue

        u_blocks = _anthropic_content_blocks(val)
        if u_blocks:
            add_turn("user", u_blocks)

    return turns


def _anthropic_tools(tools: list | None) -> list[dict]:
    out: list[dict] = []
    for t in sse_utils.clean_openai_tools(tools):
        fn = t.get("function") or {}
        declared: dict = {
            "name": fn.get("name"),
            "input_schema": copy.deepcopy(fn.get("parameters") or {"type": "object", "properties": {}}),
        }
        if fn.get("description"):
            declared["description"] = fn["description"]
        if fn.get("strict") is True:
            declared["strict"] = True
        out.append(declared)
    return out


def normalize_anthropic_usage(raw_usage: dict, bound_entry: dict | None = None) -> dict:
    """Normalize Anthropic `usage` (`input_tokens` on Anthropic ALREADY excludes `cache_read` and `cache_creation`)."""
    if not isinstance(raw_usage, dict):
        return {}
    result: dict = {}
    for src, dst in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_input_tokens", "cache_read"),
        ("cache_creation_input_tokens", "cache_write"),
    ):
        val = raw_usage.get(src)
        if type(val) is int and val >= 0:
            result[dst] = val
    result["cache_write_reported"] = "cache_creation_input_tokens" in raw_usage

    if bound_entry is not None and "input_tokens" in result:
        total_in = result["input_tokens"] + result.get("cache_read", 0) + result.get("cache_write", 0)
        calc = models_dev.calculate_cost_usd(
            bound_entry,
            {
                "input_tokens": total_in,
                "cache_read_input_tokens": result.get("cache_read", 0),
                "cache_creation_input_tokens": result.get("cache_write", 0),
                "output_tokens": result.get("output_tokens", 0),
            },
        )
        if calc is not None:
            result["provider_cost"] = calc
            result["cost_usd"] = calc
            result["provider_cost_currency"] = "USD"
            result["provider_cost_estimated"] = True
    return result


class AnthropicMessagesDriver(UpstreamDriver):
    driver_id = "anthropic-messages"
    capabilities = DriverCapabilities(
        driver_id="anthropic-messages",
        billing_unit="usd",
        uses_retool_tool_schema=False,
        requires_retool_resource=False,
        supports_native_history=True,
        supports_keepwarm=False,
        supports_cache_control=True,
        supports_prompt_cache_key=False,
    )

    async def discover_models(self, cfg: dict) -> list[dict]:
        return await discovery.discover_provider_models(cfg)

    async def stream_turn(
        self,
        cfg: dict,
        session,
        model_provider: dict,
        prompt: str,
        head: str | None,
        tools: list | None,
        tool_choice,
        max_tokens: int | None,
        chunks: list | None = None,
    ):
        bound_entry = getattr(session, "_bound_entry", None) or {}

        def single_attempt(eff_tools, eff_choice, instruction=None, text_only=False):
            return self._stream_once(
                cfg,
                session,
                model_provider,
                prompt,
                head,
                eff_tools,
                eff_choice,
                max_tokens,
                chunks,
                bound_entry=bound_entry,
                instruction=instruction,
                text_only=text_only,
            )

        async for event in cache_engine.run_guarded_turn(
            cfg, session, model_provider, tools, tool_choice, bound_entry, single_attempt
        ):
            yield event

    async def _stream_once(
        self,
        cfg: dict,
        session,
        model_provider: dict,
        prompt: str,
        head: str | None,
        tools: list | None,
        tool_choice,
        max_tokens: int | None,
        chunks: list | None,
        *,
        bound_entry: dict,
        instruction: str | None = None,
        text_only: bool = False,
    ):
        model = str(bound_entry.get("upstream_model") or model_provider.get("model") or "")
        effort = model_provider.get("reasoningEffort") or model_provider.get("reasoning_effort") or model_provider.get("effort")
        if effort == "auto":
            effort = None
        provider_opts = getattr(session, "_provider_options", None) or {}
        raw_thinking_opt = provider_opts.get("thinking") if isinstance(provider_opts.get("thinking"), dict) else {}

        # Determine thinking config before building history so thinking blocks are omitted if thinking is disabled
        thinking_spec: dict | None = None
        output_config: dict = {}
        supports_reasoning = bool(bound_entry.get("reasoning", True))

        if supports_reasoning and effort != "none" and str(raw_thinking_opt.get("type") or "").lower() != "disabled":
            if _supports_adaptive_thinking(model) or str(raw_thinking_opt.get("type") or "").lower() == "adaptive":
                if effort or raw_thinking_opt:
                    thinking_spec = {"type": "adaptive"}
                    if effort:
                        mapped_eff = {"minimal": "low", "xhigh": "max"}.get(effort, effort)
                        output_config["effort"] = mapped_eff
            elif effort or raw_thinking_opt.get("budget_tokens") or str(raw_thinking_opt.get("type") or "").lower() == "enabled":
                budget = (
                    int(raw_thinking_opt["budget_tokens"])
                    if isinstance(raw_thinking_opt.get("budget_tokens"), int) and raw_thinking_opt["budget_tokens"] >= 1024
                    else model_catalog.budget_from_effort(effort or "medium")
                )
                thinking_spec = {"type": "enabled", "budget_tokens": budget}

        clean_head = cache_engine.strip_billing_header(head)
        scope = cache_engine.build_scope(
            session, self.driver_id, cfg, model, effort if thinking_spec else None, clean_head, tools, tool_choice
        )
        if text_only:
            parent_snapshot, canonical_msgs, client_echo = None, cache_engine._fallback_canonical(chunks, prompt), None
        else:
            parent_snapshot, canonical_msgs, client_echo = cache_engine.prepare_canonical_history(
                session, scope, tools, chunks, prompt
            )

        turns = _canonical_to_anthropic_turns(canonical_msgs, thinking_enabled=(thinking_spec is not None))
        if not turns:
            turns = [{"role": "user", "content": [{"type": "text", "text": prompt or "(empty)"}]}]

        system_blocks = [{"type": "text", "text": clean_head}] if clean_head else []
        anth_tools = _anthropic_tools(tools)

        side_task = client_context.current().get("client_task") in client_context.SIDE_TASKS
        mark_spec = cache_engine.cache_control_spec(cfg, self.driver_id)
        cache_engine.apply_anthropic_cache_breakpoints(
            system_blocks, anth_tools, turns, mark_spec, mark_tail=not side_task
        )

        if instruction:
            if turns and turns[-1]["role"] == "user":
                turns[-1]["content"].append({"type": "text", "text": instruction})
            else:
                turns.append({"role": "user", "content": [{"type": "text", "text": instruction}]})

        eff_max_tokens = int(max_tokens or DEFAULT_MAX_TOKENS)
        if thinking_spec and thinking_spec.get("type") == "enabled":
            min_required = int(thinking_spec["budget_tokens"]) + 1024
            if eff_max_tokens <= int(thinking_spec["budget_tokens"]):
                cap = int(bound_entry.get("max_output") or 64000)
                if min_required <= cap:
                    eff_max_tokens = min_required
                elif eff_max_tokens > 2048:
                    thinking_spec["budget_tokens"] = max(1024, eff_max_tokens - 1024)
                else:
                    thinking_spec = None

        body: dict = {
            "model": model,
            "max_tokens": eff_max_tokens,
            "messages": turns,
            "stream": True,
        }
        if system_blocks:
            body["system"] = system_blocks
        if anth_tools:
            body["tools"] = anth_tools
            if tool_choice is not None:
                disable_parallel = provider_opts.get("disable_parallel_tool_use")
                tc_obj: dict | None = None
                if tool_choice in ("auto", "none"):
                    tc_obj = {"type": tool_choice}
                elif tool_choice == "required":
                    tc_obj = {"type": "any"}
                elif isinstance(tool_choice, dict) and (tool_choice.get("function") or {}).get("name"):
                    tc_obj = {"type": "tool", "name": tool_choice["function"]["name"]}
                elif isinstance(tool_choice, str):
                    tc_obj = {"type": "tool", "name": tool_choice}
                if tc_obj is not None:
                    if isinstance(disable_parallel, bool) and tc_obj["type"] != "none":
                        tc_obj["disable_parallel_tool_use"] = disable_parallel
                    body["tool_choice"] = tc_obj

        if thinking_spec is not None:
            body["thinking"] = thinking_spec
        if output_config:
            body["output_config"] = output_config

        # Anthropic forbids non-1 temperature / top_k / top_p when thinking is enabled
        if thinking_spec is None:
            for k in ("temperature", "top_p", "top_k"):
                if provider_opts.get(k) is not None:
                    body[k] = provider_opts[k]
        elif provider_opts.get("temperature") == 1.0:
            body["temperature"] = 1.0

        if provider_opts.get("stop_sequences"):
            body["stop_sequences"] = list(provider_opts["stop_sequences"])

        body = sse_utils.apply_body_overrides(body, cfg.get("body_overrides"))

        base = str(cfg.get("base_url") or cfg.get("base") or "").rstrip("/")
        url = http_pool.validate_endpoint_url(f"{base}/messages", allow_insecure_http=bool(cfg.get("allow_insecure_http", False)))
        headers = discovery.build_auth_headers(cfg)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"
        betas: list[str] = []
        for k in list(headers):
            if k.lower() == "anthropic-beta":
                raw_val = headers.pop(k)
                if isinstance(raw_val, str):
                    for b in raw_val.split(","):
                        bs = b.strip()
                        if bs and bs not in betas:
                            betas.append(bs)
        base_host = (urllib.parse.urlsplit(base).hostname or "").lower()
        is_anthropic_host = base_host == "anthropic.com" or base_host.endswith(".anthropic.com")
        if is_anthropic_host:
            for beta in client_context.anthropic_betas():
                if beta in SAFE_ANTHROPIC_BETAS and beta not in betas:
                    betas.append(beta)
        if mark_spec and mark_spec.get("ttl") == "1h" and "extended-cache-ttl-2025-04-11" not in betas:
            betas.append("extended-cache-ttl-2025-04-11")
        if betas:
            headers["anthropic-beta"] = ",".join(betas)

        head_key, head_role, head_gate = cache_engine.claim_universal_head(
            cfg, session, self.driver_id, model, effort, anth_tools, clean_head
        )
        head_marked = False
        client = http_pool.get_async_client(cfg)
        timeout = http_pool.make_timeout(cfg)
        active_resp_holder: dict = {"resp": None}
        abort = http_pool.make_stream_abort(session, active_resp_holder)
        request_sent_cb = getattr(session, "_provider_request_sent", None)

        yield {"type": "usage_start"}
        if head_role == "follow":
            await cache_engine.wait_for_universal_head(head_gate, model, self.driver_id)

        try:
            if callable(request_sent_cb):
                request_sent_cb()
                yield {"type": "request_sent"}

            async with http_pool.abortable_stream(
                client,
                "POST",
                url,
                active_resp_holder=active_resp_holder,
                headers=headers,
                json=body,
                timeout=timeout,
            ) as resp:
                if resp.status_code >= 400:
                    err_bytes = await resp.aread()
                    err_text = err_bytes.decode("utf-8", errors="replace")
                    err = sse_utils.map_http_error(resp.status_code, resp.headers, err_text, cfg)
                    if parent_snapshot is not None:
                        conversation_state.refuse(session, parent_snapshot, err)
                    raise err

                head_marked = True
                cache_engine.mark_head_started(head_key, head_gate)

                blocks_by_idx: dict[int, dict] = {}
                calls_by_idx: dict[int, dict] = {}
                thinking_started = False
                has_output = False
                raw_usage: dict = {}
                finish_reason: str | None = None
                saw_stop = False

                async for ev_name, data_str in sse_utils.aiter_sse_events(
                    http_pool.iter_abortable_bytes(resp, active_resp_holder),
                    label=cfg.get("provider_name") or "Anthropic",
                ):
                    if getattr(session, "_cancelled", False):
                        yield {"type": "end", "reason": "cancelled"}
                        return
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        item = sse_utils.bounded_json(data_str)
                    except (ValueError, RecursionError):
                        raise apierr.upstream_error("Anthropic stream returned malformed SSE JSON") from None
                    if not isinstance(item, dict):
                        continue

                    kind = item.get("type") or ev_name
                    if kind == "error":
                        raise sse_utils.map_sse_error(data_str, resp.headers, cfg, pre_output=not has_output)

                    if kind == "message_start":
                        msg_obj = item.get("message") if isinstance(item.get("message"), dict) else {}
                        if isinstance(msg_obj.get("usage"), dict):
                            raw_usage.update(msg_obj["usage"])

                    elif kind == "content_block_start":
                        idx = item.get("index")
                        cb = item.get("content_block")
                        if type(idx) is not int or not isinstance(cb, dict):
                            continue
                        btype = cb.get("type")
                        if btype == "thinking":
                            has_output = True
                            init_think = cb.get("thinking") if isinstance(cb.get("thinking"), str) else ""
                            blocks_by_idx[idx] = {
                                "type": "thinking",
                                "thinking": init_think,
                                "signature": cb.get("signature") if isinstance(cb.get("signature"), str) else "",
                                "signature_emitted": False,
                            }
                            if not thinking_started:
                                thinking_started = True
                                yield {"type": "thinking_start"}
                            if init_think:
                                yield {"type": "thinking", "delta": init_think}
                        elif btype == "redacted_thinking":
                            has_output = True
                            blocks_by_idx[idx] = {
                                "type": "redacted_thinking",
                                "data": str(cb.get("data") or ""),
                            }
                            if not thinking_started:
                                thinking_started = True
                                yield {"type": "thinking_start"}
                        elif btype == "text":
                            has_output = True
                            init_txt = cb.get("text") if isinstance(cb.get("text"), str) else ""
                            blocks_by_idx[idx] = {"type": "text", "text": init_txt}
                            if init_txt:
                                yield {"type": "text", "delta": init_txt}
                        elif btype == "tool_use":
                            has_output = True
                            cid = str(cb.get("id") or f"toolu_{idx}")
                            cname = str(cb.get("name") or "")
                            init_inp = cb.get("input")
                            init_args = json.dumps(init_inp, ensure_ascii=False) if isinstance(init_inp, dict) and init_inp else ""
                            calls_by_idx[idx] = {"id": cid, "name": cname, "arguments": init_args}
                            blocks_by_idx[idx] = {"type": "tool_call", "toolCallId": cid, "toolName": cname}
                            if cname:
                                yield {"type": "tool_call", "id": cid, "name": cname, "arguments": init_args, "partial": True}

                    elif kind == "content_block_delta":
                        idx = item.get("index")
                        delta = item.get("delta")
                        blk = blocks_by_idx.get(idx) if type(idx) is int else None
                        if blk is None or not isinstance(delta, dict):
                            continue
                        dtype = delta.get("type")
                        if dtype == "text_delta" and isinstance(delta.get("text"), str) and delta["text"]:
                            has_output = True
                            blk["text"] = blk.get("text", "") + delta["text"]
                            yield {"type": "text", "delta": delta["text"]}
                        elif dtype == "thinking_delta" and isinstance(delta.get("thinking"), str) and delta["thinking"]:
                            has_output = True
                            if not thinking_started:
                                thinking_started = True
                                yield {"type": "thinking_start"}
                            blk["thinking"] = blk.get("thinking", "") + delta["thinking"]
                            yield {"type": "thinking", "delta": delta["thinking"]}
                        elif dtype == "signature_delta" and isinstance(delta.get("signature"), str):
                            blk["signature"] = blk.get("signature", "") + delta["signature"]
                        elif dtype == "input_json_delta" and idx in calls_by_idx:
                            piece = delta.get("partial_json")
                            if isinstance(piece, str):
                                has_output = True
                                calls_by_idx[idx]["arguments"] += piece
                                if len(calls_by_idx[idx]["arguments"].encode("utf-8")) > sse_utils.MAX_EVENT_BYTES:
                                    raise apierr.upstream_error("Anthropic tool call arguments exceeded size limit")
                                yield {
                                    "type": "tool_call",
                                    "id": calls_by_idx[idx]["id"],
                                    "name": calls_by_idx[idx]["name"],
                                    "arguments": calls_by_idx[idx]["arguments"],
                                    "partial": True,
                                }

                    elif kind == "content_block_stop":
                        idx = item.get("index")
                        blk = blocks_by_idx.get(idx) if type(idx) is int else None
                        if blk and blk.get("type") == "thinking" and blk.get("signature") and not blk.get("signature_emitted"):
                            blk["signature_emitted"] = True
                            yield {"type": "thinking_signature", "signature": blk["signature"]}

                    elif kind == "message_delta":
                        delta = item.get("delta") if isinstance(item.get("delta"), dict) else {}
                        stop = delta.get("stop_reason")
                        if isinstance(stop, str) and stop:
                            finish_reason = _STOP_REASONS.get(stop, "stop")
                        if isinstance(item.get("usage"), dict):
                            raw_usage.update({k: v for k, v in item["usage"].items() if v is not None})

                    elif kind == "message_stop":
                        saw_stop = True
                        break

                if getattr(session, "_cancelled", False):
                    yield {"type": "end", "reason": "cancelled"}
                    return
                if not saw_stop:
                    raise apierr.upstream_error("Upstream Anthropic Messages stream ended before message_stop")
                reply_blocks: list[dict] = []
                for idx in sorted(blocks_by_idx):
                    blk = blocks_by_idx[idx]
                    btype = blk.get("type")
                    if btype == "thinking" and blk.get("signature"):
                        reply_blocks.append({
                            "type": "thinking",
                            "thinking": blk.get("thinking", ""),
                            "signature": blk["signature"],
                        })
                    elif btype == "redacted_thinking" and blk.get("data"):
                        reply_blocks.append({"type": "redacted_thinking", "data": blk["data"]})
                    elif btype == "text" and blk.get("text"):
                        reply_blocks.append({"type": "text", "text": blk["text"]})
                    elif btype == "tool_call" and idx in calls_by_idx:
                        slot = calls_by_idx[idx]
                        raw_args = slot["arguments"] or "{}"
                        _, valid_obj = sse_utils.strict_tool_arguments(raw_args)
                        ev: dict = {"type": "tool_call", "id": slot["id"], "name": slot["name"], "arguments": raw_args}
                        if not valid_obj:
                            ev["invalid"] = True
                        else:
                            parsed_dict = native_history.loads_finite(raw_args)
                            reply_blocks.append({
                                "type": "tool_call",
                                "toolCallId": slot["id"],
                                "toolName": slot["name"],
                                "toolInput": parsed_dict if isinstance(parsed_dict, dict) else {},
                            })
                        yield ev

                if raw_usage:
                    yield {"type": "usage", "usage": normalize_anthropic_usage(raw_usage, bound_entry)}

                effective_finish = finish_reason or ("tool_calls" if calls_by_idx else "stop")
                if not text_only and effective_finish in ("stop", "tool_calls") and session is not None:
                    session._pending_history_commit = lambda _rb=reply_blocks: cache_engine.record_canonical_reply(
                        session, scope, client_echo, canonical_msgs, _rb, tools, parent_snapshot
                    )
                yield {"type": "end", "reason": effective_finish}
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise http_pool.map_transport_error(exc, cfg.get("provider_name") or "Anthropic") from None
        finally:
            cache_engine.release_head(head_key, head_role, head_gate, head_marked)
            http_pool.clear_stream_abort(session, abort)


register_driver(AnthropicMessagesDriver())
