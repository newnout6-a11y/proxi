
"""Probe 2: content-length edge cases, rate limit, worker cap, disconnect cancel."""
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proxy
import config as config_module

KEY = "k" * 20


class C:
    balance = {"available": 5.0}

    def get_balance(self, force_refresh=False):
        return dict(self.balance)

    def record_turn(self, *a):
        pass

    def usage(self):
        return {}


class CamSession:
    def __init__(self):
        self.cancelled = False
        self.released = False

    def cancel(self):
        self.cancelled = True


def make(**over):
    cfg = dict(config_module.DEFAULTS)
    cfg.update({"base": "https://x.invalid", "page_uuid": "p", "thread_id": "t", "ua": "u",
                "cookie": "accessToken=abcdef123456; xsrfToken=q", "xsrf": "q",
                "relay_api_key": KEY, "workspace_root": ".", "host": "127.0.0.1", "port": 0})
    cfg.update(over)
    return cfg


def start(cfg, max_workers=4):
    rt = proxy.Runtime(cfg)
    rt.credits = C()

    class R:
        def __init__(self):
            self.script = None
            self.sessions = []
            self.mode = "slow"

        async def run(self, parsed, out, holder):
            if self.mode == "slow":
                s = CamSession()
                self.sessions.append(s)
                holder["session"] = s
                deadline = time.time() + 5
                while time.time() < deadline and not s.cancelled:
                    await proxy.asyncio.sleep(0.05)
                out.put({"type": "end", "reason": "cancelled" if s.cancelled else "stop"})
                return
            out.put({"type": "text", "delta": "ok"})
            out.put({"type": "end", "reason": "stop"})

    r = R()
    rt.runner = r
    h = proxy.make_handler(rt)
    srv = proxy.BoundedServer(("127.0.0.1", 0), h, threading.BoundedSemaphore(max_workers))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, r


def raw(port, text, read=True):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(text)
    if not read:
        return s
    data = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\r\n\r\n" in data and (b"connection: close" in data.lower() or b"[DONE]" in data):
                break
    except socket.timeout:
        pass
    s.close()
    return data


print("== content-length edge cases ==")
cfg = make(max_body_bytes=500, rate_limit_burst=100.0, rate_limit_rps=100.0)
srv, r = start(cfg)
P = srv.server_address[1]
print("cl:0        ", raw(P, b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\ncontent-type: application/json\r\ncontent-length: 0\r\nauthorization: Bearer " + KEY.encode() + b"\r\n\r\n").split(b"\r\n")[0])
print("no-cl       ", raw(P, b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\ncontent-type: application/json\r\nauthorization: Bearer " + KEY.encode() + b"\r\n\r\n{}\n").split(b"\r\n")[0])
print("cl:-5       ", raw(P, b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\ncontent-type: application/json\r\ncontent-length: -5\r\nauthorization: Bearer " + KEY.encode() + b"\r\n\r\n").split(b"\r\n")[0])
print("cl:999999   ", raw(P, b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\ncontent-type: application/json\r\ncontent-length: 999999\r\nauthorization: Bearer " + KEY.encode() + b"\r\n\r\n{}").split(b"\r\n")[0])

print("== local rate limit (burst=0.5) ==")
srv.shutdown(); srv.server_close()
cfg2 = make(rate_limit_burst=0.5, rate_limit_rps=0.01)
srv2, r2 = start(cfg2)
P2 = srv2.server_address[1]
r2.mode = "fast"
for i in range(3):
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % P2,
                                 data=json.dumps({"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]}).encode(),
                                 headers={"content-type": "application/json", "authorization": "Bearer " + KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print("  rl", i, resp.status, dict(resp.headers).get("retry-after"))
    except urllib.error.HTTPError as e:
        print("  rl", i, e.code, dict(e.headers).get("retry-after"), e.read().decode()[:90])
srv2.shutdown(); srv2.server_close()

print("== worker cap (max_workers=1, two concurrent slow requests) ==")
cfg3 = make(max_workers=1)
srv3, r3 = start(cfg3, max_workers=1)
P3 = srv3.server_address[1]


def slow_call(tag, out):
    t0 = time.time()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions?key=%s" % (P3, KEY),
                                 data=json.dumps({"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]}).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out.append((tag, resp.status, round(time.time() - t0, 2)))
    except Exception as exc:
        out.append((tag, "EXC", repr(exc)[:80]))


threads = []
outs = []
for i in range(2):
    t = threading.Thread(target=slow_call, args=(i, outs))
    t.start()
    threads.append(t)
    time.sleep(0.2)
# while both are in flight, cancel the first by closing its socket? we cannot from here; just wait
for t in threads:
    t.join()
print("  ", outs, "| sessions:", len(r3.sessions), "| cancelled:", [s.cancelled for s in r3.sessions])
srv3.shutdown(); srv3.server_close()

print("== disconnect cancels ==")
cfg4 = make()
srv4, r4 = start(cfg4)
P4 = srv4.server_address[1]
body = json.dumps({"model": "gpt-5", "stream": True, "messages": [{"role": "user", "content": "x"}]}).encode()
s = raw(P4, (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\ncontent-type: application/json\r\ncontent-length: "
             + str(len(body)).encode() + b"\r\nauthorization: Bearer " + KEY.encode() + b"\r\n\r\n" + body), read=False)
time.sleep(0.6)
print("   before close: sessions=%d cancelled=%s" % (len(r4.sessions), [x.cancelled for x in r4.sessions]))
s.close()
for _ in range(40):
    time.sleep(0.1)
    if r4.sessions and r4.sessions[0].cancelled:
        break
print("   after close:  sessions=%d cancelled=%s (waited %.1fs)" % (len(r4.sessions), [x.cancelled for x in r4.sessions], 0))
srv4.shutdown(); srv4.server_close()
