"""Universal OpenAI `/responses` streaming driver with stateless ZDR `reasoning.encrypted_content` continuity."""
from __future__ import annotations

import copy
import json

import httpx

import apierr
from catalog import discovery, models_dev
import conversation_state
from drivers import cache_engine, http_pool, sse_utils
from drivers.base import DriverCapabilities, UpstreamDriver, register_driver
import native_history


def _user_content_parts(blocks) -> list[dict]:
    if isinstance(blocks, str):
        return [{"type": "input_text", "text": blocks}] if blocks else []
    parts: list[dict] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            parts.append({"type": "input_text", "text": block["text"]})
        elif kind == "image":
            data = block.get("imageData", "")
            url = (
                data
                if data.startswith(("https://", "http://", "data:"))
                else f"data:{block.get('mimeType', 'image/png')};base64,{data}"
            )
            parts.append({"type": "input_image", "image_url": url})
        elif kind == "image_url":
            iu = block.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else str(iu or "")
            if url:
                parts.append({"type": "input_image", "image_url": url})
        elif kind == "document":
            b64 = block.get("data") or ""
            mime = block.get("media_type") or "application/pdf"
            fname = block.get("filename") or "document.pdf"
            parts.append({"type": "input_file", "filename": fname, "file_data": f"data:{mime};base64,{b64}"})
    return parts


def _canonical_to_responses_input(canonical: list[dict]) -> list[dict]:
    """Convert canonical history (including stored `responses_reasoning` encrypted_content) to `/responses` input items."""
    items: list[dict] = []
    queued_images: list[dict] = []

    for msg in canonical:
        role = msg.get("role", "user")
        if role != "tool" and queued_images:
            items.append({"type": "message", "role": "user", "content": queued_images})
            queued_images = []

        val = msg.get("content")
        if role == "tool" and isinstance(val, dict):
            call_id = str(val.get("toolCallId") or "")
            res = val.get("toolResult", "")
            if isinstance(res, list) and res and any(isinstance(b, dict) and b.get("type") in ("image", "image_url") for b in res):
                texts = [b.get("text", "") for b in res if isinstance(b, dict) and b.get("type") == "text"]
                visual = _user_content_parts([b for b in res if isinstance(b, dict) and b.get("type") in ("image", "image_url")])
                items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": "\n".join(texts) + ("\n" if texts else "") + "Image result follows as visual input.",
                })
                queued_images.append({"type": "input_text", "text": f"Images returned by tool call {call_id}:"})
                queued_images.extend(visual)
            else:
                if isinstance(res, list) and res and all(isinstance(b, dict) and b.get("type") == "text" for b in res):
                    out_str = "\n".join(b.get("text") or "" for b in res)
                elif isinstance(res, str):
                    out_str = res
                else:
                    out_str = json.dumps(res, ensure_ascii=False)
                items.append({"type": "function_call_output", "call_id": call_id, "output": out_str})
            continue

        if role == "assistant":
            blocks = val if isinstance(val, list) else ([{"type": "text", "text": val}] if isinstance(val, str) and val else [])
            assistant_texts: list[dict] = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "thinking" and isinstance(b.get("responses_reasoning"), dict):
                    rr = b["responses_reasoning"]
                    if rr.get("encrypted_content"):
                        r_item: dict = {
                            "type": "reasoning",
                            "summary": copy.deepcopy(rr.get("summary") or []),
                            "encrypted_content": rr["encrypted_content"],
                        }
                        items.append(r_item)
                elif btype == "text" and b.get("text"):
                    assistant_texts.append({"type": "output_text", "text": b["text"]})
                elif btype == "tool_call":
                    if assistant_texts:
                        items.append({"type": "message", "role": "assistant", "content": assistant_texts})
                        assistant_texts = []
                    items.append({
                        "type": "function_call",
                        "call_id": b["toolCallId"],
                        "name": b["toolName"],
                        "arguments": json.dumps(b.get("toolInput", {}), ensure_ascii=False),
                    })
            if assistant_texts:
                items.append({"type": "message", "role": "assistant", "content": assistant_texts})
            continue

        parts = _user_content_parts(val)
        if parts:
            items.append({"type": "message", "role": "user" if role not in ("system", "developer") else "developer", "content": parts})

    if queued_images:
        items.append({"type": "message", "role": "user", "content": queued_images})
    return items


