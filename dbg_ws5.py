
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
url = st["url"] + "?sandboxToken=" + st["sandboxToken"]
print("reused:", st.get("sandboxReused"))

async def main():
    try:
        async with websockets.connect(url, origin=CFG["base"], max_size=None, ping_interval=None) as ws:
            print("CONNECTED")
            first = await asyncio.wait_for(ws.recv(), timeout=25)
            print("first frame:", str(first)[:200])
    except Exception as e:
        print("FAIL", type(e).__name__, str(e)[:160])
asyncio.run(main())
