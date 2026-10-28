
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"],
     "origin": CFG["base"], "referer": CFG["base"] + "/", "x-retool-client-version": "4.84.0-16f3123 (Build 409813)"}

def get(p):
    try:
        with urllib.request.urlopen(urllib.request.Request(CFG["base"] + p, headers=H), timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)

for p in ["/api/resources/18758537", "/api/resources/18758538", "/api/ai/models", "/api/ai/providers"]:
    s, b = get(p)
    print("==", p, s, len(b))
    if s == 200:
        try:
            d = json.loads(b)
            res = d.get("resource", d)
            if isinstance(res, dict):
                for k, v in res.items():
                    sv = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
                    print("   ", k, "=", sv[:300])
        except Exception as e:
            print("   parse", e, b[:200])
    else:
        print("   ", b[:150])
