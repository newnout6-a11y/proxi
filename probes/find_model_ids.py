
import json, pathlib, re, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"],
     "origin": CFG["base"], "referer": CFG["base"] + "/", "x-retool-client-version": "4.84.0-16f3123 (Build 409813)"}

bundle = ""
for p in ["bundle.js"] + [str(f) for f in pathlib.Path("chunks").glob("*.js")]:
    try:
        bundle += pathlib.Path(p).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        pass
ids = sorted(set(re.findall(r"\b(?:claude|gpt)-[a-z0-9.\-]{3,30}\b", bundle)))
print("ids in bundle:", ids[:40])

def get(p):
    try:
        with urllib.request.urlopen(urllib.request.Request(CFG["base"] + p, headers=H), timeout=20) as r:
            return r.status, r.read()[:250].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)[:120]

for p in ["/api/ai/availableModels", "/api/ai/modelOptions", "/api/agent/models", "/api/llm/models",
          "/api/ai/retool-next-agent/models", "/api/ai/resources", "/api/ai/providers/models"]:
    s, b = get(p)
    print(s, p, "::", b[:160])
