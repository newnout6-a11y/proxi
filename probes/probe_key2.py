
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
KEY = "retool_01m61jdhsvgacc2arkk7z0padz"
CFG = json.loads(pathlib.Path(__file__).parent.parent.joinpath("config.json").read_text(encoding="utf-8"))
BASE = CFG["base"]
BASEH = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"]}

def call(path, extra=None, method="GET"):
    h = dict(BASEH); h.update(extra or {})
    r = urllib.request.Request(BASE + path, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return resp.status, resp.read()[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:150].decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)[:100]

print("with cookies + x-api-key:")
for p in ("/api/user", "/api/mcp", "/api/apiKeys", "/api/v2/resources", "/api/r2/configuration"):
    print("  ", call(p, {"x-api-key": KEY})[0], p)
print("whoami via key only (no cookies, +UA):")
h = {"accept": "application/json", "x-api-key": KEY, "user-agent": CFG["ua"]}
r = urllib.request.Request(BASE + "/api/user", headers=h)
try:
    with urllib.request.urlopen(r, timeout=25) as resp:
        print("   ", resp.status, resp.read()[:140])
except urllib.error.HTTPError as e:
    print("   ", e.code, e.read()[:140])
