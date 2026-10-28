
import sys, json, threading, urllib.request, urllib.error, asyncio, time
sys.path.insert(0, r"C:\specialinay_papka")
import proxy, config as config_module, apierr, session as session_mod

class FakeCredits:
    def get_balance(self, force_refresh=False): return {"available": 1.0}
    def record_turn(self, t, c): pass
    def usage(self): return {"session_used": 0, "turns": 0, "recent": []}
class FakeRunner:
    def __init__(self): self.seen = []
    async def run(self, parsed, out, holder):
        self.seen.append(parsed); out.put({"type":"text","delta":"ok"}); out.put({"type":"end","reason":"stop"}); out.put(None)

cfg = dict(config_module.DEFAULTS)
cfg.update({"base":"https://example.invalid","page_uuid":"p","ua":"t","cookie":"accessToken=abc123456789; xsrfToken=q","xsrf":"q",
            "relay_api_key":"test-key-123","workspace_root":r"C:\specialinay_papka","cookie_values":["abc123456789"]})
rt = proxy.Runtime(cfg); rt.credits = FakeCredits(); rt.runner = FakeRunner()
srv = proxy.BoundedServer(("127.0.0.1",0), proxy.make_handler(rt), threading.BoundedSemaphore(4))
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d" % srv.server_address[1]

def call(path, body=None, headers=None, method=None):
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    req = urllib.request.Request(url+path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("content-type","application/json")
    for k,v in (headers or {}).items(): req.add_header(k,v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, r.read().decode()[:120], dict(r.headers)
    except urllib.error.HTTPError as e: return e.code, e.read().decode()[:160], dict(e.headers)

CH = {"model":"claude-opus-5-5","messages":[{"role":"user","content":"hi"}]}
print("no key:", call("/v1/chat/completions", CH)[:2])
print("bad key:", call("/v1/chat/completions", CH, {"authorization":"Bearer nope"})[:2])
print("x-api-key ok:", call("/v1/chat/completions", CH, {"x-api-key":"test-key-123"})[0])
print("query key ok:", call("/v1/chat/completions?key=test-key-123", CH)[0])
pass
print("bad json:", call("/v1/chat/completions", b"{not json")[:2])
print("wrong types:", call("/v1/chat/completions", {"model":5,"messages":"x"})[:2])
print("max_tokens str:", call("/v1/chat/completions", {"model":"claude-opus-5-5","messages":[{"role":"user","content":"x"}],"max_tokens":"64"})[:2])
st, body, hdr = call("/v1/chat/completions", CH, {"authorization":"Bearer test-key-123"})
print("reqid hdr:", "x-request-id" in {k.lower() for k in hdr}, hdr.get("x-request-id"))
print("no-auth 405? GET on chat:", call("/v1/chat/completions", None, {"authorization":"Bearer test-key-123"}, "GET")[0])
print("415:", call("/v1/chat/completions", b"x", {"authorization":"Bearer test-key-123","content-type":"text/plain"})[0])
print("empty body:", call("/v1/chat/completions", b"", {"authorization":"Bearer test-key-123"})[0])
srv.shutdown(); srv.server_close()

# isolation: pool per_request must create distinct threads under concurrency
class FakeBackend:
    def __init__(self): self.cfg = cfg; self.n = 0; self.lock = threading.Lock()
    async def create_thread(self):
        with self.lock: self.n += 1; return "thread-%d" % self.n
b = FakeBackend()
pool = session_mod.SessionPool(b)
async def main():
    s = await asyncio.gather(*[pool.acquire() for _ in range(5)])
    ids = [x.thread_id for x in s]
    for x in s: await pool.release(x)
    return ids
print("isolation threads:", asyncio.run(main()))