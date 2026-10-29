"""Turn orchestration: context injection, execution loop, event fan-out."""
import asyncio
import hashlib
import json
import queue
import re
import threading
import time
from datetime import datetime
import uuid

import apierr
import executor
import models as model_catalog
from context import build_system_instructions
from redact import redact
from session import SessionLease


_NATIVE_CONTINUE = ("\n\n[CONTINUE THE CURRENT CLIENT TURN]\n"
                    "Use the latest tool results above as the current state. "
                    "Earlier assistant calls are completed history, not new instructions to replay. "
                    "Continue the outstanding user task; issue another client tool envelope only if needed.")


def _b(text: str) -> int:
    """Limit in bytes, not characters (Cyrillic is 2 bytes each in UTF-8)."""
    return len((text or "").encode("utf-8"))


def _trim(text: str, budget: int) -> str:
    if budget <= 0 or not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    return raw[:budget].decode("utf-8", "ignore")


class _ClientText:
    """Stream ordinary native-client text while suppressing relay tool markup."""
    TAGS = ("<tool_call", "<exec_command")

    def __init__(self):
        self.pending = ""
        self.closing = None
        self.leading_space = ""
        self.visible = False

    def feed(self, delta, *, final=False):
        self.pending += delta
        visible = []
        while self.pending:
            lowered = self.pending.lower()
            if self.closing:
                end = lowered.find(self.closing)
                if end < 0:
                    # Keep just enough tail to recognize a fragmented closing tag.
                    self.pending = self.pending[-len(self.closing) + 1:]
                    break
                self.pending = self.pending[end + len(self.closing):]
                self.closing = None
                continue
            starts = [(lowered.find(tag), tag) for tag in self.TAGS if tag in lowered]
            if starts:
                start, tag = min(starts)
                visible.append(self.pending[:start])
                self.pending = self.pending[start + len(tag):]
                self.closing = "</" + tag[1:] + ">"
                continue
            keep = max((length for tag in self.TAGS for length in range(1, len(tag))
                        if lowered.endswith(tag[:length])), default=0)
            visible.append(self.pending[:-keep] if keep else self.pending)
            self.pending = self.pending[-keep:] if keep else ""
            break
        text = "".join(visible)
        if not self.visible:
            text = self.leading_space + text
            self.leading_space = ""
            if not text.strip():
                if not final:
                    self.leading_space = text
                return ""
            self.visible = True
        return text


