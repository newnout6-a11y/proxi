
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
        return json.loads(r.read().decode())

async def main():
    st = start()
    url = st["url"]
    print("URL:", url[:120], "...")
    try:
        async with websockets.connect(url, origin=CFG["base"], max_size=None, ping_interval=None) as ws:
            print("CONNECTED")
    except Exception as e:
        print("FAIL", type(e).__name__, str(e)[:200])
        resp = getattr(e, "response", None) or getattr(e, "resp", None)
        print("has response attr:", resp)
        if resp is not None:
            print("status:", getattr(resp, "status_code", None))
            try:
                print("headers:", dict(list(resp.headers.items())[:15]))
                print("body:", (await resp.read())[:400])
            except Exception as ee:
                print("read err", ee)
asyncio.run(main())
