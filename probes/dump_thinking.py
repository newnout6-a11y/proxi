
import json, pathlib, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path(__file__).parent.parent.joinpath("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"],
     "origin": CFG["base"], "referer": CFG["base"] + "/", "x-retool-client-version": "4.84.0-16f3123 (Build 409813)"}
base = f"{CFG['base']}/api/pages/{CFG['page_uuid']}/sandpack/threads"
threads = json.loads(urllib.request.urlopen(urllib.request.Request(base, headers=H)).read().decode())["threads"]
tid = threads[0]["id"]
msgs = json.loads(urllib.request.urlopen(urllib.request.Request(f"{base}/{tid}/messages", headers=H)).read().decode())["messages"]
for m in msgs:
    c = m.get("content") or {}
    if c.get("type") == "thinking":
        print("KEYS:", list(c.keys()))
        print(json.dumps(c, ensure_ascii=False)[:400])
        print("createdAt:", m.get("createdAt"))
        break
