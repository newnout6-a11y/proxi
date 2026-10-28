
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
KEY = "retool_01m61jdhsvgacc2arkk7z0padz"
CFG = json.loads(pathlib.Path(__file__).parent.parent.joinpath("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json, text/event-stream", "content-type": "application/json",
     "authorization": "Bearer " + KEY, "user-agent": CFG["ua"]}

def post(payload):
    r = urllib.request.Request(CFG["base"] + "/mcp", data=json.dumps(payload).encode(), headers=H, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.headers.get("content-type"), resp.read()[:600].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type"), e.read()[:300].decode("utf-8", "replace")
    except Exception as e:
        return -1, "", str(e)[:120]

init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "relay-probe", "version": "1.0"}}}
s, ct, body = post(init)
print("initialize:", s, ct, "::", body[:300])
if s == 200:
    print("tools/list:", *post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})[:1],
          str(post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})[2])[:400])
