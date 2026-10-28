import asyncio, sys
sys.path.insert(0, r"C:\specialinay_papka")
from session import RelaySession
class WS:
    def __init__(self, frames): self.frames = list(frames); self.sent = []
    async def send(self, d): self.sent.append(d)
    async def recv(self):
        await asyncio.sleep(0.05)
        if not self.frames: raise asyncio.TimeoutError
        return self.frames.pop(0)
    async def close(self): pass
class Bk:
    cfg = {"session_mode": "shared", "thread_id": "t", "turn_timeout": 5, "recv_timeout": 2, "agent_mode": 2}
    refresher = None
async def main():
    s = RelaySession(Bk(), "t")
    s.ws = WS(['{"type":"text","content":"FIRST","messageId":"m1"}',
               '{"type":"text","content":"FIRST-LATE","messageId":"m1"}',
               '{"type":"agent_loop_done"}'])
    async def turn(p, stop_after):
        evs = []
        async with asyncio.timeout(3):
            async for e in s.stream(p, {}):
                evs.append(e)
                if len(evs) >= stop_after: return evs
        return evs
    t = asyncio.ensure_future(turn("p1", 99)); await asyncio.sleep(0.06)
    s.cancel()
    print("turn 1 events:", await t)
    print("leftovers in ws buffer:", len(s.ws.frames))
    s._cancelled = False
    print("turn 2 events (should be turn-2 answer, not FIRST-LATE):", await turn("p2", 99))
asyncio.run(main())
