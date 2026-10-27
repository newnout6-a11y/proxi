
import asyncio, json, pathlib, sys, urllib.request, websockets
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "content-type": "application/json", "origin": CFG["base"], "referer": CFG["base"] + "/",
     "user-agent": CFG["ua"], "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"],
     "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}

def start():
    data = json.dumps({"threadId": CFG["thread_id"], "pageUuid": CFG["page_uuid"], "environmentName": "production"}).encode()
    req = urllib.request.Request(CFG["base"] + "/api/ai/retool-next-agent/start", data=data, headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status, json.loads(r.read().decode())

async def main():
    s, st = start()
    print("start:", s, "reused:", st.get("sandboxReused"), "url_host:", st["url"].split("/")[2])
    extra = {"User-Agent": CFG["ua"], "Origin": CFG["base"], "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
    for attempt in (1, 2):
        try:
            async with websockets.connect(st["url"], origin=CFG["base"], extra_headers=extra, max_size=None, ping_interval=None) as ws:
                print("CONNECTED attempt", attempt)
                return
        except Exception as e:
            print("attempt", attempt, "FAIL:", type(e).__name__, str(e)[:300])
            if hasattr(e, "response") and getattr(e, "response", None) is not None:
                print("   resp headers:", dict(list(e.response.headers.items())[:12]))
asyncio.run(main())
