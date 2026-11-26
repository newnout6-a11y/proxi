"""Universal OpenAI `/chat/completions` streaming driver (OpenAI, OpenRouter, DeepSeek, Gemini, Groq, xAI, Kimi, Ollama, vLLM)."""
from __future__ import annotations

import copy
import json
import math

import apierr
from catalog import discovery, models_dev
import client_context
import conversation_state
from drivers import cache_engine, http_pool, sse_utils
from drivers.base import DriverCapabilities, UpstreamDriver, register_driver

_FINISH_MAP = {
    "stop": "stop",
    "end_turn": "stop",
    "length": "length",
    "max_tokens": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
    "safety": "content_filter",
}


def _convert_content_blocks(blocks):
    if isinstance(blocks, str):
        return blocks
    result = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            result.append({"type": "text", "text": block.get("text", "")})
        elif kind == "image":
            data = block.get("imageData", "")
            url = (
                data
                if data.startswith(("https://", "http://", "data:"))
                else f"data:{block.get('mimeType', 'image/png')};base64,{data}"
            )
            result.append({"type": "image_url", "image_url": {"url": url}})
        elif kind == "image_url":
            result.append(copy.deepcopy(block))
        elif kind == "document":
            b64 = block.get("data") or ""
            mime = block.get("media_type") or "application/pdf"
            fname = block.get("filename") or "document.pdf"
            result.append({
                "type": "file",
                "file": {"filename": fname, "file_data": f"data:{mime};base64,{b64}"},
            })
    return result


def _canonical_to_chat_messages(canonical: list[dict], *, preset: str = "") -> list[dict]:
    """Convert canonical history to OpenAI chat messages, replaying active-turn `reasoning_content` / `thought_signature`."""
    last_plain_user_idx = max(
        (i for i, m in enumerate(canonical) if m.get("role") == "user"),
        default=-1,
    )
    out: list[dict] = []
    queued_images: list[dict] = []
    known_visual = {"text", "image", "image_url", "document"}

    for idx, msg in enumerate(canonical):
        role = msg.get("role", "user")
        if role != "tool" and queued_images:
            out.append({"role": "user", "content": queued_images})
            queued_images = []

        val = msg.get("content")
        if role == "tool" and isinstance(val, dict):
            call_id = val.get("toolCallId")
            res = val.get("toolResult", "")
            has_visual = (
                isinstance(res, list)
                and bool(res)
                and all(isinstance(b, dict) and b.get("type") in known_visual for b in res)
                and any(b["type"] in ("image", "image_url") for b in res)
            )
            if has_visual:
                texts = [b.get("text", "") for b in res if b["type"] == "text"]
                visual = _convert_content_blocks([b for b in res if b["type"] in ("image", "image_url")])
                out.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": "\n".join(texts) + ("\n" if texts else "") + "Image result follows as visual input.",
                })
                queued_images.append({"type": "text", "text": f"Images returned by tool call {call_id}:"})
                queued_images.extend(visual)
            else:
                if isinstance(res, list) and res and all(isinstance(b, dict) and b.get("type") == "text" for b in res):
                    text_res = "\n".join(b.get("text") or "" for b in res)
                elif isinstance(res, str):
                    text_res = res
                else:
                    text_res = json.dumps(res, ensure_ascii=False)
                out.append({"role": "tool", "tool_call_id": call_id, "content": text_res})
            continue

        if role == "assistant":
            blocks = val if isinstance(val, list) else ([{"type": "text", "text": val}] if isinstance(val, str) and val else [])
            texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            calls = []
            thinking_texts = []
            or_details = []
            in_active_tool_loop = idx > last_plain_user_idx

            for b in blocks:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "tool_call":
                    tc = {
                        "id": b["toolCallId"],
                        "type": "function",
                        "function": {
                            "name": b["toolName"],
                            "arguments": json.dumps(b.get("toolInput", {}), ensure_ascii=False),
                        },
                    }
                    if isinstance(b.get("extra_content"), dict):
                        tc["extra_content"] = copy.deepcopy(b["extra_content"])
                    calls.append(tc)
                elif btype == "thinking" and in_active_tool_loop:
                    if b.get("thinking"):
                        thinking_texts.append(b["thinking"])
                    if isinstance(b.get("reasoning_details"), list):
                        or_details.extend(copy.deepcopy(b["reasoning_details"]))

            content_str = "".join(texts)
            entry: dict = {"role": "assistant", "content": content_str if content_str else (None if calls else "")}
            if calls:
                entry["tool_calls"] = calls
            # Replay reasoning_content inside active tool-call loops (required by DeepSeek R1/V3.2 & Kimi K2.5),
            # while stripping it from older completed turns before `last_plain_user_idx` to preserve prefix cache!
            if in_active_tool_loop and calls:
                if preset == "openrouter" and or_details:
                    entry["reasoning_details"] = or_details
                elif thinking_texts and preset in ("deepseek", "moonshot", "siliconflow", "vllm", "ollama", "custom", ""):
                    entry["reasoning_content"] = "".join(thinking_texts)
            out.append(entry)
            continue

        converted = _convert_content_blocks(val)
        if isinstance(converted, list) and len(converted) == 1 and converted[0].get("type") == "text":
            converted = converted[0]["text"]
        out.append({"role": role if role in ("system", "developer", "user") else "user", "content": converted})

    if queued_images:
        out.append({"role": "user", "content": queued_images})
    return out


