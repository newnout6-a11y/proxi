
import json, pathlib, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
p = pathlib.Path(__file__).parent.parent / "config.json"
CFG = json.loads(p.read_text(encoding="utf-8"))
H = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"],
     "origin": CFG["base"], "referer": CFG["base"] + "/", "x-retool-client-version": "4.84.0-16f3123 (Build 409813)"}
with urllib.request.urlopen(urllib.request.Request(CFG["base"] + "/api/resources", headers=H), timeout=30) as r:
    items = json.loads(r.read().decode()).get("resources", [])
found = {}
for it in items:
    if it.get("type") == "anthropic":
        found["provider_resource_name"] = it["name"]
    elif it.get("type") in ("openAIProvider", "openai"):
        found["openai_provider_resource_name"] = it["name"]
print("found:", found)
CFG.update(found)
p.write_text(json.dumps(CFG, indent=2), encoding="utf-8")
