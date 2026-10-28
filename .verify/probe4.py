import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apierr, auth, config as config_module, models, orchestrator, session

CFG = {"base": "https://x.invalid", "page_uuid": "p", "thread_id": "shared-thread", "ua": "u",
       "cookie": "accessToken=abcdef123456789; xsrfToken=q", "xsrf": "q", "relay_api_key": "k",
       "workspace_root": os.getcwd(), "upstream_timeout": 5, "turn_timeout": 5, "recv_timeout": 1,
       "session_mode": "per_request", "max_workers": 4}

class FakeBackend:
    def __init__(self):
        self.created = []
        self.cfg = CFG
    async def create_thread(self):
        tid = "thread-%d" % len(self.created)
        self.created.append(tid)
        return tid

async def main():
    # ---- isolation: two concurrent acquires must give distinct threads ----
    be = FakeBackend()
    pool = session.SessionPool(be)
    print("mode:", pool.mode, "| slots:", CFG["max_workers"])
    a = await pool.acquire()
    b = await pool.acquire()
    print("distinct threads:", a.thread_id, b.thread_id, "| distinct objects:", a is not b)
    await pool.release(a); await pool.release(b)
    print("backend create_thread calls:", len(be.created), be.created)
    # ---- shared mode ----
    CFG["session_mode"] = "shared"
    pool2 = session.SessionPool(FakeBackend())
    s1 = await pool2.acquire()
    s2 = await pool2.acquire()  # blocks on the lock; schedule a release
    async def rel():
        await asyncio.sleep(0.05)
        await pool2.release(s1)

    rel_task = asyncio.ensure_future(rel())
    s2 = await pool2.acquire()
    print("shared: same object:", s1 is s2, "| thread:", s2.thread_id)
    print("shared lock locked:", pool2._lock.locked())
    await pool2.release(s2)
    await rel_task
    print("after releases: locked:", pool2._lock.locked(), "| shared kept:", pool2._shared is not None)

asyncio.run(main())

# ---- config: exec_policy off ----
import tempfile
base = dict(config_module.DEFAULTS)
base.update({"base": "https://x.invalid", "page_uuid": "p", "ua": "u",
             "cookie": "accessToken=abcdef123456789", "xsrf": "q", "workspace_root": os.getcwd()})
for policy in ("allowlist", "allow_all", "off", "OFF"):
    cfg = dict(base, exec_policy=policy)
    fd, path = tempfile.mkstemp(suffix=".json")
    os.write(fd, json.dumps(cfg).encode()); os.close(fd)
    try:
        config_module.load(path)
        print("config exec_policy=%-10r -> OK" % policy)
    except apierr.ApiError as e:
        print("config exec_policy=%-10r -> %s %s" % (policy, e.status, e.message[:70]))
    os.unlink(path)

# ---- empty-turn retry loses system instructions ----
CFG2 = {"workspace_root": os.getcwd(), "project_context": True, "exec_policy": "allowlist",
        "exec_allowlist": ["^dir\\b"], "max_exec_turns": 2, "context_max_bytes": 400,
        "cookie_values": (), "show_exec_output": False, "command_log": None}

class EmptySession:
    thread_id = "t"
    def __init__(self):
        self.prompts = []
    async def stream(self, prompt, mp):
        self.prompts.append(prompt)
        yield {"type": "final_text", "text": ""}
        yield {"type": "end", "reason": "stop"}

class FakePool:
    def __init__(self, s):
        self.session = s
        import types
        self.backend = types.SimpleNamespace(get_messages=lambda tid: _none())
    async def acquire(self):
        return self.session
    async def release(self, s):
        pass

async def _none():
    return {"messages": []}

sess = EmptySession()
runner = orchestrator.TurnRunner(CFG2, type("C", (), {"record_turn": lambda *a: None})(), FakePool(sess))
entry, effort = models.resolve("claude-opus-5-5", None)
p = type("P", (), {})()
p.prompt, p.system, p.model, p.entry, p.effort = "task", "", entry["id"], entry, effort
out = __import__("queue").Queue()
asyncio.run(runner.run(p, out, {}))
print("retry prompt count:", len(sess.prompts))
for i, pr in enumerate(sess.prompts):
    print("  prompt[%d] has ROLE=%s LOCAL_EXEC=%s CONTEXT=%s len=%d" % (
          i, "[ROLE]" in pr, "[LOCAL EXECUTION]" in pr, "[PROJECT CONTEXT" in pr, len(pr)))