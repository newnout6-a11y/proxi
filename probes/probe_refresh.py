
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "content-type": "application/json", "origin": CFG["base"], "referer": CFG["base"] + "/",
     "user-agent": CFG["ua"], "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"],
     "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}

def call(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(CFG["base"] + path, data=data, headers=H, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()[:200], r.headers.get_all("set-cookie") or []
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200], e.headers.get_all("set-cookie") or []
    except Exception as e:
        return -1, str(e).encode(), []

print("user:", call("/api/user")[0])
print("balance:", call("/api/credits/balance")[0])
s, b, cookies = call("/api/refreshtoken", "POST", {})
print("refreshtoken:", s, b[:120])
for c in cookies:
    print("  set-cookie:", c[:120])
