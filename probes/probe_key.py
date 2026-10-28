
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
KEY = "retool_01m61jdhsvgacc2arkk7z0padz"
CFG = json.loads(pathlib.Path(__file__).parent.parent.joinpath("config.json").read_text(encoding="utf-8"))
BASE = CFG["base"]

def call(path, headers, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return resp.status, resp.read()[:260].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)[:120]

variants = {
    "x-api-key": {"accept": "application/json", "x-api-key": KEY},
    "bearer": {"accept": "application/json", "authorization": "Bearer " + KEY},
    "cookie-only": {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"]},
}
for name, h in variants.items():
    print("==", name)
    for path in ("/api/user", "/api/resources", "/api/pages"):
        s, b = call(path, h)
        print(f"   {s} {path} :: {b[:110]}")

print("== MCP / external API guesses with x-api-key")
for path in ("/api/v2/resources", "/api/mcp", "/api/external/v1/pages", "/api/users/me"):
    s, b = call(path, {"accept": "application/json", "x-api-key": KEY})
    print(f"   {s} {path} :: {b[:110]}")
