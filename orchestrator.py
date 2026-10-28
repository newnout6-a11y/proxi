"""Turn orchestration: context injection, execution loop, event fan-out."""
import asyncio
import queue
import uuid

import apierr
import executor
import models as model_catalog
from context import build_system_instructions
from redact import redact


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


class TurnRunner:
    def __init__(self, cfg, credits, pool):
        self.cfg = cfg
        self.credits = credits
        self.pool = pool
        self._recent = []

    def _can_exec(self, steps: int) -> bool:
        """No limits by default; an explicit max_exec_turns > 0 caps the steps."""
        if self.cfg["exec_policy"] == "off":
            return False
        max_steps = int(self.cfg.get("max_exec_turns", 0) or 0)
        return max_steps <= 0 or steps < max_steps

    def _looks_looping(self, command: str) -> bool:
        """True when this command already ran three times in the recent window."""
        key = " ".join(command.strip().lower().split())
        recent = [" ".join(c.strip().lower().split()) for c, _ in self._recent]
        return recent.count(key) >= 3

    def _remember(self, command: str) -> None:
        self._recent.append((command, 0))
        if len(self._recent) > 8:
            del self._recent[0]

    def _instructions(self):
        return build_system_instructions(
            self.cfg["workspace_root"],
            enable_context=self.cfg["project_context"],
            enable_local_exec=self.cfg["exec_policy"] != "off",
            limit=self.cfg["context_max_bytes"],
            cookie_values=self.cfg.get("cookie_values", ()),
            exec_policy=self.cfg["exec_policy"],
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
        exec_steps = 0
        empty_retries = 0
        final_summary = False
        try:
            instructions = self._instructions()
            reminder = ""
            if self.cfg["exec_policy"] != "off":
                reminder = (
                    "\n\n[LOCAL EXEC REMINDER]\n"
                    "To run anything on the user's machine you MUST output "
                    "<exec_command>command</exec_command> and stop. The relay executes it and returns output. "
                    "Do not answer that you cannot run commands."
                )
            prompt = (instructions + "\n\n" + parsed.prompt) if instructions else parsed.prompt
            prompt += reminder
            task_block = ""
            previous_steps = []
            model_provider = {
                "model": parsed.model,
                "providerResourceName": model_catalog.provider_resource(self.cfg, parsed.entry),
            }
            if parsed.effort:
                model_provider["reasoningEffort"] = parsed.effort

            base_prompt = prompt
            session = None
            try:
                session = await self.pool.acquire()
                holder["session"] = session
                while True:
                    final_text = ""
                    async for event in session.stream(prompt, model_provider):
                        if event["type"] == "text":
                            final_text += event["delta"]
                            out.put(event)
                        elif event["type"] == "final_text":
                            if event.get("text"):
                                final_text = event["text"]
                        elif event["type"] == "credits":
                            self.credits.record_turn(turn_id, event["value"])
                            out.put(event)
                        elif event["type"] == "hitl":
                            out.put(event)
                            final_text += "\n" + event["question"]
                        elif event["type"] == "end":
                            holder["reason"] = event["reason"]
                            if event["reason"] == "cancelled":
                                out.put({"type": "end", "reason": "cancelled"})
                                return
                    holder.setdefault("reason", "stop")

                    # Once the summary turn is requested, nothing is executed any more.
                    can_exec = self._can_exec(exec_steps) and not final_summary
                    command = executor.extract_command(final_text) if can_exec else None

                    if not final_summary and exec_steps and not self._can_exec(exec_steps):
                        # Explicit step cap reached (max_exec_turns > 0 only).
                        final_summary = True
                        prompt = prompt + "\n\n[ИТОГ] Команды больше не выполняй. " \
                                          "Дай итоговый ответ по уже собранной информации, на русском."
                        continue

                    # Loop guard: the same command keeps coming back (any period).
                    if command and not final_summary and self._looks_looping(command):
                        final_summary = True
                        prompt = prompt + "\n\n[ИТОГ] Команда '" + command + "' повторяется. " \
                                          "Команды больше не выполняй, дай итоговый ответ."
                        continue

                    if not command and not final_text.strip():
                        explained = await self._explain_empty(holder)
                        if explained is not None:
                            out.put({"type": "error", "error": explained})
                            return

                    if not command and not final_text.strip() and empty_retries < 1:
                        # Silent turn: insist once, keeping the full system prompt.
                        empty_retries += 1
                        prompt = (base_prompt + "\n\n[RETRY] Предыдущий ход вернул пустой ответ. "
                                  "Ответь сейчас: нужна команда — выведи <exec_command>...</exec_command>, "
                                  "иначе дай короткий текстовый ответ.")
                        continue

                    if not command and not final_text.strip():
                        out.put({"type": "error", "error": apierr.upstream_error("upstream returned an empty turn")})
                        return

                    if not can_exec or not command:
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
                            ),
                        )
                    except executor.PolicyError as exc:
                        code, output = 126, str(exc)
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
                    self._remember(command)

                    limit = int(self.cfg.get("max_prompt_bytes", 128000))
                    header = task_block + "\n\nPrevious steps: " + trail
                    wrapper = _b(executor.format_command_followup(command, code, "", header))
                    budget = limit - _b(instructions) - _b(reminder) - wrapper - 100
                    trimmed = _trim(output, max(0, budget))
                    followup = executor.format_command_followup(command, code, trimmed, header)
                    if reminder:
                        followup += reminder
                    prompt = (instructions + "\n\n" + followup) if instructions else followup
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
