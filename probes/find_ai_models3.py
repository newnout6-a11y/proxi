
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"],
     "origin": CFG["base"], "referer": CFG["base"] + "/", "x-retool-client-version": "4.84.0-16f3123 (Build 409813)"}

def get(p):
    try:
        with urllib.request.urlopen(urllib.request.Request(CFG["base"] + p, headers=H), timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)

s, b = get("/api/resources")
data = json.loads(b)
items = data.get("resources") if isinstance(data, dict) else data
for it in items:
    if it.get("type") in ("anthropic", "openAIProvider"):
        print("== FULL ITEM ==")
        print(json.dumps(it, ensure_ascii=False)[:1500])

for name in ["Anthropic", "OpenAI", "retool_email"]:
    s2, b2 = get("/api/resources/" + name)
    print("==", name, s2, b2[:300])
