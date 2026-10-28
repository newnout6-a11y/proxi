
import json, sys, threading, urllib.request, urllib.error, tempfile
sys.path.insert(0, r"C:\specialinay_papka")
import apierr, proxy, config as C, models, validate

tmp = tempfile.mkdtemp(dir=r"C:\specialinay_papka\.verify3")
KEY = "k"

class FakeCredits:
    def get_balance(self, force_refresh=False): return {"available": 1.0}
    def record_turn(self, *a): pass
    def usage(self): return {"session_used": 0, "turns": 0, "recent": []}
class FakeRunner:
    async def run(self, parsed, out, holder):
        out.put({"type":"text","delta":"ok"}); out.put({"type":"end","reason":"stop"}); out.put(None)

def serve(**over):
    cfg = dict(C.DEFAULTS); cfg.update({"base":"https://x","page_uuid":"p","ua":"u",
        "cookie":"accessToken=abc; xsrfToken=q","xsrf":"q","relay_api_key":KEY,
        "workspace_root":tmp,"host":"127.0.0.1","port":0,"cookie_values":["abc"]})
    cfg.update(over)
    rt = proxy.Runtime(cfg); rt.credits = FakeCredits(); rt.runner = FakeRunner()
    h = proxy.make_handler(rt); srv = proxy.BoundedServer(("127.0.0.1",0), h, threading.BoundedSemaphore(4))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, rt

def get(port, path, payload=None):
    body = json.dumps(payload).encode() if payload else None
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body,
                               method="POST" if body else "GET")
    r.add_header("content-type","application/json"); r.add_header("authorization","Bearer "+KEY)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp: return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

srv, rt = serve(provider_resource_name="a", openai_provider_resource_name="o",
                google_provider_resource_name="g", retool_fast_provider_resource_name="r")
st, body = get(srv.server_address[1], "/v1/models")
ids = [m["id"] for m in body.get("data", [])]
print("1) all providers set ->", st, "count=%d" % len(ids))
print("   gpt-4.1 present:", "gpt-4.1" in ids, "| gpt-6-astra/sol/luna:", [x in ids for x in ("gpt-6-astra","gpt-6-sol","gpt-6-luna")], "| gpt-6.1-sol:", "gpt-6.1-sol" in ids, "| ultrafast:", "gpt-6.1-sol-ultrafast" in ids)
srv.shutdown()

srv, rt = serve(openai_provider_resource_name="o")   # partial config
st, body = get(srv.server_address[1], "/v1/models")
ids = [m["id"] for m in body.get("data", [])]
print("2) only openai set -> status=%d count=%d claude_present=%s" % (st, len(ids), "claude-opus-5-5" in ids))
st2, b2 = get(srv.server_address[1], "/v1/chat/completions", {"model":"claude-opus-5-5","messages":[{"role":"user","content":"hi"}]})
print("2b) call hidden model -> %d %s" % (st2, (b2.get("error") or {}).get("type")))
srv.shutdown()

srv, rt = serve(show_unrouted_models=True)
st, body = get(srv.server_address[1], "/v1/models"); print("3) show_unrouted_models -> count=%d" % len(body.get("data",[])))
srv.shutdown()

srv, rt = serve()   # nothing configured
st, body = get(srv.server_address[1], "/v1/models"); print("4) no provider configured -> count=%d (catalog %d)" % (len(body.get("data",[])), len(models.catalog())))
srv.shutdown()

# max_tokens validation
def mt(model, n, strict=False):
    cfg = {"strict_models": strict, "model": "claude-opus-5-5"}
    try:
        p = validate.parse_chat({"model": model, "max_tokens": n, "messages":[{"role":"user","content":"hi"}]}, cfg)
        return "ok:%s" % p.max_tokens
    except apierr.ApiError as e: return "%d:%s" % (e.status, e.code)
print("5) claude 128000:", mt("claude-opus-5-5",128000), "| 128001:", mt("claude-opus-5-5",128001))
print("6) gpt-6 128000:", mt("gpt-6",128000), "| 128001:", mt("gpt-6",128001), "| gpt-6.1-sol 128001:", mt("gpt-6.1-sol",128001))
print("7) unknown non-strict 128000:", mt("zzz-1",128000), "| 999999:", mt("zzz-1",999999), "| 2000000:", mt("zzz-1",2000000))
print("8) unknown strict 1000:", mt("zzz-1",1000,True), "| gemini 65537:", mt("gemini-3-flash-preview",65537))
print("9) aliases gpt-6 ->", models.resolve("gpt-6")[0]["id"])
