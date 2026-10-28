
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
KEY = "retool_01m61jdhsvgacc2arkk7z0padz"
CFG = json.loads(pathlib.Path(__file__).parent.parent.joinpath("config.json").read_text(encoding="utf-8"))
BASE = CFG["base"]
H = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"]}

def call(path, extra=None, method="GET"):
    h = dict(H); h.update(extra or {})
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=h, method=method), timeout=20) as r:
            return r.status, r.read()[:160].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:120].decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)[:90]

for p in ("/api/mcp/sse", "/api/mcp/v1", "/mcp", "/api/ai/mcp", "/api/settings/apiKeys", "/api/apiKey",
          "/api/agentApiKeys", "/api/retoolApi/keys"):
    s, b = call(p, {"x-api-key": KEY})
    print(s, p, "::", b[:90].replace("\n", " "))
print("--- bearer on mcp")
print(call("/api/mcp/sse", {"authorization": "Bearer " + KEY})[0])
