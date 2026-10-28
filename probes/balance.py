
import json, pathlib, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"],
     "origin": CFG["base"], "referer": CFG["base"] + "/", "x-retool-client-version": "4.84.0-16f3123 (Build 409813)"}
try:
    with urllib.request.urlopen(urllib.request.Request(CFG["base"] + "/api/credits/balance", headers=H), timeout=20) as r:
        d = json.loads(r.read().decode()).get("org", {})
        print("credits:", d.get("balance"), "/", d.get("monthlyAllocation"), "reset", d.get("resetDate"))
except Exception as e:
    print("balance err", e)