def normalize_chat_usage(raw_usage: dict, bound_entry: dict | None = None) -> dict:
    """Normalize OpenAI/OpenRouter `usage` into relay counters (`input_tokens` = uncached input, `cache_read`, `cache_write`)."""
    if not isinstance(raw_usage, dict):
        return {}
    result: dict = {}
    p_tok = raw_usage.get("prompt_tokens")
    c_tok = raw_usage.get("completion_tokens")
    if type(p_tok) is int and p_tok >= 0:
        result["input_tokens"] = p_tok
    if type(c_tok) is int and c_tok >= 0:
        result["output_tokens"] = c_tok

    p_details = raw_usage.get("prompt_tokens_details") if isinstance(raw_usage.get("prompt_tokens_details"), dict) else {}
    c_details = raw_usage.get("completion_tokens_details") if isinstance(raw_usage.get("completion_tokens_details"), dict) else {}

    # DeepSeek also reports top-level `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
    cached = p_details.get("cached_tokens")
    if cached is None and type(raw_usage.get("prompt_cache_hit_tokens")) is int:
        cached = raw_usage["prompt_cache_hit_tokens"]
    cache_write = p_details.get("cache_write_tokens")
    reasoning_tok = c_details.get("reasoning_tokens")

    if type(cached) is int and cached >= 0:
        result["cache_read"] = cached
    if type(cache_write) is int and cache_write >= 0:
        result["cache_write"] = cache_write
        result["cache_write_reported"] = True
    if type(reasoning_tok) is int and reasoning_tok >= 0:
        result["reasoning_tokens"] = reasoning_tok

    # Relay convention: `input_tokens` excludes `cache_read` + `cache_write`; adapters/journal add them back.
    total_prompt = result.get("input_tokens")
    if total_prompt is not None:
         total_cached = result.get("cache_read", 0) + result.get("cache_write", 0)
         if total_cached <= total_prompt:
             result["input_tokens"] = total_prompt - total_cached
         else:
             result.pop("cache_read", None)
             result.pop("cache_write", None)

    reported_cost = raw_usage.get("cost")
    if type(reported_cost) in (int, float) and not isinstance(reported_cost, bool) and math.isfinite(reported_cost) and reported_cost >= 0:
        if not (reported_cost == 0 and result.get("output_tokens")):
            result["provider_cost"] = round(float(reported_cost), 10)
            result["cost_usd"] = result["provider_cost"]
            result["provider_cost_currency"] = "USD"
    if "provider_cost" not in result and bound_entry is not None and total_prompt is not None:
        calc = models_dev.calculate_cost_usd(
            bound_entry,
            {
                "input_tokens": total_prompt,
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


class OpenAIChatDriver(UpstreamDriver):
    driver_id = "openai-chat"
    capabilities = DriverCapabilities(
        driver_id="openai-chat",
        billing_unit="usd",
        uses_retool_tool_schema=False,
        requires_retool_resource=False,
        supports_native_history=True,
        supports_keepwarm=False,
        supports_cache_control=True,
        supports_prompt_cache_key=True,
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
        preset = str(cfg.get("preset") or "").lower()
        effort = model_provider.get("reasoningEffort") or model_provider.get("reasoning_effort") or model_provider.get("effort")
        if effort == "auto":
            effort = None
        clean_head = cache_engine.strip_billing_header(head)

        scope = cache_engine.build_scope(session, self.driver_id, cfg, model, effort, clean_head, tools, tool_choice)
        if text_only:
            parent_snapshot, canonical_msgs, client_echo = None, cache_engine._fallback_canonical(chunks, prompt), None
        else:
            parent_snapshot, canonical_msgs, client_echo = cache_engine.prepare_canonical_history(
                session, scope, tools, chunks, prompt
            )

        chat_messages: list[dict] = []
        if clean_head:
            chat_messages.append({"role": "system", "content": clean_head})
        chat_messages.extend(_canonical_to_chat_messages(canonical_msgs, preset=preset))

        side_task = client_context.current().get("client_task") in client_context.SIDE_TASKS
        is_openrouter = preset == "openrouter" or "openrouter.ai" in str(cfg.get("base") or "").lower()
        if is_openrouter and model.lower().startswith(("anthropic/", "google/")):
            mark_spec = cache_engine.cache_control_spec(cfg, self.driver_id, is_openrouter=True)
            cache_engine.apply_chat_cache_breakpoints(chat_messages, mark_spec, mark_tail=not side_task)

        if instruction:
            chat_messages.append({"role": "user", "content": instruction})

        cleaned_tools = sse_utils.clean_openai_tools(tools)
        body: dict = {
            "model": model,
            "messages": chat_messages,
            "stream": True,
        }
        # Local Ollama/LM Studio older builds ignore or accept stream_options; standard OpenAI/OpenRouter require it for usage
        if preset not in ("lmstudio",):
            body["stream_options"] = {"include_usage": True}

        if max_tokens is not None:
            if preset == "openai" and bound_entry.get("reasoning"):
                body["max_completion_tokens"] = max_tokens
            else:
                body["max_tokens"] = max_tokens

        if cleaned_tools:
            body["tools"] = cleaned_tools
            if tool_choice:
                body["tool_choice"] = (
                    tool_choice
                    if isinstance(tool_choice, str) and tool_choice in ("auto", "none", "required")
                    else {"type": "function", "function": {"name": tool_choice}}
                    if isinstance(tool_choice, str)
                    else copy.deepcopy(tool_choice)
                )

        if effort and preset not in ("ollama", "lmstudio"):
            body["reasoning_effort"] = effort

        provider_opts = getattr(session, "_provider_options", None) or {}
        for k in ("temperature", "top_p", "seed", "presence_penalty", "frequency_penalty", "parallel_tool_calls", "service_tier"):
            if provider_opts.get(k) is not None:
                # OpenAI reasoning models reject non-1 temperature
                if k in ("temperature", "top_p") and preset == "openai" and bound_entry.get("reasoning"):
                    continue
                body[k] = provider_opts[k]
        if provider_opts.get("stop_sequences"):
            body["stop"] = list(provider_opts["stop_sequences"])

        cache_engine.apply_openai_cache_hints(
            body, cfg, session, model, provider_opts, include_retention=(preset == "openai")
        )
        body = sse_utils.apply_body_overrides(body, cfg.get("body_overrides"))

        base = str(cfg.get("base_url") or cfg.get("base") or "").rstrip("/")
        url = http_pool.validate_endpoint_url(f"{base}/chat/completions", allow_insecure_http=bool(cfg.get("allow_insecure_http", False)))
        headers = discovery.build_auth_headers(cfg)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"

        head_key, head_role, head_gate = cache_engine.claim_universal_head(
            cfg, session, self.driver_id, model, effort, cleaned_tools, clean_head
        )
        head_marked = False
        client = http_pool.get_async_client(cfg)
        timeout = http_pool.make_timeout(cfg)
        active_resp_holder: dict = {"resp": None}

        def abort():
            resp = active_resp_holder.get("resp")
            if resp is not None:
                try:
                    # Closing underlying stream wakes pending async reads cleanly
                    resp.extensions.get("network_stream")
                except Exception:
                    pass

        if session is not None:
            session._direct_reply = None
            session._active_direct_abort = abort

        request_sent_cb = getattr(session, "_provider_request_sent", None)
        yield {"type": "usage_start"}
        if head_role == "follow":
            await cache_engine.wait_for_universal_head(head_gate, model, self.driver_id)

        try:
            if callable(request_sent_cb):
                request_sent_cb()
                yield {"type": "request_sent"}

            async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
                active_resp_holder["resp"] = resp
                if resp.status_code >= 400:
                    err_bytes = await resp.aread()
                    err_text = err_bytes.decode("utf-8", errors="replace")
                    err = sse_utils.map_http_error(resp.status_code, resp.headers, err_text, cfg)
                    if parent_snapshot is not None:
                        conversation_state.refuse(session, parent_snapshot, err)
                    raise err

                head_marked = True
                cache_engine.mark_head_started(head_key, head_gate)

                ctype = (resp.headers.get("content-type") or "").lower()
                if "application/json" in ctype and "text/event-stream" not in ctype:
                    # Non-streaming JSON fallback from custom OpenAI-compatible servers
                    raw_bytes = await resp.aread()
                    payload = sse_utils.bounded_json(raw_bytes.decode("utf-8", errors="replace"))
                    for ev in self._events_from_non_stream_json(payload, bound_entry, session, scope, client_echo, canonical_msgs, tools, parent_snapshot):
                        yield ev
                    return

                thinking_started = False
                thinking_chunks: list[str] = []
                reasoning_details_acc: list[dict] = []
                text_chunks: list[str] = []
                tool_calls_by_idx: dict[int, dict] = {}
                finish_reason: str | None = None
                latest_usage: dict | None = None
                saw_done = False

                async for _ev_name, data_str in sse_utils.aiter_sse_events(resp.aiter_bytes(), label=cfg.get("provider_name") or "OpenAI"):
                    if getattr(session, "_cancelled", False):
                        yield {"type": "end", "reason": "cancelled"}
                        return
                    if data_str.strip() == "[DONE]":
                        saw_done = True
                        break
                    try:
                        chunk = sse_utils.bounded_json(data_str)
                    except (ValueError, RecursionError):
                        raise apierr.upstream_error("Upstream returned malformed SSE JSON") from None
                    if not isinstance(chunk, dict):
                        continue
                    if isinstance(chunk.get("error"), dict):
                        raise sse_utils.map_http_error(502, resp.headers, data_str, cfg, unbilled=False)

                    if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                        latest_usage = normalize_chat_usage(chunk["usage"], bound_entry)

                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice0 = choices[0] if isinstance(choices[0], dict) else {}
                    delta = choice0.get("delta") if isinstance(choice0.get("delta"), dict) else {}

                    # 1. Reasoning / Thinking deltas (`reasoning_content`, `reasoning`, `reasoning_details`)
                    r_delta = delta.get("reasoning_content")
                    if not isinstance(r_delta, str) or not r_delta:
                        r_delta = delta.get("reasoning") if isinstance(delta.get("reasoning"), str) else ""
                    if isinstance(delta.get("reasoning_details"), list):
                        for rd in delta["reasoning_details"]:
                            if isinstance(rd, dict):
                                reasoning_details_acc.append(rd)
                                if not r_delta and isinstance(rd.get("text"), str):
                                    r_delta += rd["text"]
                    if r_delta:
                        if not thinking_started:
                            thinking_started = True
                            yield {"type": "thinking_start"}
                        thinking_chunks.append(r_delta)
                        yield {"type": "thinking", "delta": r_delta}

                    # 2. Text delta
                    t_delta = delta.get("content")
                    if isinstance(t_delta, str) and t_delta:
                        text_chunks.append(t_delta)
                        yield {"type": "text", "delta": t_delta}

                    # 3. Streaming tool calls (including Gemini `extra_content.google.thought_signature`)
                    tcs = delta.get("tool_calls")
                    if isinstance(tcs, list):
                        for pos, tc in enumerate(tcs):
                            if not isinstance(tc, dict):
                                continue
                            t_idx = tc.get("index") if type(tc.get("index")) is int else pos
                            slot = tool_calls_by_idx.get(t_idx)
                            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                            if slot is None:
                                slot = {
                                    "id": str(tc.get("id") or f"call_{t_idx}"),
                                    "name": str(fn.get("name") or ""),
                                    "arguments": "",
                                    "extra_content": None,
                                }
                                tool_calls_by_idx[t_idx] = slot
                            else:
                                if tc.get("id"):
                                    slot["id"] = str(tc["id"])
                                if fn.get("name"):
                                    slot["name"] = str(fn["name"])
                            if isinstance(tc.get("extra_content"), dict):
                                slot["extra_content"] = copy.deepcopy(tc["extra_content"])
                            arg_piece = fn.get("arguments")
                            if isinstance(arg_piece, str) and arg_piece:
                                slot["arguments"] += arg_piece
                                if len(slot["arguments"].encode("utf-8")) > sse_utils.MAX_EVENT_BYTES:
                                    raise apierr.upstream_error("Tool call arguments exceeded size limit")
                            if slot["name"]:
                                yield {
                                    "type": "tool_call",
                                    "id": slot["id"],
                                    "name": slot["name"],
                                    "arguments": slot["arguments"],
                                    "partial": True,
                                }

                    fr = choice0.get("finish_reason")
                    if isinstance(fr, str) and fr:
                        finish_reason = _FINISH_MAP.get(fr, "stop")

                # Finalize tool calls & record canonical history
                if getattr(session, "_cancelled", False):
                    yield {"type": "end", "reason": "cancelled"}
                    return
                if finish_reason is None and not saw_done:
                    raise apierr.upstream_error("Upstream Chat Completions stream ended before its terminal frame")

                reply_blocks: list[dict] = []
                full_thinking = "".join(thinking_chunks)
                if full_thinking or reasoning_details_acc:
                    t_block: dict = {
                        "type": "thinking",
                        "thinking": full_thinking,
                        "signature": "openrouter:reasoning_details" if reasoning_details_acc else "unsigned:reasoning_content",
                    }
                    if reasoning_details_acc:
                        t_block["reasoning_details"] = reasoning_details_acc
                    reply_blocks.append(t_block)

                full_text = "".join(text_chunks)
                if full_text:
                    reply_blocks.append({"type": "text", "text": full_text})

                for t_idx in sorted(tool_calls_by_idx):
                    slot = tool_calls_by_idx[t_idx]
                    if not slot["name"]:
                        continue
                    raw_args = slot["arguments"] or "{}"
                    parsed_args, valid_obj = sse_utils.strict_tool_arguments(raw_args)
                    ev: dict = {
                        "type": "tool_call",
                        "id": slot["id"],
                        "name": slot["name"],
                        "arguments": raw_args,
                    }
                    if not valid_obj:
                        ev["invalid"] = True
                    else:
                        tc_block: dict = {
                            "type": "tool_call",
                            "toolCallId": slot["id"],
                            "toolName": slot["name"],
                            "toolInput": native_history_loads_safe(raw_args),
                        }
                        if slot.get("extra_content"):
                            tc_block["extra_content"] = slot["extra_content"]
                        reply_blocks.append(tc_block)
                    yield ev

                if latest_usage is not None:
                    yield {"type": "usage", "usage": latest_usage}

                effective_finish = finish_reason or ("tool_calls" if tool_calls_by_idx else "stop")
                if not text_only and effective_finish in ("stop", "tool_calls") and session is not None:
                    session._pending_history_commit = lambda _rb=reply_blocks: cache_engine.record_canonical_reply(
                        session, scope, client_echo, canonical_msgs, _rb, tools, parent_snapshot
                    )
                yield {"type": "end", "reason": effective_finish}
        except httpx.TimeoutException:
            raise apierr.upstream_timeout(f"{cfg.get('provider_name') or 'Upstream'} request timed out") from None
        except httpx.HTTPError as exc:
            raise apierr.upstream_error(f"{cfg.get('provider_name') or 'Upstream'} connection error: {type(exc).__name__}") from None
        finally:
            cache_engine.release_head(head_key, head_role, head_gate, head_marked)
            if session is not None and getattr(session, "_active_direct_abort", None) is abort:
                session._active_direct_abort = None

    def _events_from_non_stream_json(self, payload, bound_entry, session, scope, client_echo, canonical_msgs, tools, parent_snapshot):
        choices = payload.get("choices") if isinstance(payload, dict) else None
        msg = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        msg = msg if isinstance(msg, dict) else {}
        reply_blocks: list[dict] = []

        r_text = msg.get("reasoning_content") or msg.get("reasoning")
        if isinstance(r_text, str) and r_text:
            yield {"type": "thinking_start"}
            yield {"type": "thinking", "delta": r_text}
            reply_blocks.append({"type": "thinking", "thinking": r_text, "signature": "unsigned:reasoning_content"})

        text = msg.get("content")
        if isinstance(text, str) and text:
            yield {"type": "text", "delta": text}
            reply_blocks.append({"type": "text", "text": text})

        tcs = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
        for idx, tc in enumerate(tcs):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            cid = str(tc.get("id") or f"call_{idx}")
            cname = str(fn.get("name") or "")
            raw_args = str(fn.get("arguments") or "{}")
            if not cname:
                continue
            _, valid_obj = sse_utils.strict_tool_arguments(raw_args)
            ev = {"type": "tool_call", "id": cid, "name": cname, "arguments": raw_args}
            if not valid_obj:
                ev["invalid"] = True
            else:
                blk = {"type": "tool_call", "toolCallId": cid, "toolName": cname, "toolInput": native_history_loads_safe(raw_args)}
                if isinstance(tc.get("extra_content"), dict):
                    blk["extra_content"] = copy.deepcopy(tc["extra_content"])
                reply_blocks.append(blk)
            yield ev

        if isinstance(payload.get("usage"), dict):
            yield {"type": "usage", "usage": normalize_chat_usage(payload["usage"], bound_entry)}
        fr = choices[0].get("finish_reason") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        reason = _FINISH_MAP.get(fr, "tool_calls" if tcs else "stop")
        if reason in ("stop", "tool_calls"):
            cache_engine.record_canonical_reply(session, scope, client_echo, canonical_msgs, reply_blocks, tools, parent_snapshot)
        yield {"type": "end", "reason": reason}


def native_history_loads_safe(raw_args: str) -> dict:
    import native_history

    val = native_history.loads_finite(raw_args or "{}")
    return val if isinstance(val, dict) else {}


import httpx  # noqa: E402

register_driver(OpenAIChatDriver())
