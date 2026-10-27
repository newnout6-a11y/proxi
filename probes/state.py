
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"],
     "origin": CFG["base"], "referer": CFG["base"] + "/", "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}
def get(p):
    try:
        with urllib.request.urlopen(urllib.request.Request(CFG["base"] + p, headers=H), timeout=45) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
print("cookie alive check:", get("/api/user")[0])
s, b = get(f"/api/pages/{CFG['page_uuid']}/sandpack/threads")
print("threads:", s)
if s == 200:
    for t in json.loads(b).get("threads", [])[:12]:
        print(f"  {t['updatedAt'][:19]}  {t['id']}  msgs_at={str(t['lastMessageAt'])[:19]}  {t['name'][:40]}")
