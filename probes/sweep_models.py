
import concurrent.futures as cf, json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"; H = {"content-type": "application/json", "authorization": "Bearer live-test-key"}
ids = [m["id"] for m in json.loads(urllib.request.urlopen(urllib.request.Request(BASE + "/v1/models", headers=H)).read().decode())["data"]]

def probe(model):
    data = json.dumps({"model": model, "messages": [{"role": "user", "content": "Ответь одним словом: да"}]}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=data, method="POST", headers=H)
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            b = json.loads(r.read().decode())
            return model, r.status, b["choices"][0]["message"]["content"][:40]
    except urllib.error.HTTPError as e:
        return model, e.code, e.read()[:120].decode("utf-8", "replace")
    except Exception as e:
        return model, -1, str(e)[:80]

print("sweep", len(ids), "models")
res = []
with cf.ThreadPoolExecutor(6) as ex:
    for row in ex.map(probe, ids):
        print(*row, sep=" | ", flush=True)
        res.append(row)
json.dump(res, open("probes/sweep_result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
ok = [r[0] for r in res if r[1] == 200]
print("\nOK:", len(ok), "| FAIL:", len(res) - len(ok))
print("failed:", [r[0] for r in res if r[1] != 200])
