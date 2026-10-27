
import asyncio, json, pathlib, sys, urllib.request, websockets
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "content-type": "application/json", "origin": CFG["base"], "referer": CFG["base"] + "/",
     "user-agent": CFG["ua"], "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"],
     "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}
data = json.dumps({"threadId": CFG["thread_id"], "pageUuid": CFG["page_uuid"], "environmentName": "production"}).encode()
req = urllib.request.Request(CFG["base"] + "/api/ai/retool-next-agent/start", data=data, headers=H, method="POST")
with urllib.request.urlopen(req, timeout=120) as r:
    st = json.loads(r.read().decode())
url = st["url"]
print("tail:", url[-60:])
print("has param:", "sandboxToken=" in url)
print("url len:", len(url))

async def main():
    hdrs = [("Origin", CFG["base"]), ("User-Agent", CFG["ua"]),
            ("Accept-Language", "ru-RU,ru;q=0.9,en;q=0.8"), ("Cache-Control", "no-cache"), ("Pragma", "no-cache")]
    try:
        async with websockets.connect(url, extra_headers=hdrs, max_size=None, ping_interval=None, compression=None) as ws:
            print("CONNECTED")
    except Exception as e:
        print("FAIL", type(e).__name__, str(e)[:160])
asyncio.run(main())
