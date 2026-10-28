import asyncio, queue, socket, sys, concurrent.futures
sys.path.insert(0, r"C:\specialinay_papka")
import executor, proxy, sse
from session import SessionPool

print("=== B: format_command_followup without history ===")
out = executor.format_command_followup("dir", 0, "abc")
print("own USER TASK:", "USER TASK" in out, "| len", len(out))
print("full body:", repr(out[:200]))

print()
print("=== C: _collect on disconnect ===")
class FakeSess:
    def __init__(self): self.cancelled = 0
    def cancel(self): self.cancelled += 1
class RT:
    def __init__(self): self.cfg = dict(proxy.cfg) if hasattr(proxy, "cfg") else {}
Handler = proxy.make_handler(type("R", (), {"cfg": {}})())
a, b = socket.socketpair()
b.close()
inst = object.__new__(Handler)
inst.connection = a
print("client_gone on closed peer:", sse.client_gone(inst))
events = queue.Queue(); fut = concurrent.futures.Future(); sess = FakeSess()
holder = {"session": sess}
res = type(inst)._collect(inst, fut, events, object(), "p", holder)
print("_collect ->", res, "| cancel calls:", sess.cancelled, "| fut.cancelled:", fut.cancelled())
a.close()

print()
print("=== C2: control, live peer with pending byte ===")
a2, b2 = socket.socketpair()
b2.send(b"x")
i2 = object.__new__(Handler); i2.connection = a2
print("client_gone (data pending):", sse.client_gone(i2))
print("--- _collect with live peer, fut already done ---")
q2 = queue.Queue(); q2.put(None); f2 = concurrent.futures.Future(); f2.cancel()
print("_collect ->", type(i2)._collect(i2, f2, q2, object(), "p", {"session": FakeSess()}))
a2.close(); b2.close()

print()
print("=== C3: session None at disconnect (not yet acquired) ===")
a3, b3 = socket.socketpair(); b3.close()
i3 = object.__new__(Handler); i3.connection = a3
f3 = concurrent.futures.Future()
print("_collect ->", type(i3)._collect(i3, f3, queue.Queue(), object(), "p", {"session": None}), "| fut cancelled:", f3.cancelled())
a3.close()

print()
print("=== D1: shared, cancel inside _lock.acquire ===")
class Bk:
    def __init__(self, mode): self.cfg = {"session_mode": mode, "thread_id": "t", "max_workers": 2}; self.created = 0
    async def create_thread(self): self.created += 1; return "th"
async def d1():
    pool = SessionPool(Bk("shared"))
    first = await pool.acquire()
    t = asyncio.ensure_future(pool.acquire()); await asyncio.sleep(0.01); t.cancel()
    try: await t
    except asyncio.CancelledError: pass
    print("slots after cancelled shared acquire:", pool._slots._value, "(expect 1)")
    await pool.release(first)
    print("slots after release:", pool._slots._value, "(expect 2)")
    print("re-acquire ok:", (await asyncio.wait_for(pool.acquire(), 1)) is not None)
asyncio.run(d1())

print()
print("=== D2: per_request, cancel during create_thread ===")
class SlowBk:
    def __init__(self): self.cfg = {"session_mode": "per_request", "max_workers": 2}
    async def create_thread(self): await asyncio.sleep(5); return "th"
async def d2():
    pool = SessionPool(SlowBk())
    print("slots before:", pool._slots._value)
    t = asyncio.ensure_future(pool.acquire()); await asyncio.sleep(0.02); t.cancel()
    try: await t
    except asyncio.CancelledError: pass
    print("slots after cancelled per_request acquire:", pool._slots._value, "(expect 2 -> leak if 1)")
asyncio.run(d2())

print()
print("=== D2b: per_request, create_thread raises ApiError ===")
class BadBk:
    def __init__(self): self.cfg = {"session_mode": "per_request", "max_workers": 2}
    async def create_thread(self): raise RuntimeError("boom")
async def d2b():
    pool = SessionPool(BadBk())
    try: await pool.acquire()
    except RuntimeError: pass
    print("slots after failed acquire:", pool._slots._value, "(expect 2)")
asyncio.run(d2b())

print()
print("=== D3: double release (Semaphore not bounded) ===")
async def d3():
    pool = SessionPool(Bk("per_request"))
    s = await pool.acquire(); await pool.release(s); await pool.release(s)
    print("slots after 1 acquire + 2 releases:", pool._slots._value, "of 2")
asyncio.run(d3())

print()
print("=== D4: dead lease helpers ===")
print("SessionPool._release exists:", hasattr(SessionPool, "_release"))
