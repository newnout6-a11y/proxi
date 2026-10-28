
import asyncio, queue, sys, tempfile
sys.path.insert(0, r"C:\specialinay_papka")
import executor, models, config as C
from orchestrator import TurnRunner
CALLS=[]
executor.run_command = lambda cmd,*a,**k: (CALLS.append(cmd) or (0,"out"))
class S:
    thread_id="t"
    def __init__(s,g,cap=400): s.prompts=[]; s.g=g; s.cap=cap
    async def stream(s,prompt,mp):
        s.prompts.append(prompt)
        if len(s.prompts)>s.cap: raise RuntimeError("cap")
        for e in s.g(len(s.prompts),prompt): yield e
        yield {"type":"end","reason":"stop"}
class Pool:
    def __init__(s,x): s.x=x
    async def acquire(s): return s.x
    async def release(s,x): return None
def cfg(tmp,**kw):
    c={"workspace_root":str(tmp),"project_context":False,"exec_policy":"allowlist","exec_allowlist":["^dir\\b"],
       "max_exec_turns":0,"command_timeout":5,"command_output_limit":500,"show_exec_output":False,
       "cookie_values":[],"context_max_bytes":300,"max_prompt_bytes":5000,
       "provider_resource_name":"a","openai_provider_resource_name":"o"}; c.update(kw); return c
def parsed():
    e,f=models.resolve("claude-opus-5-5",None); o=type("P",(),{})()
    o.prompt,o.system,o.model,o.entry,o.effort="t","",e["id"],e,f; return o
tmp=tempfile.mkdtemp(dir=r"C:\specialinay_papka\.verify3")
CR=type("C",(),{"record_turn":lambda *a:None})()

def run(s,**kw):
    CALLS.clear()
    asyncio.run(TurnRunner(cfg(tmp,**kw),CR,Pool(s)).run(parsed(),queue.Queue(),{}))
    itog=[i for i,p in enumerate(s.prompts) if "[ИТОГ]" in p]
    return len(s.prompts),len(CALLS),itog

# identical command forever
s=S(lambda n,p:[{"type":"final_text","text":"<exec_command>dir</exec_command>"}],cap=60)
n,e,it=run(s); print("A) same cmd forever: prompts=%d executions=%d itog_positions=%s" % (n,e,it[:3]))
# alternate case
s=S(lambda n,p:[{"type":"final_text","text":"<exec_command>"+("dir" if n%2 else "DIR")+"</exec_command>"}],cap=60)
n,e,it=run(s); print("B) dir/DIR alternate: prompts=%d executions=%d itog=%s" % (n,e,it))
# answer text duplication at max_exec_turns=2
def g5(n,p):
    if n<=2: return [{"type":"text","delta":"CMD "},{"type":"final_text","text":"<exec_command>dir</exec_command>"}]
    return [{"type":"text","delta":"ANSWER "},{"type":"final_text","text":"ANSWER "}]
s=S(g5,cap=20); CALLS.clear()
q=queue.Queue()
asyncio.run(TurnRunner(cfg(tmp,max_exec_turns=2),CR,Pool(s)).run(parsed(),q,{}))
ev=[]
while not q.empty(): ev.append(q.get())
txt="".join(x["delta"] for x in ev if x and x.get("type")=="text")
print("C) cap=2: prompts=%d executions=%d answer_emitted=%d" % (len(s.prompts),len(CALLS),txt.count("ANSWER")))
print("C) final prompt carries prev output:", "Command result" in s.prompts[-1], "| carries task:", "t" == s.prompts[-1][:1])
# exec_policy=off
s=S(lambda n,p:[{"type":"text","delta":"ANS "},{"type":"final_text","text":"ANS "}],cap=9)
q=queue.Queue(); asyncio.run(TurnRunner(cfg(tmp,exec_policy="off"),CR,Pool(s)).run(parsed(),q,{}))
ev=[]
while not q.empty(): ev.append(q.get())
print("D) exec_policy=off: prompts=%d answer_emitted=%d" % (len(s.prompts),"".join(x["delta"] for x in ev if x and x.get("type")=="text").count("ANS")))
# config validation of exec_policy=off
import json, os
p=os.path.join(tmp,"c.json")
json.dump({"base":"https://x","page_uuid":"p","ua":"u","cookie":"a=1","xsrf":"x","exec_policy":"off"},open(p,"w"))
try:
    c=C.load(p); print("E) load(exec_policy=off) ->", c["exec_policy"])
except Exception as ex: print("E) load(exec_policy=off) -> ERROR:", ex)
# prompt size vs max_prompt_bytes with cyrillic output
big="я"*5000
executor.run_command=lambda cmd,*a,**k:(CALLS.append(cmd) or (0,big))
s=S(lambda n,p:[{"type":"final_text","text":"<exec_command>dir</exec_command>"}] if n==1 else [{"type":"final_text","text":"done"}],cap=4)
CALLS.clear(); asyncio.run(TurnRunner(cfg(tmp,max_prompt_bytes=2000,context_max_bytes=400),CR,Pool(s)).run(parsed(),queue.Queue(),{}))
pr=s.prompts[-1]
print("F) max_prompt_bytes=2000 -> last prompt chars=%d bytes=%d exceeds=%s" % (len(pr),len(pr.encode()),len(pr.encode())>2000))
print("F) [ROLE] in instructions:", "[ROLE]" in s.prompts[0], "| exec tag intact:", "<exec_command>" in s.prompts[0])
