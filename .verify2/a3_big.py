import asyncio, queue, sys
sys.path.insert(0, r"C:\specialinay_papka")
import executor, models
from orchestrator import TurnRunner
LONGCMD = "dir " + "x" * 3000
class S:
    thread_id = "t"
    def __init__(self): self.prompts = []; self.calls = 0
    async def stream(self, prompt, mp):
        self.prompts.append(prompt); self.calls += 1
        if self.calls <= 5:
            yield {"type": "final_text", "text": "<exec_command>" + LONGCMD + str(self.calls) + "</exec_command>"}
        else:
            yield {"type": "final_text", "text": "ok"}
        yield {"type": "end", "reason": "stop"}
class P:
    def __init__(self, s): self.s = s
    async def acquire(self): return self.s
    async def release(self, s): pass
executor.run_command = lambda *a, **kw: (0, "X" * 4000)
cfg = {"workspace_root": r"C:\specialinay_papka", "project_context": True, "exec_policy": "allowlist",
       "exec_allowlist": ["^dir"], "max_exec_turns": 5, "command_timeout": 5, "command_output_limit": 4000,
       "show_exec_output": False, "cookie_values": [], "context_max_bytes": 2500, "max_prompt_bytes": 24000,
       "provider_resource_name": "a", "openai_provider_resource_name": "o"}
s = S(); r = TurnRunner(cfg, type("C", (), {"record_turn": lambda *a: None})(), P(s))
asyncio.run(r.run(type("X", (), {"prompt": "посмотри папку", "system": "", "model": "claude-opus-5-5",
    "entry": models.resolve("claude-opus-5-5", None)[0], "effort": None})(), queue.Queue(), {}))
lens = [len(p) for p in s.prompts]
print("lens:", lens, "limit 24000 -> over:", [l > 24000 for l in lens])
print("last prompt starts with instructions:", s.prompts[-1][:40].replace(chr(10), " "))
print("reminder in last prompt:", "[LOCAL EXEC REMINDER]" in s.prompts[-1])
