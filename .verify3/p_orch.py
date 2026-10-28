
import asyncio, queue, os, sys
sys.path.insert(0, r"C:\specialinay_papka")
import executor, models
from orchestrator import TurnRunner

CALLS = []
def fake_run(cmd, *a, **kw):
    CALLS.append(cmd)
    return 0, "out:" + cmd

executor.run_command = fake_run

class S:
    thread_id = "t"
    def __init__(self, gen, cap=40):
        self.prompts = []; self.gen = gen; self.cap = cap
    async def stream(self, prompt, model_provider):
        self.prompts.append(prompt)
        if len(self.prompts) > self.cap:
            raise RuntimeError("cap")
        for ev in self.gen(len(self.prompts), prompt):
            yield ev
        yield {"type": "end", "reason": "stop"}

class Pool:
    def __init__(self, s): self.s = s
    async def acquire(self): return self.s
    async def release(self, s): return None

def cfg(tmp, **kw):
    c = {"workspace_root": str(tmp), "project_context": False, "exec_policy": "allowlist",
         "exec_allowlist": ["^dir\\b", "^cd\\b"], "max_exec_turns": 0, "command_timeout": 5,
         "command_output_limit": 500, "show_exec_output": False, "cookie_values": [],
         "context_max_bytes": 300, "max_prompt_bytes": 5000,
         "provider_resource_name": "anth", "openai_provider_resource_name": "oai"}
    c.update(kw); return c

def parsed(p="задача", instr_count=0):
    entry, effort = models.resolve("claude-opus-5-5", None)
    o = type("P", (), {})()
    o.prompt, o.system, o.model, o.entry, o.effort = p, "", entry["id"], entry, effort
    return o

def drain(q):
    ev = []
    while not q.empty(): ev.append(q.get())
    return [e for e in ev if e]

import tempfile
tmp = tempfile.mkdtemp(dir=r"C:\specialinay_papka\.verify3")

# 1) unlimited steps at max_exec_turns=0
big = models  # placeholder
cmds = ["dir", "dir .", "dir a", "dir b", "dir c", "dir d", "dir e", "dir f", "dir g", "dir h", "dir i", "dir j"]
s = S(lambda n, p: [{"type":"final_text","text":"<exec_command>"+cmds[(n-1) % len(cmds)]+"</exec_command>"}] if n <= 12 else [{"type":"final_text","text":"done"}])
CALLS.clear()
asyncio.run(TurnRunner(cfg(tmp), type("C",(),{"record_turn":lambda *a: None})(), Pool(s)).run(parsed(), queue.Queue(), {}))
print("PROBE1 unlimited steps: prompts=%d executions=%d" % (len(s.prompts), len(CALLS)))
for c in cmds[:12]:
    if len(s.prompts) < 13 and not any(c in x for x in CALLS): print("   missing exec:", c)

# 2) loop guard fires on triple
s2 = S(lambda n, p: [{"type":"final_text","text":"<exec_command>dir</exec_command>"}])
CALLS.clear()
q = queue.Queue()
asyncio.run(TurnRunner(cfg(tmp), type("C",(),{"record_turn":lambda *a: None})(), Pool(s2)).run(parsed(), q, {}))
print("PROBE2 identical dir: prompts=%d executions=%d last_has_ITOG=%s" % (len(s2.prompts), len(CALLS), "[ИТОГ]" in s2.prompts[-1]))

# 3) final turn still emits a command -> executed?
def g3(n, p):
    if "[ИТОГ]" in p:
        return [{"type":"final_text","text":"<exec_command>dir</exec_command>"}]
    return [{"type":"final_text","text":"<exec_command>dir</exec_command>"}]
s3 = S(g3, cap=15)
CALLS.clear()
q3 = queue.Queue()
try:
    asyncio.run(TurnRunner(cfg(tmp), type("C",(),{"record_turn":lambda *a: None})(), Pool(s3)).run(parsed(), q3, {}))
    print("PROBE3 model ignores [ИТОГ] and repeats: prompts=%d executions=%d (cap 15 not hit)" % (len(s3.prompts), len(CALLS)))
except RuntimeError as e:
    print("PROBE3 INFINITE LOOP: prompts>%d executions=%d" % (len(s3.prompts)-1, len(CALLS)))

# 4) formatting variance defeats the guard: "dir" vs "DIR"
def g4(n, p):
    if "[ИТОГ]" in p: return [{"type":"final_text","text":"итог"}]
    return [{"type":"final_text","text":"<exec_command>" + ("dir" if n % 2 else "DIR") + "</exec_command>"}]
s4 = S(g4, cap=30)
CALLS.clear()
try:
    asyncio.run(TurnRunner(cfg(tmp), type("C",(),{"record_turn":lambda *a: None})(), Pool(s4)).run(parsed(), queue.Queue(), {}))
    print("PROBE4 dir/DIR alternating: prompts=%d executions=%d guard_fired=%s" % (len(s4.prompts), len(CALLS), any("[ИТОГ]" in p for p in s4.prompts)))
except RuntimeError:
    print("PROBE4 INFINITE: prompts>%d executions=%d" % (len(s4.prompts)-1, len(CALLS)))

# 4b) whitespace/quotes variance
def g4b(n, p):
    if "[ИТОГ]" in p: return [{"type":"final_text","text":"итог"}]
    return [{"type":"final_text","text":"<exec_command>" + (" dir " if n % 2 else "dir") + "</exec_command>"}]
s4b = S(g4b, cap=25)
CALLS.clear()
asyncio.run(TurnRunner(cfg(tmp), type("C",(),{"record_turn":lambda *a: None})(), Pool(s4b)).run(parsed(), queue.Queue(), {}))
print("PROBE4b ' dir ' vs 'dir': prompts=%d guard_fired=%s" % (len(s4b.prompts), any("[ИТОГ]" in p for p in s4b.prompts)))

# 5) max_exec_turns=2 with two execs then text: duplicated streamed text?
def g5(n, p):
    if n <= 2: return [{"type":"text","delta":"<exec_command>dir</exec_command>"},{"type":"final_text","text":"<exec_command>dir</exec_command>"}]
    return [{"type":"text","delta":"ГОТОВО"},{"type":"final_text","text":"ГОТОВО"}]
s5 = S(g5, cap=10)
CALLS.clear()
q5 = queue.Queue()
asyncio.run(TurnRunner(cfg(tmp, max_exec_turns=2), type("C",(),{"record_turn":lambda *a: None})(), Pool(s5)).run(parsed(), q5, {}))
ev = drain(q5)
texts = "".join(e["delta"] for e in ev if e.get("type") == "text")
print("PROBE5 max_exec_turns=2: prompts=%d executions=%d streamed_text=%r" % (len(s5.prompts), len(CALLS), texts))
print("PROBE5 last prompt has ИТОГ:", "[ИТОГ]" in s5.prompts[-1], "| final turn had command executed:", len(CALLS) > 2)
print("PROBE5 prompt keeps trail/output in final turn:", "Command result" in s5.prompts[-1])

# 6) exec_policy=off
def g6(n, p):
    return [{"type":"text","delta":"ответ"},{"type":"final_text","text":"ответ"}]
s6 = S(g6, cap=8)
q6 = queue.Queue()
asyncio.run(TurnRunner(cfg(tmp, exec_policy="off"), type("C",(),{"record_turn":lambda *a: None})(), Pool(s6)).run(parsed(), q6, {}))
ev6 = drain(q6)
print("PROBE6 exec_policy=off: prompts=%d streamed_text=%r" % (len(s6.prompts), "".join(e["delta"] for e in ev6 if e.get("type")=="text")))
