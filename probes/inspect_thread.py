
import json, pathlib, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path(__file__).parent.parent.joinpath("config.json").read_text(encoding="utf-8"))
tid = sys.argv[1] if len(sys.argv) > 1 else ""
H = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"],
     "origin": CFG["base"], "referer": CFG["base"] + "/", "x-retool-client-version": "4.84.0-16f3123 (Build 409813)"}
base = f"{CFG['base']}/api/pages/{CFG['page_uuid']}/sandpack/threads"
if not tid:
    with urllib.request.urlopen(urllib.request.Request(base, headers=H), timeout=30) as r:
        threads = json.loads(r.read().decode())["threads"]
    tid = threads[0]["id"]
    print("latest thread:", tid, "updated", threads[0]["updatedAt"])
url = f"{base}/{tid}/messages"
with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=30) as r:
    msgs = json.loads(r.read().decode())["messages"]
print("messages:", len(msgs))
for m in msgs:
    c = m.get("content") or {}
    key = c.get("text") or c.get("thinking") or json.dumps(c)[:160]
    print(f'  {m["createdAt"][11:19]} {m.get("senderRole")} {m.get("contentType")}: {str(key)[:110]}')