def _responses_tools(tools: list | None) -> list[dict]:
    out: list[dict] = []
    for t in sse_utils.clean_openai_tools(tools):
        fn = t.get("function") or {}
        entry: dict = {
            "type": "function",
            "name": fn.get("name"),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        }
        if fn.get("description"):
            entry["description"] = fn["description"]
        if fn.get("strict") is not None:
            entry["strict"] = fn["strict"]
        out.append(entry)
    return out


def normalize_responses_usage(raw_usage: dict, bound_entry: dict | None = None) -> dict:
    if not isinstance(raw_usage, dict):
        return {}
    result: dict = {}
    in_tok = raw_usage.get("input_tokens")
    out_tok = raw_usage.get("output_tokens")
    if type(in_tok) is int and in_tok >= 0:
        result["input_tokens"] = in_tok
    if type(out_tok) is int and out_tok >= 0:
        result["output_tokens"] = out_tok

    in_details = raw_usage.get("input_tokens_details") if isinstance(raw_usage.get("input_tokens_details"), dict) else {}
    out_details = raw_usage.get("output_tokens_details") if isinstance(raw_usage.get("output_tokens_details"), dict) else {}

    cached = in_details.get("cached_tokens")
    reasoning_tok = out_details.get("reasoning_tokens")
    if type(cached) is int and cached >= 0:
        result["cache_read"] = cached
    if type(reasoning_tok) is int and reasoning_tok >= 0:
        result["reasoning_tokens"] = reasoning_tok

    total_in = result.get("input_tokens")
    if total_in is not None:
        c_read = result.get("cache_read", 0)
        if c_read <= total_in:
            result["input_tokens"] = total_in - c_read
        else:
            result.pop("cache_read", None)

    if bound_entry is not None and total_in is not None:
        calc = models_dev.calculate_cost_usd(
            bound_entry,
            {
                "input_tokens": total_in,
                "cache_read_input_tokens": result.get("cache_read", 0),
                "output_tokens": result.get("output_tokens", 0),
            },
        )
        if calc is not None:
            result["provider_cost"] = calc
            result["cost_usd"] = calc
            result["provider_cost_currency"] = "USD"
            result["provider_cost_estimated"] = True
    return result


