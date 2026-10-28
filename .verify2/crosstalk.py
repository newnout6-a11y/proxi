import asyncio, sys
sys.path.insert(0, r"C:\specialinay_papka")
from session import RelaySession

class WS:
    def __init__(self, frames, delay=0.05): self.frames = list(frames); self.delay = delay; self.sent = []
    async def send(self, d): self.sent.append(d)
    async def recv(self):
        await asyncio.sleep(self.delay)
        if not self.frames: raise asyncio.TimeoutError
        return self.frames.pop(0)
    async def close(self): pass

class Bk:
    cfg = {"session_mode": "shared", "thread_id": "t", "turn_timeout": 5, "recv_timeout": 2, "agent_mode": 2}
    refresher = None

async def main():
    s = RelaySession(Bk(), "t")
    s.ws = WS(['{"type":"text","content":"ANSWER-OF-TURN-1","messageId":"m1"}',
               '{"type":"agent_loop_done"}', "PAD1", "PAD2"])
    t = asyncio.ensure_future(_drain(s, "p1"))
    await asyncio.sleep(0.01)
    print("  _cancelled now:", end=" ")
    s.cancel()
    await t
    print("frames of turn 1 left in buffer:", len(s.ws.frames))
    print("  turn 2 reads leftovers (proves cross-talk if ANSWER-OF-TURN-1 re-delivered):")
    s._cancelled = False                       # emulate the missing reset
    evs = []
    async with asyncio.timeout(2):
        async for e in s.stream("p2", {}):
            evs.append(e)
    print("  turn 2 events:", evs)

async def _drain(s, p):
    async with asyncio.timeout(2):
        try:
            async for e in s.stream(p, {}):
                pass
        except TimeoutError:
            pass

asyncio.run(main())
