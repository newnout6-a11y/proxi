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

def probe(url, method="GET", payload=None):
    req = urllib.request.Request(CFG["base"] + url, headers=H, method=method)
    if payload:
        req.data = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            if r.headers.get("content-encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        if e.headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
        return e.code, raw.decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)

endpoints = [
    "/api/credits/adminBalance",
    "/api/credits/balance",
    "/api/credits/usage",
    "/api/credits/purchaseEligibility",
    "/api/organization/current",
    "/api/user/current",
    "/api/ai/usage",
]

for ep in endpoints:
    st, resp = probe(ep)
    print(f"{ep} -> {st}: {resp[:300]}")
