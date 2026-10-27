
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H0 = {"accept": "application/json", "content-type": "application/json", "origin": CFG["base"], "referer": CFG["base"] + "/",
      "user-agent": CFG["ua"], "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"],
      "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}
data = json.dumps({"threadId": CFG["thread_id"], "pageUuid": CFG["page_uuid"], "environmentName": "production"}).encode()
req = urllib.request.Request(CFG["base"] + "/api/ai/retool-next-agent/start", data=data, headers=H0, method="POST")
with urllib.request.urlopen(req, timeout=120) as r:
    st = json.loads(r.read().decode())
url = st["url"].replace("wss://", "https://")
H1 = {"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
      "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Origin": CFG["base"], "User-Agent": CFG["ua"]}
for variant, hdrs in (("plain", H1), ("with-cookie", {**H1, "Cookie": CFG["cookie"]})):
    try:
        rq = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(rq, timeout=30) as rr:
            print(variant, rr.status, dict(rr.headers).get("sec-websocket-accept"))
    except urllib.error.HTTPError as e:
        print(variant, "HTTP", e.code, "::", e.read()[:300].decode("utf-8", "replace"))
    except Exception as e:
        print(variant, "ERR", type(e).__name__, str(e)[:150])