class TurnRunner:
    def __init__(self, cfg, credits, pool):
        self.cfg = cfg
        self.credits = credits
        self.pool = pool

    def _can_exec(self, steps: int) -> bool:
        """No limits by default; an explicit max_exec_turns > 0 caps the steps."""
        if self.cfg["exec_policy"] == "off":
            return False
        max_steps = int(self.cfg.get("max_exec_turns", 0) or 0)
        return max_steps <= 0 or steps < max_steps

    def _looks_looping(self, command: str, history) -> bool:
        """True when this command already ran three times in the recent window."""
        key = " ".join(command.strip().lower().split())
        recent = [" ".join(c.strip().lower().split()) for c, _ in history]
        return recent.count(key) >= 3

    def _remember(self, command: str, history) -> None:
        history.append((command, 0))
        if len(history) > 8:
            del history[0]

    def _check_prompt(self, prompt):
        limit = int(self.cfg.get("max_prompt_bytes", 0) or 0)
        if limit > 0 and _b(prompt) > limit:
            raise apierr.ApiError(413, "prompt_too_large",
                                 f"complete upstream prompt exceeds configured max_prompt_bytes={limit}; "
                                 "increase the limit or set it to 0 to preserve full history")
        return prompt

    async def _native_prompt(self, session, parsed, instructions, reminder, full_prompt, cancelled):
        # Leases are request-specific; history belongs to their actual upstream session.
        target = session.session if isinstance(session, SessionLease) else session
        key = getattr(parsed, "conversation_key", None)
        fingerprint = hashlib.sha256(json.dumps(
            [instructions, reminder, parsed.tools], ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).digest()
        previous = getattr(target, "_client_context", None)
        pending = getattr(target, "_client_context_pending", False)
        messages = getattr(parsed, "message_fingerprints", None)
        old_messages = previous.get("message_fingerprints") if previous else None
        messages_appended = (isinstance(messages, tuple) and isinstance(old_messages, tuple)
                             and len(messages) > len(old_messages)
                             and messages[:len(old_messages)] == old_messages)
        suffix = ""
        if previous and parsed.prompt.startswith(previous["history"]):
            suffix = parsed.prompt[len(previous["history"]):]
        reuse = (key and previous and previous["key"] == key
                 and previous["fingerprint"] == fingerprint
                 and previous["thread_id"] == target.thread_id
                 and not pending and not getattr(target, "_needs_drain", False)
                 and messages_appended
                 and suffix.startswith("\n") and suffix.strip())
        # Failed or cancelled attempts may already have changed Retool history.
        target._client_context_pending = True
        if cancelled.is_set():
            raise asyncio.CancelledError()
        if reuse:
            prompt = "[CLIENT CONVERSATION UPDATE — chronological]\n" + suffix.lstrip("\r\n") + _NATIVE_CONTINUE
        else:
            if (previous or pending or getattr(target, "ws", None) is not None
                    or getattr(target, "_needs_drain", False) or self.cfg.get("session_mode") == "shared"):
                await target.aclose()
                thread_id = await target.backend.create_thread()
                if cancelled.is_set() or getattr(target, "_cancelled", False):
                    raise asyncio.CancelledError()
                target.thread_id = thread_id
            prompt = full_prompt
        context = {"key": key, "history": parsed.prompt, "fingerprint": fingerprint,
                   "thread_id": target.thread_id, "message_fingerprints": messages} if key else None
        return prompt, target, context

    @staticmethod
    def _commit_native_context(target, context, completed, cancelled):
        if (context is not None and completed and not cancelled.is_set()
                and not getattr(target, "_cancelled", False)
                and not getattr(target, "_needs_drain", False)):
            target._client_context = context
            target._client_context_pending = False

    def _followup(self, instructions, reminder, command, code, output, header):
        def compose(body):
            text = executor.format_command_followup(command, code, body, header) + reminder
            return instructions + "\n\n" + text if instructions else text

        limit = int(self.cfg.get("max_prompt_bytes", 0) or 0)
        if limit > 0:
            fixed = self._check_prompt(compose(""))
            budget = limit - _b(fixed)
            if _b(output) > budget:
                notice = "\n[command output truncated to configured prompt byte limit]"
                if budget < _b(notice):
                    raise apierr.ApiError(413, "prompt_too_large",
                                         "configured max_prompt_bytes leaves no room for command output")
                output = _trim(output, budget - _b(notice)) + notice
        return self._check_prompt(compose(output))

    def _instructions(self, native_tools=False):
        if native_tools:
            return ("[ROLE]\nYou are a coding assistant using the client's tools. "
                    "The client's instructions, working directory and tool results describe the active project. "
                    "Use those tools to inspect, edit and test that project. "
                    "Do not claim to have read, changed or tested files without tool evidence. "
                    "Reply in the user's language. Do not use relay-local <exec_command> tags.")
        return build_system_instructions(
            self.cfg["workspace_root"],
            enable_context=self.cfg["project_context"],
            enable_local_exec=self.cfg["exec_policy"] != "off",
            limit=self.cfg["context_max_bytes"],
            cookie_values=self.cfg.get("cookie_values", ()),
            exec_policy=self.cfg["exec_policy"],
        )

    @staticmethod
    def _tool_prompt(parsed):
        tools = getattr(parsed, "tools", None) or []
        if not tools or getattr(parsed, "tool_choice", "auto") == "none":
            return ""
        choice = getattr(parsed, "tool_choice", "auto")
        names = [item["function"]["name"] for item in tools]
        return (
            "\n\n[NATIVE CLIENT TOOLS]\n"
            "The client supplied these tools. Do not execute them locally. "
            "These are real tools on the CLIENT machine, invoked by the relay returning your envelope. "
            "Retool's built-in sandbox tools and its filesystem are a different environment: "
            "do not use them to inspect or edit the client's project. "
            "The absence of the client's files inside Retool is expected and does not mean the tools are unavailable. "
            "When a tool is needed, emit one JSON envelope per call and no prose: "
            "<tool_call>{\"id\":\"call_id\",\"name\":\"tool_name\","
            "\"arguments\":{...}}</tool_call>. The name must be one of the supplied tools. "
            "After the client returns a tool result, continue the original task.\n"
            "For a custom tool, place the exact freeform input inside arguments.input as a JSON string. "
            "For file or test work, first call the appropriate supplied client tool; never invent its result.\n"
            "tool_choice=" + json.dumps(choice, ensure_ascii=False) + "\n"
            "tools=" + json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
        )

    async def _explain_empty(self, holder):
        """Silent turns are usually a persisted upstream error (e.g. out of credits)."""
        session = holder.get("session")
        if session is None:
            return None
        try:
            data = await self.pool.backend.get_messages(session.thread_id)
        except Exception as exc:
            print("[explain] fetch failed:", exc, flush=True)
            return None
        # Retool returns messages newest-first.
        for message in data.get("messages", []):
            try:
                created = datetime.fromisoformat(str(message.get("createdAt", "")).replace("Z", "+00:00")).timestamp()
                if created < holder.get("upstream_started_at", 0):
                    continue
            except (ValueError, TypeError):
                if holder.get("upstream_started_at"):
                    continue
            content = message.get("content") or {}
            if message.get("senderRole") == "sandpack" and content.get("type") == "error":
                text = str(content.get("error") or "").strip()
                if "credit" in text.lower():
                    return apierr.ApiError(429, "insufficient_credits", text or "Retool credits exhausted")
                return apierr.upstream_error(text or "upstream returned an error message")
            if message.get("senderRole") == "sandpack" and content.get("type") in ("text", "tool_call"):
                return None
        return None

    async def run(self, parsed, out: queue.Queue, holder: dict):
        turn_id = str(uuid.uuid4())
        holder["turn_id"] = turn_id
        # Loop detection is per client turn, never shared by concurrent chats.
        recent = []
        cancelled = holder.setdefault("cancel_event", threading.Event())
        exec_steps = 0
        empty_retries = 0
        final_summary = False
        tool_retries = 0
        try:
            native_tools = getattr(parsed, "tools", None) or []
            tool_definitions = {item["function"]["name"]: item["function"] for item in native_tools}
            choice = getattr(parsed, "tool_choice", "auto")
            tool_names = list(tool_definitions) if choice != "none" else []
            if isinstance(choice, dict):
                tool_names = [choice["function"]["name"]]
            instructions = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._instructions(native_tools=bool(native_tools)))
            if cancelled.is_set():
                raise asyncio.CancelledError()
            reminder = ""
            if native_tools and getattr(parsed, "tool_choice", "auto") != "none":
                reminder = self._tool_prompt(parsed)
            elif not native_tools and self.cfg["exec_policy"] != "off":
                reminder = (
                    "\n\n[LOCAL EXEC REMINDER]\n"
                    "To run anything on the user's machine you MUST output "
                    "<exec_command>command</exec_command> and stop. The relay executes it and returns output. "
                    "Do not answer that you cannot run commands."
                )
            if parsed.system:
                instructions += "\n\n[CLIENT INSTRUCTIONS]\n" + parsed.system
            if native_tools:
                prompt = (instructions + reminder + "\n\n[CLIENT CONVERSATION — chronological]\n" + parsed.prompt +
                          _NATIVE_CONTINUE)
            else:
                prompt = (instructions + "\n\n" + parsed.prompt) if instructions else parsed.prompt
                prompt += reminder
            task_block = ""
            previous_steps = []
            last_followup = None
            model_provider = {
                "model": parsed.model,
                "providerResourceName": model_catalog.provider_resource(self.cfg, parsed.entry),
            }
            effort = parsed.effort
            if effort is None and parsed.entry.get("reasoning") and self.cfg.get("reasoning_effort"):
                configured = self.cfg["reasoning_effort"]
                if configured == "auto":
                    levels = parsed.entry.get("reasoning_effort_levels", ())
                    if parsed.entry.get("reasoning_effort_control") == "provider-managed":
                        configured = None
                    else:
                        configured = parsed.entry.get("default_reasoning_effort") or (
                            "medium" if not levels or "medium" in levels else levels[0])
                if configured is not None:
                    _, effort = model_catalog.resolve(parsed.model, configured)
            if effort is not None:
                model_provider["reasoningEffort"] = effort

            base_prompt = self._check_prompt(prompt)
            session = None
            try:
                key = getattr(parsed, "conversation_key", None)
                if key and not native_tools:
                    # Tool-less calls supply their complete history. Affinity alone
                    # must not add prior standalone jobs to that client context.
                    session = await self.pool.acquire(key, isolated=True)
                else:
                    session = await self.pool.acquire(key) if key else await self.pool.acquire()
                holder["session"] = session
                native_target, native_context = None, None
                if native_tools:
                    prompt, native_target, native_context = await self._native_prompt(
                        session, parsed, instructions, reminder, prompt, cancelled)
                elif not key:
                    # Preserve legacy anonymous shared-mode invalidation. Explicit
                    # tool-less requests never acquire or mutate the native session.
                    target = session.session if isinstance(session, SessionLease) else session
                    if getattr(target, "_client_context", None):
                        target._client_context_pending = True
                while True:
                    self._check_prompt(prompt)
                    if cancelled.is_set():
                        session.cancel()
                        raise asyncio.CancelledError()
                    final_text = ""
                    native_text = _ClientText() if native_tools else None
                    upstream_text = ""
                    visible_text = ""
                    holder["upstream_started_at"] = time.time()
                    streamed_tool_call = False
                    upstream_completed = False
                    stream = (session.stream(prompt, model_provider, tool_names=tool_names)
                              if native_tools else session.stream(prompt, model_provider))
                    async for event in stream:
                        if event["type"] == "text":
                            final_text += event["delta"]
                            upstream_text += event["delta"]
                            # Tool envelopes are parsed only after the complete
                            # upstream turn, so do not leak their markup to a
                            # native client. Ordinary text remains streamed.
                            if not native_tools:
                                out.put(event)
                            else:
                                visible = native_text.feed(event["delta"])
                                if visible:
                                    visible_text += visible
                                    out.put({"type": "text", "delta": visible})
                        elif event["type"] == "final_text":
                            if event.get("text"):
                                final_text = event["text"]
                        elif event["type"] in ("thinking", "thinking_signature"):
                            out.put(event)
                        elif event["type"] == "tool_call" and event.get("name") in tool_names:
                            streamed_tool_call = True
                            definition = tool_definitions[event["name"]]
                            out.put({**event, "tool_type": definition.get("x-client-type", "function")})
                        elif event["type"] == "credits":
                            self.credits.record_turn(turn_id, event["value"])
                            out.put(event)
                        elif event["type"] == "hitl":
                            out.put(event)
                            final_text += "\n" + event["question"]
                        elif event["type"] == "end":
                            holder["reason"] = event["reason"]
                            upstream_completed = event["reason"] in ("stop", "tool_calls")
                    holder.setdefault("reason", "stop")
                    if holder.get("reason") == "cancelled":
                        out.put({"type": "end", "reason": "cancelled"})
                        return
                    if holder.get("reason") == "timeout":
                        out.put({"type": "end", "reason": "timeout"})
                        return

                    native_calls = executor.extract_tool_calls(final_text, tool_names) if tool_names else []
                    if not native_calls and tool_names:
                        command_call = executor.command_tool_call(final_text, tool_names)
                        if command_call is not None:
                            native_calls = [command_call]
                    if native_calls:
                        if not streamed_tool_call:
                            for native_call in native_calls:
                                definition = tool_definitions[native_call["name"]]
                                out.put({"type": "tool_call", **native_call,
                                         "tool_type": definition.get("x-client-type", "function")})
                        holder["reason"] = "tool_calls"
                        self._commit_native_context(native_target, native_context, upstream_completed, cancelled)
                        out.put({"type": "end", "reason": "tool_calls"})
                        return
                    if streamed_tool_call:
                        holder["reason"] = "tool_calls"
                        self._commit_native_context(native_target, native_context, upstream_completed, cancelled)
                        out.put({"type": "end", "reason": "tool_calls"})
                        return
                    if native_tools and re.search(r"<(?:tool_call|exec_command)\b", final_text, re.IGNORECASE):
                        raise apierr.upstream_error("upstream returned a malformed or disallowed client tool call")
                    if native_tools:
                        # Some upstream transports only provide a final snapshot.
                        # Feed its unstreamed suffix once instead of duplicating it.
                        suffix = (final_text[len(upstream_text):]
                                  if final_text.startswith(upstream_text) else "")
                        visible = native_text.feed(suffix, final=True)
                        if visible:
                            visible_text += visible
                            out.put({"type": "text", "delta": visible})
                    refuses_tools = re.search(
                        r"(?:can(?:not|'t)|could(?:\s+not|n't)|unable|not (?:available|accessible)|"
                        r"no (?:access|tools)|не (?:могу|доступ|имею)|нет доступа)", final_text, re.IGNORECASE)
                    if tool_names and refuses_tools and tool_retries < 1 and not visible_text.strip():
                        tool_retries += 1
                        native_context = None  # Retry prompts are not part of the client history.
                        prompt = (base_prompt + "\n\n[CLIENT TOOL TRANSPORT CORRECTION]\n"
                                  "Your previous reply treated the client's tools as unavailable. "
                                  "Invoke a supplied tool using <tool_call> JSON instead of Retool sandbox tools. "
                                  "The relay passes it to the real client and returns its result next turn. "
                                  "For this requested task, issue the appropriate client tool call now.")
                        continue
                    if tool_names and (choice == "required" or isinstance(choice, dict)):
                        raise apierr.upstream_error("upstream did not honor the required client tool choice")

                    # Once the summary turn is requested, nothing is executed any more.
                    can_exec = self._can_exec(exec_steps) and not final_summary
                    command = executor.extract_command(final_text) if can_exec and not native_tools else None

                    if not final_summary and exec_steps and not self._can_exec(exec_steps):
                        # Explicit step cap reached (max_exec_turns > 0 only).
                        final_summary = True
                        summary = ("\n\n[ИТОГ] Команды больше не выполняй. "
                                   "Дай итоговый ответ по уже собранной информации, на русском.")
                        prompt = (self._followup(instructions, reminder + summary, *last_followup)
                                  if last_followup else self._check_prompt(prompt + summary))
                        continue

                    # Loop guard: the same command keeps coming back (any period).
                    if command and not final_summary and self._looks_looping(command, recent):
                        final_summary = True
                        summary = ("\n\n[ИТОГ] Команда '" + command + "' повторяется. "
                                   "Команды больше не выполняй, дай итоговый ответ.")
                        prompt = (self._followup(instructions, reminder + summary, *last_followup)
                                  if last_followup else self._check_prompt(prompt + summary))
                        continue

                    if not command and not final_text.strip():
                        explained = await self._explain_empty(holder)
                        if explained is not None:
                            out.put({"type": "error", "error": explained})
                            return

                    if not command and not final_text.strip() and empty_retries < 1:
                        # Silent turn: insist once, keeping the full system prompt.
                        empty_retries += 1
                        native_context = None  # Retry prompts are not part of the client history.
                        prompt = (base_prompt + "\n\n[RETRY] Предыдущий ход вернул пустой ответ. "
                                  "Ответь сейчас: нужна команда — выведи <exec_command>...</exec_command>, "
                                  "иначе дай короткий текстовый ответ.")
                        continue

                    if not command and not final_text.strip():
                        out.put({"type": "error", "error": apierr.upstream_error("upstream returned an empty turn")})
                        return

                    if not can_exec or not command:
                        self._commit_native_context(native_target, native_context, upstream_completed, cancelled)
                        out.put({"type": "end", "reason": holder.get("reason", "stop")})
                        return

                    if self.cfg["show_exec_output"]:
                        out.put({"type": "exec_start", "cmd": command})

                    try:
                        code, output = await asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: executor.run_command(
                                command, self.cfg["workspace_root"],
                                timeout=self.cfg["command_timeout"],
                                limit=self.cfg["command_output_limit"],
                                policy=self.cfg["exec_policy"],
                                allowlist=self.cfg["exec_allowlist"],
                                audit_path=self.cfg.get("command_log"),
                                cancel_event=cancelled,
                            ),
                        )
                    except executor.PolicyError as exc:
                        code, output = 126, str(exc)
                    if cancelled.is_set():
                        session.cancel()
                        raise asyncio.CancelledError()
                    output = redact(output, cookie_values=self.cfg.get("cookie_values", ()))
                    exec_steps += 1

                    if self.cfg["show_exec_output"]:
                        out.put({"type": "exec_result", "cmd": command, "output": output, "exit": code})

                    # Keep the task text once; earlier steps go in as a short trail.
                    if not task_block:
                        task_block = "USER TASK:\n" + parsed.prompt
                    trail = "; ".join(
                        (c[:60] + "…" if len(c) > 60 else c) + " -> exit " + str(e)
                        for c, e in previous_steps[-4:]
                    ) or "(none yet)"
                    previous_steps.append((command, code))
                    self._remember(command, recent)

                    header = task_block + "\n\nPrevious steps: " + trail
                    last_followup = (command, code, output, header)
                    prompt = self._followup(instructions, reminder, *last_followup)
            finally:
                if session is not None:
                    await self.pool.release(session)
        except apierr.ApiError as err:
            out.put({"type": "error", "error": err})
        except asyncio.CancelledError:
            out.put({"type": "end", "reason": "cancelled"})
            raise
        except Exception as exc:  # defensive: never mask a crash as success
            out.put({"type": "error", "error": apierr.internal(str(exc))})
        finally:
            out.put(None)
