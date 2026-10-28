
import sys, json, threading, urllib.request, urllib.error
sys.path.insert(0, r"C:\specialinay_papka")
import proxy, config as config_module
class FC:
    def get_balance(self, force_refresh=False): return {"available":1.0}
    def record_turn(self,t,c): pass
    def usage(self): return {"session_used":0,"turns":0,"recent":[]}
class FR:
    async def run(self, parsed, out, holder):
        out.put({"type":"text","delta":"ok"}); out.put({"type":"end","reason":"stop"}); out.put(None)
cfg = dict(config_module.DEFAULTS)
cfg.update({"base":"https://example.invalid","page_uuid":"p","ua":"t","cookie":"accessToken=abc123456789","xsrf":"q",
            "relay_api_key":"test-key-123","workspace_root":r"C:\specialinay_papka","cookie_values":[]})
rt = proxy.Runtime(cfg); rt.credits = FC(); rt.runner = FR()
srv = proxy.BoundedServer(("127.0.0.1",0), proxy.make_handler(rt), threading.BoundedSemaphore(4))
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d/v1/chat/completions" % srv.server_address[1]
def post(body, ctype="application/json"):
    req = urllib.request.Request(url, data=body if isinstance(body,bytes) else json.dumps(body).encode(), method="POST")
    req.add_header("content-type", ctype); req.add_header("authorization","Bearer test-key-123")
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, r.read().decode()[:150]
    except urllib.error.HTTPError as e: return e.code, e.read().decode()[:200]
print("bad json      :", post(b"{not json"))
print("array body    :", post(b"[1,2]"))
print("wrong types   :", post({"model":5,"messages":"x"}))
print("model unknown :", post({"model":"zzz","messages":[{"role":"user","content":"x"}]}))
print("max_tokens str:", post({"model":"claude-opus-5-5","messages":[{"role":"user","content":"x"}],"max_tokens":"64"}))
print("empty body    :", post(b""))
print("no msgs       :", post({"model":"claude-opus-5-5"}))
print("effort bogus  :", post({"model":"claude-opus-5-5","messages":[{"role":"user","content":"x"}],"reasoning_effort":"ultra"}))
print("effort openai on anthropic model:", post({"model":"claude-opus-5-5","messages":[{"role":"user","content":"x"}],"reasoning_effort":"minimal"}))
print("budget 10     :", post({"model":"claude-opus-5-5","messages":[{"role":"user","content":"x"}],"thinking":{"budget_tokens":10}}))
print("text/plain    :", post(b"hello", "text/plain"))
srv.shutdown(); srv.server_close()
