"""Turn orchestration: context injection, execution loop, event fan-out."""
import asyncio
import queue
import uuid

import apierr
import executor
from context import build_system_instructions


class TurnRunner:
    def __init__(self, cfg, credits, pool):
        self.cfg = cfg
        self.credits = credits
        self.pool = pool

    def _instructions(self):
        return build_system_instructions(
            self.cfg["workspace_root"],
            enable_context=self.cfg["project_context"],
            enable_local_exec=self.cfg["exec_policy"] != "off",
            limit=self.cfg["context_max_bytes"],
            cookie_values=self.cfg.get("cookie_values", ()),
            exec_policy=self.cfg["exec_policy"],
        )

    async def run(self, parsed, out: queue.Queue, holder: dict):
        turn_id = str(uuid.uuid4())
        holder["turn_id"] = turn_id
        exec_steps = 0
        try:
            instructions = self._instructions()
            prompt = (instructions + "\n\n" + parsed.prompt) if instructions else parsed.prompt
            history_note = ""
            model_provider = {
                "model": parsed.model,
                "providerResourceName": self.cfg["provider_resource_name"]
                if parsed.entry.get("provider") != "openai"
                else self.cfg["openai_provider_resource_name"],
            }
            if parsed.effort:
                model_provider["reasoningEffort"] = parsed.effort

            while True:
                session = await self.pool.acquire()
                try:
                    holder["session"] = session
                    final_text = ""
                    async for event in session.stream(prompt, model_provider):
                        if event["type"] == "text":
                            final_text += event["delta"]
                            out.put(event)
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
                finally:
                    await self.pool.release(session)

                can_exec = (self.cfg["exec_policy"] != "off") and exec_steps < self.cfg["max_exec_turns"]
                command = executor.extract_command(final_text) if can_exec else None
                if not can_exec:
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
                            audit_path=self.cfg["command_log"],
                        ),
                    )
                except executor.PolicyError as exc:
                    code, output = 126, str(exc)
                exec_steps += 1

                if self.cfg["show_exec_output"]:
                    out.put({"type": "exec_result", "cmd": command, "output": output, "exit": code})

                history_note = (history_note + "\n\n" if history_note else "") + \
                    "USER TASK:\n" + parsed.prompt
                prompt = executor.format_command_followup(command, code, output, history_note)
        except apierr.ApiError as err:
            out.put({"type": "error", "error": err})
        except asyncio.CancelledError:
            out.put({"type": "end", "reason": "cancelled"})
            raise
        except Exception as exc:  # defensive: never mask a crash as success
            out.put({"type": "error", "error": apierr.internal(str(exc))})
        finally:
            out.put(None)
