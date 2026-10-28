import asyncio, queue, sys
sys.path.insert(0, r"C:\specialinay_papka")
import executor, models
from orchestrator import TurnRunner

TASK = "посмотри папку"
LONGCMD = "dir " + "x" * 800

class S:
    thread_id = "t"
    def __init__(self): self.prompts = []; self.calls = 0
    async def stream(self, prompt, mp):
        self.prompts.append(prompt); self.calls += 1
        if self.calls <= 5:
            t = "<exec_command>" + LONGCMD + str(self.calls) + "</exec_command>"
            yield {"type": "final_text", "text": t}
        else:
            yield {"type": "final_text", "text": "ok"}
        yield {"type": "end", "reason": "stop"}

class P:
    def __init__(self, s): self.s = s
    async def acquire(self): return self.s
    async def release(self, s): pass

def go(task, limit, outlen):
    executor.run_command = lambda *a, **kw: (0, "X" * outlen)
    cfg = {"workspace_root": r"C:\specialinay_papka", "project_context": True, "exec_policy": "allowlist",
           "exec_allowlist": ["^dir"], "max_exec_turns": 5, "command_timeout": 5,
           "command_output_limit": outlen, "show_exec_output": False, "cookie_values": [],
           "context_max_bytes": 2500, "max_prompt_bytes": limit,
           "provider_resource_name": "a", "openai_provider_resource_name": "o"}
    s = S(); r = TurnRunner(cfg, type("C", (), {"record_turn": lambda *a: None})(), P(s))
    asyncio.run(r.run(type("X", (), {"prompt": task, "system": "", "model": "claude-opus-5-5",
        "entry": models.resolve("claude-opus-5-5", None)[0], "effort": None})(), queue.Queue(), {}))
    return s.prompts

print("DEFAULT limit 24000, default-sized output 4000:")
print(" lens:", [len(p) for p in go(TASK, 24000, 4000)], "(limit 24000)")
print("ANY over 24000:", any(len(p) > 24000 for p in go(TASK, 24000, 4000)))
print()
print("LONG commands (800 chars each), limit 2000:")
lens = [len(p) for p in go(TASK, 2000, 100)]
print(" lens:", lens, "-> grows", lens[-1] - lens[1], "chars over 4 steps, over limit:", [l > 2000 for l in lens])
