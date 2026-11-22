"""Universal prompt-cache stabilizer, 4-breakpoint `cache_control`, `HeadCoalescer`, and `native_history` coordinator."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import time

import apierr
import conversation_state
import forced_choice
import journal
import native_history
import summary_guard

BILLING_PREFIX = "x-anthropic-billing-header:"
HEAD_WARM_1H = 3300.0
HEAD_MIN_CHARS = 3500


def strip_billing_header(head: str | None) -> str:
    """Strip per-conversation `x-anthropic-billing-header: ... cch=...` so subagents share one cached system prefix."""
    text = head or ""
    first, sep, rest = text.partition("\n")
    if first.startswith(BILLING_PREFIX) and sep:
        return rest
    return text


def cache_control_spec(cfg: dict, driver_id: str, *, is_openrouter: bool = False) -> dict | None:
    """Resolve `cache_control` object (`{'type': 'ephemeral', ...}`) from provider `cache_policy`, or `None` if disabled."""
    if not cfg.get("direct_cache", True):
        return None
    policy = str(cfg.get("cache_policy") or "auto").lower()
    if policy == "off":
        return None
    if policy == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    if policy == "5m":
        return {"type": "ephemeral"}
    # "auto": on direct Anthropic API, 5m ephemeral is standard (1h costs 2x write); on OpenRouter/custom respect 5m ephemeral
    return {"type": "ephemeral"}


def _mark_chat_message(message: dict, mark_spec: dict) -> bool:
    """Attach `cache_control` to the last text block of an OpenAI-chat-shaped message."""
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return False
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list) or not content:
        return False
    for idx in range(len(content) - 1, -1, -1):
        part = content[idx]
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            updated = list(content)
            updated[idx] = {**part, "cache_control": copy.deepcopy(mark_spec)}
            message["content"] = updated
            return True
    return False


def apply_chat_cache_breakpoints(messages: list[dict], mark_spec: dict | None, *, mark_tail: bool = True) -> None:
    """Place up to 4 explicit `cache_control` breakpoints on OpenRouter (`anthropic/*`, `google/*`) chat messages."""
    if not mark_spec or not messages:
        return
    marks = 0
    if messages[0].get("role") in ("system", "developer") and _mark_chat_message(messages[0], mark_spec):
        marks += 1

    user_or_tool = [i for i, m in enumerate(messages) if m.get("role") in ("user", "tool")]
    wanted: list[int] = []
    if user_or_tool:
        if mark_tail:
            wanted.append(user_or_tool[-1])
        elif len(user_or_tool) > 1:
            wanted.append(user_or_tool[-2])
        latest_assistant = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=None)
        if latest_assistant is not None:
            before = [i for i in user_or_tool if i < latest_assistant]
            if before and before[-1] not in wanted:
                wanted.append(before[-1])

    lowest = 1 if messages[0].get("role") in ("system", "developer") else 0
    for idx in wanted:
        if marks >= 4:
            break
        for candidate in range(idx, lowest - 1, -1):
            if _mark_chat_message(messages[candidate], mark_spec):
                marks += 1
                break


def _mark_anthropic_turn(turn: dict, mark_spec: dict) -> bool:
    blocks = turn.get("content")
    if not isinstance(blocks, list) or not blocks:
        return False
    for idx in range(len(blocks) - 1, -1, -1):
        blk = blocks[idx]
        if not isinstance(blk, dict):
            continue
        btype = blk.get("type")
        if btype in ("text", "tool_result", "tool_use", "image", "document") and "thinking" not in str(btype):
            if btype == "text" and not blk.get("text"):
                continue
            blk["cache_control"] = copy.deepcopy(mark_spec)
            return True
    return False


def apply_anthropic_cache_breakpoints(
    system_blocks: list[dict],
    tools_list: list[dict],
    turns: list[dict],
    mark_spec: dict | None,
    *,
    mark_tail: bool = True,
) -> None:
    """Place up to 4 Anthropic `cache_control` breakpoints (`tools[-1]`, `system[-1]`, rolling `before[-1]`, `tail`)."""
    if not mark_spec:
        return
    marks = 0
    if tools_list:
        tools_list[-1]["cache_control"] = copy.deepcopy(mark_spec)
        marks += 1
    if system_blocks:
        for idx in range(len(system_blocks) - 1, -1, -1):
            if system_blocks[idx].get("text"):
                system_blocks[idx]["cache_control"] = copy.deepcopy(mark_spec)
                marks += 1
                break

    user_indices = [i for i, t in enumerate(turns) if t.get("role") == "user"]
    wanted: list[int] = []
    if user_indices:
        if mark_tail:
            wanted.append(user_indices[-1])
        elif len(user_indices) > 1:
            wanted.append(user_indices[-2])
        latest_assistant = max((i for i, t in enumerate(turns) if t.get("role") == "assistant"), default=None)
        if latest_assistant is not None:
            before = [i for i in user_indices if i < latest_assistant]
            if before and before[-1] not in wanted:
                wanted.append(before[-1])

    for idx in wanted:
        if marks >= 4:
            break
        for candidate in range(idx, -1, -1):
            if turns[candidate].get("role") == "user" and _mark_anthropic_turn(turns[candidate], mark_spec):
                marks += 1
                break


def apply_openai_cache_hints(
    body: dict,
    cfg: dict,
    session,
    model: str,
    provider_options: dict | None = None,
    *,
    include_retention: bool = False,
    head: str | None = None,
    tools: list | None = None,
) -> None:
    """Attach deterministic `prompt_cache_key` (and optional `prompt_cache_retention`) for OpenAI / OpenRouter."""
    if not cfg.get("direct_cache", True) or str(cfg.get("cache_policy") or "auto").lower() == "off":
        return
    opts = provider_options or {}
    preset = str(cfg.get("preset") or "").lower()
    if preset in ("xai", "deepseek"):
        return
    low_model = (model or "").lower()

    supports_cache_key = preset in ("openai", "openai-responses", "openrouter") or low_model.startswith(
        ("gpt-", "o1", "o3", "o4", "openai/", "anthropic/", "google/")
    )
    if not supports_cache_key:
        return

    conv_key = getattr(session, "_native_conversation_key", None) if session is not None else None
    if conv_key:
        if head is not None:
            head_part = head
        elif body.get("instructions") is not None:
            head_part = body.get("instructions")
        else:
            msgs = body.get("messages") or []
            head_part = (
                msgs[0].get("content")
                if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") in ("system", "developer")
                else None
            )
        tools_part = tools if tools is not None else body.get("tools")
        seed = [cfg.get("provider_id") or preset, model, conv_key, head_part, tools_part]
        # An administrator's hint can select a group within a conversation, but never share the key between chats.
        explicit_key = opts.get("prompt_cache_key")
        if isinstance(explicit_key, str) and explicit_key.strip():
            seed.append(explicit_key.strip()[:64])
        digest = hashlib.sha256(json.dumps(seed, sort_keys=True, ensure_ascii=False, default=str).encode(
            "utf-8", "surrogatepass")).hexdigest()[:32]
        body["prompt_cache_key"] = f"proxi-{digest}"

    if include_retention and preset in ("openai", "openai-responses"):
        ret = opts.get("prompt_cache_retention")
        if ret in ("in_memory", "24h"):
            body["prompt_cache_retention"] = ret
        elif str(cfg.get("cache_policy") or "").lower() == "1h":
            body["prompt_cache_retention"] = "24h"


def claim_universal_head(
    cfg: dict,
    session,
    driver_id: str,
    model: str,
    effort: str | None,
    tools: list | None,
    head_text: str,
) -> tuple[str | None, str | None, object | None]:
    """Coalesce parallel cold-prefix writes across subagents sharing the same `(provider, model, effort, tools, head)`."""
    if not cfg.get("direct_cache", True) or str(cfg.get("cache_policy") or "auto").lower() == "off":
        return None, None, None
    size = len(head_text or "") + (len(json.dumps(tools, ensure_ascii=False, default=str)) if tools else 0)
    if size < HEAD_MIN_CHARS:
        return None, None, None
    import direct

    provider_scope = cfg.get("provider_id") or cfg.get("base") or driver_id
    payload = json.dumps([driver_id, provider_scope, model, effort, tools, head_text], sort_keys=True, ensure_ascii=False, default=str)
    head_key = hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()
    warm = HEAD_WARM_1H if str(cfg.get("cache_policy") or "").lower() == "1h" else None
    role, gate = direct._claim_head(head_key, time.monotonic(), id(session), warm=warm)
    return head_key, role, gate


async def wait_for_universal_head(head_gate, model: str, driver_id: str) -> None:
    if head_gate is None:
        return
    import direct

    started = time.monotonic()
    outcome = await direct._wait_for_head(head_gate)
    journal.emit(
        "cache.head_wait",
        "Запрос дождался прогрева общей системной шапки соседним под-агентом",
        "info",
        outcome=outcome,
        model=model,
        upstream=driver_id,
        waited_ms=round((time.monotonic() - started) * 1000),
    )


def mark_head_started(head_key: str | None, head_gate) -> None:
    if head_key and head_gate is not None:
        import direct

        direct._head_started(head_gate, head_key)


def release_head(head_key: str | None, head_role: str | None, head_gate, head_marked: bool) -> None:
    if head_role == "lead" and not head_marked and head_key and head_gate is not None:
        import direct

        direct._head_released(head_key, head_gate)


def build_scope(
    session,
    driver_id: str,
    cfg: dict,
    model: str,
    effort: str | None,
    head: str,
    tools: list | None,
    tool_choice,
) -> str:
    """Build deterministic scope string matching `native_history._SCOPE_PARTS` (`conversation, provider, head, tools, tool_choice`).

    Note: On universal APIs (`openai-chat`, `openai-responses`, `anthropic-messages`), `tool_choice` is a per-turn
    generation parameter (`required` on Turn 1 -> `auto` on Turn 2) that never alters the cached `system`/`tools`
    prefix, so the scope records `"tools_enabled"` vs `"none"` rather than splitting history on `required` -> `auto`.
    """
    conv = getattr(session, "_native_conversation_key", None) if session is not None else None
    provider_meta = {
        "driver": driver_id,
        "provider_id": cfg.get("provider_id") or "",
        "model": model,
        "effort": effort or "",
    }
    tool_names = [
        (t.get("function") or t).get("name")
        for t in (tools or [])
        if isinstance(t, dict)
    ]
    choice_scope = "none" if (not tool_names or tool_choice == "none") else "enabled"
    return json.dumps([conv, provider_meta, head or "", tool_names, choice_scope], sort_keys=True, ensure_ascii=False, default=str)


def prepare_canonical_history(
    session,
    scope: str,
    tools: list | None,
    chunks: list | None,
    prompt: str | None,
) -> tuple[object | None, list[dict], list[dict] | None]:
    """Resolve `(resumed_snapshot, canonical_messages, client_echo_messages)` for universal drivers.

    If `session._native_history` matches `scope` and `client_messages`, returns the resumed branch WITH all stored
    provider reasoning blocks/signatures (`thinking`+`signature`, `redacted_thinking`, `reasoning_details`,
    `encrypted_content`, `thought_signature`). On cross-provider failover (`scope` mismatch), cleanly falls back
    to unsigned canonical messages!
    """
    if session is None:
        return None, _fallback_canonical(chunks, prompt), None

    client = getattr(session, "_native_client_messages", None)
    history = getattr(session, "_native_history", None)
    if history is None:
        history = session._native_history = native_history.History()

    if isinstance(client, list) and client:
        session._native_tool_defaults = native_history.tool_defaults(tools)
        snapshot, resumed = conversation_state.resume(session, history, scope, client)
        if resumed is not None:
            return snapshot, copy.deepcopy(resumed), client
        fork_snap, forked = history.resume_fork(scope, client)
        if forked is not None:
            return fork_snap, copy.deepcopy(forked), None  # forks are not recorded back over the main branch
        if getattr(session, "_native_allow_cold_replay", True):
            unsigned = native_history.unsigned_tool_transcript(client, without_results=True)
            if unsigned is not None:
                return None, unsigned, client
            return None, copy.deepcopy(client), client

    return None, _fallback_canonical(chunks, prompt), None


def _fallback_canonical(chunks: list | None, prompt: str | None) -> list[dict]:
    msgs: list[dict] = []
    if chunks:
        for part in chunks:
            if isinstance(part, dict) and part.get("role"):
                msgs.append(copy.deepcopy(part))
            elif isinstance(part, list):
                msgs.append({"role": "user", "content": copy.deepcopy(part)})
            else:
                msgs.append({"role": "user", "content": [{"type": "text", "text": str(part)}]})
    elif prompt:
        msgs.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
    return msgs


def _identity_coerce(raw_json: str, _param_types) -> str:
    return raw_json


def record_canonical_reply(
    session,
    scope: str,
    client_messages: list[dict] | None,
    upstream_messages: list[dict],
    reply_blocks: list[dict],
    tools: list | None,
    parent_snapshot,
) -> None:
    """Record a completed assistant reply (including signatures/reasoning blocks) into `session._native_history`."""
    if session is None or client_messages is None or not reply_blocks:
        return
    visible = [b for b in reply_blocks if b.get("type") in ("text", "tool_call")]
    if not visible:
        return
    history = getattr(session, "_native_history", None)
    if history is None:
        history = session._native_history = native_history.History()

    # Build client-echo blocks stripped of provider-only fields (like Gemini `extra_content.google.thought_signature`),
    # while preserving `extra_content` inside `reply_blocks` (`snapshot.assistant`) for upstream replay!
    echo_source = [
        {"type": "tool_call", "toolCallId": b["toolCallId"], "toolName": b["toolName"], "toolInput": b["toolInput"]}
        if b.get("type") == "tool_call"
        else b
        for b in reply_blocks
        if b.get("type") in ("text", "tool_call")
    ]
    expected = native_history.echo(echo_source, {}, _identity_coerce)
    declared_names = {
        (t.get("function") or t).get("name")
        for t in (tools or [])
        if isinstance(t, dict) and (t.get("function") or t).get("name")
    }
    pruned = False
    if declared_names:
        kept_blocks = [b for b in reply_blocks if b.get("type") != "tool_call" or b.get("toolName") in declared_names]
        if not any(b.get("type") in ("text", "tool_call") for b in kept_blocks):
            return
        if len(kept_blocks) != len(reply_blocks):
            reply_blocks = kept_blocks
            kept_echo_src = [b for b in echo_source if b.get("type") != "tool_call" or b.get("toolName") in declared_names]
            expected = native_history.echo(kept_echo_src, {}, _identity_coerce)
            pruned = True

    session._direct_reply = (reply_blocks, expected)
    history.record(
        scope,
        client_messages,
        copy.deepcopy(upstream_messages),
        copy.deepcopy(reply_blocks),
        expected,
        parent=parent_snapshot,
        pruned=pruned,
    )


async def run_guarded_turn(
    cfg: dict,
    session,
    model_provider: dict,
    tools: list | None,
    tool_choice,
    bound_entry: dict,
    single_attempt_fn,
):
    """Wrap a driver's single-attempt generator with `summary_guard.Gate` and `forced_choice.Gate`."""
    driver_id = str(cfg.get("driver") or cfg.get("upstream") or "openai-chat")

    # 1. Codex compaction continuity guard (`_summary_tools`: tools kept for prefix cache, reply must be text summary)
    if getattr(session, "_summary_tools", False):
        for attempt in range(1, summary_guard.TRIES_WITH_TOOLS + 2):
            fallback = attempt > summary_guard.TRIES_WITH_TOOLS
            gate = summary_guard.Gate()
            end_event = None
            instruction = summary_guard.INSTRUCTION if attempt == 2 else None
            eff_tools = None if fallback else tools
            eff_choice = None if fallback else tool_choice
            async with contextlib.aclosing(
                single_attempt_fn(eff_tools, eff_choice, instruction=instruction, text_only=fallback)
            ) as events:
                async for event in events:
                    if event.get("type") == "end":
                        end_event = event
                        continue
                    for ready in gate.feed(event):
                        yield ready
            if end_event is None:
                return
            if not gate.called or end_event.get("reason") == "cancelled" or fallback:
                for ready in gate.release():
                    yield ready
                yield end_event
                return
            journal.emit(
                "turn.compaction_tool_call",
                "Модель вызвала инструмент вместо сводки сжатия: ответ отброшен и повторён",
                "warn",
                model=model_provider.get("model"),
                called=gate.called,
                wrote_text=gate.text,
                attempt=attempt,
                tools_kept=attempt < summary_guard.TRIES_WITH_TOOLS,
                upstream=driver_id,
            )
        return

    # 2. Forced tool-choice gate when thinking + forced tool_choice collide (e.g. Anthropic Messages API)
    raw_effort = (
        model_provider.get("reasoningEffort")
        or model_provider.get("reasoning_effort")
        or model_provider.get("effort")
    )
    eff = None if raw_effort == "auto" else raw_effort
    provider_opts = getattr(session, "_provider_options", None) or {}
    raw_thinking_opt = provider_opts.get("thinking") if isinstance(provider_opts.get("thinking"), dict) else {}
    t_type = str(raw_thinking_opt.get("type") or "").lower()
    supports_reasoning = bool(bound_entry.get("reasoning", True))
    effort_active = supports_reasoning and eff != "none" and t_type != "disabled" and bool(
        eff
        or raw_thinking_opt.get("budget_tokens")
        or t_type in ("enabled", "adaptive")
        or (raw_thinking_opt and bound_entry.get("reasoning", False))
    )
    needs_word_forced_choice = not bound_entry.get("forced_tool_choice", True) and (
        driver_id != "anthropic-messages"
        or effort_active
        or (bound_entry.get("reasoning", False) and eff != "none" and t_type != "disabled")
    )
    required = (
        forced_choice.requirement(model_provider.get("model"), tools, tool_choice, entry=bound_entry)
        if needs_word_forced_choice
        else None
    )
    if required is None:
        if session is not None:
            session._pending_history_commit = None
        async with contextlib.aclosing(single_attempt_fn(tools, tool_choice, instruction=None, text_only=False)) as events:
            async for event in events:
                yield event
        commit_fn = getattr(session, "_pending_history_commit", None) if session is not None else None
        if callable(commit_fn):
            session._pending_history_commit = None
            commit_fn()
        return

    for attempt in (1, 2):
        if session is not None:
            session._pending_history_commit = None
        gate = forced_choice.Gate(required)
        end_event = None
        instr = forced_choice.instruction(required, firm=attempt > 1)
        async with contextlib.aclosing(
            single_attempt_fn(tools, "auto", instruction=instr, text_only=False)
        ) as events:
            async for event in events:
                if event.get("type") == "end":
                    end_event = event
                    continue
                for ready in gate.feed(event):
                    yield ready
        if end_event is None:
            return
        reason = end_event.get("reason")
        terminal = reason == "content_filter" or (reason == "length" and not gate.text and not gate.called)
        if gate.complied or reason == "cancelled" or terminal:
            if gate.complied and session is not None and callable(getattr(session, "_pending_history_commit", None)):
                commit_fn = session._pending_history_commit
                session._pending_history_commit = None
                commit_fn()
            elif session is not None:
                session._pending_history_commit = None
            if terminal and not gate.complied:
                for held in gate.held:
                    if held.get("type") != "tool_call":
                        yield held
            yield end_event
            return
        journal.emit(
            "turn.forced_choice_missed",
            "Модель не вызвала требуемый инструмент" + (": повтор с усиленной просьбой" if attempt == 1 else ""),
            "warn",
            model=model_provider.get("model"),
            tool=required.named,
            called=gate.called,
            wrote_text=gate.text,
            attempt=attempt,
        )
    raise apierr.ApiError(502, "upstream_tool_choice_ignored", "upstream did not honor the required client tool choice")
