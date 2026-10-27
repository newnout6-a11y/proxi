
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"],
     "origin": CFG["base"], "referer": CFG["base"] + "/", "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}
url = f"{CFG['base']}/api/pages/{CFG['page_uuid']}/sandpack/threads/{CFG['thread_id']}/messages"
with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=60) as r:
    d = json.loads(r.read().decode())
print("count:", len(d.get("messages", [])))
for m in d.get("messages", []):
    c = m.get("content", {})
    print(f'  {m["createdAt"][11:19]} {m.get("senderRole")} {m.get("contentType")} :: {str(c.get("text") or c.get("thinking") or "")[:120]}')
