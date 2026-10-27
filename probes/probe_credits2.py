import json, urllib.request, urllib.error, gzip, os

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))

H = {
    "accept": "application/json",
    "accept-encoding": "gzip",
    "content-type": "application/json",
    "cookie": CFG["cookie"],
    "origin": CFG["base"],
    "referer": CFG["base"] + "/",
    "user-agent": CFG["ua"],
    "x-xsrf-token": CFG["xsrf"],
    "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"
}

eps = [
    '/api/credits/balance',
    '/api/credits/adminBalance',
    '/api/credits/orgUsage',
    '/api/credits/orgUsedBreakdown',
    '/api/credits/orgTokenUsage',
    '/api/credits/cycleDates',
]

for ep in eps:
    req = urllib.request.Request(CFG["base"] + ep, headers=H, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            if r.headers.get("content-encoding") == "gzip":
                raw = gzip.decompress(raw)
            print(f"{ep} -> {r.status}: {raw.decode('utf-8', 'replace')[:200]}")
    except Exception as e:
        print(f"{ep} -> err: {e}")
