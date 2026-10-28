import asyncio, sys
sys.path.insert(0, r"C:\specialinay_papka")
from session import RelaySession, SessionPool

class FakeWS:
    def __init__(self, frames): self.frames = list(frames); self.sent = []
    async def send(self, data): self.sent.append(data)
    async def recv(self):
        if not self.frames: raise asyncio.TimeoutError
        return self.frames.pop(0)
    async def close(self): pass

class Bk:
    def __init__(self): self.cfg = {"session_mode": "shared", "thread_id": "t", "max_workers": 2,
                                    "turn_timeout": 2, "recv_timeout": 1, "agent_mode": 2}
    refresher = None

async def drive(sess, prompt):
    evs = []
    try:
        async with asyncio.timeout(6):
            async for e in sess.stream(prompt, {}):
                evs.append(e)
    except TimeoutError:
        evs.append({"type": "HANG"})
    return evs

async def main():
    bk = Bk()
    sess = RelaySession(bk, "t")
    sess.ws = FakeWS([])
    print("turn 1 (ws empty -> timeout path):")
    print("  events:", await drive(sess, "p1"))
    print("  _cancelled after turn 1:", sess._cancelled)
    sess.cancel()                                # simulates proxy disconnect
    sess.ws = FakeWS(['{"type":"text","content":"hello","messageId":"m1"}',
                      '{"type":"agent_loop_done"}'])
    print("turn 2 after cancel (fresh ws frames ready):")
    evs = await drive(sess, "p2")
    print("  events:", evs)
    print("  ws got the new prompt:", len(sess.ws.sent))

    print()
    print("turn 3, still cancelled?", sess._cancelled)
    sess.ws = FakeWS(['{"type":"text","content":"world","messageId":"m2"}', '{"type":"agent_loop_done"}'])
    evs = await drive(sess, "p3")
    print("  events:", evs)

asyncio.run(main())
