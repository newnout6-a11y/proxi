import json, urllib.request, os
CFG = json.load(open("config.json", encoding="utf-8"))
H = {
    "accept": "application/json",
    "cookie": CFG["cookie"],
    "origin": CFG["base"],
    "referer": CFG["base"] + "/",
    "user-agent": CFG["ua"],
    "x-xsrf-token": CFG["xsrf"],
    "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"
}
req = urllib.request.Request(CFG["base"] + "/api/resources", headers=H)
with urllib.request.urlopen(req) as r:
    res = json.loads(r.read().decode())
    for item in res.get("resources", []):
        print(f"ID: {item.get('id')} | Type: {item.get('type')} | Name: {item.get('displayName') or item.get('name')}")
