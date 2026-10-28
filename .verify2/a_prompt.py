"""Claim A: prompt must not blow up and must not repeat USER TASK."""
import asyncio, queue, sys
sys.path.insert(0, r"C:\specialinay_papka")
import executor, models
from orchestrator import TurnRunner

TASK = "посмотри папку и скажи что там"

class FakeSession:
    thread_id = "t1"
    def __init__(self):
        self.prompts = []
        self.calls = 0
    async def stream(self, prompt, model_provider):
        self.prompts.append(prompt)
        self.calls += 1
        if self.calls <= 4:
            cmd = "dir step" + str(self.calls)
            yield {"type": "text", "delta": "<exec_command>" + cmd + "</exec_command>"}
            yield {"type": "final_text", "text": "<exec_command>" + cmd + "</exec_command>"}
        else:
            yield {"type": "text", "delta": "готово"}
            yield {"type": "final_text", "text": "готово"}
        yield {"type": "end", "reason": "stop"}

class FakePool:
    def __init__(self, session): self.session = session
    async def acquire(self): return self.session
    async def release(self, session): pass

def parsed(prompt, effort=None):
    entry, _ = models.resolve("claude-opus-5-5", None)
    o = type("P", (), {})()
    o.prompt, o.system, o.model, o.entry, o.effort = prompt, "", entry["id"], entry, effort
    return o

def run(task, limit, output_len=5000, max_exec_turns=5):
    executor.run_command = lambda *a, **kw: (0, "X" * output_len)
    cfg = {"workspace_root": r"C:\specialinay_papka", "project_context": False,
           "exec_policy": "allowlist", "exec_allowlist": ["^dir\\b"], "max_exec_turns": max_exec_turns,
           "command_timeout": 5, "command_output_limit": output_len, "show_exec_output": False,
           "cookie_values": [], "context_max_bytes": 500, "max_prompt_bytes": limit,
           "provider_resource_name": "anth", "openai_provider_resource_name": "oai"}
    session = FakeSession()
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *a: None})(), FakePool(session))
    out = queue.Queue()
    asyncio.run(runner.run(parsed(task), out, {}))
    return session.prompts

print("--- case 1: small task, limit=1200, output 5000 ---")
ps = run(TASK, 1200)
for i, p in enumerate(psi := ps, 1):
    print("step", i, "len", len(p), "over_limit", len(p) > 1200, "USER TASK count", p.count("USER TASK"), "task repeats", p.count(TASK), "role", "[ROLE]" in p, "reminder", "[LOCAL EXEC REMINDER]" in p)
print()
print("--- case 2: LARGE task (3000 chars), limit=1200 ---")
ps2 = run("Z" * 3000, 1200, output_len=5000)
for i, p in enumerate(ps2, 1):
    print("step", i, "len", len(p), "over_limit", len(p) > 1200, "USER TASK count", p.count("USER TASK"))
print()
print("--- case 3: long command text, 5 steps, limit=2000 ---")
cfgss = run(TASK, 2000, output_len=100)
print("lens:", [len(p) for p in cfgss])