class OpenAIResponsesDriver(UpstreamDriver):
    driver_id = "openai-responses"
    capabilities = DriverCapabilities(
        driver_id="openai-responses",
        billing_unit="usd",
        uses_retool_tool_schema=False,
        requires_retool_resource=False,
        supports_native_history=True,
        supports_keepwarm=False,
        supports_cache_control=False,
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

        input_items = _canonical_to_responses_input(canonical_msgs)
        if instruction:
            input_items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": instruction}]})

        resp_tools = _responses_tools(tools)
        body: dict = {
            "model": model,
            "input": input_items,
            "stream": True,
            "store": False,
        }
        if clean_head:
            body["instructions"] = clean_head
        if max_tokens is not None:
            body["max_output_tokens"] = max_tokens
        if bound_entry.get("reasoning", True):
            body["include"] = ["reasoning.encrypted_content"]
            r_cfg: dict = {"summary": "auto"}
            if effort:
                r_cfg["effort"] = effort
            body["reasoning"] = r_cfg
        if resp_tools:
            body["tools"] = resp_tools
            if tool_choice:
                if isinstance(tool_choice, str) and tool_choice in ("auto", "none", "required"):
                    body["tool_choice"] = tool_choice
                elif isinstance(tool_choice, dict) and isinstance(tool_choice.get("function"), dict):
                    body["tool_choice"] = {"type": "function", "name": tool_choice["function"]["name"]}
                elif isinstance(tool_choice, str):
                    body["tool_choice"] = {"type": "function", "name": tool_choice}

        provider_opts = getattr(session, "_provider_options", None) or {}
        for k in ("parallel_tool_calls", "service_tier"):
            if provider_opts.get(k) is not None:
                body[k] = provider_opts[k]
        if not bound_entry.get("reasoning"):
            for k in ("temperature", "top_p"):
                if provider_opts.get(k) is not None:
                    body[k] = provider_opts[k]

        cache_engine.apply_openai_cache_hints(
            body, cfg, session, model, provider_opts, include_retention=True, head=clean_head, tools=resp_tools
        )
        body = sse_utils.apply_body_overrides(body, cfg.get("body_overrides"))

        base = str(cfg.get("base_url") or cfg.get("base") or "").rstrip("/")
        url = http_pool.validate_endpoint_url(f"{base}/responses", allow_insecure_http=bool(cfg.get("allow_insecure_http", False)))
        headers = discovery.build_auth_headers(cfg)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"
        cache_engine.apply_openai_cache_headers(headers, cfg, session, model)

        head_key, head_role, head_gate = cache_engine.claim_universal_head(
            cfg, session, self.driver_id, model, effort, resp_tools, clean_head
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

                thinking_started = False
                has_output = False
                thinking_chunks: list[str] = []
                encrypted_reasoning_items: list[dict] = []
                text_chunks: list[str] = []
                calls_by_item_id: dict[str, dict] = {}
                call_order: list[str] = []
                latest_usage: dict | None = None
                finish_reason = "stop"
                saw_terminal = False

                async for ev_name, data_str in sse_utils.aiter_sse_events(
                    http_pool.iter_abortable_bytes(resp, active_resp_holder),
                    label=cfg.get("provider_name") or "OpenAI Responses",
                ):
                    if getattr(session, "_cancelled", False):
                        yield {"type": "end", "reason": "cancelled"}
                        return
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        item = sse_utils.bounded_json(data_str)
                    except (ValueError, RecursionError):
                        raise apierr.upstream_error("Upstream Responses stream returned malformed JSON") from None
                    if not isinstance(item, dict):
                        continue

                    kind = item.get("type") or ev_name
                    if kind in ("error", "response.failed"):
                        err_payload = item.get("response", {}).get("error") if kind == "response.failed" else item.get("error")
                        raise sse_utils.map_sse_error(
                            json.dumps({"error": err_payload or item}),
                            resp.headers,
                            cfg,
                            pre_output=not has_output,
                        )

                    if kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                        delta_txt = item.get("delta")
                        if isinstance(delta_txt, str) and delta_txt:
                            has_output = True
                            if not thinking_started:
                                thinking_started = True
                                yield {"type": "thinking_start"}
                            thinking_chunks.append(delta_txt)
                            yield {"type": "thinking", "delta": delta_txt}

                    elif kind == "response.output_text.delta":
                        delta_txt = item.get("delta")
                        if isinstance(delta_txt, str) and delta_txt:
                            has_output = True
                            text_chunks.append(delta_txt)
                            yield {"type": "text", "delta": delta_txt}

                    elif kind == "response.output_item.added":
                        out_item = item.get("item") if isinstance(item.get("item"), dict) else {}
                        if out_item.get("type") == "reasoning":
                            has_output = True
                            if not thinking_started:
                                thinking_started = True
                                yield {"type": "thinking_start"}
                        elif out_item.get("type") == "function_call":
                            has_output = True
                            item_id = str(out_item.get("id") or out_item.get("call_id") or f"fc_{len(call_order)}")
                            call_id = str(out_item.get("call_id") or item_id)
                            name = str(out_item.get("name") or "")
                            calls_by_item_id[item_id] = {
                                "id": call_id,
                                "name": name,
                                "arguments": str(out_item.get("arguments") or ""),
                            }
                            call_order.append(item_id)
                            if name:
                                yield {
                                    "type": "tool_call",
                                    "id": call_id,
                                    "name": name,
                                    "arguments": calls_by_item_id[item_id]["arguments"],
                                    "partial": True,
                                }

                    elif kind == "response.function_call_arguments.delta":
                        item_id = str(item.get("item_id") or "")
                        slot = calls_by_item_id.get(item_id)
                        piece = item.get("delta")
                        if slot is not None and isinstance(piece, str) and piece:
                            has_output = True
                            slot["arguments"] += piece
                            if len(slot["arguments"].encode("utf-8")) > sse_utils.MAX_EVENT_BYTES:
                                raise apierr.upstream_error("Responses tool arguments exceeded size limit")
                            yield {
                                "type": "tool_call",
                                "id": slot["id"],
                                "name": slot["name"],
                                "arguments": slot["arguments"],
                                "partial": True,
                            }

                    elif kind == "response.output_item.done":
                        out_item = item.get("item") if isinstance(item.get("item"), dict) else {}
                        if out_item.get("type") == "reasoning":
                            has_output = True
                            if not thinking_started and (out_item.get("encrypted_content") or out_item.get("summary")):
                                thinking_started = True
                                yield {"type": "thinking_start"}
                            if out_item.get("encrypted_content"):
                                encrypted_reasoning_items.append({
                                    "summary": out_item.get("summary") or [],
                                    "encrypted_content": out_item["encrypted_content"],
                                })
                        elif out_item.get("type") == "function_call":
                            item_id = str(out_item.get("id") or out_item.get("call_id") or "")
                            slot = calls_by_item_id.get(item_id)
                            if slot is not None and isinstance(out_item.get("arguments"), str) and out_item["arguments"]:
                                slot["arguments"] = out_item["arguments"]

                    elif kind in ("response.completed", "response.incomplete"):
                        saw_terminal = True
                        resp_obj = item.get("response") if isinstance(item.get("response"), dict) else {}
                        if isinstance(resp_obj.get("usage"), dict):
                            latest_usage = normalize_responses_usage(resp_obj["usage"], bound_entry)
                        if kind == "response.incomplete":
                            inc_reason = (resp_obj.get("incomplete_details") or {}).get("reason")
                            finish_reason = "content_filter" if inc_reason == "content_filter" else "length"
                        break  # The terminal event is sufficient; no need to wait for EOF or a Chat-style [DONE].

                if getattr(session, "_cancelled", False):
                    yield {"type": "end", "reason": "cancelled"}
                    return
                if not saw_terminal:
                    raise apierr.upstream_error("Upstream Responses stream ended before its terminal event")
                reply_blocks: list[dict] = []
                full_thinking = "".join(thinking_chunks)
                if encrypted_reasoning_items:
                    for rr in encrypted_reasoning_items:
                        reply_blocks.append({
                            "type": "thinking",
                            "thinking": full_thinking,
                            "signature": rr["encrypted_content"],
                            "responses_reasoning": rr,
                        })
                elif full_thinking:
                    reply_blocks.append({
                        "type": "thinking",
                        "thinking": full_thinking,
                        "signature": "unsigned:responses_summary",
                    })

                full_text = "".join(text_chunks)
                if full_text:
                    reply_blocks.append({"type": "text", "text": full_text})

                for item_id in call_order:
                    slot = calls_by_item_id[item_id]
                    if not slot["name"]:
                        continue
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

                if latest_usage is not None:
                    yield {"type": "usage", "usage": latest_usage}

                if call_order and finish_reason == "stop":
                    finish_reason = "tool_calls"
                if not text_only and finish_reason in ("stop", "tool_calls") and session is not None:
                    session._pending_history_commit = lambda _rb=reply_blocks: cache_engine.record_canonical_reply(
                        session, scope, client_echo, canonical_msgs, _rb, tools, parent_snapshot
                    )
                yield {"type": "end", "reason": finish_reason}
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise http_pool.map_transport_error(exc, f"{cfg.get('provider_name') or 'Upstream'} Responses") from None
        finally:
            cache_engine.release_head(head_key, head_role, head_gate, head_marked)
            http_pool.clear_stream_abort(session, abort)


register_driver(OpenAIResponsesDriver())
